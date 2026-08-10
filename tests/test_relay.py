"""Tests for the relay: cluster config relay block + client relay rendering."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from prima_pool_server.app import create_app
from prima_pool_server.config import ModelDef, Settings
from prima_pool_server.store import Store

from prima_pool_client.config import ClientConfig
from prima_pool_client.models import (
    ClusterConfig as ClientClusterConfig,
    InterfaceConfig,
    PeerConfig,
    Preferred,
    RelayConfig,
)
from prima_pool_client.wireguard import render_wg_conf

RELAY_PUBKEY = "relay_pubkey_1234"
RELAY_ENDPOINT = "relay1.pool.example.com:51822"


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        models={"demo-model": ModelDef(slug="demo-model", gguf_sha256="a" * 64, required_memory_mb=4096)},
        assignable_grace_s=0,
        heartbeat_timeout_s=30,
        relay_enabled=True,
        relay_pubkey=RELAY_PUBKEY,
        relay_endpoint=RELAY_ENDPOINT,
    )


@pytest.fixture()
def store() -> Store:
    return Store(path=None)


@pytest.fixture()
def client(settings: Settings, store: Store):
    app = create_app(settings=settings, store=store)
    with TestClient(app) as c:
        yield c


def _new_worker(client: TestClient, username: str, wg_pubkey: str):
    client.post("/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"})
    sess = client.post("/v1/accounts/login", json={"username": username, "password": "hunter2hunter2"}).json()
    acc = sess["session_token"].split(".")[0].replace("sess_", "")
    wkey = client.post(
        f"/v1/accounts/{acc}/keys",
        headers={"Authorization": f"Bearer {sess['session_token']}"},
        json={"name": "w", "scope": "worker"},
    ).json()["api_key"]
    worker = client.post(
        "/v1/workers/register",
        headers={"Authorization": f"Bearer {wkey}"},
        json={
            "model": "demo-model",
            "gguf_sha256": "a" * 64,
            "memory_allocated_mb": 2048,
            "wg_pubkey": wg_pubkey,
            "endpoint": {"host": "8.8.8.8", "port": 51820, "behind_nat": True, "nat_type": "symmetric"},
        },
    ).json()
    return wkey, worker


def test_relay_block_in_cluster_config(client: TestClient):
    k1, w1 = _new_worker(client, "alice", "pk1")
    k2, w2 = _new_worker(client, "bob", "pk2")
    client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {k1}"})
    client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {k2}"})
    st = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {k1}"}).json()
    cid = st["cluster"]["cluster_id"]
    cfg = client.get(f"/v1/clusters/{cid}/config", headers={"Authorization": f"Bearer {k1}"}).json()
    assert cfg["relay"]["enabled"] is True
    assert cfg["relay"]["pubkey"] == RELAY_PUBKEY
    assert cfg["relay"]["endpoint"] == RELAY_ENDPOINT
    # behind_nat workers should be marked preferred=relay.
    for peer in cfg["peers"]:
        if peer.get("role") != "server":
            assert peer["preferred"] == "relay", peer


def test_client_renders_relay_peer():
    cfg = ClientClusterConfig(
        cluster_id="clu_1",
        interface=InterfaceConfig(private_ip="10.23.1.2", subnet="10.23.1.0/24", mtu=1280),
        relay=RelayConfig(pubkey=RELAY_PUBKEY, endpoint=RELAY_ENDPOINT, enabled=True),
        peers=[
            PeerConfig(pubkey="A", allowed_ips=["10.23.1.1/32"], preferred=Preferred.direct),
            PeerConfig(pubkey="B", allowed_ips=["10.23.1.3/32"], preferred=Preferred.relay),
        ],
    )
    conf = render_wg_conf(cfg, "PRIV", 51820)
    assert f"PublicKey = {RELAY_PUBKEY}" in conf
    assert f"Endpoint = {RELAY_ENDPOINT}" in conf
    # The relay peer's AllowedIPs cover all member IPs.
    assert "10.23.1.1/32, 10.23.1.3/32" in conf


def test_client_config_default_relay_check():
    cfg = ClientConfig()
    assert cfg.wg_relay_check_s == 10
