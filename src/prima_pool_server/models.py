"""Pydantic request/response schemas and domain dataclasses.

These mirror the OpenAPI spec in docs/openapi/prima-pool.yaml.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_serializer


# Balance is stored in a SQLite INTEGER (64-bit signed). Values outside this
# range raise OverflowError on insert, so request fields are bounded here to
# yield a clean 422 instead, and the store re-checks results against the same
# bounds so a computed overflow (e.g. balance + delta) can't 500 either.
# NOTE: 2^63-1 minor units == ~9.2 tokens (unit is 10^-18 token), so the
# INTEGER ceiling is a real limit for large balances — see the billing-balances
# design note (re-denomination / TEXT storage is the escape hatch when real
# billing lands).
BALANCE_MIN = -(2**63)
BALANCE_MAX = 2**63 - 1


# ── Enums ────────────────────────────────────────────────────────────────
class WorkerStatus(str, Enum):
    registered = "registered"
    waitlisted = "waitlisted"
    assigned = "assigned"


class ClusterStatus(str, Enum):
    assembling = "assembling"
    live = "live"
    # Terminal state: the cluster has been dissolved (a member went offline or
    # left). The row is retained for history/accounting rather than deleted.
    terminated = "terminated"


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


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=256)


class UpdateAccountPermissionsRequest(BaseModel):
    """Admin-gated toggle for one account's permissions.

    Each field, when present, sets the account's value; absent fields are left
    untouched. The request must set at least one field. `is_admin` demotion is
    guarded by "cannot demote the last admin" in the endpoint.
    """

    is_admin: bool | None = None
    can_work: bool | None = None
    can_use: bool | None = None
    banned: bool | None = None


class SetBalanceRequest(BaseModel):
    """Admin: set an account's balance to an exact integer value.

    The unit is 10^-18 token (ERC20-style minor units); the field accepts a
    JSON integer or a numeric string (Pydantic coerces), so a client may send
    a >2^53 value as a string to avoid float64 loss.
    """

    balance: int = Field(ge=BALANCE_MIN, le=BALANCE_MAX)
    reason: str | None = None


class AdjustBalanceRequest(BaseModel):
    """Admin: adjust an account's balance by a signed integer delta.

    No sign restriction — negative deltas are allowed (deductions), as is
    going negative.
    """

    delta: int = Field(ge=BALANCE_MIN, le=BALANCE_MAX)
    reason: str | None = None


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
    # SHA-256 of the GGUF file this worker will serve. The server matches
    # workers per (model, hash) so a cluster always runs the same model with
    # the same quantization — mismatched GGUFs are never grouped together.
    gguf_sha256: str = Field(min_length=64, max_length=64)
    memory_allocated_mb: int = Field(ge=1)
    wg_pubkey: str
    endpoint: EndpointInfo
    hardware: Hardware | None = None


class ReportReadyRequest(BaseModel):
    """Readiness report. The caller is identified by the worker-scoped API key.

    `layer_windows` (rank-keyed: rank -> layer count) is OPTIONAL and only
    meaningful from the head (rank 0): it carries the Halda allocation so the
    server can record it even if the WS distribution frame was lost. Workers
    leave it unset.
    """

    layer_windows: dict[str, int] | None = None


# ── Response schemas ─────────────────────────────────────────────────────
class Account(BaseModel):
    account_id: str
    username: str
    created_at: str
    # Integer balance in 10^-18 token units (ERC20-style minor units).
    # Transported as a JSON string so clients (and float64) never lose
    # precision once the value exceeds 2^53 (~0.009 tokens).
    balance: int = 0

    @field_serializer("balance")
    def _balance_str(self, value: int) -> str:
        return str(value)


class AdminAccount(BaseModel):
    """Admin view of an account: identity + the four permission booleans + the
    EFFECTIVE capabilities.

    `can_work`/`can_use` are the raw per-account flags. `effective_can_work`/
    `effective_can_use` are what the account can ACTUALLY do, computed as
    `(not banned) and (flag or *_PERMISSIONLESS)` — the same formula the
    enforcement layer uses, so the admin UI never disagrees with reality.
    """

    account_id: str
    username: str
    is_admin: bool
    can_work: bool
    can_use: bool
    banned: bool
    effective_can_work: bool
    effective_can_use: bool
    created_at: str
    # Integer balance in 10^-18 token units (serialized as a string).
    balance: int = 0

    @field_serializer("balance")
    def _balance_str(self, value: int) -> str:
        return str(value)


class BalanceEvent(BaseModel):
    """One balance-mutation event, visible to the account owner.

    `kind` is "set" (balance set to an absolute value) or "adjust" (balance
    changed by a signed delta). `delta` is the signed change; `balance_after`
    is the resulting balance. `reason` is an optional operator memo.
    """

    event_id: str
    kind: str
    delta: int
    balance_after: int
    reason: str | None
    created_at: str

    @field_serializer("delta", "balance_after")
    def _int_str(self, value: int) -> str:
        return str(value)


class AdminBalanceEvent(BalanceEvent):
    """Admin view of a balance event: adds the acting admin's identity.

    `admin_username` is None when the mutation had no recorded admin (e.g. a
    store-level operation without an actor, or the acting admin account has
    since been deleted).
    """

    admin_username: str | None = None


class AccountBalance(BaseModel):
    """A user's own balance (10^-18 token units, serialized as a string)."""

    account_id: str
    balance: int

    @field_serializer("balance")
    def _balance_str(self, value: int) -> str:
        return str(value)


