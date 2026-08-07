"""Background liveness monitor.

Periodically marks workers offline when they miss heartbeats, and dissolves
clusters whose members go offline. Runs as an asyncio task in the app.
"""
from __future__ import annotations

import asyncio
import logging
import time

from .config import Settings
from .models import WorkerStatus
from .store import Store

logger = logging.getLogger(__name__)


class LivenessMonitor:
    def __init__(self, store: Store, settings: Settings, scheduler) -> None:
        self.store = store
        self.settings = settings
        self.scheduler = scheduler
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        interval = max(1.0, self.settings.heartbeat_timeout_s / 3)
        while True:
            await asyncio.sleep(interval)
            self._check()

    def _check(self) -> None:
        now = time.time()
        timeout = self.settings.heartbeat_timeout_s
        for w in self.store.list_workers():
            if not w.online:
                continue
            if now - w.last_heartbeat > timeout:
                logger.info("worker %s offline (no heartbeat)", w.worker_id)
                w.online = False
                self.store.update_worker(w)
                self.scheduler.on_worker_offline(w)
