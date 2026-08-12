"""SQLite-backed state store.

Replaces the JSON-snapshot store with a proper SQLite database (single file,
atomic transactions, WAL for concurrent readers, relational integrity via FKs).

Schema:
  accounts(id, username unique, password_hash, created_at)
  api_keys(id, account_id FK, name, scope, key_hash unique, created_at)
  workers(id, account_id FK, model, gguf_sha256, memory_mb, status, online,
          cluster_id FK, last_heartbeat, assignable_at, created_at,
          wg_pubkey, endpoint_json, hardware_json, assigned_ip, ring_position)
  clusters(id, model, subnet, status, created_at, distribution_reported)
  cluster_members(cluster_id FK, worker_id, ring_position, assigned_ip, ready,
                  layer_window, PRIMARY KEY(cluster_id, worker_id),
                  UNIQUE(cluster_id, ring_position))
  requests(id, account_id FK, key_id FK, model, cluster_id FK, tokens, created_at)

JSON columns are used ONLY for document-shaped fields that are read/written as
a whole (endpoint/hardware on workers). Membership (order, ready set, ip map,
layer distribution) is RELATIONAL — a worker can belong to many clusters over
time (current + terminated history), so it lives in the junction table
`cluster_members` (composite PK = one row per (cluster, worker)). Scalar
columns provide relational queries and integrity.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time

from . import security
from .models import (
    AccountRecord,
    ApiKeyRecord,
    ClusterRecord,
    ClusterStatus,
    EndpointInfo,
    Hardware,
    RequestRecord,
    WorkerRecord,
    WorkerStatus,
)

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    account_id   TEXT PRIMARY KEY,
    username     TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);

CREATE TABLE IF NOT EXISTS api_keys (
    key_id     TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    scope      TEXT NOT NULL,
    key_hash   TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    worker_id  TEXT REFERENCES workers(worker_id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_api_keys_account ON api_keys(account_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
-- idx_api_keys_worker is created in _migrate_schema (after the ALTER adds
-- worker_id to pre-existing DBs).

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    subnet     TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at REAL NOT NULL,
    -- Whether the head has reported the layer distribution (possibly as
    -- "unknown"). DISTINCT from the per-member layer_window values: this is a
    -- cluster-level fact (None in ClusterRecord == not reported == 0 here),
    -- and the liveness gate requires it to be set for a cluster to go live.
    distribution_reported INTEGER NOT NULL DEFAULT 0
        CHECK (distribution_reported IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_clusters_model ON clusters(model);

-- Junction table: which workers are (or were) in which cluster, in ring
-- order, with their assigned IP, readiness, and layer share. A worker can be
-- a member of MANY clusters over time (one live/assembling + every terminated
-- cluster it served in), so membership is many-to-many → composite PK. The
-- UNIQUE (cluster_id, ring_position) constraint means a cluster can never
-- have two members claiming the same ring slot (two "heads").
--
-- worker_id deliberately has NO FK: membership is HISTORICAL (terminated
-- clusters keep their members for accounting), and revoking a worker deletes
-- its row — a CASCADE would silently erase that worker from every terminated
-- cluster's history (same rationale as requests.cluster_id not cascading).
-- cluster_id cascades: cluster rows are soft-deleted (status=terminated), so
-- a hard delete should remove its membership rows.
CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id    TEXT NOT NULL REFERENCES clusters(cluster_id) ON DELETE CASCADE,
    worker_id     TEXT NOT NULL,
    ring_position INTEGER NOT NULL CHECK (ring_position >= 0),
    assigned_ip   TEXT,
    ready         INTEGER NOT NULL DEFAULT 0 CHECK (ready IN (0, 1)),
    layer_window  INTEGER CHECK (layer_window IS NULL OR layer_window >= 0),
    PRIMARY KEY (cluster_id, worker_id),
    UNIQUE (cluster_id, ring_position)
);
CREATE INDEX IF NOT EXISTS idx_cluster_members_worker ON cluster_members(worker_id);

CREATE TABLE IF NOT EXISTS workers (
    worker_id      TEXT PRIMARY KEY,
    account_id     TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    model          TEXT NOT NULL,
    gguf_sha256    TEXT NOT NULL,
    memory_allocated_mb INTEGER NOT NULL,
    status         TEXT NOT NULL,
    online         INTEGER NOT NULL DEFAULT 0,
    cluster_id     TEXT REFERENCES clusters(cluster_id) ON DELETE SET NULL,
    last_heartbeat REAL NOT NULL DEFAULT 0,
    assignable_at  REAL NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    wg_pubkey      TEXT NOT NULL,
    endpoint_json  TEXT NOT NULL,
    hardware_json  TEXT,
    assigned_ip    TEXT,
    ring_position  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_workers_account ON workers(account_id);
CREATE INDEX IF NOT EXISTS idx_workers_model ON workers(model, gguf_sha256);
CREATE INDEX IF NOT EXISTS idx_workers_cluster ON workers(cluster_id);

CREATE TABLE IF NOT EXISTS requests (
    request_id       TEXT PRIMARY KEY,
    account_id       TEXT NOT NULL REFERENCES accounts(account_id) ON DELETE CASCADE,
    key_id           TEXT NOT NULL REFERENCES api_keys(key_id) ON DELETE CASCADE,
    model            TEXT NOT NULL,
    cluster_id       TEXT NOT NULL REFERENCES clusters(cluster_id),
    prompt_tokens    INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_requests_account ON requests(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_requests_key ON requests(key_id);
CREATE INDEX IF NOT EXISTS idx_requests_cluster ON requests(cluster_id);
"""