class PermissionState(BaseModel):
    """Admin view of the pool-wide permissionless switches."""

    work_permissionless: bool
    use_permissionless: bool


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


class ModelInfo(BaseModel):
    """A model the pool serves, keyed by (slug, gguf_sha256)."""

    slug: str
    gguf_sha256: str
    required_memory_mb: int
    live: bool  # whether at least one live cluster currently serves it


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


class WorkerInfo(BaseModel):
    """Public worker info (unauthenticated, deliberately minimal).

    Exposes only class-level, anonymized data: the worker id (already known
    to the caller), the advertised RAM pool share, and the model the worker
    serves. NO account id, NO endpoint/WG/IP data, NO availability history —
    those stay behind auth.

    memory_allocated_mb is the share the worker ADVERTISED at registration
    (updated on re-registration of the same device) — it is a stable capacity
    signal, NOT live free RAM (which would leak availability patterns).

    Future hardware stats must stay class-level (e.g. device class, CPU
    family) and must NOT become instance fingerprints (exact CPU model
    strings, OS versions) — this endpoint is public.
    """

    worker_id: str
    model: str
    memory_allocated_mb: int


class ClusterMemberInfo(BaseModel):
    """One member of a cluster, as shown in the public cluster info.

    Anonymized: worker_id is an opaque random id (no owner, no endpoint).
    """

    worker_id: str
    layer_window: int | None = None


class ClusterInfo(BaseModel):
    """Public cluster info (unauthenticated, deliberately minimal).

    Exposes the member list (in ring order, index 0 = head) and the layer
    distribution per worker — the "what kind of machines ran my prompt"
    amazement. NO account ids, NO WG IPs, NO endpoint data.

    layer_window may be None (head has not reported a distribution yet) or 0
    (a pure forwarder — a valid value, not "no data").
    """

    cluster_id: str
    model: str
    status: ClusterStatus
    members: list[ClusterMemberInfo]


class RequestLogEntry(BaseModel):
    """A single logged inference request (user-facing view)."""

    request_id: str
    model: str
    cluster_id: str
    prompt_tokens: int
    completion_tokens: int
    created_at: float


class UsageStatsRequest(BaseModel):
    """A list of (begin, end) time windows to aggregate usage over."""

    windows: list[tuple[float, float]]


class ModelUsage(BaseModel):
    requests: int
    prompt_tokens: int
    completion_tokens: int


