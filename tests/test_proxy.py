"""Tests for the inference proxy and server peer in cluster config."""
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
        server_join_wg=True,
        server_wg_endpoint_host="203.0.113.99",
        server_wg_listen_port=51821,
        server_wg_ip_offset=254,
        api_port=8080,
    )


@pytest.fixture()
def store() -> Store:
    return Store(path=None)


@pytest.fixture()
def client(settings: Settings, store: Store):
    app = create_app(settings=settings, store=store)
    with TestClient(app) as c:
        yield c


def _new_user(client: TestClient, username="alice"):
    r = client.post("/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"})
    acc = r.json()
    sess = client.post("/v1/accounts/login", json={"username": username, "password": "hunter2hunter2"}).json()
    key = client.post(
        f"/v1/accounts/{acc['account_id']}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "user", "scope": "user"},
    ).json()["api_key"]
    return key


def _new_worker(client: TestClient, username: str, wg_pubkey: str, memory_mb=2048):
    r = client.post("/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"})
    acc = r.json()
    sess = client.post("/v1/accounts/login", json={"username": username, "password": "hunter2hunter2"}).json()
    key = client.post(
        f"/v1/accounts/{acc['account_id']}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "worker", "scope": "worker"},
    ).json()["api_key"]
    worker = client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "demo-model",
            "gguf_sha256": "a" * 64,
            "memory_allocated_mb": memory_mb,
            "wg_pubkey": wg_pubkey,
            "endpoint": {"host": "203.0.113.10", "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    ).json()
    return key, worker


def test_cluster_config_includes_server_peer(client: TestClient):
    # Form a live cluster and capture a member key.
    key1, w1 = _new_worker(client, "bob", "pubkey1")
    key2, w2 = _new_worker(client, "carol", "pubkey2")
    client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
    client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})
    st1 = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
    cluster_id = st1["cluster"]["cluster_id"]

    cfg = client.get(f"/v1/clusters/{cluster_id}/config", headers={"Authorization": f"Bearer {key1}"})
    assert cfg.status_code == 200, cfg.text
    body = cfg.json()
    # The server peer is appended with role="server".
    server_peers = [p for p in body["peers"] if p.get("role") == "server"]
    assert len(server_peers) == 1
    assert server_peers[0]["allowed_ips"] == ["10.23.1.254/32"]
    # Ring members are the two workers.
    ring_peers = [p for p in body["peers"] if p.get("role") != "server"]
    assert len(ring_peers) == 2


def test_proxy_requires_user_key(client: TestClient):
    # A worker key cannot call the proxy.
    key, _ = _new_worker(client, "frank", "pubkey5")
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": "demo-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 403


def test_proxy_no_live_cluster(client: TestClient):
    user_key = _new_user(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {user_key}"},
        json={"model": "demo-model", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 404


def test_proxy_missing_model(client: TestClient):
    user_key = _new_user(client)
    r = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {user_key}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 400
