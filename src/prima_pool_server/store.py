"""In-memory state store with optional JSON persistence.

v0 keeps everything in memory for simplicity. If PRIMA_POOL_STORE_PATH is set,
the store snapshots to disk on every mutation so a restart can recover state.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from . import security
from .models import (
    AccountRecord,
    ApiKeyRecord,
    ClusterRecord,
    WorkerRecord,
    WorkerStatus,
)


class Store:
    def __init__(self, path: str | None = None) -> None:
        self._path = path
        self._lock = threading.RLock()
        self.accounts: dict[str, AccountRecord] = {}
        self.accounts_by_username: dict[str, str] = {}
        self.api_keys: dict[str, ApiKeyRecord] = {}
        self.api_keys_by_hash: dict[str, str] = {}
        self.workers: dict[str, WorkerRecord] = {}
        self.clusters: dict[str, ClusterRecord] = {}
        self._load()

    # ── persistence ──────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path or not os.path.exists(self._path):
            return
        try:
            with open(self._path) as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return
        for a in data.get("accounts", []):
            rec = AccountRecord(**a)
            self.accounts[rec.account_id] = rec
            self.accounts_by_username[rec.username] = rec.account_id
        for k in data.get("api_keys", []):
            rec = ApiKeyRecord(**k)
            self.api_keys[rec.key_id] = rec
            self.api_keys_by_hash[rec.key_hash] = rec.key_id
        for w in data.get("workers", []):
            rec = WorkerRecord(**w)
            self.workers[rec.worker_id] = rec
        for c in data.get("clusters", []):
            rec = ClusterRecord(**c)
            rec.ready = set(rec.ready)
            self.clusters[rec.cluster_id] = rec

    def _save(self) -> None:
        if not self._path:
            return
        data = {
            "accounts": [vars(a) for a in self.accounts.values()],
            "api_keys": [vars(k) for k in self.api_keys.values()],
            "workers": [vars(w) for w in self.workers.values()],
            "clusters": [
                {**vars(c), "ready": sorted(c.ready)} for c in self.clusters.values()
            ],
        }
        tmp = self._path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh, default=str)
        os.replace(tmp, self._path)

    def _mutate(self) -> None:
        self._save()

    # ── accounts ─────────────────────────────────────────────────────────
    def create_account(self, username: str, password: str) -> AccountRecord:
        with self._lock:
            if username in self.accounts_by_username:
                return None  # type: ignore[return-value]
            rec = AccountRecord(
                account_id=security.new_id("acc"),
                username=username,
                password_hash=security.hash_password(password),
                created_at=time.time(),
            )
            self.accounts[rec.account_id] = rec
            self.accounts_by_username[username] = rec.account_id
            self._mutate()
            return rec

    def get_account_by_username(self, username: str) -> AccountRecord | None:
        with self._lock:
            aid = self.accounts_by_username.get(username)
            return self.accounts.get(aid) if aid else None

    def get_account(self, account_id: str) -> AccountRecord | None:
        with self._lock:
            return self.accounts.get(account_id)

    # ── api keys ─────────────────────────────────────────────────────────
    def create_api_key(self, account_id: str, name: str, scope: str) -> tuple[ApiKeyRecord, str]:
        with self._lock:
            secret = security.new_api_key(scope)
            rec = ApiKeyRecord(
                key_id=security.new_id("key"),
                account_id=account_id,
                name=name,
                scope=scope,
                key_hash=security.hash_password(secret),
                created_at=time.time(),
            )
            self.api_keys[rec.key_id] = rec
            self.api_keys_by_hash[rec.key_hash] = rec.key_id
            self._mutate()
            return rec, secret

    def list_api_keys(self, account_id: str) -> list[ApiKeyRecord]:
        with self._lock:
            return [k for k in self.api_keys.values() if k.account_id == account_id]

    def get_api_key(self, key_id: str) -> ApiKeyRecord | None:
        with self._lock:
            return self.api_keys.get(key_id)

    def revoke_api_key(self, key_id: str) -> bool:
        with self._lock:
            rec = self.api_keys.pop(key_id, None)
            if rec is None:
                return False
            self.api_keys_by_hash.pop(rec.key_hash, None)
            self._mutate()
            return True

    def resolve_api_key(self, secret: str) -> ApiKeyRecord | None:
        """Look up an API key by its plaintext secret (hash lookup)."""
        with self._lock:
            key_hash = security.hash_password(secret)
            key_id = self.api_keys_by_hash.get(key_hash)
            return self.api_keys.get(key_id) if key_id else None

    # ── workers ──────────────────────────────────────────────────────────
    def create_worker(self, rec: WorkerRecord) -> None:
        with self._lock:
            self.workers[rec.worker_id] = rec
            self._mutate()

    def get_worker(self, worker_id: str) -> WorkerRecord | None:
        with self._lock:
            return self.workers.get(worker_id)

    def list_workers(self) -> list[WorkerRecord]:
        with self._lock:
            return list(self.workers.values())

    def list_workers_for_account(self, account_id: str) -> list[WorkerRecord]:
        with self._lock:
            return [w for w in self.workers.values() if w.account_id == account_id]

    def update_worker(self, rec: WorkerRecord) -> None:
        with self._lock:
            self.workers[rec.worker_id] = rec
            self._mutate()

    def delete_worker(self, worker_id: str) -> bool:
        with self._lock:
            if worker_id not in self.workers:
                return False
            del self.workers[worker_id]
            self._mutate()
            return True

    # ── clusters ─────────────────────────────────────────────────────────
    def create_cluster(self, rec: ClusterRecord) -> None:
        with self._lock:
            self.clusters[rec.cluster_id] = rec
            self._mutate()

    def get_cluster(self, cluster_id: str) -> ClusterRecord | None:
        with self._lock:
            return self.clusters.get(cluster_id)

    def update_cluster(self, rec: ClusterRecord) -> None:
        with self._lock:
            self.clusters[rec.cluster_id] = rec
            self._mutate()

    def delete_cluster(self, cluster_id: str) -> bool:
        with self._lock:
            if cluster_id not in self.clusters:
                return False
            del self.clusters[cluster_id]
            self._mutate()
            return True

    def list_clusters(self) -> list[ClusterRecord]:
        with self._lock:
            return list(self.clusters.values())