class WorkerLogEntry(BaseModel):
    """A single inference request attributed to one of the account's workers.

    A request served by a cluster appears once per worker the account owns in
    that cluster. `share` is the worker's layer share (layer_window / total
    layers across the whole cluster); `effective_*` = token count * share.
    `share`/`effective_*` are None when the cluster's layer distribution is
    unknown (or this worker's window is missing from the report). A forwarder
    (layer_window 0) has share 0.0 and effective 0.0.
    """

    request_id: str
    worker_id: str
    model: str
    cluster_id: str
    prompt_tokens: int
    completion_tokens: int
    share: float | None
    effective_prompt: float | None
    effective_completion: float | None
    created_at: float


class WorkerStatsRequest(BaseModel):
    """A list of (begin, end) windows to aggregate worker usage over, with an
    optional worker_id filter (intersected with the account's owned workers)."""

    windows: list[tuple[float, float]]
    worker_ids: list[str] | None = None


# ── Domain dataclasses (internal state) ──────────────────────────────────
@dataclass
class AccountRecord:
    account_id: str
    username: str
    password_hash: str
    created_at: float
    # Permission model (v0.7): is_admin gates account management; can_work /
    # can_use gate worker operation / inference; banned is a hard gate that
    # overrides everything (and, unlike clearing the flags, preserves the
    # account's prior permissions on unban). New accounts default to
    # non-admin, can_use + cannot work (a fresh registrant must be granted
    # can_work by an admin before contributing compute), not banned. NOTE the
    # v0.7 migration backfills EXISTING rows differently (can_work=True, the
    # historical open-pool behavior) — see store._migrate_schema.
    is_admin: bool = False
    can_work: bool = False
    can_use: bool = True
    banned: bool = False
    # Account balance in 10^-18 token units (ERC20-style minor units).
    # Exact integer arithmetic only — no floats. Defaults to 0 for fresh and
    # migrated accounts alike.
    balance: int = 0


@dataclass
class ApiKeyRecord:
    key_id: str
    account_id: str
    name: str
    scope: str
    key_hash: str
    created_at: float
    # Optional link to the worker registered with this key. Set at
    # registration so a worker-scoped key uniquely identifies its worker,
    # even when several workers belong to the same account (needed to
    # disambiguate cluster readiness/config when >1 worker of an account is
    # in the same cluster).
    worker_id: str | None = None


@dataclass
class WorkerRecord:
    worker_id: str
    account_id: str
    model: str
    gguf_sha256: str
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
    # worker_id -> number of model layers assigned by prima.cpp (Halda) at
    # cluster formation. Reported by the head once its process is ready:
    #   - world > 1: parsed from the head's stdout "Allocation Strategy" table
    #   - world == 1: the head reports it handles all layers (100% of the work)
    # `None` means the head has not reported a distribution yet (required for
    # the cluster to go live, so a live cluster always carries one).
    layer_windows: dict[str, int] | None = None


@dataclass
class RequestRecord:
    """A single inference request logged for accounting (user-side, v0).

    Captured by the proxy in `chat_completions`. Token counts come from the
    upstream llama-server `usage` object; for streaming requests they are
    parsed from the final SSE chunk before `data: [DONE]`.
    """

    request_id: str
    account_id: str
    key_id: str
    model: str
    cluster_id: str
    prompt_tokens: int
    completion_tokens: int
    created_at: float = field(default_factory=time.time)


@dataclass
class BalanceEventRecord:
    """One balance-mutation event (append-only audit trail).

    `account_id` is deliberately NOT a foreign key: balance history must
    survive account deletion (same rationale as requests.cluster_id and
    cluster_members.worker_id). `admin_account_id` records who performed the
    mutation; it is also FK-less so history survives even that account's
    deletion. `delta` is the signed change (set = balance_after - balance_before);
    `balance_before`/`balance_after` capture the full state transition.
    """

    event_id: str
    account_id: str
    admin_account_id: str | None
    kind: str  # "set" | "adjust"
    delta: int
    balance_before: int
    balance_after: int
    reason: str | None
    created_at: float = field(default_factory=time.time)
