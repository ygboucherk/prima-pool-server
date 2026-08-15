"""Tests for pay-per-use (usage debit + worker crediting, v0.9).

Covers config parsing of prices, the store's `settle_request` (weighted
integer credits + conservation + equal-split fallback), the insufficient-balance
gate (402), and cost surfacing on the usage endpoints.
"""
from __future__ import annotations

import sqlite3

import pytest
from fastapi.testclient import TestClient

from prima_pool_server.app import create_app
from prima_pool_server.config import ModelDef, Settings, _parse_models
from prima_pool_server.models import (
    ClusterRecord,
    ClusterStatus,
    RequestRecord,
    WorkerRecord,
    WorkerStatus,
    EndpointInfo,
)
from prima_pool_server.store import Store


# ── config parsing ───────────────────────────────────────────────────────
def test_parse_models_with_prices(monkeypatch):
    monkeypatch.setenv(
        "PRIMA_POOL_MODELS",
        "a:<hash>:4096:1000000:3000000,b:<hash>:6144",
    )
    models = _parse_models()
    assert models["a"].input_price == 1000000
    assert models["a"].output_price == 3000000
    # Legacy 3-field entry → both 0.
    assert models["b"].input_price == 0
    assert models["b"].output_price == 0


def test_parse_models_invalid_price_defaults_zero(monkeypatch):
    monkeypatch.setenv("PRIMA_POOL_MODELS", "a:<hash>:4096:notanumber:alsonot")
    models = _parse_models()
    assert models["a"].input_price == 0
    assert models["a"].output_price == 0


def _priced_settings() -> Settings:
    return Settings(
        models={
            "priced-model": ModelDef(
                slug="priced-model",
                gguf_sha256="a" * 64,
                required_memory_mb=4096,
                input_price=1000,
                output_price=3000,
            ),
            "free-model": ModelDef(
                slug="free-model", gguf_sha256="b" * 64, required_memory_mb=4096
            ),
        },
        assignable_grace_s=0,
        heartbeat_timeout_s=30,
    )


# ── store: settle_request ────────────────────────────────────────────────
def _worker(account_id: str, worker_id: str, wg_pubkey: str) -> WorkerRecord:
    return WorkerRecord(
        worker_id=worker_id,
        account_id=account_id,
        model="m",
        gguf_sha256="a" * 64,
        memory_allocated_mb=2048,
        wg_pubkey=wg_pubkey,
        endpoint=EndpointInfo(host="1.2.3.4", port=51820, behind_nat=False, nat_type="none"),
        hardware=None,
        status=WorkerStatus.waitlisted,
        online=True,
    )


def _settle_fixture(tmp_path):
    s = Store(path=str(tmp_path / "store.db"))
    alice = s.create_account("alice", "hunter2hunter2")
    bob = s.create_account("bob", "hunter2hunter2")
    alice_key, _ = s.create_api_key(alice.account_id, "user", "user")
    wrk_a = "wrk_a"
    wrk_b = "wrk_b"
    s.create_worker_if_available(_worker(alice.account_id, wrk_a, "pub-a"), max_per_account=4)
    s.create_worker_if_available(_worker(bob.account_id, wrk_b, "pub-b"), max_per_account=4)
    clu = ClusterRecord(
        cluster_id="clu_1",
        model="m",
        subnet="10.23.1.0/24",
        members=[wrk_a, wrk_b],
        ips={wrk_a: "10.23.1.1", wrk_b: "10.23.1.2"},
        status=ClusterStatus.live,
        ready={wrk_a, wrk_b},
        layer_windows={wrk_a: 20, wrk_b: 10},
    )
    s.create_cluster(clu)
    return s, alice, bob, alice_key, wrk_a, wrk_b


def test_settle_request_debits_and_credits_weighted(tmp_path):
    """cost = input_cost + output_cost; workers credited by layer share with
    exact integer division; sum of credits == debit; head absorbs remainder."""
    s, alice, bob, alice_key, wrk_a, wrk_b = _settle_fixture(tmp_path)
    # Debit alice's account, credit the two workers' owners.
    s.settle_request(
        RequestRecord(
            request_id="req_1",
            account_id=alice.account_id,
            key_id=alice_key.key_id,
            model="m",
            cluster_id="clu_1",
            prompt_tokens=10,
            completion_tokens=5,
            input_cost_minor=100,   # cost = 100
            output_cost_minor=0,
        ),
        worker_account_ids={wrk_a: alice.account_id, wrk_b: bob.account_id},
        layer_windows={wrk_a: 20, wrk_b: 10},
    )
    # Debit 100 from alice; credit wrk_a's owner (alice) 67 and wrk_b's owner
    # (bob) 33. 100*20//30 = 66, 100*10//30 = 33, remainder 1 → head (wrk_a).
    assert s.get_balance(bob.account_id) == 33
    # alice net = -100 (debit) + 67 (credit for her worker) = -33.
    assert s.get_balance(alice.account_id) == -33
    # Conservation: debits == credits.
    events = s.list_balance_events(alice.account_id)
    assert [e.kind for e in events] == ["credit", "debit"]
    assert events[0].reason == "req_1"
    assert events[1].delta == -100  # the debit
    assert events[0].delta == 67    # the credit (head absorbs remainder)


