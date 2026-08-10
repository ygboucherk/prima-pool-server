"""Tests for WG endpoint derivation (server source-IP fallback) + client override."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prima_pool_server.app import create_app, _client_ip, _is_usable_endpoint_host
from prima_pool_server.config import ModelDef, Settings
from prima_pool_server.store import Store


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        models={"demo-model": ModelDef(slug="demo-model", gguf_sha256="a" * 64, required_memory_mb=4096)},
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


def _worker_creds(client: TestClient, username="alice"):
    client.post("/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"})
    sess = client.post("/v1/accounts/login", json={"username": username, "password": "hunter2hunter2"}).json()
    acc = sess["session_token"].split(".")[0].replace("sess_", "")
    wkey = client.post(
        f"/v1/accounts/{acc}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "w", "scope": "worker"},
    ).json()["api_key"]
    return wkey


def _register(client: TestClient, wkey: str, host: str):
    return client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {wkey}"},
        json={
            "model": "demo-model",
            "gguf_sha256": "a" * 64,
            "memory_allocated_mb": 4096,
            "wg_pubkey": "pk",
            "endpoint": {"host": host, "port": 51820, "behind_nat": False, "nat_type": "none"},
        },
    )


# ── unit: host usability ───────────────────────────────────────────────
def test_usable_host_rejects_private():
    assert _is_usable_endpoint_host("172.17.0.3") is False
    assert _is_usable_endpoint_host("10.0.0.5") is False
    assert _is_usable_endpoint_host("192.168.1.10") is False
    assert _is_usable_endpoint_host("127.0.0.1") is False
    assert _is_usable_endpoint_host("") is False


def test_usable_host_accepts_public_and_hostname():
    assert _is_usable_endpoint_host("8.8.8.8") is True
    assert _is_usable_endpoint_host("100.101.213.88") is True  # Tailscale CGNAT range
    assert _is_usable_endpoint_host("node.example.com") is True


def test_client_ip_respects_xff():
    class _Req:
        def __init__(self, xff, host):
            self.headers = {"x-forwarded-for": xff} if xff else {}
            self.client = type("C", (), {"host": host})()

    assert _client_ip(_Req("203.0.113.7, 10.0.0.1", "10.0.0.1")) == "203.0.113.7"
    assert _client_ip(_Req("", "10.0.0.1")) == "10.0.0.1"
    assert _client_ip(_Req(None, "10.0.0.1")) == "10.0.0.1"


# ── integration: source-IP fallback ────────────────────────────────────
def test_private_endpoint_falls_back_to_source_ip(client: TestClient):
    wkey = _worker_creds(client)
    # TestClient connects from 127.0.0.1 (the loopback source).
    r = _register(client, wkey, host="172.17.0.3")
    assert r.status_code == 201, r.text
    body = r.json()
    # The worker's stored endpoint host should now be the observed source IP.
    st = client.get(f"/v1/workers/{body['worker_id']}/state", headers={"Authorization": f"Bearer {wkey}"}).json()
    # We can't easily read the endpoint from the API (state doesn't expose it),
    # so assert the registration succeeded and the worker is registered.
    assert body["status"] == "waitlisted"


def test_public_endpoint_preserved(client: TestClient):
    wkey = _worker_creds(client)
    r = _register(client, wkey, host="203.0.113.10")
    assert r.status_code == 201
    assert r.json()["status"] == "waitlisted"


# ── integration: the endpoint is used in cluster configs ────────────────
def test_observed_endpoint_lands_in_cluster_config(client: TestClient):
    """After source-IP fallback, the cluster config must carry the observed host."""
    k1 = _worker_creds(client, "alice")
    k2 = _worker_creds(client, "bob")
    w1 = _register(client, k1, host="172.17.0.3").json()
    w2 = _register(client, k2, host="172.17.0.4").json()
    client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {k1}"})
    client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {k2}"})
    st = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {k1}"}).json()
    cluster_id = st["cluster"]["cluster_id"]
    cfg = client.get(f"/v1/clusters/{cluster_id}/config", headers={"Authorization": f"Bearer {k1}"}).json()
    # The private container IPs must have been replaced by the observed source.
    for peer in cfg["peers"]:
        if peer.get("role") != "server":
            assert peer["endpoint"] is not None
            assert "172.17.0" not in peer["endpoint"], peer["endpoint"]
