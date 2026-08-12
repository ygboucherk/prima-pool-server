"""WebSocket push-channel tests.

Verifies that cluster_assigned / cluster_dissolved frames are actually pushed
over the WS channel (these were previously never sent).
"""
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


def _new_worker_account(client: TestClient, username: str, wg_pubkey: str, memory_mb: int = 2048):
    r = client.post("/v1/accounts/register", json={"username": username, "password": "hunter2hunter2"})
    assert r.status_code == 201, r.text
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


def test_cluster_assigned_pushed_over_ws(client: TestClient):
    key1, w1 = _new_worker_account(client, "alice", "pubkey1")
    key2, w2 = _new_worker_account(client, "bob", "pubkey2")

    # Open WS for worker 1 before the cluster forms.
    with client.websocket_connect(f"/v1/workers/{w1['worker_id']}/events?api_key={key1}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello"

        # Heartbeat both to trigger cluster formation.
        client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
        client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})

        # Worker 1 should receive cluster_assigned.
        frame = ws.receive_json()
        assert frame["type"] == "cluster_assigned"
        assert frame["cluster_id"].startswith("clu_")
        assert frame["ring_position"] == 0  # w1 registered first -> head


def test_cluster_dissolved_pushed_over_ws(client: TestClient):
    key1, w1 = _new_worker_account(client, "alice", "pubkey1")
    key2, w2 = _new_worker_account(client, "bob", "pubkey2")

    with client.websocket_connect(f"/v1/workers/{w1['worker_id']}/events?api_key={key1}") as ws:
        ws.receive_json()  # hello
        client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
        client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})
        assigned = ws.receive_json()
        assert assigned["type"] == "cluster_assigned"

        # w2 leaves -> cluster dissolves -> w1 gets cluster_dissolved.
        client.delete(f"/v1/workers/{w2['worker_id']}", headers={"Authorization": f"Bearer {key2}"})
        frame = ws.receive_json()
        assert frame["type"] == "cluster_dissolved"
        assert frame["reason"] == "member_left"


def test_layer_distribution_over_ws_marks_cluster_live(client: TestClient, store: Store):
    """The head reports the layer distribution over WS; combined with all
    members reporting ready, the cluster goes live and the distribution is
    recorded keyed by worker_id."""
    key1, w1 = _new_worker_account(client, "alice", "pubkey1", memory_mb=2048)
    key2, w2 = _new_worker_account(client, "bob", "pubkey2", memory_mb=2048)

    # Open WS for the head (w1) before the cluster forms.
    with client.websocket_connect(f"/v1/workers/{w1['worker_id']}/events?api_key={key1}") as ws:
        ws.receive_json()  # hello
        client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
        client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})
        assigned = ws.receive_json()
        assert assigned["type"] == "cluster_assigned"
        cluster_id = assigned["cluster_id"]

        # Both members report ready; the head also sends the distribution over WS.
        client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key1}"}, json={})
        client.post(f"/v1/clusters/{cluster_id}/ready", headers={"Authorization": f"Bearer {key2}"}, json={})
        # Cluster must NOT be live yet (no distribution reported).
        st = client.get(f"/v1/workers/{w1['worker_id']}/state", headers={"Authorization": f"Bearer {key1}"}).json()
        assert st["status"] == "assigned"

        # Head sends the distribution over WS (rank-keyed).
        ws.send_json(
            {
                "type": "layer_distribution",
                "cluster_id": cluster_id,
                "layer_windows": {"0": 24, "1": 24},
            }
        )

        # The server processes the WS frame asynchronously; poll until the
        # distribution is recorded (with a timeout).
        import time as _time

        cluster = None
        deadline = _time.time() + 5.0
        while _time.time() < deadline:
            cluster = store.get_cluster(cluster_id)
            if cluster is not None and cluster.layer_windows is not None:
                break
            _time.sleep(0.05)
        assert cluster is not None
        assert cluster.status.value == "live"
        assert cluster.layer_windows == {w1["worker_id"]: 24, w2["worker_id"]: 24}


def test_layer_distribution_rejected_from_worker(client: TestClient, store: Store):
    """A non-head worker's distribution frame must be ignored."""
    key1, w1 = _new_worker_account(client, "alice", "pubkey1", memory_mb=2048)
    key2, w2 = _new_worker_account(client, "bob", "pubkey2", memory_mb=2048)

    with client.websocket_connect(f"/v1/workers/{w2['worker_id']}/events?api_key={key2}") as ws:
        ws.receive_json()  # hello
        client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
        client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})
        assigned = ws.receive_json()
        assert assigned["type"] == "cluster_assigned"
        cluster_id = assigned["cluster_id"]

        # w2 is rank 1 (worker). Its distribution frame must be ignored.
        ws.send_json(
            {
                "type": "layer_distribution",
                "cluster_id": cluster_id,
                "layer_windows": {"0": 1, "1": 47},
            }
        )
        # Give the server a moment to (attempt to) process the frame.
        import time as _time

        _time.sleep(0.2)
        cluster = store.get_cluster(cluster_id)
        assert cluster is not None
        assert cluster.layer_windows is None


def test_layer_distribution_does_not_resurrect_terminated_cluster(client: TestClient, store: Store):
    """A late distribution frame racing a dissolve must NOT flip a terminated
    cluster back to live — the proxy would otherwise target a dead cluster."""
    key1, w1 = _new_worker_account(client, "alice", "pubkey1", memory_mb=2048)
    key2, w2 = _new_worker_account(client, "bob", "pubkey2", memory_mb=2048)

    with client.websocket_connect(f"/v1/workers/{w1['worker_id']}/events?api_key={key1}") as ws:
        ws.receive_json()  # hello
        client.post(f"/v1/workers/{w1['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key1}"})
        client.post(f"/v1/workers/{w2['worker_id']}/heartbeat", headers={"Authorization": f"Bearer {key2}"})
        assigned = ws.receive_json()
        assert assigned["type"] == "cluster_assigned"
        cluster_id = assigned["cluster_id"]

        # Dissolve: w2 leaves → cluster terminated.
        client.delete(f"/v1/workers/{w2['worker_id']}", headers={"Authorization": f"Bearer {key2}"})
        ws.receive_json()  # cluster_dissolved for w1

        # A late distribution frame from the head must NOT resurrect it.
        ws.send_json(
            {
                "type": "layer_distribution",
                "cluster_id": cluster_id,
                "layer_windows": {"0": 24, "1": 24},
            }
        )
        import time as _time

        _time.sleep(0.2)
        cluster = store.get_cluster(cluster_id)
        assert cluster is not None
        assert cluster.status.value == "terminated"