def test_settle_request_unknown_distribution_equal_split(tmp_path):
    """No layer windows → equal split (cost / N), remainder to the head."""
    s, alice, bob, alice_key, wrk_a, wrk_b = _settle_fixture(tmp_path)
    s.settle_request(
        RequestRecord(
            request_id="req_1",
            account_id=alice.account_id,
            key_id=alice_key.key_id,
            model="m",
            cluster_id="clu_1",
            prompt_tokens=1,
            completion_tokens=0,
            input_cost_minor=5,   # odd cost → remainder 1 → head
            output_cost_minor=0,
        ),
        worker_account_ids={wrk_a: alice.account_id, wrk_b: bob.account_id},
        layer_windows=None,
    )
    # Equal split of 5 across 2 members = 2 each, remainder 1 → head (wrk_a).
    assert s.get_balance(bob.account_id) == 2  # wrk_b gets 2
    # alice net = -5 (debit) + 3 (credit) = -2.
    assert s.get_balance(alice.account_id) == -2


def test_settle_request_zero_cost_no_balance_change(tmp_path):
    """A free request (cost 0) records the request but moves no balance."""
    s, alice, bob, alice_key, wrk_a, wrk_b = _settle_fixture(tmp_path)
    s.settle_request(
        RequestRecord(
            request_id="req_1",
            account_id=alice.account_id,
            key_id=alice_key.key_id,
            model="m",
            cluster_id="clu_1",
            prompt_tokens=10,
            completion_tokens=5,
            input_cost_minor=0,
            output_cost_minor=0,
        ),
        worker_account_ids={wrk_a: alice.account_id, wrk_b: bob.account_id},
        layer_windows={wrk_a: 20, wrk_b: 10},
    )
    assert s.get_balance(alice.account_id) == 0
    assert s.get_balance(bob.account_id) == 0
    assert len(s.list_requests_for_account(alice.account_id)) == 1


def test_settle_request_records_frozen_costs(tmp_path):
    """The request row stores the frozen input/output costs."""
    s, alice, bob, alice_key, wrk_a, wrk_b = _settle_fixture(tmp_path)
    s.settle_request(
        RequestRecord(
            request_id="req_1",
            account_id=alice.account_id,
            key_id=alice_key.key_id,
            model="m",
            cluster_id="clu_1",
            prompt_tokens=10,
            completion_tokens=5,
            input_cost_minor=100,
            output_cost_minor=250,
        ),
        worker_account_ids={wrk_a: alice.account_id, wrk_b: bob.account_id},
        layer_windows={wrk_a: 20, wrk_b: 10},
    )
    reqs = s.list_requests_for_account(alice.account_id)
    assert reqs[0].input_cost_minor == 100
    assert reqs[0].output_cost_minor == 250


