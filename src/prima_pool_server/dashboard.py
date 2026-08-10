"""Account-scoped dashboard data.

The GUI is served as static files; this endpoint provides the account's own
view: its workers and their states, plus its API keys. It is authenticated by
the account session token (from /accounts/login), consistent with the rest of
the per-account API.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header

from .errors import ForbiddenError, UnauthorizedError
from .models import Account, WorkerStatus
from .store import Store


def build_account_overview(store: Store, account_id: str) -> dict:
    """Aggregate an account's workers + keys into a dashboard-friendly shape."""
    workers = store.list_workers_for_account(account_id)
    keys = store.list_api_keys(account_id)
    account = store.get_account(account_id)
    return {
        "account_id": account_id,
        "username": account.username if account else None,
        "workers": [
            {
                "worker_id": w.worker_id,
                "model": w.model,
                "status": w.status.value,
                "online": w.online,
                "memory_mb": w.memory_allocated_mb,
                "cluster_id": w.cluster_id,
            }
            for w in workers
        ],
        "keys": [
            {"key_id": k.key_id, "name": k.name, "scope": k.scope} for k in keys
        ],
    }