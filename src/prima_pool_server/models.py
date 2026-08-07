"""Pydantic request/response schemas and domain dataclasses.

These mirror the OpenAPI spec in docs/openapi/prima-pool.yaml.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


# ── Enums ────────────────────────────────────────────────────────────────
class WorkerStatus(str, Enum):
    registered = "registered"
    waitlisted = "waitlisted"
    assigned = "assigned"


class ClusterStatus(str, Enum):
    assembling = "assembling"
    live = "live"


class NatType(str, Enum):
    none = "none"
    cone = "cone"
    symmetric = "symmetric"
    unknown = "unknown"


class Preferred(str, Enum):
    direct = "direct"
    relay = "relay"


# ── Request schemas ──────────────────────────────────────────────────────
class RegisterAccountRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)


class LoginRequest(BaseModel):
    username: str
    password: str


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scope: Literal["worker", "user"]


class EndpointInfo(BaseModel):
    host: str
    port: int = Field(ge=1, le=65535)
    behind_nat: bool = False
    nat_type: NatType = NatType.unknown


class Hardware(BaseModel):
    cpu: str | None = None
    gpu: str | None = None
    ram_gb: float | None = None
    os: str | None = None
    prima_version: str | None = None


class RegisterWorkerRequest(BaseModel):
    model: str
    memory_allocated_mb: int = Field(ge=1)
    wg_pubkey: str
    endpoint: EndpointInfo
    hardware: Hardware | None = None


class ReportReadyRequest(BaseModel):
    """Empty body; the caller is identified by the worker-scoped API key."""

    pass


# ── Response schemas ─────────────────────────────────────────────────────
class Account(BaseModel):
    account_id: str
    username: str
    created_at: str


class Session(BaseModel):
    session_token: str
    expires_at: str


class ApiKey(BaseModel):
    key_id: str
    name: str
    scope: str
    api_key: str
    created_at: str


class ApiKeySummary(BaseModel):
    key_id: str
    name: str
    scope: str
    created_at: str


class Worker(BaseModel):
    worker_id: str
    account_id: str
    status: WorkerStatus
    model: str
    waitlist_position: int | None = None
    online: bool = True


class ClusterAssignment(BaseModel):
    cluster_id: str
    assigned_ip: str
    config_url: str


class WorkerState(BaseModel):
    worker_id: str
    account_id: str
    status: WorkerStatus
    online: bool
    model: str
    cluster: ClusterAssignment | None = None


class InterfaceConfig(BaseModel):
    private_ip: str
    subnet: str
    mtu: int = 1280


class RelayConfig(BaseModel):
    pubkey: str = ""
    endpoint: str = ""
    enabled: bool = False


class PeerConfig(BaseModel):
    pubkey: str
    endpoint: str | None = None
    allowed_ips: list[str]
    persistent_keepalive: int = 25
    preferred: Preferred = Preferred.direct
    # Optional role marker. "server" = the control plane (NOT a ring member);
    # clients must exclude server peers from ring topology computation.
    role: str | None = None


class ClusterConfig(BaseModel):
    cluster_id: str
    interface: InterfaceConfig
    relay: RelayConfig = RelayConfig()
    peers: list[PeerConfig]


class ClusterStatusResponse(BaseModel):
    cluster_id: str
    status: ClusterStatus
    members_ready: int
    members_total: int


# ── Domain dataclasses (internal state) ──────────────────────────────────
@dataclass
class AccountRecord:
    account_id: str
    username: str
    password_hash: str
    created_at: float


@dataclass
class ApiKeyRecord:
    key_id: str
    account_id: str
    name: str
    scope: str
    key_hash: str
    created_at: float


@dataclass
class WorkerRecord:
    worker_id: str
    account_id: str
    model: str
    memory_allocated_mb: int
    wg_pubkey: str
    endpoint: EndpointInfo
    hardware: Hardware | None
    status: WorkerStatus = WorkerStatus.registered
    online: bool = False
    last_heartbeat: float = 0.0
    assignable_at: float = 0.0
    cluster_id: str | None = None
    assigned_ip: str | None = None
    ring_position: int | None = None
    created_at: float = field(default_factory=time.time)

    def to_worker(self, waitlist_position: int | None = None) -> Worker:
        return Worker(
            worker_id=self.worker_id,
            account_id=self.account_id,
            status=self.status,
            model=self.model,
            waitlist_position=waitlist_position,
            online=self.online,
        )

    def to_state(self, config_url: str | None = None) -> WorkerState:
        cluster = None
        if self.cluster_id and self.assigned_ip:
            cluster = ClusterAssignment(
                cluster_id=self.cluster_id,
                assigned_ip=self.assigned_ip,
                config_url=config_url or "",
            )
        return WorkerState(
            worker_id=self.worker_id,
            account_id=self.account_id,
            status=self.status,
            online=self.online,
            model=self.model,
            cluster=cluster,
        )


@dataclass
class ClusterRecord:
    cluster_id: str
    model: str
    subnet: str
    members: list[str]  # worker_ids, in ring order (index 0 = head)
    status: ClusterStatus = ClusterStatus.assembling
    ready: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)
    # worker_id -> assigned private IP
    ips: dict[str, str] = field(default_factory=dict)
