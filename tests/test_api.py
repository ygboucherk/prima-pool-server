"""End-to-end API tests for the control plane using FastAPI TestClient."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prima_pool_server.app import create_app
from prima_pool_server.config import ModelDef, Settings
from prima_pool_server.store import Store


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        models={
            "demo-model": ModelDef(
                slug="demo-model", gguf_sha256="a" * 64, required_memory_mb=4096
            )
        },
        assignable_grace_s=0,
        heartbeat_timeout_s=30,
    )


@pytest.fixture()
def store() -> Store:
    return Store(path=None)


@pytest.fixture()
def client(settings: Settings, store: Store):
    app = create_app(settings=settings, store=store)
    with TestClient(app) as c:
        yield c


def _register_account(client: TestClient, username="alice", password="hunter2hunter2"):
    r = client.post("/v1/accounts/register", json={"username": username, "password": password})
    assert r.status_code == 201, r.text
    return r.json()


def _login(client: TestClient, username="alice", password="hunter2hunter2"):
    r = client.post("/v1/accounts/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _create_worker_key(client: TestClient, account_id: str, session_token: str):
    r = client.post(
        f"/v1/accounts/{account_id}/keys",
        headers={"Authorization": f"Bearer {session_token}"},
        json={"name": "worker", "scope": "worker"},
    )
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def _register_worker(client: TestClient, api_key: str, model="demo-model", memory_mb=4096, wg_pubkey="pubkey1"):
    r = client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": model,
            "gguf_sha256": "a" * 64,
            "memory_allocated_mb": memory_mb,
            "wg_pubkey": wg_pubkey,
            "endpoint": {"host": "203.0.113.10", "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    )
    return r


def _new_worker_account(client: TestClient, username: str, wg_pubkey: str, memory_mb: int = 4096):
    """Create an account + worker key + registered worker. Returns (key, worker)."""
    acc = _register_account(client, username=username)
    sess = _login(client, username=username)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])
    worker = _register_worker(client, key, wg_pubkey=wg_pubkey, memory_mb=memory_mb).json()
    return key, worker


def test_register_and_login(client: TestClient):
    acc = _register_account(client)
    assert acc["account_id"].startswith("acc_")
    sess = _login(client)
    assert sess["session_token"].startswith("sess_")


def test_duplicate_username_conflict(client: TestClient):
    _register_account(client)
    r = client.post("/v1/accounts/register", json={"username": "alice", "password": "hunter2hunter2"})
    assert r.status_code == 409


def test_login_wrong_password(client: TestClient):
    _register_account(client)
    r = client.post("/v1/accounts/login", json={"username": "alice", "password": "wrong"})
    assert r.status_code == 401


def test_create_and_list_keys(client: TestClient):
    acc = _register_account(client)
    sess = _login(client)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])
    assert key.startswith("sk-worker-")
    keys = client.get(
        f"/v1/accounts/{acc['account_id']}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
    )
    assert keys.status_code == 200
    assert len(keys.json()) == 1
    assert "api_key" not in keys.json()[0]


def test_worker_register_and_waitlist(client: TestClient):
    acc = _register_account(client)
    sess = _login(client)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])
    r = _register_worker(client, key)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["worker_id"].startswith("wrk_")
    assert body["status"] == "waitlisted"
    assert body["online"] is False


def test_worker_heartbeat_marks_online(client: TestClient):
    acc = _register_account(client)
    sess = _login(client)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])
    worker = _register_worker(client, key).json()
    r = client.post(
        f"/v1/workers/{worker['worker_id']}/heartbeat",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert r.status_code == 200
    assert r.json()["online"] is True


def test_cluster_formation_and_config(client: TestClient):
    # Two separate devices (accounts) that together meet the 4096 MB requirement.
    key1, w1 = _new_worker_account(client, "alice", "pubkey1", memory_mb=2048)
    key2, w2 = _new_worker_account(client, "bob", "pubkey2", memory_mb=2048)

    # Heartbeat both to make them online + assignable.
    for key, w in ((key1, w1), (key2, w2)):
        client.post(f"/v1/workers/{w['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key}"})

    # After the second heartbeat, the scheduler should have formed a cluster.
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    assert st1["status"] == "assigned", st1
    assert st1["cluster"] is not None
    cluster_id = st1["cluster"]["cluster_id"]

    cfg = client.get(f"/v1/clusters/{cluster_id}/config", headers={"Authorization": f"Bearer {key1}"})
    assert cfg.status_code == 200, cfg.text
    body = cfg.json()
    assert len(body["peers"]) == 2
    # Ring order: peers[0] is the head.
    assert body["peers"][0]["pubkey"] == "pubkey1"


def test_cluster_ready_and_live(client: TestClient):
    key1, w1 = _new_worker_account(client, "alice", "pubkey1", memory_mb=2048)
    key2, w2 = _new_worker_account(client, "bob", "pubkey2", memory_mb=2048)
    for key, w in ((key1, w1), (key2, w2)):
        client.post(f"/v1/workers/{w['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key}"})
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    cluster_id = st1["cluster"]["cluster_id"]

    r1 = client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key1}"}, json={})
    assert r1.status_code == 202
    assert r1.json()["status"] == "assembling"
    r2 = client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key2}"}, json={})
    assert r2.json()["status"] == "live"


def test_worker_leave_dissolves_cluster(client: TestClient):
    key1, w1 = _new_worker_account(client, "alice", "pubkey1", memory_mb=2048)
    key2, w2 = _new_worker_account(client, "bob", "pubkey2", memory_mb=2048)
    for key, w in ((key1, w1), (key2, w2)):
        client.post(f"/v1/workers/{w['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key}"})
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    assert st1["status"] == "assigned"

    # w2 leaves -> cluster dissolves, w1 returns to waitlist.
    r = client.delete(f"/v1/workers/{w2['worker_id']}", headers={"Authorization": f"Bearer {key2}"})
    assert r.status_code == 204
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    assert st1["status"] == "waitlisted"
    assert st1["cluster"] is None


def test_worker_key_cannot_access_other_worker(client: TestClient):
    acc = _register_account(client)
    sess = _login(client)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])
    w1 = _register_worker(client, key, wg_pubkey="pubkey1").json()
    # A second account's key should not access w1.
    acc2 = _register_account(client, username="bob", password="hunter2hunter2")
    sess2 = _login(client, username="bob")
    key2 = _create_worker_key(client, acc2["account_id"], sess2["session_token"])
    r = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key2}"})
    assert r.status_code == 403


# ── dual-auth: worker key OR account session ──────────────────────────────
def test_session_token_can_access_own_worker_state(client: TestClient):
    acc = _register_account(client)
    sess = _login(client)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])
    w = _register_worker(client, key, wg_pubkey="pubkey1").json()
    # Owner session token (not the worker key) can read state.
    r = client.get(
        f"/v1/workers/{w['worker_id']}/state",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["worker_id"] == w["worker_id"]


def test_session_token_can_heartbeat_own_worker(client: TestClient):
    acc = _register_account(client)
    sess = _login(client)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])
    w = _register_worker(client, key, wg_pubkey="pubkey1").json()
    r = client.post(
        f"/v1/workers/{w['worker_id']}/heartbeat",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
    )
    assert r.status_code == 200
    assert r.json()["online"] is True


def test_session_token_can_revoke_own_worker(client: TestClient):
    acc = _register_account(client)
    sess = _login(client)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])
    w = _register_worker(client, key, wg_pubkey="pubkey1").json()
    r = client.delete(
        f"/v1/workers/{w['worker_id']}",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
    )
    assert r.status_code == 204
    # Worker is gone.
    r2 = client.get(
        f"/v1/workers/{w['worker_id']}/state",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
    )
    assert r2.status_code == 404


def test_session_token_cannot_access_other_accounts_worker(client: TestClient):
    acc = _register_account(client)
    sess = _login(client)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])
    w = _register_worker(client, key, wg_pubkey="pubkey1").json()
    # Bob's session must NOT access Alice's worker.
    _register_account(client, username="bob", password="hunter2hunter2")
    bobs_sess = _login(client, username="bob")["session_token"]
    r = client.get(
        f"/v1/workers/{w['worker_id']}/state",
        headers={"Authorization": f"Bearer {bobs_sess}"},
    )
    assert r.status_code == 403


def test_user_key_still_rejected_for_workers(client: TestClient):
    acc = _register_account(client)
    sess = _login(client)
    wkey = _create_worker_key(client, acc["account_id"], sess["session_token"])
    w = _register_worker(client, wkey, wg_pubkey="pubkey1").json()
    # User-scoped key is created; it cannot manage workers.
    ukey = client.post(
        f"/v1/accounts/{acc['account_id']}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "user", "scope": "user"},
    ).json()["api_key"]
    r = client.get(
        f"/v1/workers/{w['worker_id']}/state",
        headers={"Authorization": f"Bearer {ukey}"},
    )
    assert r.status_code == 403


def test_session_token_can_fetch_cluster_config_as_member(client: TestClient):
    acc = _register_account(client)
    sess = _login(client)
    wkey = _create_worker_key(client, acc["account_id"], sess["session_token"])
    w = _register_worker(client, wkey, wg_pubkey="pubkey1").json()
    # Second account for cluster formation.
    acc2 = _register_account(client, username="bob", password="hunter2hunter2")
    sess2 = _login(client, username="bob")
    wkey2 = _create_worker_key(client, acc2["account_id"], sess2["session_token"])
    w2 = _register_worker(client, wkey2, wg_pubkey="pubkey2").json()
    for wk, ww in ((wkey, w), (wkey2, w2)):
        client.post(f"/v1/workers/{ww['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {wk}"})
    st = client.get(f"/v1/workers/{w['worker_id']}/state", headers={"Authorization": f"Bearer {sess['session_token']}"}).json()
    cluster_id = st["cluster"]["cluster_id"]
    # Owner session can fetch the cluster config (it owns a member).
    r = client.get(
        f"/v1/clusters/{cluster_id}/config",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
    )
    assert r.status_code == 200
    assert r.json()["cluster_id"] == cluster_id
