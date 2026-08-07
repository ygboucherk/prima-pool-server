"""Cluster router: find a live cluster's head and build the proxy target.

The head (ring position 0) runs llama-server on the WG private IP at
`api_port` (default 8080). The server reaches it over the WireGuard tunnel
(option A: server joins the cluster network).
"""
from __future__ import annotations

import logging

from .config import Settings
from .models import ClusterStatus
from .store import Store

logger = logging.getLogger(__name__)


class ClusterRouter:
    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings

    def find_live_cluster(self, model: str):
        """Return the first live cluster serving `model`, or None."""
        for cluster in self.store.list_clusters():
            if cluster.model == model and cluster.status == ClusterStatus.live:
                return cluster
        return None

    def head_ip(self, cluster) -> str | None:
        """Return the head's WG private IP (ring position 0)."""
        if not cluster.members:
            return None
        head_id = cluster.members[0]
        return cluster.ips.get(head_id)

    def head_url(self, cluster) -> str | None:
        """Return the base URL of the head's llama-server, or None."""
        ip = self.head_ip(cluster)
        if not ip:
            return None
        return f"http://{ip}:{self.settings.api_port}"
