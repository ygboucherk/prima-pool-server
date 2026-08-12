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
from .wg_server import ServerWireGuard

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, store: Store, settings: Settings, hub, wg_server: ServerWireGuard | None = None) -> None:
        self.store = store
        self.settings = settings
        self.hub = hub  # WsHub, set after construction to avoid circular import
        self.wg_server = wg_server or ServerWireGuard(settings)

    def _waitlist(self, model: str, gguf_sha256: str) -> list[WorkerRecord]:
        """Waitlisted, online, assignable workers for a (model, hash), FIFO."""
        now = time.time()
        workers = [
            w
            for w in self.store.list_workers()
            if w.model == model
            and w.gguf_sha256 == gguf_sha256
            and w.status == WorkerStatus.waitlisted
            and w.online
            and w.assignable_at <= now
        ]
        workers.sort(key=lambda w: w.created_at)
        return workers

    def _next_subnet(self) -> str:
        """Allocate a /24 subnet under the configured prefix.

        Avoids reusing a subnet that is still referenced by any worker's
        assigned IP (a dissolved cluster's members may still have their WG
        interface up with the old IP until they process cluster_dissolved).
        """
        # Only live/assembling clusters hold an active subnet. Terminated
        # clusters' subnets are freed for reuse: their members' assigned IPs
        # are cleared on dissolution, so nothing references them anymore.
        used = {
            c.subnet
            for c in self.store.list_clusters()
            if c.status != ClusterStatus.terminated
        }
        base = self.settings.cluster_subnet_prefix
        # Collect subnets still referenced by any worker's assigned IP.
        for w in self.store.list_workers():
            if w.assigned_ip:
                parts = w.assigned_ip.split(".")
                if len(parts) == 4:
                    used.add(f"{parts[0]}.{parts[1]}.{parts[2]}.0/24")
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
        """Build the per-member WireGuard config (ring order = peers order).

        If the server joins the WG network, the server is appended as an extra
        peer so members can reach it (and it can reach them).
        """
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

        # Append the server as a peer so members can reach the control plane
        # over the tunnel (option A: server joins the cluster network).
        if self.wg_server.enabled:
            if not self.wg_server.has_endpoint:
                logger.warning(
                    "server WG join enabled but PRIMA_POOL_SERVER_WG_ENDPOINT_HOST "
                    "is not set; workers cannot reach the server peer"
                )
            peers.append(self.wg_server.server_peer(cluster))

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

    def _schedule(self, coro) -> None:
        """Schedule an async coroutine on the running event loop, if any."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            loop.create_task(coro)
        else:
            logger.warning("no running event loop; dropping async notification")

    def _form_cluster(self, model: str) -> ClusterRecord | None:
        """Form a cluster from the waitlist if enough memory is available.

        Workers are matched per (model, gguf_sha256): a cluster only ever
        groups workers serving the exact same GGUF (same model, same
        quantization). Mismatched hashes are never joined.
        """
        model_def = self.settings.models.get(model)
        if model_def is None:
            return None
        waitlist = self._waitlist(model, model_def.gguf_sha256)
        if not waitlist:
            return None

        total = 0
        chosen: list[WorkerRecord] = []
        for w in waitlist:
            chosen.append(w)
            total += w.memory_allocated_mb
            if total >= model_def.required_memory_mb:
                break
        if total < model_def.required_memory_mb:
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
            # Push cluster_assigned to each member over its WebSocket.
            self._schedule(
                self.hub.broadcast_cluster_assigned(
                    cluster_id,
                    w.worker_id,
                    {
                        "cluster_id": cluster_id,
                        "worker_id": w.worker_id,
                        "model": model,
                        "assigned_ip": ips[i],
                        "subnet": subnet,
                        "ring_position": i,
                        "config_url": f"{self.settings.public_base_url}/v1/clusters/{cluster_id}/config",
                    },
                )
            )

        logger.info("formed cluster %s for model %s (%d members)", cluster_id, model, len(chosen))
        # Option A: the server joins the cluster's WG network so it can proxy
        # requests to the head. Do this after members are assigned.
        if self.wg_server.enabled:
            try:
                self.wg_server.up(cluster, chosen)
            except Exception as exc:  # noqa: BLE001
                logger.error("server failed to join cluster %s: %s", cluster_id, exc)
        return cluster

    def _dissolve_cluster(self, cluster: ClusterRecord, reason: str) -> None:
        """Return all members to the waitlist and mark the cluster terminated.

        The cluster row is retained (status=terminated) rather than deleted so
        its history survives for accounting/auditing — e.g. the `requests`
        table references it, and future worker crediting needs to know which
        members hosted each request.
        """
        member_ids = list(cluster.members)
        for wid in member_ids:
            w = self.store.get_worker(wid)
            if w is None:
                continue
            w.status = WorkerStatus.waitlisted
            w.cluster_id = None
            w.assigned_ip = None
            w.ring_position = None
            self.store.update_worker(w)
        cluster.status = ClusterStatus.terminated
        self.store.update_cluster(cluster)
        logger.info("terminated cluster %s (%s)", cluster.cluster_id, reason)
        # Option A: the server leaves the cluster's WG network.
        if self.wg_server.enabled:
            try:
                self.wg_server.down(cluster.cluster_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("server failed to leave cluster %s: %s", cluster.cluster_id, exc)
        # Notify members asynchronously (pass member ids explicitly, since their
        # cluster_id has already been cleared).
        self._schedule(
            self.hub.broadcast_cluster_dissolved(cluster.cluster_id, reason, member_ids)
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

    def _maybe_go_live(self, cluster: ClusterRecord) -> bool:
        """A cluster is live when ALL members reported ready AND the head has
        reported the layer distribution (a live cluster always carries one).

        The distribution is 'reported' even when the head's parse failed
        (layer_windows={} records 'unknown'), so a parse failure can never
        block availability — it only marks the accounting data as unknown.

        Never resurrects a TERMINATED cluster: a late ready/distribution frame
        racing a dissolve must not flip a dissolved cluster back to live.
        """
        if cluster.status == ClusterStatus.terminated:
            return False
        all_members_ready = len(cluster.ready) >= len(cluster.members)
        distribution_reported = cluster.layer_windows is not None
        if all_members_ready and distribution_reported:
            cluster.status = ClusterStatus.live
            return True
        return False

    def on_ready(self, cluster_id: str, worker_id: str) -> ClusterStatus:
        """Record a member's readiness. Returns the cluster status."""
        cluster = self.store.get_cluster(cluster_id)
        if cluster is None:
            raise KeyError(cluster_id)
        cluster.ready.add(worker_id)
        self._maybe_go_live(cluster)
        self.store.update_cluster(cluster)
        return cluster.status

    def on_layer_distribution(self, cluster_id: str, layer_windows: dict[str, int] | None) -> ClusterStatus:
        """Record the head's layer-distribution report.

        Only the head (rank 0 / ring_position 0) is expected to send this.
        An EMPTY dict ({}) is the 'reported unknown' marker (parse failure) and
        must still satisfy the liveness gate — only None ('not reported')
        blocks it. Returns the cluster status.
        """
        cluster = self.store.get_cluster(cluster_id)
        if cluster is None:
            raise KeyError(cluster_id)
        cluster.layer_windows = layer_windows
        self._maybe_go_live(cluster)
        self.store.update_cluster(cluster)
        return cluster.status
