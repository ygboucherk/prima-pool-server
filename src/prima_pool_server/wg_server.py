"""Server-side WireGuard management (option A).

The pool server joins each cluster's WireGuard network so it can reach the
head's llama-server (port 8080) and proxy inference requests to it.

The server:
  - generates its own WG keypair (private key never leaves the server)
  - gets a private IP in each cluster subnet (default .254)
  - is added as a peer in every member's config (so workers can reach it)
  - brings up a WG interface per cluster (or one interface, multiple peers)

This module is pure logic (keygen + config rendering + command building) so it
is testable without root/WireGuard. The actual `wg-quick` invocation is only
performed when `server_join_wg` is enabled.
"""
from __future__ import annotations

import base64
import logging
import shutil
import subprocess
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from .config import Settings
from .models import ClusterRecord

logger = logging.getLogger(__name__)


def generate_keypair() -> tuple[str, str]:
    """Generate a (private_key, public_key) WireGuard keypair (base64)."""
    private = x25519.X25519PrivateKey.generate()
    public = private.public_key()
    priv_b64 = base64.b64encode(
        private.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
    ).decode()
    pub_b64 = base64.b64encode(
        public.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode()
    return priv_b64, pub_b64


def derive_public_key(private_key_b64: str) -> str:
    """Derive the public key from a base64 private key."""
    raw = base64.b64decode(private_key_b64)
    private = x25519.X25519PrivateKey.from_private_bytes(raw)
    pub = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(pub).decode()


class ServerWireGuard:
    """Manages the server's WireGuard interfaces for joining clusters."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._private_key = settings.server_wg_private_key
        self._public_key = ""
        if self._private_key:
            self._public_key = derive_public_key(self._private_key)
        self._wg_quick = shutil.which("wg-quick")
        self._wg = shutil.which("wg")

    @property
    def enabled(self) -> bool:
        return self.settings.server_join_wg

    @property
    def public_key(self) -> str:
        """Return the server's WG public key, generating a keypair if needed."""
        if not self._public_key:
            self._private_key, self._public_key = generate_keypair()
        return self._public_key

    @property
    def private_key(self) -> str:
        if not self._private_key:
            self._private_key, self._public_key = generate_keypair()
        return self._private_key

    def server_ip(self, subnet: str) -> str:
        """The server's private IP within a cluster subnet (e.g. 10.23.1.254)."""
        parts = subnet.split(".")
        return f"{parts[0]}.{parts[1]}.{parts[2]}.{self.settings.server_wg_ip_offset}"

    def server_peer(self, cluster: ClusterRecord) -> dict:
        """The server's peer entry, to be added to every member's config.

        Marked with `role: "server"` so clients can distinguish the control
        plane from ring members (the server is NOT part of the prima.cpp ring).
        """
        host = self.settings.server_wg_endpoint_host
        return {
            "pubkey": self.public_key,
            "endpoint": f"{host}:{self.settings.server_wg_listen_port}" if host else None,
            "allowed_ips": [f"{self.server_ip(cluster.subnet)}/32"],
            "persistent_keepalive": self.settings.wg_persistent_keepalive,
            "preferred": "direct",
            "role": "server",
        }

    def render_server_conf(self, cluster: ClusterRecord, members: list) -> str:
        """Render the server's WG config for a cluster (server as interface,
        all members as peers)."""
        lines: list[str] = []
        lines.append("[Interface]")
        lines.append(f"PrivateKey = {self.private_key}")
        lines.append(f"Address = {self.server_ip(cluster.subnet)}/24")
        lines.append(f"MTU = {self.settings.wg_mtu}")
        lines.append(f"ListenPort = {self.settings.server_wg_listen_port}")
        lines.append("")

        for member in members:
            if member is None or member.wg_pubkey is None:
                continue
            lines.append("[Peer]")
            lines.append(f"PublicKey = {member.wg_pubkey}")
            if member.endpoint and member.endpoint.host:
                lines.append(f"Endpoint = {member.endpoint.host}:{member.endpoint.port}")
            lines.append(f"AllowedIPs = {cluster.ips[member.worker_id]}/32")
            lines.append(f"PersistentKeepalive = {self.settings.wg_persistent_keepalive}")
            lines.append("")

        return "\n".join(lines)

    def conf_path(self, cluster_id: str) -> Path:
        return Path(self.settings.server_wg_conf_dir) / f"{self.settings.server_wg_interface}-{cluster_id}.conf"

    def up(self, cluster: ClusterRecord, members: list) -> None:
        """Bring up the server's WG interface for a cluster (if enabled)."""
        if not self.enabled:
            logger.info("server WG join disabled; skipping %s", cluster.cluster_id)
            return
        if not self._wg_quick or not self._wg:
            raise RuntimeError("wg-quick/wg not found; cannot join WireGuard network")
        conf = self.render_server_conf(cluster, members)
        path = self.conf_path(cluster.cluster_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(conf)
        path.chmod(0o600)
        iface = f"{self.settings.server_wg_interface}-{cluster.cluster_id}"
        subprocess.run([self._wg_quick, "up", iface], check=True, capture_output=True, text=True)
        logger.info("server joined cluster %s via %s", cluster.cluster_id, iface)

    def down(self, cluster_id: str) -> None:
        if not self.enabled:
            return
        iface = f"{self.settings.server_wg_interface}-{cluster_id}"
        subprocess.run([self._wg_quick, "down", iface], capture_output=True, text=True)
        logger.info("server left cluster %s", cluster_id)
