"""Store persistence tests.

Verifies SQLite round-trips (endpoint/hardware, ready set, ips dict), legacy
JSON migration (including FK ordering with worker→cluster refs), and that data
is not erased between restarts.
"""
from __future__ import annotations

import os

from prima_pool_server.models import (
    ClusterRecord,
    ClusterStatus,
    EndpointInfo,
    Hardware,
    RequestRecord,
    WorkerRecord,
    WorkerStatus,
)
from prima_pool_server.store import Store


def _worker(account_id: str, worker_id: str, wg_pubkey: str, model="demo-model") -> WorkerRecord:
    return WorkerRecord(
        worker_id=worker_id,
        account_id=account_id,
        model=model,
        gguf_sha256="a" * 64,
        memory_allocated_mb=2048,
        wg_pubkey=wg_pubkey,
        endpoint=EndpointInfo(host="1.2.3.4", port=51820, behind_nat=False, nat_type="none"),
        hardware=None,
        status=WorkerStatus.waitlisted,
        online=True,
    )


def test_create_worker_if_available_cap(tmp_path):
    s = Store(path=str(tmp_path / "store.db"))
    acc = s.create_account("alice", "hunter2hunter2")
    assert acc is not None
    assert s.create_worker_if_available(_worker(acc.account_id, "wrk_1", "pub-a"), max_per_account=1)
    # Cap reached — no more workers, even with a different pubkey.
    assert not s.create_worker_if_available(_worker(acc.account_id, "wrk_2", "pub-b"), max_per_account=1)


def test_create_worker_if_available_allows_same_model_different_device(tmp_path):
    s = Store(path=str(tmp_path / "store.db"))
    acc = s.create_account("alice", "hunter2hunter2")
    assert acc is not None
    assert s.create_worker_if_available(_worker(acc.account_id, "wrk_1", "pub-a"), max_per_account=4)
    # Same model + different WG pubkey = a second device in the same account.
    assert s.create_worker_if_available(_worker(acc.account_id, "wrk_2", "pub-b"), max_per_account=4)
    assert len(s.list_workers_for_account(acc.account_id)) == 2


def test_create_worker_if_available_rejects_same_device(tmp_path):
    s = Store(path=str(tmp_path / "store.db"))
    acc = s.create_account("alice", "hunter2hunter2")
    assert acc is not None
    assert s.create_worker_if_available(_worker(acc.account_id, "wrk_1", "pub-a"), max_per_account=4)
    # Same WG pubkey = the same physical device re-registering.
    assert not s.create_worker_if_available(_worker(acc.account_id, "wrk_2", "pub-a"), max_per_account=4)


def test_migrate_existing_db_adds_api_key_worker_id(tmp_path):
    """A pre-existing DB (created before api_keys.worker_id) must be upgraded
    in place — CREATE TABLE IF NOT EXISTS never adds columns, so without the
    migration the server crashes at startup on `no such column: worker_id`."""
    import sqlite3

    from prima_pool_server.security import hash_api_key

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
        """
    )
    conn.execute("INSERT INTO accounts VALUES ('acc_1','alice','h',1.0)")
    conn.execute(
        "INSERT INTO workers (worker_id, account_id, model, gguf_sha256, memory_allocated_mb, "
        "status, created_at, wg_pubkey, endpoint_json) "
        "VALUES ('wrk_1','acc_1','m','a'*64,4096,'waitlisted',1.0,'pk','null')"
    )
    key_hash = hash_api_key("sk-worker-test")
    conn.execute(
        "INSERT INTO api_keys (key_id, account_id, name, scope, key_hash, created_at) "
        "VALUES ('key_1','acc_1','n','worker',?,1.0)",
        (key_hash,),
    )
    conn.commit()
    conn.close()

    # Opening with the current Store must migrate (no crash) and allow binding.
    s = Store(path=db)
    s.bind_api_key_to_worker("key_1", "wrk_1")
    assert s.resolve_api_key("sk-worker-test").worker_id == "wrk_1"
    s.close()

    # Idempotent: reopening again is fine and the binding survives.
    s2 = Store(path=db)
    assert s2.resolve_api_key("sk-worker-test").worker_id == "wrk_1"


def test_migrate_existing_db_adds_account_permissions(tmp_path):
    """A pre-v0.7 accounts table is upgraded in place: the four permission
    columns are added with the historical defaults (non-admin, can work + use,
    not banned), and existing rows read back with those defaults."""
    import sqlite3

    db = str(tmp_path / "store.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE accounts (account_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, created_at REAL NOT NULL);
        """
    )
    conn.execute("INSERT INTO accounts VALUES ('acc_1','alice','h',1.0)")
    conn.commit()
    conn.close()

    s = Store(path=db)
    acc = s.get_account("acc_1")
    assert acc is not None
    # Migrated (pre-v0.7) rows keep the HISTORICAL open-pool default: can_work=True.
    assert acc.is_admin is False
    assert acc.can_work is True
    assert acc.can_use is True
    assert acc.banned is False
    # NEW accounts use the new default: can_work=False, can_use=True.
    new = s.create_account("bob", "hunter2hunter2")
    assert new is not None
    assert new.is_admin is False
    assert new.can_work is False
    assert new.can_use is True
    assert new.banned is False
    s.close()


