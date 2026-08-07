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
    # When true, credentials (passwords, API keys, session tokens) are returned
    # in plaintext responses. Intended for local development only.
    dev_insecure: bool = _env_bool("PRIMA_POOL_DEV_INSECURE", True)
    # Base URL advertised in WS frames / config URLs (defaults to localhost).
    public_base_url: str = os.environ.get("PRIMA_POOL_PUBLIC_BASE_URL", "http://127.0.0.1:8000")

    # Security / crypto
    password_bcrypt_rounds: int = _env_int("PRIMA_POOL_BCRYPT_ROUNDS", 10)
    session_ttl_s: int = _env_int("PRIMA_POOL_SESSION_TTL_S", 3600)
    session_secret: str = os.environ.get("PRIMA_POOL_SESSION_SECRET", "dev-session-secret-change-me")
    api_key_prefix_worker: str = "sk-worker-"
    api_key_prefix_user: str = "sk-user-"

    # Cadence / liveness
    heartbeat_timeout_s: float = float(_env_int("PRIMA_POOL_HEARTBEAT_TIMEOUT_S", 30))
    heartbeat_interval_s: float = float(_env_int("PRIMA_POOL_HEARTBEAT_INTERVAL_S", 10))
    assignable_grace_s: float = float(_env_int("PRIMA_POOL_ASSIGNABLE_GRACE_S", 5))
    readiness_timeout_s: float = float(_env_int("PRIMA_POOL_READINESS_TIMEOUT_S", 60))

    # Worker policy
    max_workers_per_account: int = _env_int("PRIMA_POOL_MAX_WORKERS_PER_ACCOUNT", 5)

    # Model registry (v0: static config)
    models: dict[str, int] = field(default_factory=lambda: _parse_models())
    # Cluster settings
    cluster_subnet_prefix: str = "10.23"
    wg_endpoint_base: str = os.environ.get("PRIMA_POOL_WG_ENDPOINT_BASE", "")
    wg_mtu: int = _env_int("PRIMA_POOL_WG_MTU", 1280)
    wg_persistent_keepalive: int = 25

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
