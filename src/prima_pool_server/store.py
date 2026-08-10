"""SQLite-backed state store.

Replaces the JSON-snapshot store with a proper SQLite database (single file,
atomic transactions, WAL for concurrent readers, relational integrity via FKs).

Schema:
  accounts(id, username unique, password_hash, created_at)
  api_keys(id, account_id FK, name, scope, key_hash unique, created_at)
  workers(id, account_id FK, model, gguf_sha256, memory_mb, status, online,
          cluster_id FK, last_heartbeat, assignable_at, created_at,
          wg_pubkey, endpoint_json, hardware_json, assigned_ip, ring_position)
  clusters(id, model, subnet, status, created_at,
           members_json, ready_json, ips_json)

JSON columns hold the structured fields (endpoint/hardware, member order,
ready set, ip map); scalar columns provide relational queries and integrity.
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
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_api_keys_account ON api_keys(account_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id TEXT PRIMARY KEY,
    model      TEXT NOT NULL,
    subnet     TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_at REAL NOT NULL,
    members_json TEXT NOT NULL,
    ready_json   TEXT NOT NULL,
    ips_json     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clusters_model ON clusters(model);

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
        "members_json": json.dumps(c.members),
        "ready_json": json.dumps(sorted(c.ready)),
        "ips_json": json.dumps(c.ips),
    }


def _row_to_cluster(row: sqlite3.Row) -> ClusterRecord:
    return ClusterRecord(
        cluster_id=row["cluster_id"],
        model=row["model"],
        subnet=row["subnet"],
        status=ClusterStatus(row["status"]),
        created_at=row["created_at"],
        members=json.loads(row["members_json"]),
        ready=set(json.loads(row["ready_json"])),
        ips=json.loads(row["ips_json"]),
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
                conn.execute(
                    "INSERT OR IGNORE INTO clusters (cluster_id, model, subnet, status, created_at, "
                    "members_json, ready_json, ips_json) "
                    "VALUES (:cluster_id, :model, :subnet, :status, :created_at, "
                    ":members_json, :ready_json, :ips_json)",
                    _cluster_to_row(clu),
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
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "INSERT INTO clusters (cluster_id, model, subnet, status, created_at, "
                    "members_json, ready_json, ips_json) "
                    "VALUES (:cluster_id, :model, :subnet, :status, :created_at, "
                    ":members_json, :ready_json, :ips_json)",
                    _cluster_to_row(rec),
                )

    def get_cluster(self, cluster_id: str) -> ClusterRecord | None:
        row = self._fetch_one("SELECT * FROM clusters WHERE cluster_id = ?", (cluster_id,))
        return _row_to_cluster(row) if row else None

    def update_cluster(self, rec: ClusterRecord) -> None:
        with self._lock:
            with self._conn:
                self._conn.execute(
                    "UPDATE clusters SET model=:model, subnet=:subnet, status=:status, "
                    "created_at=:created_at, members_json=:members_json, ready_json=:ready_json, "
                    "ips_json=:ips_json WHERE cluster_id=:cluster_id",
                    _cluster_to_row(rec),
                )

    def delete_cluster(self, cluster_id: str) -> bool:
        return self._mutating("DELETE FROM clusters WHERE cluster_id = ?", (cluster_id,)) > 0

    def list_clusters(self) -> list[ClusterRecord]:
        rows = self._fetch_all("SELECT * FROM clusters")
        return [_row_to_cluster(r) for r in rows]