def _worker_to_row(w: WorkerRecord) -> dict:
    return {
        "worker_id": w.worker_id,
        "account_id": w.account_id,
        "model": w.model,
        "gguf_sha256": w.gguf_sha256,
        "memory_allocated_mb": w.memory_allocated_mb,
        "status": w.status.value,
        "online": int(w.online),
        "cluster_id": w.cluster_id,
        "last_heartbeat": w.last_heartbeat,
        "assignable_at": w.assignable_at,
        "created_at": w.created_at,
        "wg_pubkey": w.wg_pubkey,
        "endpoint_json": w.endpoint.model_dump_json() if w.endpoint else "null",
        "hardware_json": w.hardware.model_dump_json() if w.hardware else "null",
        "assigned_ip": w.assigned_ip,
        "ring_position": w.ring_position,
    }


def _row_to_worker(row: sqlite3.Row) -> WorkerRecord:
    endpoint = EndpointInfo.model_validate_json(row["endpoint_json"]) if row["endpoint_json"] not in (None, "null") else None
    hjson = row["hardware_json"]
    hardware = Hardware.model_validate_json(hjson) if hjson not in (None, "null") else None
    return WorkerRecord(
        worker_id=row["worker_id"],
        account_id=row["account_id"],
        model=row["model"],
        gguf_sha256=row["gguf_sha256"],
        memory_allocated_mb=row["memory_allocated_mb"],
        status=WorkerStatus(row["status"]),
        online=bool(row["online"]),
        cluster_id=row["cluster_id"],
        last_heartbeat=row["last_heartbeat"],
        assignable_at=row["assignable_at"],
        created_at=row["created_at"],
        wg_pubkey=row["wg_pubkey"],
        endpoint=endpoint,
        hardware=hardware,
        assigned_ip=row["assigned_ip"],
        ring_position=row["ring_position"],
    )


def _cluster_to_row(c: ClusterRecord) -> dict:
    return {
        "cluster_id": c.cluster_id,
        "model": c.model,
        "subnet": c.subnet,
        "status": c.status.value,
        "created_at": c.created_at,
        # ClusterRecord.layer_windows is None (not reported) or a dict
        # (reported — possibly {} = reported-unknown). The column is the
        # boolean "has been reported".
        "distribution_reported": int(c.layer_windows is not None),
    }


def _load_cluster_members(conn: sqlite3.Connection, cluster_id: str) -> ClusterRecord | None:
    """Hydrate a ClusterRecord's membership fields from the junction table.

    `members` is in ring order (ORDER BY ring_position); `ready` is the set of
    members with ready=1; `ips` maps worker_id → assigned_ip; `layer_windows`
    maps worker_id → layer_window for members the head credited (NULL row =
    that member not in the report — e.g. a forwarder that did no work, or an
    'unknown' report). The `distribution_reported` column is authoritative:
    it preserves the None-vs-{} distinction the liveness gate depends on.
    """
    row = conn.execute(
        "SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,)
    ).fetchone()
    if row is None:
        return None
    rows = conn.execute(
        "SELECT worker_id, ring_position, assigned_ip, ready, layer_window "
        "FROM cluster_members WHERE cluster_id = ? ORDER BY ring_position",
        (cluster_id,),
    ).fetchall()
    members = [r["worker_id"] for r in rows]
    ready = {r["worker_id"] for r in rows if r["ready"]}
    ips = {r["worker_id"]: r["assigned_ip"] for r in rows if r["assigned_ip"]}
    if row["distribution_reported"]:
        layer_windows: dict[str, int] | None = {
            r["worker_id"]: r["layer_window"]
            for r in rows
            if r["layer_window"] is not None
        }
    else:
        layer_windows = None
    return ClusterRecord(
        cluster_id=row["cluster_id"],
        model=row["model"],
        subnet=row["subnet"],
        status=ClusterStatus(row["status"]),
        created_at=row["created_at"],
        members=members,
        ready=ready,
        ips=ips,
        layer_windows=layer_windows,
    )


