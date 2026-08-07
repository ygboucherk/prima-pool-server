"""Cluster formation scheduler.

v0 scheduler: when the sum of memory of waitlisted, online, assignable workers
for a model meets the model's required memory, form a cluster from the head of
the waitlist (FIFO). The protocol only defines the trigger and the resulting
assignment; the exact grouping policy is the scheduler's job.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import security
from .config import Settings
from .models import (
    ClusterRecord,
    ClusterStatus,
    WorkerRecord,
    WorkerStatus,
)
from .store import Store

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, store: Store, settings: Settings, hub) -> None:
        self.store = store
        self.settings = settings
        self.hub = hub  # WsHub, set after construction to avoid circular import

    def _waitlist(self, model: str) -> list[WorkerRecord]:
        """Waitlisted, online, assignable workers for a model, FIFO by creation."""
        now = time.time()
        workers = [
            w
            for w in self.store.list_workers()
            if w.model == model
            and w.status == WorkerStatus.waitlisted
            and w.online
            and w.assignable_at <= now
        ]
        workers.sort(key=lambda w: w.created_at)
        return workers

    def _next_subnet(self) -> str:
        """Allocate a /24 subnet under the configured prefix."""
        used = {c.subnet for c in self.store.list_clusters()}
        base = self.settings.cluster_subnet_prefix
        for third in range(1, 256):
            subnet = f"{base}.{third}.0/24"
            if subnet not in used:
                return subnet
        raise RuntimeError("no free cluster subnets")

    def _assign_ips(self, subnet: str, n: int) -> list[str]:
        """Assign .1..n host IPs within the subnet."""
        third = subnet.split(".")[2]
        return [f"{self.settings.cluster_subnet_prefix}.{third}.{i}" for i in range(1, n + 1)]

    def _build_cluster_config(self, cluster: ClusterRecord) -> dict:
        """Build the per-member WireGuard config (ring order = peers order)."""
        settings = self.settings
        members = [self.store.get_worker(wid) for wid in cluster.members]
        members = [m for m in members if m is not None]

        peers: list[dict] = []
        for idx, member in enumerate(members):
            peers.append(
                {
                    "pubkey": member.wg_pubkey,
                    "endpoint": (
                        f"{member.endpoint.host}:{member.endpoint.port}"
                        if member.endpoint.host
                        else None
                    ),
                    "allowed_ips": [f"{cluster.ips[member.worker_id]}/32"],
                    "persistent_keepalive": settings.wg_persistent_keepalive,
                    "preferred": "relay" if member.endpoint.behind_nat else "direct",
                }
            )

        relay = {
            "pubkey": settings.relay_pubkey,
            "endpoint": settings.relay_endpoint,
            "enabled": settings.relay_enabled,
        }

        return {
            "cluster_id": cluster.cluster_id,
            "interface": {
                "private_ip": "",  # filled per-member
                "subnet": cluster.subnet,
                "mtu": settings.wg_mtu,
            },
            "relay": relay,
            "peers": peers,
        }

    def _form_cluster(self, model: str) -> ClusterRecord | None:
        """Form a cluster from the waitlist if enough memory is available."""
        required = self.settings.models.get(model)
        if required is None:
            return None
        waitlist = self._waitlist(model)
        if not waitlist:
            return None

        total = 0
        chosen: list[WorkerRecord] = []
        for w in waitlist:
            chosen.append(w)
            total += w.memory_allocated_mb
            if total >= required:
                break
        if total < required:
            return None

        cluster_id = security.new_id("clu")
        subnet = self._next_subnet()
        ips = self._assign_ips(subnet, len(chosen))
        cluster = ClusterRecord(
            cluster_id=cluster_id,
            model=model,
            subnet=subnet,
            members=[w.worker_id for w in chosen],
            ips={w.worker_id: ips[i] for i, w in enumerate(chosen)},
        )
        self.store.create_cluster(cluster)

        for i, w in enumerate(chosen):
            w.status = WorkerStatus.assigned
            w.cluster_id = cluster_id
            w.assigned_ip = ips[i]
            w.ring_position = i
            self.store.update_worker(w)

        logger.info("formed cluster %s for model %s (%d members)", cluster_id, model, len(chosen))
        return cluster

    def _dissolve_cluster(self, cluster: ClusterRecord, reason: str) -> None:
        """Return all members to the waitlist and notify them."""
        for wid in cluster.members:
            w = self.store.get_worker(wid)
            if w is None:
                continue
            w.status = WorkerStatus.waitlisted
            w.cluster_id = None
            w.assigned_ip = None
            w.ring_position = None
            self.store.update_worker(w)
        self.store.delete_cluster(cluster.cluster_id)
        logger.info("dissolved cluster %s (%s)", cluster.cluster_id, reason)
        # Notify members asynchronously
        asyncio.get_event_loop().create_task(
            self.hub.broadcast_cluster_dissolved(cluster.cluster_id, reason)
        )

    def check_and_form(self) -> None:
        """Called after any waitlist-affecting change."""
        for model in self.settings.models:
            self._form_cluster(model)

    def on_worker_offline(self, worker: WorkerRecord) -> None:
        """A worker went offline. If it was in a cluster, dissolve it."""
        if worker.cluster_id:
            cluster = self.store.get_cluster(worker.cluster_id)
            if cluster:
                self._dissolve_cluster(cluster, "member_offline")

    def on_worker_leave(self, worker: WorkerRecord) -> None:
        """A worker left. If it was in a cluster, dissolve it."""
        if worker.cluster_id:
            cluster = self.store.get_cluster(worker.cluster_id)
            if cluster:
                self._dissolve_cluster(cluster, "member_left")

    def on_ready(self, cluster_id: str, worker_id: str) -> ClusterStatus:
        """Record a member's readiness. Returns the cluster status."""
        cluster = self.store.get_cluster(cluster_id)
        if cluster is None:
            raise KeyError(cluster_id)
        cluster.ready.add(worker_id)
        if len(cluster.ready) >= len(cluster.members):
            cluster.status = ClusterStatus.live
        self.store.update_cluster(cluster)
        return cluster.status
