"""End-to-end API tests for the control plane using FastAPI TestClient."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from prima_pool_server.app import create_app
from prima_pool_server.config import ModelDef, Settings
from prima_pool_server.models import ClusterRecord, ClusterStatus
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
def settings_with_grace(settings: Settings) -> Settings:
    """Same models, but a non-zero assignable grace period."""
    return Settings(
        models=settings.models,
        assignable_grace_s=5,
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


@pytest.fixture()
def client_with_grace(settings_with_grace: Settings):
    app = create_app(settings=settings_with_grace, store=Store(path=None))
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


def _create_worker_key(client: TestClient, account_id: str, session_token: str, name="worker"):
    r = client.post(
        f"/v1/accounts/{account_id}/keys",
        headers={"Authorization": f"Bearer {session_token}"},
        json={"name": name, "scope": "worker"},
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


def test_same_account_can_register_multiple_workers_same_model(client: TestClient):
    """One account may run the same model on several machines (different WG pubkeys)."""
    acc = _register_account(client)
    sess = _login(client)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])

    w1 = _register_worker(client, key, wg_pubkey="pubkey-device-1").json()
    w2 = _register_worker(client, key, wg_pubkey="pubkey-device-2").json()

    assert w1["worker_id"] != w2["worker_id"]
    assert w1["status"] == "waitlisted"
    assert w2["status"] == "waitlisted"


def test_same_device_reregister_updates_endpoint(client: TestClient, store: Store):
    """Re-registering the same device (same WG pubkey) is NOT a conflict — it's
    the device re-advertising its endpoint (e.g. moved networks or set
    PRIMA_POOL_WG_ENDPOINT_HOST). The server must update the existing worker's
    endpoint in place and keep the same worker_id."""
    acc = _register_account(client)
    sess = _login(client)
    key = _create_worker_key(client, acc["account_id"], sess["session_token"])

    # First registration with a public endpoint.
    r1 = client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "demo-model",
            "gguf_sha256": "a" * 64,
            "memory_allocated_mb": 4096,
            "wg_pubkey": "pubkey-device-1",
            "endpoint": {"host": "8.8.8.8", "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    )
    assert r1.status_code == 201, r1.text
    prev_id = r1.json()["worker_id"]

    # Re-register the same device with a DIFFERENT public endpoint host.
    r2 = client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "demo-model",
            "gguf_sha256": "a" * 64,
            "memory_allocated_mb": 4096,
            "wg_pubkey": "pubkey-device-1",
            "endpoint": {"host": "1.1.1.1", "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    )
    assert r2.status_code == 201, r2.text
    w2 = r2.json()
    # Same worker (identity preserved), endpoint updated.
    assert w2["worker_id"] == prev_id
    stored = store.get_worker(prev_id)
    assert stored.endpoint.host == "1.1.1.1"


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


def test_cluster_formation_after_grace_period(client_with_grace, settings_with_grace, monkeypatch):
    """Regression: with assignable_grace_s > 0, both workers need a SECOND
    heartbeat before the cluster forms. The old code only re-checked formation
    on offline→online transitions, so this scenario stayed waitlisted forever.
    """
    # Deterministic clock: keep real time as a base, but let the test advance it.
    real_time = time.time
    fake_now = [real_time()]
    monkeypatch.setattr(time, "time", lambda: fake_now[0])

    def advance(seconds: float):
        fake_now[0] += seconds

    client = client_with_grace
    key1, w1 = _new_worker_account(client, "alice", "pubkey1", memory_mb=2048)
    key2, w2 = _new_worker_account(client, "bob", "pubkey2", memory_mb=2048)

    # First heartbeat: worker comes online, assignable_at = now + 5s → NOT yet eligible.
    client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
    client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    assert st1["status"] == "waitlisted", st1

    # Advance past the grace period, then the next heartbeat must form the cluster.
    advance(10.0)
    client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
    client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    assert st1["status"] == "assigned", st1
    assert st1["cluster"] is not None


def test_same_account_two_workers_same_cluster_both_ready(client: TestClient):
    """Regression: two workers of ONE account in the SAME cluster must BOTH be
    able to report ready, and the cluster must go live.

    The old code resolved the reporting member by account_id + cluster_id with
    a `next()`, which attributed both readiness reports to the SAME worker —
    so with one account running two machines, the cluster never went live.
    """
    acc = _register_account(client)
    sess = _login(client)
    key1 = _create_worker_key(client, acc["account_id"], sess["session_token"], "device-1")
    key2 = _create_worker_key(client, acc["account_id"], sess["session_token"], "device-2")

    w1 = _register_worker(client, key1, wg_pubkey="pubkey-device-1", memory_mb=2048).json()
    w2 = _register_worker(client, key2, wg_pubkey="pubkey-device-2", memory_mb=2048).json()
    assert w1["worker_id"] != w2["worker_id"]

    # Heartbeat both → cluster forms (2048+2048 = 4096).
    client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
    client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})

    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    assert st1["status"] == "assigned", st1
    cluster_id = st1["cluster"]["cluster_id"]
    assert st1["cluster"]["assigned_ip"] != 0

    st2 = client.get(f"/v1/workers/{w2['worker_id']}/state", headers={"Authorization": f"Bearer {key2}"}).json()
    assert st2["status"] == "assigned", st2
    assert st2["cluster"]["cluster_id"] == cluster_id

    # Both members must be able to GET the config with their own key.
    cfg1 = client.get(f"/v1/clusters/{cluster_id}/config", headers={"Authorization": f"Bearer {key1}"})
    assert cfg1.status_code == 200, cfg1.text
    cfg2 = client.get(f"/v1/clusters/{cluster_id}/config", headers={"Authorization": f"Bearer {key2}"})
    assert cfg2.status_code == 200, cfg2.text
    # Each config carries the member's OWN private IP.
    assert cfg1.json()["interface"]["private_ip"] == st1["cluster"]["assigned_ip"]
    assert cfg2.json()["interface"]["private_ip"] == st2["cluster"]["assigned_ip"]

    # Both report ready → live (each with its own key). The head (key1)
    # carries the layer distribution, required for the cluster to go live.
    r1 = client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key1}"}, json={"layer_windows": {"0": 24, "1": 24}})
    assert r1.status_code == 202 and r1.json()["status"] == "assembling", r1.text
    r2 = client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key2}"}, json={})
    assert r2.status_code == 202, r2.text
    assert r2.json()["status"] == "live", r2.text
    assert r2.json()["members_ready"] == 2


def test_unbound_keys_self_heal_on_heartbeat(client: TestClient, store: Store):
    """Regression: keys issued BEFORE the key→worker binding feature existed
    are unbound. When one account has two workers in the SAME cluster, an
    unbound key must still work for config/ready — the worker's heartbeat
    (which carries worker_id) binds the key. Readiness then works for both.
    """
    acc = _register_account(client)
    sess = _login(client)
    key1 = _create_worker_key(client, acc["account_id"], sess["session_token"], "device-1")
    key2 = _create_worker_key(client, acc["account_id"], sess["session_token"], "device-2")

    w1 = _register_worker(client, key1, wg_pubkey="pubkey-device-1", memory_mb=2048).json()
    w2 = _register_worker(client, key2, wg_pubkey="pubkey-device-2", memory_mb=2048).json()

    # Simulate the UPGRADE path: registration now binds the key, but keys
    # created before the feature shipped are unbound. Unbind both to emulate
    # an existing pre-upgrade deployment.
    conn = store._conn
    for key in (key1, key2):
        rec = store.resolve_api_key(key)
        conn.execute("UPDATE api_keys SET worker_id = NULL WHERE key_id = ?", (rec.key_id,))
    conn.commit()

    # Sanity: keys are now unbound.
    assert store.resolve_api_key(key1).worker_id is None
    assert store.resolve_api_key(key2).worker_id is None

    # Heartbeat both → cluster forms. The heartbeat heals each key's binding.
    client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
    client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})

    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    assert st1["status"] == "assigned", st1
    cluster_id = st1["cluster"]["cluster_id"]

    # Both can fetch config now (their keys were bound by the heartbeats).
    cfg1 = client.get(f"/v1/clusters/{cluster_id}/config", headers={"Authorization": f"Bearer {key1}"})
    assert cfg1.status_code == 200, cfg1.text
    cfg2 = client.get(f"/v1/clusters/{cluster_id}/config", headers={"Authorization": f"Bearer {key2}"})
    assert cfg2.status_code == 200, cfg2.text

    # Both report ready → live.
    client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key1}"}, json={"layer_windows": {"0": 24, "1": 24}})
    r2 = client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key2}"}, json={})
    assert r2.json()["status"] == "live", r2.text
    assert r2.json()["members_ready"] == 2

    # Keys are now actually bound to the right workers.
    assert store.resolve_api_key(key1).worker_id == w1["worker_id"]  # type: ignore[union-attr]
    assert store.resolve_api_key(key2).worker_id == w2["worker_id"]  # type: ignore[union-attr]


def test_cluster_ready_and_live(client: TestClient):
    key1, w1 = _new_worker_account(client, "alice", "pubkey1", memory_mb=2048)
    key2, w2 = _new_worker_account(client, "bob", "pubkey2", memory_mb=2048)
    for key, w in ((key1, w1), (key2, w2)):
        client.post(f"/v1/workers/{w['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key}"})
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    cluster_id = st1["cluster"]["cluster_id"]

    r1 = client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key1}"}, json={"layer_windows": {"0": 24, "1": 24}})
    assert r1.status_code == 202
    assert r1.json()["status"] == "assembling"
    r2 = client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key2}"}, json={})
    assert r2.json()["status"] == "live"


def test_worker_leave_dissolves_cluster(client: TestClient, store: Store):
    key1, w1 = _new_worker_account(client, "alice", "pubkey1", memory_mb=2048)
    key2, w2 = _new_worker_account(client, "bob", "pubkey2", memory_mb=2048)
    for key, w in ((key1, w1), (key2, w2)):
        client.post(f"/v1/workers/{w['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key}"})
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    assert st1["status"] == "assigned"
    cluster_id = st1["cluster"]["cluster_id"]

    # w2 leaves -> cluster dissolves, w1 returns to waitlist.
    r = client.delete(f"/v1/workers/{w2['worker_id']}", headers={"Authorization": f"Bearer {key2}"})
    assert r.status_code == 204
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    assert st1["status"] == "waitlisted"
    assert st1["cluster"] is None

    # The cluster row is retained (not deleted) and marked terminated, so its
    # history survives for accounting/auditing.
    cluster = store.get_cluster(cluster_id)
    assert cluster is not None
    assert cluster.status.value == "terminated"


def test_terminated_cluster_subnet_is_reused(store: Store):
    """A terminated cluster's subnet must be freed for reuse.

    Since terminated clusters are retained forever, their subnets must not
    count as 'used' — otherwise the 255-subnet pool would eventually exhaust
    and block all future cluster formation.
    """
    from prima_pool_server.scheduler import Scheduler

    scheduler = Scheduler(store=store, settings=Settings(), hub=None)

    # A live cluster occupies 10.23.1.0/24.
    live = ClusterRecord(
        cluster_id="clu_live",
        model="demo-model",
        subnet="10.23.1.0/24",
        members=["w1"],
        ips={"w1": "10.23.1.1"},
        status=ClusterStatus.live,
    )
    store.create_cluster(live)
    assert scheduler._next_subnet() == "10.23.2.0/24"

    # Terminate it: the subnet is freed for reuse.
    live.status = ClusterStatus.terminated
    store.update_cluster(live)
    assert scheduler._next_subnet() == "10.23.1.0/24"


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
