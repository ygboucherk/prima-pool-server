"""Server configuration.

All values are overridable via environment variables (PRIMA_POOL_*).
See the README for a full description of each setting.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return int(raw)


@dataclass(frozen=True)
class Settings:
    """Runtime settings, read from the environment."""

    # Networking / serving
    host: str = os.environ.get("PRIMA_POOL_HOST", "0.0.0.0")
    port: int = _env_int("PRIMA_POOL_PORT", 8000)
    # Base URL advertised in WS frames / config URLs (defaults to localhost).
    public_base_url: str = os.environ.get("PRIMA_POOL_PUBLIC_BASE_URL", "http://127.0.0.1:8000")

    # Security / crypto
    session_ttl_s: int = _env_int("PRIMA_POOL_SESSION_TTL_S", 3600)
    session_secret: str = os.environ.get("PRIMA_POOL_SESSION_SECRET", "dev-session-secret-change-me")

    # Cadence / liveness
    heartbeat_timeout_s: float = float(_env_int("PRIMA_POOL_HEARTBEAT_TIMEOUT_S", 30))
    heartbeat_interval_s: float = float(_env_int("PRIMA_POOL_HEARTBEAT_INTERVAL_S", 10))
    assignable_grace_s: float = float(_env_int("PRIMA_POOL_ASSIGNABLE_GRACE_S", 5))

    # Worker policy
    max_workers_per_account: int = _env_int("PRIMA_POOL_MAX_WORKERS_PER_ACCOUNT", 5)

    # Model registry (v0: static config)
    models: dict[str, int] = field(default_factory=lambda: _parse_models())
    # The port the head's llama-server listens on (proxied by the server).
    api_port: int = _env_int("PRIMA_POOL_API_PORT", 8080)
    # Cluster settings
    cluster_subnet_prefix: str = "10.23"
    wg_mtu: int = _env_int("PRIMA_POOL_WG_MTU", 1280)
    wg_persistent_keepalive: int = 25

    # Server-side WireGuard (option A: server joins clusters to proxy requests).
    # The server generates its own keypair and gets a private IP in each cluster
    # subnet so it can reach the head's llama-server over the tunnel.
    server_wg_private_key: str = os.environ.get("PRIMA_POOL_SERVER_WG_PRIVATE_KEY", "")
    server_wg_listen_port: int = _env_int("PRIMA_POOL_SERVER_WG_LISTEN_PORT", 51821)
    server_wg_interface: str = os.environ.get("PRIMA_POOL_SERVER_WG_INTERFACE", "prima-pool-srv")
    server_wg_conf_dir: str = os.environ.get("PRIMA_POOL_SERVER_WG_CONF_DIR", "/etc/wireguard")
    # Host IP advertised to workers as the server's WG endpoint.
    server_wg_endpoint_host: str = os.environ.get("PRIMA_POOL_SERVER_WG_ENDPOINT_HOST", "")
    # The server's private IP offset within each cluster subnet (e.g. .254).
    server_wg_ip_offset: int = _env_int("PRIMA_POOL_SERVER_WG_IP_OFFSET", 254)
    # Whether the server actually joins the WG network (proxy enabled).
    server_join_wg: bool = _env_bool("PRIMA_POOL_SERVER_JOIN_WG", False)

    # Relay (not implemented in v0; present so configs can carry the field)
    relay_enabled: bool = _env_bool("PRIMA_POOL_RELAY_ENABLED", False)
    relay_pubkey: str = os.environ.get("PRIMA_POOL_RELAY_PUBKEY", "")
    relay_endpoint: str = os.environ.get("PRIMA_POOL_RELAY_ENDPOINT", "")

    # WS
    ws_reconnect_backoff_s: list[int] = field(default_factory=lambda: [1, 30])


def _parse_models() -> dict[str, int]:
    """Parse PRIMA_POOL_MODELS as 'name:required_memory_mb[,name:required_memory_mb...]'.

    Example: PRIMA_POOL_MODELS="llama-3.1-8b-instruct:16384,qwen2.5-3b:6144"
    """
    raw = os.environ.get("PRIMA_POOL_MODELS", "demo-model:4096")
    result: dict[str, int] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, _, mem = chunk.partition(":")
        name = name.strip()
        try:
            required_mb = int(mem) if mem else 4096
        except ValueError:
            required_mb = 4096
        result[name] = required_mb
    return result
