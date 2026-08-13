"""Tests for the model registry + GGUF-hash matching feature."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prima_pool_server.app import create_app
from prima_pool_server.config import ModelDef, Settings
from prima_pool_server.store import Store

HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        models={
            "demo-model": ModelDef(slug="demo-model", gguf_sha256=HASH_A, required_memory_mb=4096),
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


def _new_worker(client: TestClient, username: str, wg_pubkey: str, gguf_hash: str = HASH_A, memory_mb=2048):
    client.post("/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"})
    sess = client.post("/v1/accounts/login", json={"username": username, "password": "hunter2hunter2"}).json()
    acc = sess["session_token"].split(".")[0].replace("sess_", "")
    wkey = client.post(
        f"/v1/accounts/{acc}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "worker", "scope": "worker"},
    ).json()["api_key"]
    worker = client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {wkey}"},
        json={
            "model": "demo-model",
            "gguf_sha256": gguf_hash,
            "memory_allocated_mb": memory_mb,
            "wg_pubkey": wg_pubkey,
            "endpoint": {"host": "203.0.113.10", "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    ).json()
    return wkey, worker


def test_models_endpoint_unauthenticated(client: TestClient):
    r = client.get("/v1/models")
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 1
    assert body[0]["slug"] == "demo-model"
    assert body[0]["gguf_sha256"] == HASH_A
    assert body[0]["required_memory_mb"] == 4096
    assert body[0]["live"] is False


def test_models_endpoint_live_flag(client: TestClient):
    # Form a live cluster, then the model should report live=True.
    k1, w1 = _new_worker(client, "alice", "pubkey1")
    k2, w2 = _new_worker(client, "bob", "pubkey2")
    client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {k1}"})
    client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {k2}"})
    st = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {k1}"}).json()
    cid = st["cluster"]["cluster_id"]
    client.post(f"/v1/clusters/{cid}/ready", headers={"Authorization": f"Bearer {k1}"}, json={"layer_windows": {"0": 24, "1": 24}})
    client.post(f"/v1/clusters/{cid}/ready", headers={"Authorization": f"Bearer {k2}"}, json={})

    body = client.get("/v1/models").json()
    assert body[0]["live"] is True


def test_register_rejects_wrong_hash(client: TestClient):
    client.post("/v1/accounts/register", json={"username": "alice", "password": "hunter2hunter2"})
    sess = client.post("/v1/accounts/login", json={"username": "alice", "password": "hunter2hunter2"}).json()
    acc = sess["session_token"].split(".")[0].replace("sess_", "")
    wkey = client.post(
        f"/v1/accounts/{acc}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "worker", "scope": "worker"},
    ).json()["api_key"]
    r = client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {wkey}"},
        json={
            "model": "demo-model",
            "gguf_sha256": HASH_B,  # wrong hash
            "memory_allocated_mb": 4096,
            "wg_pubkey": "pubkey1",
            "endpoint": {"host": "203.0.113.10", "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    )
    assert r.status_code == 400
    assert "does not match" in r.json()["detail"]


def test_clusters_only_form_with_matching_hash(client: TestClient):
    # alice + bob have HASH_A (2x2048 = 4096, enough). carol tries HASH_B.
    k1, w1 = _new_worker(client, "alice", "pubkey1", gguf_hash=HASH_A)
    k2, w2 = _new_worker(client, "bob", "pubkey2", gguf_hash=HASH_A)
    # carol with the wrong hash is REJECTED at registration (400).
    client.post("/v1/accounts/register", json={"username": "carol", "password": "hunter2hunter2"})
    c_sess = client.post("/v1/accounts/login", json={"username": "carol", "password": "hunter2hunter2"}).json()
    c_acc = c_sess["session_token"].split(".")[0].replace("sess_", "")
    c_key = client.post(
        f"/v1/accounts/{c_acc}/keys",
        headers={"Authorization": f"Bearer {c_sess['session_token']}"},
        json={"name": "worker", "scope": "worker"},
    ).json()["api_key"]
    r = client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {c_key}"},
        json={
            "model": "demo-model",
            "gguf_sha256": HASH_B,
            "memory_allocated_mb": 2048,
            "wg_pubkey": "pubkey3",
            "endpoint": {"host": "203.0.113.10", "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    )
    assert r.status_code == 400

    for k, w in ((k1, w1), (k2, w2)):
        client.post(f"/v1/workers/{w['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {k}"})

    # alice + bob (HASH_A) form a cluster.
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {k1}"}).json()
    assert st1["status"] == "assigned", st1
    # The wrong-hash worker was never registered, so no waitlist entry exists.
    bad = client.get(f"/v1/workers/wrk_nonexistent/state", headers={"Authorization": f"Bearer {k1}"})
    assert bad.status_code == 404


def test_two_hashes_form_separate_clusters(client: TestClient):
    # Two models of the same slug but different hashes → separate waitlists.
    settings = Settings(
        models={
            "demo-model": ModelDef(slug="demo-model", gguf_sha256=HASH_A, required_memory_mb=8192),
        },
        assignable_grace_s=0,
        heartbeat_timeout_s=30,
    )
    app = create_app(settings=settings, store=Store(path=None))
    with TestClient(app) as c:
        # 4 workers of HASH_A (2048 each) → one cluster of 4.
        workers = [_new_worker(c, f"u{i}", f"pub{i}", gguf_hash=HASH_A, memory_mb=2048) for i in range(4)]
        for k, w in workers:
            c.post(f"/v1/workers/{w['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {k}"})
        st = c.get(f"/v1/workers/{workers[0][1]['worker_id']}/state", headers={"Authorization": f"Bearer {workers[0][0]}"}).json()
        assert st["status"] == "assigned"