def _save_cluster_members(conn: sqlite3.Connection, c: ClusterRecord) -> None:
    """Rewrite the membership rows for a cluster from the record's fields.

    Runs inside the caller's transaction. Uses INSERT ... ON CONFLICT (PK)
    DO UPDATE so ready/layer_window/ip updates apply without deleting rows
    first (preserves membership history and avoids churn). Ring order is
    preserved by ring_position = index.
    """
    for i, wid in enumerate(c.members):
        conn.execute(
            "INSERT INTO cluster_members "
            "(cluster_id, worker_id, ring_position, assigned_ip, ready, layer_window) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (cluster_id, worker_id) DO UPDATE SET "
            "ring_position = excluded.ring_position, "
            "assigned_ip = excluded.assigned_ip, "
            "ready = excluded.ready, "
            "layer_window = excluded.layer_window",
            (
                c.cluster_id,
                wid,
                i,
                c.ips.get(wid),
                int(wid in c.ready),
                c.layer_windows.get(wid) if c.layer_windows else None,
            ),
        )
    # Drop any member rows that are no longer in the ring (e.g. a re-formed
    # cluster after a dissolve). FKs cascade, so removed workers are cleaned
    # from the junction automatically; this just prunes stale rows here.
    placeholders = ",".join("?" for _ in c.members)
    if c.members:
        conn.execute(
            f"DELETE FROM cluster_members WHERE cluster_id = ? AND worker_id NOT IN ({placeholders})",
            (c.cluster_id, *c.members),
        )
    else:
        conn.execute(
            "DELETE FROM cluster_members WHERE cluster_id = ?", (c.cluster_id,)
        )


def _worker_from_legacy_dict(d: dict) -> WorkerRecord:
    """Reconstruct a WorkerRecord from the old JSON snapshot shape."""
    d = dict(d)
    endpoint = EndpointInfo(**d["endpoint"]) if d.get("endpoint") else None
    hardware = Hardware(**d["hardware"]) if d.get("hardware") else None
    # Old snapshots may lack gguf_sha256 (pre-hash feature): default empty.
    d.setdefault("gguf_sha256", "")
    d["endpoint"] = endpoint
    d["hardware"] = hardware
    if "status" in d and not isinstance(d["status"], WorkerStatus):
        d["status"] = WorkerStatus(d["status"])
    return WorkerRecord(**d)


def _cluster_from_legacy_dict(d: dict) -> ClusterRecord:
    """Reconstruct a ClusterRecord from the old JSON snapshot shape."""
    d = dict(d)
    if "status" in d and not isinstance(d["status"], ClusterStatus):
        d["status"] = ClusterStatus(d["status"])
    if "ready" in d and isinstance(d["ready"], list):
        d["ready"] = set(d["ready"])
    return ClusterRecord(**d)


