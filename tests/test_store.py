"""Store persistence round-trip tests.

Verifies that the JSON store correctly serializes/deserializes Pydantic models
(endpoint, hardware) and cluster state (ready set, ips dict) — these were
previously corrupted to strings by json.dump(default=str).
"""
from __future__ import annotations

import os

from prima_pool_server.models import (
    ClusterRecord,
    ClusterStatus,
    EndpointInfo,
    Hardware,
    WorkerRecord,
    WorkerStatus,
)
from prima_pool_server.store import Store


def test_worker_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "store.json")
    s = Store(path=path)
    rec = WorkerRecord(
        worker_id="wrk_1",
        account_id="acc_1",
        model="demo-model",
        memory_allocated_mb=2048,
        wg_pubkey="pub",
        endpoint=EndpointInfo(host="1.2.3.4", port=51820, behind_nat=True, nat_type="cone"),
        hardware=Hardware(cpu="x", os="linux"),
        status=WorkerStatus.waitlisted,
        online=True,
    )
    s.create_worker(rec)

    s2 = Store(path=path)
    w = s2.get_worker("wrk_1")
    assert w is not None
    # endpoint/hardware must be restored as Pydantic objects, not strings.
    assert isinstance(w.endpoint, EndpointInfo)
    assert w.endpoint.host == "1.2.3.4"
    assert w.endpoint.behind_nat is True
    assert isinstance(w.hardware, Hardware)
    assert w.hardware.cpu == "x"
    assert w.status == WorkerStatus.waitlisted
    assert w.online is True


def test_cluster_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "store.json")
    s = Store(path=path)
    clu = ClusterRecord(
        cluster_id="clu_1",
        model="demo-model",
        subnet="10.23.1.0/24",
        members=["wrk_1", "wrk_2"],
        ips={"wrk_1": "10.23.1.1", "wrk_2": "10.23.1.2"},
        status=ClusterStatus.live,
        ready={"wrk_1", "wrk_2"},
    )
    s.create_cluster(clu)

    s2 = Store(path=path)
    c = s2.get_cluster("clu_1")
    assert c is not None
    assert c.ready == {"wrk_1", "wrk_2"}
    assert c.ips == {"wrk_1": "10.23.1.1", "wrk_2": "10.23.1.2"}
    assert c.status == ClusterStatus.live
    assert c.members == ["wrk_1", "wrk_2"]


def test_account_and_key_persistence(tmp_path):
    path = str(tmp_path / "store.json")
    s = Store(path=path)
    acc = s.create_account("alice", "hunter2hunter2")
    assert acc is not None
    key, secret = s.create_api_key(acc.account_id, "worker", "worker")

    s2 = Store(path=path)
    assert s2.get_account_by_username("alice") is not None
    # The key must resolve by its plaintext secret after reload.
    resolved = s2.resolve_api_key(secret)
    assert resolved is not None
    assert resolved.key_id == key.key_id