def test_migrate_existing_db_adds_request_cost_columns(tmp_path):
    """A pre-v0.9 requests table is upgraded in place with cost columns
    defaulting to 0, and balance_events.kind is widened."""
    db = str(tmp_path / "store.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE accounts (account_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE api_keys (key_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            name TEXT NOT NULL, scope TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL);
        CREATE TABLE workers (worker_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            model TEXT NOT NULL, gguf_sha256 TEXT NOT NULL, memory_allocated_mb INTEGER NOT NULL,
            status TEXT NOT NULL, online INTEGER NOT NULL DEFAULT 0, cluster_id TEXT,
            last_heartbeat REAL NOT NULL DEFAULT 0, assignable_at REAL NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, wg_pubkey TEXT NOT NULL, endpoint_json TEXT NOT NULL,
            hardware_json TEXT, assigned_ip TEXT, ring_position INTEGER);
        CREATE TABLE clusters (cluster_id TEXT PRIMARY KEY, model TEXT NOT NULL,
            subnet TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            distribution_reported INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE cluster_members (cluster_id TEXT NOT NULL, worker_id TEXT NOT NULL,
            ring_position INTEGER NOT NULL, assigned_ip TEXT, ready INTEGER NOT NULL DEFAULT 0,
            layer_window INTEGER, PRIMARY KEY (cluster_id, worker_id),
            UNIQUE (cluster_id, ring_position));
        CREATE TABLE requests (request_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            key_id TEXT NOT NULL, model TEXT NOT NULL, cluster_id TEXT NOT NULL,
            prompt_tokens INTEGER NOT NULL DEFAULT 0, completion_tokens INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL);
        CREATE TABLE balance_events (event_id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
            admin_account_id TEXT, kind TEXT NOT NULL CHECK (kind IN ('set', 'adjust')),
            delta INTEGER NOT NULL, balance_before INTEGER NOT NULL, balance_after INTEGER NOT NULL,
            reason TEXT, created_at REAL NOT NULL);
        """
    )
    conn.execute("INSERT INTO accounts VALUES ('acc_1','alice','h',1.0)")
    conn.execute(
        "INSERT INTO requests (request_id, account_id, key_id, model, cluster_id, "
        "prompt_tokens, completion_tokens, created_at) VALUES "
        "('req_1','acc_1','k','m','c',1,2,1.0)"
    )
    conn.commit()
    conn.close()

    s = Store(path=db)
    reqs = s.list_requests_for_account("acc_1")
    assert reqs[0].input_cost_minor == 0
    assert reqs[0].output_cost_minor == 0
    # The kind CHECK now admits debit/credit.
    s.settle_request(
        RequestRecord(
            request_id="req_2",
            account_id="acc_1",
            key_id="k",
            model="m",
            cluster_id="c",
            prompt_tokens=1,
            completion_tokens=0,
            input_cost_minor=100,
        ),
    )
    events = s.list_balance_events("acc_1")
    assert events[0].kind == "debit"
    s.close()


# ── API: gate + cost surfacing ───────────────────────────────────────────
def _make_client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings, store=Store(path=None)))


def _user_key(client: TestClient, username="alice") -> str:
    acc = client.post(
        "/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"}
    ).json()
    sess = client.post(
        "/v1/accounts/login", json={"username": username, "password": "hunter2hunter2"}
    ).json()
    return client.post(
        f"/v1/accounts/{acc['account_id']}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "user", "scope": "user"},
    ).json()["api_key"]


def test_priced_model_gate_blocks_zero_balance():
    """A zero-balance user is 402'd on a priced model, but free models pass."""
    client = _make_client(_priced_settings())
    with client:
        user_key = _user_key(client)
        # Priced model with 0 balance → 402.
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": "priced-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 402
        assert r.json()["type"] == "https://prima-pool.dev/errors/insufficient_balance"
        # Free model with 0 balance → passes the balance gate (404 = no live cluster).
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": "free-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 404


def test_priced_model_gate_allows_positive_balance():
    client = _make_client(_priced_settings())
    with client:
        user_key = _user_key(client)
        account_id = client.app.state.store.resolve_api_key(user_key).account_id
        client.app.state.store.set_balance(account_id, 1000000)
        # Positive balance → passes the gate (404 = no live cluster).
        r = client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {user_key}"},
            json={"model": "priced-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert r.status_code == 404


def test_models_endpoint_exposes_prices():
    client = _make_client(_priced_settings())
    with client:
        body = client.get("/v1/models").json()
        by_slug = {m["slug"]: m for m in body}
        assert by_slug["priced-model"]["input_price"] == 1000
        assert by_slug["priced-model"]["output_price"] == 3000
        assert by_slug["free-model"]["input_price"] == 0
        assert by_slug["free-model"]["output_price"] == 0


def test_usage_stats_and_logs_carry_costs():
    client = _make_client(_priced_settings())
    with client:
        user_key = _user_key(client)
        store = client.app.state.store
        account_id = store.resolve_api_key(user_key).account_id
        key_id = store.resolve_api_key(user_key).key_id
        store.create_cluster(
            ClusterRecord(
                cluster_id="clu_1",
                model="priced-model",
                subnet="10.23.1.0/24",
                members=["w1"],
                ips={"w1": "10.23.1.1"},
                status=ClusterStatus.live,
            )
        )
        store.settle_request(
            RequestRecord(
                request_id="req_1",
                account_id=account_id,
                key_id=key_id,
                model="priced-model",
                cluster_id="clu_1",
                prompt_tokens=10,
                completion_tokens=5,
                input_cost_minor=10000,   # 1000 * 10
                output_cost_minor=15000,  # 3000 * 5
            ),
        )
        logs = client.get(
            f"/v1/accounts/{account_id}/usage/logs/latest",
            headers={"Authorization": f"Bearer {user_key}"},
        ).json()
        assert logs[0]["input_cost_minor"] == 10000
        assert logs[0]["output_cost_minor"] == 15000

        stats = client.post(
            f"/v1/accounts/{account_id}/usage/stats",
            json={"windows": [[0, 9999999999]]},
            headers={"Authorization": f"Bearer {user_key}"},
        ).json()
        assert stats[0]["priced-model"]["input_cost_minor"] == 10000
        assert stats[0]["priced-model"]["output_cost_minor"] == 15000
