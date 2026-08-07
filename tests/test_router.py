"""Tests for the cluster router and server-side WireGuard peer."""
from __future__ import annotations

from prima_pool_server.config import Settings
from prima_pool_server.models import ClusterRecord, ClusterStatus
from prima_pool_server.router import ClusterRouter
from prima_pool_server.store import Store
from prima_pool_server.wg_server import ServerWireGuard, derive_public_key, generate_keypair


def _settings(**kw) -> Settings:
    defaults = dict(
        models={"demo-model": 4096},
        server_join_wg=True,
        server_wg_endpoint_host="203.0.113.99",
        server_wg_listen_port=51821,
        server_wg_ip_offset=254,
        api_port=8080,
    )
    defaults.update(kw)
    return Settings(**defaults)


def test_generate_keypair_roundtrip():
    priv, pub = generate_keypair()
    assert derive_public_key(priv) == pub


def test_server_ip_offset():
    wg = ServerWireGuard(_settings())
    assert wg.server_ip("10.23.1.0/24") == "10.23.1.254"


def test_server_peer_has_role_marker():
    wg = ServerWireGuard(_settings())
    cluster = ClusterRecord(cluster_id="clu_1", model="demo-model", subnet="10.23.1.0/24", members=["w1"])
    peer = wg.server_peer(cluster)
    assert peer["role"] == "server"
    assert peer["allowed_ips"] == ["10.23.1.254/32"]
    assert peer["endpoint"] == "203.0.113.99:51821"


def test_router_finds_live_cluster_head():
    store = Store(path=None)
    store.create_cluster(
        ClusterRecord(
            cluster_id="clu_1",
            model="demo-model",
            subnet="10.23.1.0/24",
            members=["w1", "w2"],
            ips={"w1": "10.23.1.1", "w2": "10.23.1.2"},
            status=ClusterStatus.live,
        )
    )
    router = ClusterRouter(store, _settings())
    cluster = router.find_live_cluster("demo-model")
    assert cluster is not None
    assert router.head_ip(cluster) == "10.23.1.1"
    assert router.head_url(cluster) == "http://10.23.1.1:8080"


def test_router_ignores_assembling_cluster():
    store = Store(path=None)
    store.create_cluster(
        ClusterRecord(
            cluster_id="clu_1",
            model="demo-model",
            subnet="10.23.1.0/24",
            members=["w1"],
            ips={"w1": "10.23.1.1"},
            status=ClusterStatus.assembling,
        )
    )
    router = ClusterRouter(store, _settings())
    assert router.find_live_cluster("demo-model") is None


def test_router_no_cluster_for_model():
    store = Store(path=None)
    router = ClusterRouter(store, _settings())
    assert router.find_live_cluster("demo-model") is None