class Store:
    def __init__(self, path: str | None = None) -> None:
        """Open (or create) the SQLite DB at `path`.

        If `path` is None, use an in-memory database (no persistence —
        intentional for tests / ephemeral runs).

        Backward compatibility: if `path` ends in `.json` (the old default),
        it is treated as a legacy store. The actual SQLite DB is opened at the
        sibling `.db` path, and the JSON snapshot is migrated into it once.
        This prevents opening a JSON file as if it were a SQLite DB.
        """
        self._path = path or ":memory:"
        if self._path.endswith(".json") and self._path != ":memory:":
            legacy_json = self._path
            self._path = os.path.splitext(self._path)[0] + ".db"
            if not os.path.exists(self._path) and os.path.exists(legacy_json):
                logger.info("migrating legacy JSON store %s into %s", legacy_json, self._path)
                self._migrate_from_json(legacy_json, self._path)
        # Fresh DB path but a legacy snapshot exists alongside (e.g. the old
        # PRIMA_POOL_STORE_PATH=.../store.json with a new .db path).
        if self._path != ":memory:" and not os.path.exists(self._path):
            legacy = self._find_legacy_json(self._path)
            if legacy:
                logger.info("migrating legacy JSON store %s into %s", legacy, self._path)
                self._migrate_from_json(legacy, self._path)
        self._lock = threading.RLock()
        # check_same_thread=False keeps the connection usable from FastAPI's
        # thread pool; the RLock serializes ALL access (reads + writes) to
        # avoid SQLite "database is locked" livelocks. timeout bounds any
        # OS-level lock wait so a stuck writer raises instead of hanging.
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA busy_timeout = 15000")
        with self._lock:
            with self._conn:
                self._conn.executescript(_SCHEMA)
                self._migrate_schema()

    # ── low-level helpers ────────────────────────────────────────────────
    def _fetch_one(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchone()

    def _fetch_all(self, sql: str, params: tuple = ()):
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def _mutating(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            with self._conn:  # transaction/commit
                cur = self._conn.execute(sql, params)
                return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── schema migrations (idempotent) ───────────────────────────────────
    def _migrate_schema(self) -> None:
        """Bring an EXISTING database up to date, idempotently.

        `CREATE TABLE IF NOT EXISTS` never adds columns to an existing table,
        so schema additions must be applied explicitly. Runs after
        `executescript(_SCHEMA)` on every open.
        """
        # v0.4: api_keys.worker_id links a worker key to the worker it
        # registered (needed to disambiguate cluster readiness/config when one
        # account runs several workers in the same cluster).
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(api_keys)")}
        if "worker_id" not in cols:
            # The FK is set NULL on worker delete; workers exists by now
            # (created by _SCHEMA, workers table has no dep on api_keys).
            self._conn.execute(
                "ALTER TABLE api_keys ADD COLUMN worker_id TEXT "
                "REFERENCES workers(worker_id) ON DELETE SET NULL"
            )
            logger.info("migrated api_keys: added column worker_id")
        # The index is created here (not in _SCHEMA) so it never runs against
        # a pre-existing api_keys table that lacks the column yet.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_keys_worker ON api_keys(worker_id)"
        )
        # v0.5: clusters.layer_windows_json stores the per-worker layer
        # distribution reported by the head (Halda). `CREATE TABLE IF NOT
        # EXISTS` won't add the column to pre-existing DBs, so ALTER it here.
        #
        # This step is gated on the presence of `members_json` — the definitive
        # marker of a pre-v0.6 database. A fresh or already-migrated DB has no
        # membership JSON columns, so this ALTER must NOT run (it would
        # resurrect layer_windows_json after v0.6 dropped it, on every reopen).
        # The backfill + v0.6 population below share the same gate.
        ccols = {r[1] for r in self._conn.execute("PRAGMA table_info(clusters)")}
        pre_v06 = "members_json" in ccols
        if pre_v06 and "layer_windows_json" not in ccols:
            self._conn.execute(
                "ALTER TABLE clusters ADD COLUMN layer_windows_json TEXT"
            )
            logger.info("migrated clusters: added column layer_windows_json")
        if pre_v06:
            # Backfill the invariant: a LIVE cluster that predates layer accounting
            # must carry an explicit "unknown" distribution ({}) rather than NULL —
            # the new liveness rule says a live cluster always has a distribution
            # field. This matters because a server upgrade does NOT dissolve live
            # clusters (liveness is worker-driven; workers re-heartbeat on
            # reconnect and the head never re-reports), so a NULL here would
            # persist indefinitely otherwise. Idempotent safety net: only touches
            # rows that are live AND NULL.
            cur = self._conn.execute(
                "UPDATE clusters SET layer_windows_json = '{}' "
                "WHERE status = 'live' AND layer_windows_json IS NULL"
            )
            if cur.rowcount:
                logger.info(
                    "backfilled unknown layer distribution for %d live cluster(s)",
                    cur.rowcount,
                )
        # v0.6: membership moves OUT of JSON blobs on the clusters row into the
        # relational junction table `cluster_members`. Existing databases have
        # members_json/ready_json/ips_json/layer_windows_json columns — copy
        # their contents into the table once, then drop the columns (SQLite
        # 3.35+; DROP COLUMN is supported since 3.35.0).
        if pre_v06:
            # The column might be absent in DBs that already dropped the
            # JSON columns (e.g. a failed partial v0.6 run) — add it first
            # so the population + drop below are safe.
            if "distribution_reported" not in ccols:
                self._conn.execute(
                    "ALTER TABLE clusters ADD COLUMN distribution_reported "
                    "INTEGER NOT NULL DEFAULT 0 CHECK (distribution_reported IN (0, 1))"
                )
                logger.info("migrated clusters: added column distribution_reported")
            self._populate_cluster_members_from_json()
            self._drop_cluster_json_columns()
            logger.info("migrated clusters: membership JSON → cluster_members table")

    def _populate_cluster_members_from_json(self) -> None:
        """Copy membership from the legacy JSON columns into cluster_members.

        The columns still exist at this point (they are dropped right after).
        `members_json` is the authoritative order; each worker's ready/ip/
        layer_window come from the sibling JSON columns. Rows are inserted
        with INSERT OR IGNORE — the migration is idempotent (re-running after
        a partial failure must not duplicate or fail on the PK).
        """
        for row in self._conn.execute(
            "SELECT cluster_id, members_json, ready_json, ips_json, layer_windows_json FROM clusters"
        ):
            try:
                members = json.loads(row["members_json"] or "[]")
            except (ValueError, TypeError):
                members = []
            try:
                ready = set(json.loads(row["ready_json"] or "[]"))
            except (ValueError, TypeError):
                ready = set()
            try:
                ips = json.loads(row["ips_json"] or "{}")
            except (ValueError, TypeError):
                ips = {}
            try:
                lw = json.loads(row["layer_windows_json"]) if row["layer_windows_json"] not in (None, "null") else None
            except (ValueError, TypeError):
                lw = None
            for i, wid in enumerate(members):
                self._conn.execute(
                    "INSERT OR IGNORE INTO cluster_members "
                    "(cluster_id, worker_id, ring_position, assigned_ip, ready, layer_window) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        row["cluster_id"],
                        wid,
                        i,
                        ips.get(wid) if isinstance(ips, dict) else None,
                        int(wid in ready),
                        (lw or {}).get(wid) if isinstance(lw, dict) else None,
                    ),
                )
            # The legacy backfill invariant: a LIVE cluster always has a
            # distribution (possibly {} = unknown). The old code wrote
            # layer_windows_json='{}' for those rows, which is lw == {} here →
            # reported. Assembling/terminated clusters with NULL keep
            # distribution_reported = 0.
            if lw is not None:
                self._conn.execute(
                    "UPDATE clusters SET distribution_reported = 1 WHERE cluster_id = ?",
                    (row["cluster_id"],),
                )

    def _drop_cluster_json_columns(self) -> None:
        """Drop the four legacy membership JSON columns from `clusters`.

        SQLite requires them to be absent from indexes/triggers/views before
        DROP COLUMN; ours have none (the migration runs before any of that is
        created). Safe to call repeatedly — the columns won't exist the second
        time, so each DROP is guarded.
        """
        for col in ("members_json", "ready_json", "ips_json", "layer_windows_json"):
            if any(r[1] == col for r in self._conn.execute("PRAGMA table_info(clusters)")):
                self._conn.execute(f"ALTER TABLE clusters DROP COLUMN {col}")

    # ── legacy migration ─────────────────────────────────────────────────
    def _find_legacy_json(self, db_path: str) -> str | None:
        """Look for a legacy JSON snapshot for a `.db` path.

        Accepts the old `PRIMA_POOL_STORE_PATH` pointing at a `.json` file
        directly (same path), or a sibling `store.json` next to a `.db`.
        """
        candidates = []
        cand = db_path
        if not cand.endswith(".json"):
            cand = os.path.splitext(cand)[0] + ".json"
        candidates.append(cand)
        if db_path.endswith(".db"):
            candidates.append(os.path.join(os.path.dirname(db_path), "store.json"))
        for c in candidates:
            if os.path.exists(c):
                return c
        return None

    def _migrate_from_json(self, json_path: str, db_path: str) -> None:
        """Copy a legacy JSON snapshot into a fresh SQLite DB.

        The snapshot shares the same logical shape (accounts/keys/workers/
        clusters) with the schemas defined here, so rows are inserted directly
        using SQL. Runs before the main connection is opened.
        """
        try:
            with open(json_path) as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            logger.error("legacy migration: cannot read %s: %s", json_path, exc)
            return

        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            conn.executescript(_SCHEMA)
            for a in data.get("accounts", []):
                conn.execute(
                    "INSERT OR IGNORE INTO accounts (account_id, username, password_hash, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (a["account_id"], a["username"], a["password_hash"], a.get("created_at", 0)),
                )
            for k in data.get("api_keys", []):
                conn.execute(
                    "INSERT OR IGNORE INTO api_keys (key_id, account_id, name, scope, key_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (k["key_id"], k["account_id"], k["name"], k["scope"], k["key_hash"], k.get("created_at", 0)),
                )
            # Clusters BEFORE workers: workers.cluster_id has an FK to clusters,
            # so a worker referencing a cluster cannot be inserted first.
            for c in data.get("clusters", []):
                clu = _cluster_from_legacy_dict(c)
                # Backfill the live-cluster invariant BEFORE the row is
                # written: a LIVE cluster in the snapshot predates layer
                # accounting and carries no distribution — give it the
                # explicit "unknown" ({}) marker. This matters because
                # `_migrate_schema`'s backfill step is gated on `pre_v06`
                # (members_json present), which is False on the freshly
                # created DB here — so without this, a migrated live cluster
                # would read back layer_windows=None and violate the
                # "live ⇒ distribution reported" invariant indefinitely.
                # Assembling/terminated clusters stay unreported (None).
                if clu.layer_windows is None and clu.status == ClusterStatus.live:
                    clu.layer_windows = {}
                conn.execute(
                    "INSERT OR IGNORE INTO clusters (cluster_id, model, subnet, status, created_at, "
                    "distribution_reported) "
                    "VALUES (:cluster_id, :model, :subnet, :status, :created_at, "
                    ":distribution_reported)",
                    _cluster_to_row(clu),
                )
                # Membership rows (junction table) — same shape as the JSON
                # snapshot: members_json order + ready/ips/layer_windows.
                for i, wid in enumerate(clu.members):
                    conn.execute(
                        "INSERT OR IGNORE INTO cluster_members "
                        "(cluster_id, worker_id, ring_position, assigned_ip, ready, layer_window) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            clu.cluster_id,
                            wid,
                            i,
                            clu.ips.get(wid),
                            int(wid in clu.ready),
                            clu.layer_windows.get(wid) if clu.layer_windows else None,
                        ),
                    )
            for w in data.get("workers", []):
                rec = _worker_from_legacy_dict(w)
                conn.execute(
                    "INSERT OR IGNORE INTO workers (worker_id, account_id, model, gguf_sha256, memory_allocated_mb, "
                    "status, online, cluster_id, last_heartbeat, assignable_at, created_at, "
                    "wg_pubkey, endpoint_json, hardware_json, assigned_ip, ring_position) "
                    "VALUES (:worker_id, :account_id, :model, :gguf_sha256, :memory_allocated_mb, "
                    ":status, :online, :cluster_id, :last_heartbeat, :assignable_at, :created_at, "
                    ":wg_pubkey, :endpoint_json, :hardware_json, :assigned_ip, :ring_position)",
                    _worker_to_row(rec),
                )
        conn.close()
        logger.info("legacy JSON store migrated %s → %s", json_path, db_path)

    # ── accounts ─────────────────────────────────────────────────────────

    # ── accounts ─────────────────────────────────────────────────────────
    def create_account(self, username: str, password: str) -> AccountRecord | None:
        if self.get_account_by_username(username) is not None:
            return None
        rec = AccountRecord(
            account_id=security.new_id("acc"),
            username=username,
            password_hash=security.hash_password(password),
            created_at=time.time(),
        )
        try:
            with self._lock:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO accounts (account_id, username, password_hash, created_at) "
                        "VALUES (?, ?, ?, ?)",
                        (rec.account_id, rec.username, rec.password_hash, rec.created_at),
                    )
        except sqlite3.IntegrityError:
            return None
        return rec

    def get_account_by_username(self, username: str) -> AccountRecord | None:
        row = self._fetch_one("SELECT * FROM accounts WHERE username = ?", (username,))
        return AccountRecord(**dict(row)) if row else None

    def get_account(self, account_id: str) -> AccountRecord | None:
        row = self._fetch_one("SELECT * FROM accounts WHERE account_id = ?", (account_id,))
        return AccountRecord(**dict(row)) if row else None

    # ── api keys ─────────────────────────────────────────────────────────
    def create_api_key(self, account_id: str, name: str, scope: str) -> tuple[ApiKeyRecord, str]:
        secret = security.new_api_key(scope)
        rec = ApiKeyRecord(
            key_id=security.new_id("key"),
            account_id=account_id,
            name=name,
            scope=scope,
            key_hash=security.hash_api_key(secret),
            created_at=time.time(),
        )
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO api_keys (key_id, account_id, name, scope, key_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rec.key_id, rec.account_id, rec.name, rec.scope, rec.key_hash, rec.created_at),
                )
        return rec, secret

    def bind_api_key_to_worker(self, key_id: str, worker_id: str) -> None:
        """Link a worker-scoped API key to the worker it registered.

        This lets the key uniquely identify its worker even when several
        workers belong to the same account (needed to disambiguate cluster
        readiness/config when >1 worker of an account is in the same cluster).
        """
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE api_keys SET worker_id = ? WHERE key_id = ?",
                    (worker_id, key_id),
                )

    def list_api_keys(self, account_id: str) -> list[ApiKeyRecord]:
        rows = self._fetch_all("SELECT * FROM api_keys WHERE account_id = ?", (account_id,))
        return [ApiKeyRecord(**dict(r)) for r in rows]

    def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        row = self._fetch_one("SELECT * FROM api_keys WHERE key_id = ?", (key_id,))
        return ApiKeyRecord(**dict(row)) if row else None

    def revoke_api_key(self, key_id: str) -> bool:
        return self._mutating("DELETE FROM api_keys WHERE key_id = ?", (key_id,)) > 0

    def resolve_api_key(self, secret: str) -> ApiKeyRecord | None:
        key_hash = security.hash_api_key(secret)
        row = self._fetch_one("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,))
        return ApiKeyRecord(**dict(row)) if row else None

    # ── requests (usage accounting) ──────────────────────────────────────
    def record_request(self, rec: RequestRecord) -> None:
        """Persist a single inference request for accounting."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO requests "
                    "(request_id, account_id, key_id, model, cluster_id, "
                    " prompt_tokens, completion_tokens, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        rec.request_id,
                        rec.account_id,
                        rec.key_id,
                        rec.model,
                        rec.cluster_id,
                        rec.prompt_tokens,
                        rec.completion_tokens,
                        rec.created_at,
                    ),
                )

    def list_requests_for_account(self, account_id: str, limit: int = 100) -> list[RequestRecord]:
        """Return the most recent requests for an account, newest first."""
        rows = self._fetch_all(
            "SELECT * FROM requests WHERE account_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (account_id, limit),
        )
        return [RequestRecord(**dict(r)) for r in rows]

    def list_requests_in_range(
        self, account_id: str, begin: float, end: float, limit: int = 1000
    ) -> list[RequestRecord]:
        """Return an account's requests with `begin <= created_at < end`,
        newest first."""
        rows = self._fetch_all(
            "SELECT * FROM requests WHERE account_id = ? AND created_at >= ? AND created_at < ? "
            "ORDER BY created_at DESC LIMIT ?",
            (account_id, begin, end, limit),
        )
        return [RequestRecord(**dict(r)) for r in rows]

    def usage_stats_in_range(
        self, account_id: str, begin: float, end: float
    ) -> dict[str, tuple[int, int, int]]:
        """Aggregate an account's usage in [begin, end) per model.

        Returns {model: (requests, prompt_tokens, completion_tokens)}.
        """
        rows = self._fetch_all(
            "SELECT model, COUNT(*) AS requests, "
            "SUM(prompt_tokens) AS prompt_tokens, SUM(completion_tokens) AS completion_tokens "
            "FROM requests WHERE account_id = ? AND created_at >= ? AND created_at < ? "
            "GROUP BY model",
            (account_id, begin, end),
        )
        return {
            r["model"]: (r["requests"], r["prompt_tokens"], r["completion_tokens"])
            for r in rows
        }

    # ── workers ──────────────────────────────────────────────────────────
    def create_worker(self, rec: WorkerRecord) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO workers (worker_id, account_id, model, gguf_sha256, memory_allocated_mb, "
                    "status, online, cluster_id, last_heartbeat, assignable_at, created_at, "
                    "wg_pubkey, endpoint_json, hardware_json, assigned_ip, ring_position) "
                    "VALUES (:worker_id, :account_id, :model, :gguf_sha256, :memory_allocated_mb, "
                    ":status, :online, :cluster_id, :last_heartbeat, :assignable_at, :created_at, "
                    ":wg_pubkey, :endpoint_json, :hardware_json, :assigned_ip, :ring_position)",
                    _worker_to_row(rec),
                )

    def create_worker_if_available(self, rec: WorkerRecord, max_per_account: int) -> bool:
        """Atomically create a worker, enforcing the per-account worker cap and
        the one-device-one-worker rule (one worker per WG pubkey per account).

        Multiple workers may serve the same model — e.g. one account running
        the same model on several machines. Only reuse of the SAME physical
        device (same WG pubkey) is rejected.
        """
        with self._lock:
            existing = self.list_workers_for_account(rec.account_id)
            if len(existing) >= max_per_account:
                return False
            for w in existing:
                if w.wg_pubkey == rec.wg_pubkey and w.status != WorkerStatus.registered:
                    return False
            try:
                with self._conn:
                    self._conn.execute(
                        "INSERT INTO workers (worker_id, account_id, model, gguf_sha256, memory_allocated_mb, "
                        "status, online, cluster_id, last_heartbeat, assignable_at, created_at, "
                        "wg_pubkey, endpoint_json, hardware_json, assigned_ip, ring_position) "
                        "VALUES (:worker_id, :account_id, :model, :gguf_sha256, :memory_allocated_mb, "
                        ":status, :online, :cluster_id, :last_heartbeat, :assignable_at, :created_at, "
                        ":wg_pubkey, :endpoint_json, :hardware_json, :assigned_ip, :ring_position)",
                        _worker_to_row(rec),
                    )
                return True
            except sqlite3.IntegrityError:
                return False

    def get_worker(self, worker_id: str) -> WorkerRecord | None:
        row = self._fetch_one("SELECT * FROM workers WHERE worker_id = ?", (worker_id,))
        return _row_to_worker(row) if row else None

    def list_workers(self) -> list[WorkerRecord]:
        rows = self._fetch_all("SELECT * FROM workers")
        return [_row_to_worker(r) for r in rows]

    def list_workers_for_account(self, account_id: str) -> list[WorkerRecord]:
        rows = self._fetch_all("SELECT * FROM workers WHERE account_id = ?", (account_id,))
        return [_row_to_worker(r) for r in rows]

    def update_worker(self, rec: WorkerRecord) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE workers SET account_id=:account_id, model=:model, gguf_sha256=:gguf_sha256, "
                    "memory_allocated_mb=:memory_allocated_mb, status=:status, online=:online, "
                    "cluster_id=:cluster_id, last_heartbeat=:last_heartbeat, assignable_at=:assignable_at, "
                    "created_at=:created_at, wg_pubkey=:wg_pubkey, endpoint_json=:endpoint_json, "
                    "hardware_json=:hardware_json, assigned_ip=:assigned_ip, ring_position=:ring_position "
                    "WHERE worker_id=:worker_id",
                    _worker_to_row(rec),
                )

    def delete_worker(self, worker_id: str) -> bool:
        return self._mutating("DELETE FROM workers WHERE worker_id = ?", (worker_id,)) > 0

    # ── clusters ─────────────────────────────────────────────────────────
    def create_cluster(self, rec: ClusterRecord) -> None:
        """Persist a cluster and its membership (junction rows)."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO clusters (cluster_id, model, subnet, status, created_at, "
                    "distribution_reported) "
                    "VALUES (:cluster_id, :model, :subnet, :status, :created_at, "
                    ":distribution_reported)",
                    _cluster_to_row(rec),
                )
                _save_cluster_members(self._conn, rec)

    def get_cluster(self, cluster_id: str) -> ClusterRecord | None:
        with self._lock:
            return _load_cluster_members(self._conn, cluster_id)

    def set_cluster_layer_windows(self, cluster_id: str, layer_windows: dict[str, int] | None) -> bool:
        """Record the head-reported per-worker layer distribution.

        Returns True if the cluster exists (and the value was persisted).

        Semantics (preserved from the JSON era):
          - None  → "not reported": clears the reported flag and every
                    member's window. Reads back as `layer_windows is None`,
                    which BLOCKS the liveness gate.
          - {}    → "reported unknown" (head parse failure): sets the reported
                    flag, no windows. Reads back as {} — satisfies liveness.
          - {wid: n, ...} → reported with windows.
        """
        cluster = self.set_cluster_distribution(cluster_id, layer_windows)
        return cluster is not None

    def update_cluster(self, rec: ClusterRecord) -> None:
        """Update a cluster's scalar row and rewrite its membership rows."""
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE clusters SET model=:model, subnet=:subnet, status=:status, "
                    "created_at=:created_at, distribution_reported=:distribution_reported "
                    "WHERE cluster_id=:cluster_id",
                    _cluster_to_row(rec),
                )
                _save_cluster_members(self._conn, rec)

    def mark_member_ready(self, cluster_id: str, worker_id: str) -> ClusterRecord | None:
        """Atomically mark a member ready and re-evaluate the liveness gate.

        Runs the read-modify-write (add member to ready set, check gate, flip
        live) under the store RLock, so two concurrent `ready` reports can't
        lose each other's update, and a dissolve racing a ready report can't
        resurrect a terminated cluster (the gate refuses terminated). Returns
        the fresh cluster record, or None if it doesn't exist.
        """
        with self._lock:
            with self._conn:
                cluster = _load_cluster_members(self._conn, cluster_id)
                if cluster is None:
                    return None
                if cluster.status == ClusterStatus.terminated:
                    return cluster
                # Targeted member-row update — no whole-cluster rewrite.
                self._conn.execute(
                    "UPDATE cluster_members SET ready = 1 "
                    "WHERE cluster_id = ? AND worker_id = ?",
                    (cluster_id, worker_id),
                )
                # Re-read to compute the gate on fresh state.
                cluster = _load_cluster_members(self._conn, cluster_id)
                all_ready = len(cluster.ready) >= len(cluster.members)
                reported = cluster.layer_windows is not None
                if all_ready and reported and cluster.status != ClusterStatus.terminated:
                    self._conn.execute(
                        "UPDATE clusters SET status = 'live' WHERE cluster_id = ?",
                        (cluster_id,),
                    )
                    cluster.status = ClusterStatus.live
                return cluster

    def set_cluster_distribution(self, cluster_id: str, layer_windows: dict[str, int] | None) -> ClusterRecord | None:
        """Atomically record the layer distribution and re-evaluate liveness.

        Same read-modify-write protection as `mark_member_ready`: the gate is
        computed inside the RLock on fresh state. `None` = not reported (clears
        the flag — blocks liveness); `{}` = reported-unknown (satisfies it).
        Returns the fresh cluster record, or None if it doesn't exist.
        """
        with self._lock:
            with self._conn:
                cluster = _load_cluster_members(self._conn, cluster_id)
                if cluster is None:
                    return None
                if cluster.status == ClusterStatus.terminated:
                    return cluster
                if layer_windows is None:
                    self._conn.execute(
                        "UPDATE clusters SET distribution_reported = 0 WHERE cluster_id = ?",
                        (cluster_id,),
                    )
                    self._conn.execute(
                        "UPDATE cluster_members SET layer_window = NULL WHERE cluster_id = ?",
                        (cluster_id,),
                    )
                else:
                    self._conn.execute(
                        "UPDATE clusters SET distribution_reported = 1 WHERE cluster_id = ?",
                        (cluster_id,),
                    )
                    for wid, count in layer_windows.items():
                        self._conn.execute(
                            "UPDATE cluster_members SET layer_window = ? "
                            "WHERE cluster_id = ? AND worker_id = ?",
                            (count, cluster_id, wid),
                        )
                    placeholders = ",".join("?" for _ in layer_windows)
                    if placeholders:
                        self._conn.execute(
                            "UPDATE cluster_members SET layer_window = NULL "
                            "WHERE cluster_id = ? AND layer_window IS NOT NULL "
                            "AND worker_id NOT IN (%s)" % placeholders,
                            (cluster_id, *layer_windows.keys()),
                        )
                    else:
                        self._conn.execute(
                            "UPDATE cluster_members SET layer_window = NULL "
                            "WHERE cluster_id = ? AND layer_window IS NOT NULL",
                            (cluster_id,),
                        )
                # Re-read and evaluate the gate on fresh state.
                cluster = _load_cluster_members(self._conn, cluster_id)
                all_ready = len(cluster.ready) >= len(cluster.members)
                reported = cluster.layer_windows is not None
                if all_ready and reported and cluster.status != ClusterStatus.terminated:
                    self._conn.execute(
                        "UPDATE clusters SET status = 'live' WHERE cluster_id = ?",
                        (cluster_id,),
                    )
                    cluster.status = ClusterStatus.live
                return cluster

    def list_clusters(self) -> list[ClusterRecord]:
        with self._lock:
            clusters: list[ClusterRecord] = []
            rows = self._conn.execute("SELECT cluster_id FROM clusters ORDER BY created_at").fetchall()
            for row in rows:
                c = _load_cluster_members(self._conn, row["cluster_id"])
                if c is not None:
                    clusters.append(c)
            return clusters