"""WebSocket push hub.

Workers connect to /v1/workers/{worker_id}/events and receive cluster
assignment / dissolution frames. The WS is an accelerator — every event is
recoverable via REST (GET /workers/{id}/state).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import WebSocket

from .config import Settings
from .store import Store

logger = logging.getLogger(__name__)


class WsHub:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self._connections: dict[str, WebSocket] = {}  # worker_id -> ws

    async def connect(self, worker_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._connections[worker_id] = ws
        logger.info("ws connected for worker %s", worker_id)

    def disconnect(self, worker_id: str) -> None:
        self._connections.pop(worker_id, None)

    async def send(self, worker_id: str, frame: dict[str, Any]) -> None:
        ws = self._connections.get(worker_id)
        if ws is None:
            return
        try:
            await ws.send_json(frame)
        except Exception:
            self.disconnect(worker_id)

    async def broadcast_cluster_assigned(self, cluster_id: str, worker_id: str, payload: dict[str, Any]) -> None:
        await self.send(worker_id, {"type": "cluster_assigned", **payload})

    async def broadcast_cluster_dissolved(self, cluster_id: str, reason: str) -> None:
        frame = {"type": "cluster_dissolved", "cluster_id": cluster_id, "reason": reason}
        # Notify all members of the cluster (we don't track membership here, so
        # the scheduler passes the cluster_id; members are looked up via store).
        for wid in list(self._connections.keys()):
            w = self.store.get_worker(wid)
            if w and w.cluster_id == cluster_id:
                await self.send(wid, frame)

    async def send_hello(self, worker_id: str) -> None:
        w = self.store.get_worker(worker_id)
        if w is None:
            return
        await self.send(
            worker_id,
            {
                "type": "hello",
                "state": {"status": w.status.value, "online": w.online},
                "cadence": {
                    "heartbeat_s": int(self.settings.heartbeat_interval_s),
                    "ws_reconnect_backoff_s": self.settings.ws_reconnect_backoff_s,
                },
            },
        )
