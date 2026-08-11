"""Tests for the background liveness monitor.

Verifies workers are marked offline after missing heartbeats, and that a
transient error during a sweep does NOT kill the monitor (which would leave
workers stuck online forever).
"""
from __future__ import annotations

import asyncio
import time

import pytest

from prima_pool_server.config import ModelDef, Settings
from prima_pool_server.liveness import LivenessMonitor
from prima_pool_server.models import EndpointInfo, WorkerRecord, WorkerStatus
from prima_pool_server.store import Store


def _settings(**overrides) -> Settings:
    base = dict(
        models={
            "demo-model": ModelDef(
                slug="demo-model", gguf_sha256="a" * 64, required_memory_mb=4096
            )
        },
        assignable_grace_s=0,
        heartbeat_timeout_s=30,
    )
    base.update(overrides)
    return Settings(**base)


def _worker(store: Store, worker_id: str, online: bool, last_heartbeat: float) -> WorkerRecord:
    acc = store.create_account(f"user_{worker_id}", "hunter2hunter2")
    assert acc is not None
    rec = WorkerRecord(
        worker_id=worker_id,
        account_id=acc.account_id,
        model="demo-model",
        gguf_sha256="a" * 64,
        memory_allocated_mb=4096,
        wg_pubkey=f"pk_{worker_id}",
        endpoint=EndpointInfo(host="1.2.3.4", port=51820, behind_nat=False, nat_type="none"),
        hardware=None,
        status=WorkerStatus.waitlisted,
        online=online,
        last_heartbeat=last_heartbeat,
    )
    store.create_worker(rec)
    return rec


class _FakeScheduler:
    def __init__(self) -> None:
        self.offline_calls: list[str] = []

    def on_worker_offline(self, worker: WorkerRecord) -> None:
        self.offline_calls.append(worker.worker_id)


def test_marks_stale_worker_offline():
    store = Store(path=None)
    now = time.time()
    _worker(store, "wrk_stale", online=True, last_heartbeat=now - 100)  # way past 30s
    _worker(store, "wrk_fresh", online=True, last_heartbeat=now)  # recent

    scheduler = _FakeScheduler()
    monitor = LivenessMonitor(store, _settings(heartbeat_timeout_s=30), scheduler)
    monitor._check()

    assert store.get_worker("wrk_stale").online is False
    assert store.get_worker("wrk_fresh").online is True
    assert scheduler.offline_calls == ["wrk_stale"]


def test_survives_exception_in_sweep():
    """A transient error while marking one worker offline must not prevent the
    monitor from sweeping the rest (and must not kill the loop)."""
    store = Store(path=None)
    now = time.time()
    _worker(store, "wrk_bad", online=True, last_heartbeat=now - 100)
    _worker(store, "wrk_ok", online=True, last_heartbeat=now - 100)

    class _BoomScheduler:
        def on_worker_offline(self, worker: WorkerRecord) -> None:
            if worker.worker_id == "wrk_bad":
                raise RuntimeError("boom")

    monitor = LivenessMonitor(store, _settings(heartbeat_timeout_s=30), _BoomScheduler())
    # Must not raise; the bad worker's failure is contained.
    monitor._check()

    # The good worker is still marked offline despite the earlier failure.
    assert store.get_worker("wrk_ok").online is False
    # The bad worker is also offline (its online flag is persisted before the
    # scheduler notification, which is what raised) — the point is the sweep
    # continued past the failure.
    assert store.get_worker("wrk_bad").online is False


def test_run_loop_survives_exception():
    """The _run loop must keep sweeping after a _check() exception (otherwise
    workers stay online forever)."""
    store = Store(path=None)
    now = time.time()
    _worker(store, "wrk_1", online=True, last_heartbeat=now - 100)

    class _BoomScheduler:
        def __init__(self) -> None:
            self.calls = 0

        def on_worker_offline(self, worker: WorkerRecord) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("boom")

    scheduler = _BoomScheduler()
    monitor = LivenessMonitor(store, _settings(heartbeat_timeout_s=30), scheduler)

    async def scenario():
        # Patch the loop's sleep to be instant and cancel after a few passes.
        import prima_pool_server.liveness as liveness_mod

        original_sleep = liveness_mod.asyncio.sleep
        passes = {"n": 0}

        async def fast_sleep(_s):
            passes["n"] += 1
            if passes["n"] >= 3:
                monitor._task.cancel()
            await original_sleep(0)

        liveness_mod.asyncio.sleep = fast_sleep
        try:
            monitor.start()
            await monitor._task
        except asyncio.CancelledError:
            pass
        finally:
            liveness_mod.asyncio.sleep = original_sleep

    asyncio.run(scenario())
    # The loop survived the first exception and kept sweeping (didn't die).
    # The worker was marked offline on the first pass (before the scheduler
    # notification raised), so it's offline now.
    assert store.get_worker("wrk_1").online is False