def test_account_permission_roundtrip_and_last_admin(tmp_path):
    s = Store(path=str(tmp_path / "store.db"))
    acc = s.create_account("alice", "hunter2hunter2")
    assert acc is not None
    assert s.count_admins() == 0
    # New account default: cannot work, can use.
    assert acc.can_work is False and acc.can_use is True
    assert s.update_account_permissions(acc.account_id, is_admin=True)
    assert s.count_admins() == 1
    got = s.get_account(acc.account_id)
    assert got.is_admin and not got.can_work and got.can_use and not got.banned
    # Toggle flags.
    assert s.update_account_permissions(acc.account_id, can_work=True, banned=True)
    got = s.get_account(acc.account_id)
    assert got.can_work is True and got.banned is True
    # List accounts reflects the flags.
    assert s.list_accounts()[0].username == "alice"


def test_worker_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "store.db")
    s = Store(path=path)
    # The worker FK references an account; create it first.
    acc = s.create_account("alice", "hunter2hunter2")
    assert acc is not None
    rec = WorkerRecord(
        worker_id="wrk_1",
        account_id=acc.account_id,
        model="demo-model",
        gguf_sha256="a" * 64,
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
    assert w.gguf_sha256 == "a" * 64
    # endpoint/hardware must be restored as Pydantic objects, not strings.
    assert isinstance(w.endpoint, EndpointInfo)
    assert w.endpoint.host == "1.2.3.4"
    assert w.endpoint.behind_nat is True
    assert isinstance(w.hardware, Hardware)
    assert w.hardware.cpu == "x"
    assert w.status == WorkerStatus.waitlisted
    assert w.online is True


def test_cluster_persistence_roundtrip(tmp_path):
    path = str(tmp_path / "store.db")
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


def test_cluster_layer_windows_persistence_roundtrip(tmp_path):
    """The head-reported layer distribution persists across store reopens."""
    path = str(tmp_path / "store.db")
    s = Store(path=path)
    clu = ClusterRecord(
        cluster_id="clu_1",
        model="demo-model",
        subnet="10.23.1.0/24",
        members=["wrk_1", "wrk_2"],
        ips={"wrk_1": "10.23.1.1", "wrk_2": "10.23.1.2"},
    )
    s.create_cluster(clu)
    assert s.set_cluster_layer_windows("clu_1", {"wrk_1": 24, "wrk_2": 24}) is True

    s2 = Store(path=path)
    c = s2.get_cluster("clu_1")
    assert c is not None
    assert c.layer_windows == {"wrk_1": 24, "wrk_2": 24}

    # Unknown (None) is recorded as present-but-empty, not missing.
    assert s2.set_cluster_layer_windows("clu_1", None) is True
    c2 = s2.get_cluster("clu_1")
    assert c2 is not None
    assert c2.layer_windows is None


def test_migrate_existing_db_adds_cluster_layer_windows(tmp_path):
    """A pre-existing clusters table (without layer_windows_json) must be
    upgraded in place so the server doesn't crash on `no such column`, and its
    membership JSON must be lifted into the cluster_members junction table."""
    import sqlite3

    db = str(tmp_path / "store.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE clusters (cluster_id TEXT PRIMARY KEY, model TEXT NOT NULL,
            subnet TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            members_json TEXT NOT NULL, ready_json TEXT NOT NULL, ips_json TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO clusters VALUES ('clu_1','m','10.0.0.0/24','assembling',1.0,"
        "'[\"wrk_1\"]','[]','{\"wrk_1\":\"10.0.0.1\"}')"
    )
    conn.commit()
    conn.close()

    s = Store(path=db)
    c = s.get_cluster("clu_1")
    assert c is not None
    # Membership lifted into the junction table: the member + its IP survive.
    assert c.members == ["wrk_1"]
    assert c.ips == {"wrk_1": "10.0.0.1"}
    assert c.layer_windows is None
    # The new column is writable after migration (member row exists now).
    assert s.set_cluster_layer_windows("clu_1", {"wrk_1": 24}) is True
    assert s.get_cluster("clu_1").layer_windows == {"wrk_1": 24}
    s.close()


def test_migrate_backfills_unknown_distribution_for_live_clusters(tmp_path):
    """A LIVE cluster created before layer accounting must be backfilled with
    an explicit 'unknown' distribution ({}) on upgrade — the invariant is that
    a live cluster always carries a distribution field. This matters because a
    server upgrade does NOT dissolve live clusters (workers re-heartbeat), so a
    NULL would persist indefinitely. Non-live clusters stay untouched."""
    import sqlite3

    db = str(tmp_path / "store.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE clusters (cluster_id TEXT PRIMARY KEY, model TEXT NOT NULL,
            subnet TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            members_json TEXT NOT NULL, ready_json TEXT NOT NULL, ips_json TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO clusters VALUES ('clu_live','m','10.0.0.0/24','live',1.0,'[]','[]','{}')"
    )
    conn.execute(
        "INSERT INTO clusters VALUES ('clu_assem','m','10.0.1.0/24','assembling',1.0,'[]','[]','{}')"
    )
    conn.execute(
        "INSERT INTO clusters VALUES ('clu_term','m','10.0.2.0/24','terminated',1.0,'[]','[]','{}')"
    )
    conn.commit()
    conn.close()

    s = Store(path=db)
    # Live cluster: backfilled to {} (reported-unknown), not None.
    assert s.get_cluster("clu_live").layer_windows == {}
    # Non-live clusters: untouched (None = not reported).
    assert s.get_cluster("clu_assem").layer_windows is None
    assert s.get_cluster("clu_term").layer_windows is None
    s.close()


def test_request_recording_roundtrip(tmp_path):
    path = str(tmp_path / "store.db")
    s = Store(path=path)
    acc = s.create_account("alice", "hunter2hunter2")
    assert acc is not None
    key, _ = s.create_api_key(acc.account_id, "user", "user")
    clu = ClusterRecord(
        cluster_id="clu_1",
        model="demo-model",
        subnet="10.23.1.0/24",
        members=["wrk_1"],
        ips={"wrk_1": "10.23.1.1"},
        status=ClusterStatus.live,
    )
    s.create_cluster(clu)

    s.record_request(
        RequestRecord(
            request_id="req_1",
            account_id=acc.account_id,
            key_id=key.key_id,
            model="demo-model",
            cluster_id="clu_1",
            prompt_tokens=12,
            completion_tokens=34,
        )
    )

    s2 = Store(path=path)
    reqs = s2.list_requests_for_account(acc.account_id)
    assert len(reqs) == 1
    r = reqs[0]
    assert r.request_id == "req_1"
    assert r.key_id == key.key_id
    assert r.model == "demo-model"
    assert r.cluster_id == "clu_1"
    assert r.prompt_tokens == 12
    assert r.completion_tokens == 34
    assert r.created_at > 0


def test_request_recording_orders_newest_first(tmp_path):
    s = Store(path=str(tmp_path / "store.db"))
    acc = s.create_account("alice", "hunter2hunter2")
    key, _ = s.create_api_key(acc.account_id, "user", "user")
    clu = ClusterRecord(
        cluster_id="clu_1",
        model="demo-model",
        subnet="10.23.1.0/24",
        members=["wrk_1"],
        ips={"wrk_1": "10.23.1.1"},
        status=ClusterStatus.live,
    )
    s.create_cluster(clu)

    for i in range(3):
        s.record_request(
            RequestRecord(
                request_id=f"req_{i}",
                account_id=acc.account_id,
                key_id=key.key_id,
                model="demo-model",
                cluster_id="clu_1",
                prompt_tokens=i,
                completion_tokens=i * 2,
            )
        )

    reqs = s.list_requests_for_account(acc.account_id)
    assert [r.request_id for r in reqs] == ["req_2", "req_1", "req_0"]


def test_request_survives_cluster_termination(tmp_path):
    """A request record must NOT be deleted when its cluster is terminated.

    Clusters are soft-deleted (status=terminated) rather than removed, and the
    requests.cluster_id FK does not cascade — so usage history survives the
    cluster lifecycle (needed for accounting and future worker crediting).
    """
    s = Store(path=str(tmp_path / "store.db"))
    acc = s.create_account("alice", "hunter2hunter2")
    key, _ = s.create_api_key(acc.account_id, "user", "user")
    clu = ClusterRecord(
        cluster_id="clu_1",
        model="demo-model",
        subnet="10.23.1.0/24",
        members=["wrk_1"],
        ips={"wrk_1": "10.23.1.1"},
        status=ClusterStatus.live,
    )
    s.create_cluster(clu)
    s.record_request(
        RequestRecord(
            request_id="req_1",
            account_id=acc.account_id,
            key_id=key.key_id,
            model="demo-model",
            cluster_id="clu_1",
            prompt_tokens=5,
            completion_tokens=7,
        )
    )

    # Terminate the cluster (soft-delete: mark terminated, keep the row).
    clu.status = ClusterStatus.terminated
    s.update_cluster(clu)

    # The request record must still be there, referencing the retained cluster.
    reqs = s.list_requests_for_account(acc.account_id)
    assert len(reqs) == 1
    assert reqs[0].cluster_id == "clu_1"
    assert s.get_cluster("clu_1") is not None
    assert s.get_cluster("clu_1").status == ClusterStatus.terminated


def _worker_attribution_fixture(s: Store) -> tuple[str, str, str, str]:
    """Create two accounts, two workers (alice owns wrk_a, bob owns wrk_b),
    a live two-member cluster with a layer distribution, and one request.
    Returns (alice_account_id, alice_key_id, wrk_a, wrk_b)."""
    alice = s.create_account("alice", "hunter2hunter2")
    bob = s.create_account("bob", "hunter2hunter2")
    alice_key, _ = s.create_api_key(alice.account_id, "user", "user")
    _ = s.create_api_key(bob.account_id, "user", "user")
    wrk_a = "wrk_a"
    wrk_b = "wrk_b"
    assert s.create_worker_if_available(_worker(alice.account_id, wrk_a, "pub-a"), max_per_account=4)
    assert s.create_worker_if_available(_worker(bob.account_id, wrk_b, "pub-b"), max_per_account=4)
    clu = ClusterRecord(
        cluster_id="clu_1",
        model="demo-model",
        subnet="10.23.1.0/24",
        members=[wrk_a, wrk_b],
        ips={wrk_a: "10.23.1.1", wrk_b: "10.23.1.2"},
        status=ClusterStatus.live,
        ready={wrk_a, wrk_b},
        layer_windows={wrk_a: 20, wrk_b: 10},
    )
    s.create_cluster(clu)
    return alice.account_id, alice_key.key_id, wrk_a, wrk_b


def test_worker_attribution_share_and_effective(tmp_path):
    """Attribution rows carry the worker's layer_window and the cluster-wide
    total, so shares can be computed (alice owns one of two members)."""
    s = Store(path=str(tmp_path / "store.db"))
    acc_id, key_id, wrk_a, wrk_b = _worker_attribution_fixture(s)
    s.record_request(
        RequestRecord(
            request_id="req_1",
            account_id=acc_id,
            key_id=key_id,
            model="demo-model",
            cluster_id="clu_1",
            prompt_tokens=30,
            completion_tokens=60,
            created_at=100.0,
        )
    )
    rows = s.worker_attribution(acc_id, 0.0, 1000.0)
    # One row for alice's worker (wrk_a). bob's wrk_b is not alice's.
    assert len(rows) == 1
    row = rows[0]
    assert row["worker_id"] == wrk_a
    assert row["layer_window"] == 20
    assert row["cluster_total"] == 30  # 20 + 10 across ALL members
    # share = 20/30, effective = 30*(2/3) = 20, 60*(2/3) = 40
    assert row["prompt_tokens"] == 30
    assert row["completion_tokens"] == 60


def test_worker_attribution_worker_ids_filter_intersects_owned(tmp_path):
    """The worker_ids filter restricts to (requested ∩ owned) workers."""
    s = Store(path=str(tmp_path / "store.db"))
    acc_id, key_id, wrk_a, wrk_b = _worker_attribution_fixture(s)
    s.record_request(
        RequestRecord(
            request_id="req_1",
            account_id=acc_id,
            key_id=key_id,
            model="demo-model",
            cluster_id="clu_1",
            prompt_tokens=30,
            completion_tokens=60,
            created_at=100.0,
        )
    )
    # Requesting bob's worker too: bob's worker is NOT owned by alice → still 1 row.
    rows = s.worker_attribution(acc_id, 0.0, 1000.0, worker_ids=[wrk_a, wrk_b])
    assert len(rows) == 1
    assert rows[0]["worker_id"] == wrk_a
    # Requesting a worker alice does not own → 0 rows.
    rows = s.worker_attribution(acc_id, 0.0, 1000.0, worker_ids=[wrk_b])
    assert rows == []


def test_worker_attribution_unknown_distribution_null_windows(tmp_path):
    """An unknown (not reported) distribution yields NULL layer_window and
    NULL cluster_total, so the caller reports share/effective as None."""
    s = Store(path=str(tmp_path / "store.db"))
    acc_id, key_id, wrk_a, wrk_b = _worker_attribution_fixture(s)
    # Overwrite the distribution with None (not reported).
    s.set_cluster_layer_windows("clu_1", None)
    s.record_request(
        RequestRecord(
            request_id="req_1",
            account_id=acc_id,
            key_id=key_id,
            model="demo-model",
            cluster_id="clu_1",
            prompt_tokens=10,
            completion_tokens=20,
            created_at=100.0,
        )
    )
    rows = s.worker_attribution(acc_id, 0.0, 1000.0)
    assert len(rows) == 1
    assert rows[0]["layer_window"] is None
    assert rows[0]["cluster_total"] is None


def test_worker_attribution_forwarder_zero_window(tmp_path):
    """A forwarder (layer_window 0) is credited with 0 — the row is emitted
    with window 0 so the share computes to 0.0, not None."""
    s = Store(path=str(tmp_path / "store.db"))
    acc_id, key_id, wrk_a, wrk_b = _worker_attribution_fixture(s)
    # wrk_a becomes a forwarder: 0 layers.
    s.set_cluster_layer_windows("clu_1", {wrk_a: 0, wrk_b: 10})
    s.record_request(
        RequestRecord(
            request_id="req_1",
            account_id=acc_id,
            key_id=key_id,
            model="demo-model",
            cluster_id="clu_1",
            prompt_tokens=10,
            completion_tokens=20,
            created_at=100.0,
        )
    )
    rows = s.worker_attribution(acc_id, 0.0, 1000.0)
    assert len(rows) == 1
    assert rows[0]["layer_window"] == 0
    assert rows[0]["cluster_total"] == 10


def test_worker_logs_latest_orders_newest_first(tmp_path):
    s = Store(path=str(tmp_path / "store.db"))
    acc_id, key_id, wrk_a, wrk_b = _worker_attribution_fixture(s)
    for i in range(3):
        s.record_request(
            RequestRecord(
                request_id=f"req_{i}",
                account_id=acc_id,
                key_id=key_id,
                model="demo-model",
                cluster_id="clu_1",
                prompt_tokens=i,
                completion_tokens=i,
                created_at=100.0 + i,
            )
        )
    rows = s.worker_logs_latest(acc_id, limit=2)
    assert [r["request_id"] for r in rows] == ["req_2", "req_1"]


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


def test_migrate_from_legacy_json(tmp_path):
    """A legacy JSON snapshot is migrated into the SQLite DB on first open."""
    import json

    legacy = tmp_path / "store.json"
    legacy.write_text(
        json.dumps(
            {
                "accounts": [
                    {"account_id": "acc_1", "username": "alice", "password_hash": "h", "created_at": 1.0}
                ],
                "api_keys": [],
                "workers": [],
                "clusters": [],
            }
        )
    )
    db = tmp_path / "store.db"
    s = Store(path=str(db))
    acc = s.get_account("acc_1")
    assert acc is not None
    assert acc.username == "alice"
    assert acc.password_hash == "h"  # preserved verbatim from the snapshot
    # The legacy JSON is not deleted by the migration.
    assert legacy.exists()


def test_migrate_worker_referencing_cluster(tmp_path):
    """A migrated worker whose cluster_id references a cluster must survive.

    Clusters are inserted BEFORE workers in the migration (FK ordering);
    a worker pointing at a cluster must not be silently dropped.
    """
    import json

    legacy = tmp_path / "store.json"
    legacy.write_text(
        json.dumps(
            {
                "accounts": [
                    {"account_id": "acc_1", "username": "alice", "password_hash": "h", "created_at": 1.0}
                ],
                "api_keys": [],
                "clusters": [
                    {
                        "cluster_id": "clu_1",
                        "model": "demo-model",
                        "subnet": "10.23.1.0/24",
                        "status": "live",
                        "members": ["w1"],
                        "ready": ["w1"],
                        "ips": {"w1": "10.23.1.1"},
                        "created_at": 1.0,
                    }
                ],
                "workers": [
                    {
                        "worker_id": "w1",
                        "account_id": "acc_1",
                        "model": "demo-model",
                        "gguf_sha256": "a" * 64,
                        "memory_allocated_mb": 4096,
                        "status": "assigned",
                        "online": True,
                        "cluster_id": "clu_1",
                        "assigned_ip": "10.23.1.1",
                        "ring_position": 0,
                        "last_heartbeat": 1.0,
                        "assignable_at": 1.0,
                        "created_at": 1.0,
                        "wg_pubkey": "pk",
                        "endpoint": {"host": "8.8.8.8", "port": 51820, "behind_nat": False, "nat_type": "unknown"},
                        "hardware": None,
                    }
                ],
            }
        )
    )
    s = Store(path=str(tmp_path / "store.db"))
    w = s.get_worker("w1")
    assert w is not None, "worker referencing a cluster was dropped by migration"
    assert w.cluster_id == "clu_1"
    assert w.status.value == "assigned"
    assert s.get_cluster("clu_1") is not None


def test_restart_persists_data(tmp_path):
    """Nothing is erased between startups: reopen the DB and data survives."""
    db = str(tmp_path / "store.db")
    s1 = Store(path=db)
    acc = s1.create_account("alice", "hunter2hunter2")
    assert acc is not None
    key, _secret = s1.create_api_key(acc.account_id, "worker", "worker")
    s1.close()

    # "Restart": open again with the same path.
    s2 = Store(path=db)
    assert s2.get_account_by_username("alice") is not None
    assert s2.get_api_key(key.key_id) is not None
    s2.close()


def test_legacy_json_path_redirects_to_db(tmp_path):
    """A path ending in .json (old default) must open the .db sibling, not
    treat the JSON as a SQLite DB."""
    import json

    legacy = tmp_path / "store.json"
    legacy.write_text(json.dumps({"accounts": [], "api_keys": [], "workers": [], "clusters": []}))
    s = Store(path=str(legacy))
    assert s._path.endswith(".db")
    assert os.path.exists(s._path)
    s.close()


def test_legacy_json_migration_backfills_live_cluster_distribution(tmp_path):
    """A LIVE cluster in a legacy JSON snapshot must migrate with an explicit
    'unknown' distribution ({}), not None — the invariant is that a live
    cluster always carries a distribution field.

    Regression: the JSON→SQLite migration ran `_migrate_schema` on a freshly
    created DB, where `pre_v06` (members_json present) is False — so the
    v0.5 live-cluster backfill step never ran, and the migrated live cluster
    read back layer_windows=None, violating the invariant indefinitely.
    """
    import json

    legacy = tmp_path / "store.json"
    legacy.write_text(
        json.dumps(
            {
                "accounts": [
                    {"account_id": "acc_1", "username": "alice", "password_hash": "h", "created_at": 1.0}
                ],
                "api_keys": [],
                "clusters": [
                    {
                        "cluster_id": "clu_live",
                        "model": "demo-model",
                        "subnet": "10.23.1.0/24",
                        "status": "live",
                        "members": ["w1", "w2"],
                        "ready": ["w1", "w2"],
                        "ips": {"w1": "10.23.1.1", "w2": "10.23.1.2"},
                        "created_at": 1.0,
                    },
                    {
                        "cluster_id": "clu_assem",
                        "model": "demo-model",
                        "subnet": "10.23.2.0/24",
                        "status": "assembling",
                        "members": ["w1"],
                        "ready": [],
                        "ips": {"w1": "10.23.2.1"},
                        "created_at": 1.0,
                    },
                ],
                "workers": [],
            }
        )
    )
    s = Store(path=str(tmp_path / "store.db"))
    live = s.get_cluster("clu_live")
    assert live is not None
    assert live.members == ["w1", "w2"]
    assert live.ready == {"w1", "w2"}
    assert live.layer_windows == {}, f"live cluster must be reported-unknown, got {live.layer_windows}"
    # Non-live clusters stay untouched (None = not reported).
    assem = s.get_cluster("clu_assem")
    assert assem is not None
    assert assem.layer_windows is None
    s.close()


def test_worker_can_be_member_of_multiple_clusters(tmp_path):
    """Membership is many-to-many over time: a worker belongs to its current
    cluster AND every terminated cluster it previously served in. The junction
    table (composite PK) holds one row per (cluster, worker), so history
    survives while the worker's own row only points at the current cluster."""
    s = Store(path=str(tmp_path / "store.db"))
    # Two terminated clusters + one live cluster, all containing wrk_1.
    for cid, status in (("clu_old_1", ClusterStatus.terminated),
                        ("clu_old_2", ClusterStatus.terminated),
                        ("clu_now", ClusterStatus.live)):
        s.create_cluster(
            ClusterRecord(
                cluster_id=cid,
                model="demo-model",
                subnet=f"10.23.1.{0}/24",
                members=["wrk_1", "wrk_2"],
                ips={"wrk_1": "10.23.1.1", "wrk_2": "10.23.1.2"},
                status=status,
                layer_windows={"wrk_1": 1, "wrk_2": 35} if status == ClusterStatus.live else None,
            )
        )
    # The worker row points at ONE (current) cluster.
    # (No workers table rows needed — cluster_members.worker_id is not an FK.)

    # Every cluster still knows its own membership history.
    assert s.get_cluster("clu_old_1").members == ["wrk_1", "wrk_2"]
    assert s.get_cluster("clu_old_2").members == ["wrk_1", "wrk_2"]
    assert s.get_cluster("clu_now").members == ["wrk_1", "wrk_2"]

    # And the cluster's own layer windows are snapshotted per cluster.
    assert s.get_cluster("clu_now").layer_windows == {"wrk_1": 1, "wrk_2": 35}
    assert s.get_cluster("clu_old_1").layer_windows is None


def test_membership_history_survives_worker_revocation(tmp_path):
    """Revoking a worker (DELETE workers row) must NOT erase its rows from the
    cluster_members junction — terminated clusters keep their membership for
    accounting (who processed what). cluster_members.worker_id has no FK for
    exactly this reason."""
    import sqlite3

    s = Store(path=str(tmp_path / "store.db"))
    clu = ClusterRecord(
        cluster_id="clu_1",
        model="demo-model",
        subnet="10.23.1.0/24",
        members=["wrk_1", "wrk_2"],
        ips={"wrk_1": "10.23.1.1", "wrk_2": "10.23.1.2"},
        status=ClusterStatus.terminated,
    )
    s.create_cluster(clu)

    # A worker row that references this cluster must exist for the FK check.
    acc = s.create_account("alice", "hunter2hunter2")
    w = _worker(acc.account_id, "wrk_1", "pub-a")
    w.cluster_id = "clu_1"
    s.create_worker(w)

    # Revoking the worker deletes its row (this is what the API does).
    assert s.delete_worker("wrk_1") is True

    # The terminated cluster's membership must be INTACT — wrk_1 is still a
    # member (no FK cascade) so "who was in this cluster" survives.
    c = s.get_cluster("clu_1")
    assert c is not None
    assert c.members == ["wrk_1", "wrk_2"]
    assert c.ips == {"wrk_1": "10.23.1.1", "wrk_2": "10.23.1.2"}

    # But the FK on workers.cluster_id means the worker row itself is gone.
    assert s.get_worker("wrk_1") is None


def test_zero_layer_window_forwarder_preserved(tmp_path):
    """A member credited with 0 layers (a forwarder) must survive the
    round-trip: layer_window=0 is a VALID value, not 'missing'."""
    s = Store(path=str(tmp_path / "store.db"))
    clu = ClusterRecord(
        cluster_id="clu_1",
        model="demo-model",
        subnet="10.23.1.0/24",
        members=["wrk_head", "wrk_fwd"],
        ips={"wrk_head": "10.23.1.1", "wrk_fwd": "10.23.1.2"},
        status=ClusterStatus.live,
        layer_windows={"wrk_head": 24, "wrk_fwd": 0},
    )
    s.create_cluster(clu)
    c = s.get_cluster("clu_1")
    assert c is not None
    assert c.layer_windows == {"wrk_head": 24, "wrk_fwd": 0}


def test_membership_migration_is_idempotent(tmp_path):
    """Reopening a migrated DB (already in v0.6 shape) must NOT re-run the
    JSON→junction population (the JSON columns are gone) and must not crash."""
    import sqlite3

    db = str(tmp_path / "store.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE clusters (cluster_id TEXT PRIMARY KEY, model TEXT NOT NULL,
            subnet TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            members_json TEXT NOT NULL, ready_json TEXT NOT NULL, ips_json TEXT NOT NULL);
        """
    )
    conn.execute(
        "INSERT INTO clusters VALUES ('clu_1','m','10.0.0.0/24','live',1.0,"
        "'[\"wrk_1\"]','[\"wrk_1\"]','{\"wrk_1\":\"10.0.0.1\"}')"
    )
    conn.commit()
    conn.close()

    # First open: migrates (adds distribution_reported, lifts membership, drops
    # the JSON columns).
    s = Store(path=db)
    c = s.get_cluster("clu_1")
    assert c is not None
    assert c.members == ["wrk_1"]
    assert c.ready == {"wrk_1"}
    assert c.layer_windows == {}
    s.close()

    # Second open: already in v0.6 shape → no crash, data intact.
    s2 = Store(path=db)
    c2 = s2.get_cluster("clu_1")
    assert c2 is not None
    assert c2.members == ["wrk_1"]
    assert c2.ready == {"wrk_1"}
    assert c2.layer_windows == {}
    s2.close()


def test_migrated_db_never_resurrects_json_columns(tmp_path):
    """The migration must NOT re-add the dropped JSON columns on reopen.

    Regression for a real bug: the v0.5 ALTER (add layer_windows_json) ran on
    EVERY open — its guard only checked 'column absent', and v0.6 had dropped
    it, so a fresh or already-migrated DB got layer_windows_json resurrected
    on each reopen. The v0.5 step is now gated on members_json (the true
    pre-v0.6 marker), so this can't happen.
    """
    import sqlite3

    # Fresh DB.
    db = str(tmp_path / "fresh.db")
    s = Store(path=db)
    cols = {r[1] for r in s._conn.execute("PRAGMA table_info(clusters)")}
    assert "layer_windows_json" not in cols
    assert "members_json" not in cols
    assert "distribution_reported" in cols
    s.close()

    # Reopen of fresh DB — still no resurrection.
    s2 = Store(path=db)
    cols2 = {r[1] for r in s2._conn.execute("PRAGMA table_info(clusters)")}
    assert "layer_windows_json" not in cols2
    assert "members_json" not in cols2
    s2.close()

    # An old v0.5 DB migrates fully, and reopening it does NOT resurrect the
    # JSON columns either.
    db2 = str(tmp_path / "old.db")
    conn = sqlite3.connect(db2)
    conn.executescript(
        """
        CREATE TABLE clusters (cluster_id TEXT PRIMARY KEY, model TEXT NOT NULL,
            subnet TEXT NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
            members_json TEXT NOT NULL, ready_json TEXT NOT NULL, ips_json TEXT NOT NULL,
            layer_windows_json TEXT);
        """
    )
    conn.execute(
        "INSERT INTO clusters VALUES ('clu_1','m','10.0.0.0/24','live',1.0,"
        "'[\"wrk_1\"]','[\"wrk_1\"]','{\"wrk_1\":\"10.0.0.1\"}','{\"wrk_1\": 5}')"
    )
    conn.commit()
    conn.close()

    s3 = Store(path=db2)
    assert s3.get_cluster("clu_1").layer_windows == {"wrk_1": 5}
    s3.close()
    s4 = Store(path=db2)
    cols4 = {r[1] for r in s4._conn.execute("PRAGMA table_info(clusters)")}
    assert "layer_windows_json" not in cols4
    assert "members_json" not in cols4
    assert s4.get_cluster("clu_1").layer_windows == {"wrk_1": 5}
    s4.close()


def test_atomic_ready_gate_flips_live_under_lock(tmp_path):
    """mark_member_ready atomically applies the ready bit AND the liveness
    gate: when all members are ready and the distribution is reported, the
    cluster flips to live in the same lock-protected operation."""
    s = Store(path=str(tmp_path / "store.db"))
    s.create_cluster(
        ClusterRecord(
            cluster_id="clu_1",
            model="demo-model",
            subnet="10.23.1.0/24",
            members=["w1", "w2"],
            ips={"w1": "10.23.1.1", "w2": "10.23.1.2"},
            layer_windows={"w1": 12, "w2": 12},  # distribution reported
        )
    )
    # First ready: not all members ready yet → stays assembling.
    c = s.mark_member_ready("clu_1", "w1")
    assert c.status == ClusterStatus.assembling
    # Second ready: gate satisfied → live.
    c = s.mark_member_ready("clu_1", "w2")
    assert c.status == ClusterStatus.live
    assert c.ready == {"w1", "w2"}


def test_atomic_ready_never_resurrects_terminated(tmp_path):
    """A ready report racing a dissolve must never flip a terminated cluster
    back to live (regression: the old read-modify-write could interleave)."""
    s = Store(path=str(tmp_path / "store.db"))
    s.create_cluster(
        ClusterRecord(
            cluster_id="clu_1",
            model="demo-model",
            subnet="10.23.1.0/24",
            members=["w1"],
            ips={"w1": "10.23.1.1"},
            layer_windows={"w1": 24},
        )
    )
    s.mark_member_ready("clu_1", "w1")
    assert s.get_cluster("clu_1").status == ClusterStatus.live
    # Dissolve (terminate).
    clu = s.get_cluster("clu_1")
    clu.status = ClusterStatus.terminated
    s.update_cluster(clu)
    # A late ready report must be refused and leave the cluster terminated.
    c = s.mark_member_ready("clu_1", "w1")
    assert c.status == ClusterStatus.terminated
    assert s.get_cluster("clu_1").status == ClusterStatus.terminated
