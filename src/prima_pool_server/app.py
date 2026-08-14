"""FastAPI application: REST + WebSocket control plane endpoints."""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings
from .dashboard import build_account_overview
from .errors import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ProblemError,
    UnauthorizedError,
    problem_exception_handler,
)
from .liveness import LivenessMonitor
from .models import (
    Account,
    AccountBalance,
    AccountRecord,
    AdminAccount,
    AdminBalanceEvent,
    AdjustBalanceRequest,
    ApiKey,
    ApiKeySummary,
    BalanceEvent,
    ClusterConfig,
    ClusterInfo,
    ClusterMemberInfo,
    ClusterStatus,
    ClusterStatusResponse,
    ChangePasswordRequest,
    CreateKeyRequest,
    LoginRequest,
    ModelInfo,
    PermissionState,
    RegisterAccountRequest,
    RegisterWorkerRequest,
    ReportReadyRequest,
    RequestLogEntry,
    RequestRecord,
    Session,
    SetBalanceRequest,
    UpdateAccountPermissionsRequest,
    UsageStatsRequest,
    Worker,
    WorkerInfo,
    WorkerLogEntry,
    WorkerRecord,
    WorkerState,
    WorkerStatsRequest,
    WorkerStatus,
)
from .router import ClusterRouter
from .scheduler import Scheduler
from .security import hash_password, new_id, sign_session, verify_password, verify_session
from .store import Store
from .wg_server import ServerWireGuard
from .ws_hub import WsHub

logger = logging.getLogger(__name__)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _attribution_entry(row: dict) -> WorkerLogEntry:
    """Build a WorkerLogEntry from a store attribution row.

    share = layer_window / cluster_total (over ALL cluster members).
    None when the distribution is unknown (no window / no total).
    Forwarders (layer_window 0) get share 0.0 and effective 0.0.
    """
    lw = row["layer_window"]
    total = row["cluster_total"]
    if lw is not None and total:
        share = lw / total
        effective_prompt = row["prompt_tokens"] * share
        effective_completion = row["completion_tokens"] * share
    else:
        share = None
        effective_prompt = None
        effective_completion = None
    return WorkerLogEntry(
        request_id=row["request_id"],
        worker_id=row["worker_id"],
        model=row["model"],
        cluster_id=row["cluster_id"],
        prompt_tokens=row["prompt_tokens"],
        completion_tokens=row["completion_tokens"],
        share=share,
        effective_prompt=effective_prompt,
        effective_completion=effective_completion,
        created_at=row["created_at"],
    )


def _parse_sse_usage(raw: bytes) -> tuple[int, int] | None:
    """Extract (prompt_tokens, completion_tokens) from a buffered SSE body.

    llama-server emits a final `data: {...}` chunk carrying a `usage` object
    before `data: [DONE]`. We scan every `data:` line, parse the JSON, and
    return the last `usage` seen. Returns None if no `usage` chunk is present
    (e.g. the upstream closed before sending usage, or the body isn't SSE) —
    callers must not record a request in that case.
    """
    import json

    prompt = 0
    completion = 0
    seen = False
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line.startswith(b"data:"):
            continue
        payload = line[len(b"data:"):].strip()
        if not payload or payload == b"[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            continue
        usage = obj.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            # A chunk may carry only one of the two counts; keep the other
            # from a prior chunk (or 0 if none seen yet).
            prompt = int(usage.get("prompt_tokens", prompt))
            completion = int(usage.get("completion_tokens", completion))
            seen = True
        except (TypeError, ValueError):
            # Malformed token count — ignore this chunk rather than crash.
            continue
    if not seen:
        return None
    return prompt, completion


def _client_ip(request: Request) -> str:
    """Return the client's IP as seen by the server (handles proxies)."""
    # Respect X-Forwarded-For when behind a reverse proxy (first hop = client).
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    if request.client:
        return request.client.host
    return ""


def _is_usable_endpoint_host(host: str) -> bool:
    """True if a host is a plausible externally-reachable WG endpoint.

    Rejects empty, loopback, and RFC1918 private/container addresses — those
    are meaningless to other peers (e.g. a container's 172.17.x.x).

    Note: Tailscale uses the CGNAT range 100.64.0.0/10, which Python's
    `ipaddress.is_private` flags as private. Those are routable VPN addresses,
    so we explicitly allow them (a common deployment pattern).
    """
    import ipaddress

    host = host.strip()
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostname (e.g. a domain or Tailscale magicDNS name) — assume usable.
        return True
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return False
    # RFC1918 private LAN / Docker bridge ranges are unusable by peers.
    if ip.is_private and ip not in ipaddress.ip_network("100.64.0.0/10"):
        return False
    return True


def create_app(settings: Settings | None = None, store: Store | None = None) -> FastAPI:
    settings = settings or Settings()
    store = store or Store(settings_path_from_env())

    def _bootstrap_first_account() -> None:
        """Create the first admin account from PRIMA_POOL_FIRST_ACCOUNT.

        Format: "username:password" (username has no colon). The account is
        created as admin IFF no admin account exists yet — so a fresh deploy
        gains an operator, an existing pool with an admin is left untouched,
        and a deleted/demoted admin is never silently re-promoted. Idempotent.
        """
        raw = settings.first_account.strip()
        if not raw:
            return
        if ":" not in raw:
            logger.warning(
                "PRIMA_POOL_FIRST_ACCOUNT must be 'username:password'; ignoring invalid value"
            )
            return
        username, password = raw.split(":", 1)
        username = username.strip()
        if not username or not password:
            logger.warning("PRIMA_POOL_FIRST_ACCOUNT has an empty username or password; ignoring")
            return
        if store.count_admins() > 0:
            return
        if store.get_account_by_username(username) is not None:
            # An account with this name already exists (non-admin) — don't
            # clobber or promote it; leave bootstrap to the operator.
            logger.info(
                "first-account bootstrap skipped: username %r already exists and no admin exists",
                username,
            )
            return
        rec = store.create_account(username, password)
        if rec is not None:
            store.update_account_permissions(rec.account_id, is_admin=True)
            logger.info("created first admin account %r from PRIMA_POOL_FIRST_ACCOUNT", username)
        else:
            logger.warning("failed to create first admin account %r (username taken)", username)

    hub = WsHub(store, settings)
    wg_server = ServerWireGuard(settings)
    scheduler = Scheduler(store, settings, hub, wg_server)
    router = ClusterRouter(store, settings)
    monitor = LivenessMonitor(store, settings, scheduler)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _bootstrap_first_account()
        monitor.start()
        yield
        await monitor.stop()

    app = FastAPI(
        title="prima-pool control plane API",
        version="0.3.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.store = store
    app.state.hub = hub
    app.state.scheduler = scheduler
    app.state.router = router
    app.add_exception_handler(ProblemError, problem_exception_handler)

    # ── auth helpers ─────────────────────────────────────────────────────
    def _session_account(authorization: str | None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise UnauthorizedError()
        token = authorization.split(" ", 1)[1].strip()
        verified = verify_session(token, settings.session_secret)
        if verified is None:
            raise UnauthorizedError("Session token is invalid or expired.")
        # The session token embeds the account_id as the token body.
        account_id = verified[0]
        rec = store.get_account(account_id)
        if rec is None:
            raise UnauthorizedError()
        if rec.banned:
            raise ForbiddenError("This account is banned.")
        return account_id

    def _api_key(authorization: str | None) -> tuple[str, str, str]:
        """Return (account_id, scope, key_id) for a scoped API key."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise UnauthorizedError()
        secret = authorization.split(" ", 1)[1].strip()
        rec = store.resolve_api_key(secret)
        if rec is None:
            raise UnauthorizedError("The provided API key is not valid.")
        acct = store.get_account(rec.account_id)
        if acct is not None and acct.banned:
            raise ForbiddenError("This account is banned.")
        return rec.account_id, rec.scope, rec.key_id

    def _can_work(account_id: str) -> bool:
        """Effective can_work = (not banned) and (can_work or permissionless)."""
        rec = store.get_account(account_id)
        if rec is None or rec.banned:
            return False
        return rec.can_work or settings.work_permissionless

    def _can_use(account_id: str) -> bool:
        """Effective can_use = (not banned) and (can_use or permissionless)."""
        rec = store.get_account(account_id)
        if rec is None or rec.banned:
            return False
        return rec.can_use or settings.use_permissionless

    def _require_admin(authorization: str | None) -> AccountRecord:
        """Resolve the caller as an admin (session token only)."""
        account_id = _session_account(authorization)
        rec = store.get_account(account_id)
        if rec is None or not rec.is_admin:
            raise ForbiddenError("Admin privileges required.")
        return rec

    def _assert_not_banned(account_id: str) -> None:
        """Reject a banned account (hard gate, overrides everything)."""
        rec = store.get_account(account_id)
        if rec is not None and rec.banned:
            raise ForbiddenError("This account is banned.")

    def _user_credential(authorization: str | None) -> str:
        """Resolve the account_id for a user-scoped credential.

        Accepts EITHER a user-scoped API key (sk-user-...) OR the account
        session token. Worker-scoped keys are rejected. Used by the
        account-scoped usage/log endpoints.
        """
        if authorization and authorization.lower().startswith("bearer "):
            secret = authorization.split(" ", 1)[1].strip()
            rec = store.resolve_api_key(secret)
            if rec is not None:
                if rec.scope != "user":
                    raise ForbiddenError("A worker key cannot view usage.")
                _assert_not_banned(rec.account_id)
                return rec.account_id
        return _session_account(authorization)

    def _worker_credential(authorization: str | None) -> tuple[str, str, str | None, str | None]:
        """Resolve (account_id, scope, key_id, bound_worker_id) from a
        worker-scoped credential.

        Accepts EITHER:
        - a worker-scoped API key (device path), or
        - the account session token (owner path).

        User-scoped API keys are rejected. Raises UnauthorizedError if no valid
        credential, or ForbiddenError for a user-scoped key.

        key_id is the id of the API key when authenticating with one (None for
        session tokens). bound_worker_id is the worker that key registered
        (None if the key has not yet been bound or auth is via session).
        """
        account_id: str | None = None
        scope: str | None = None
        key_id: str | None = None
        bound_worker_id: str | None = None
        if authorization and authorization.lower().startswith("bearer "):
            secret = authorization.split(" ", 1)[1].strip()
            rec = store.resolve_api_key(secret)
            if rec is not None:
                account_id, scope = rec.account_id, rec.scope
                key_id = rec.key_id
                bound_worker_id = rec.worker_id
        if account_id is None:
            try:
                account_id = _session_account(authorization)
                scope = "session"
            except UnauthorizedError:
                pass
        if account_id is None:
            raise UnauthorizedError("The provided credentials are not valid.")
        if scope != "worker" and scope != "session":
            raise ForbiddenError("A user key cannot manage workers.")
        _assert_not_banned(account_id)
        return account_id, scope, key_id, bound_worker_id

    def _worker_from_key(authorization: str | None, worker_id: str) -> WorkerRecord:
        """Resolve a worker, authenticating with EITHER:
        - a worker-scoped API key (device path), or
        - the account session token that owns the worker (owner path).

        User-scoped API keys are rejected in both paths.

        Side effect: if the caller used a worker API key and the key is not yet
        bound (or bound to a different worker), it is (re)bound to `worker_id`.
        This self-heals deployments that upgraded after keys were first issued —
        a stale/unbound key becomes unambiguous for cluster config/ready on the
        worker's next heartbeat.
        """
        account_id, _scope, key_id, bound_worker_id = _worker_credential(authorization)
        w = store.get_worker(worker_id)
        if w is None:
            raise NotFoundError("Worker does not exist.")
        if w.account_id != account_id:
            raise ForbiddenError("This credential does not own this worker.")
        # Heal the key→worker binding if it's stale/missing (worker-key path).
        if key_id and bound_worker_id != worker_id:
            try:
                store.bind_api_key_to_worker(key_id, worker_id)
                logger.info("bound key %s to worker %s", key_id, worker_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to bind key %s to worker %s: %s", key_id, worker_id, exc)
        return w

    def _bound_worker(authorization: str | None, cluster_id: str) -> WorkerRecord:
        """Resolve the specific worker for a cluster-scoped call.

        Prefers the worker bound to the presenting API key (the key that
        registered that worker). This is unambiguous when several workers of
        one account are in the same cluster — e.g. one account running the
        same model on multiple machines.

        Falls back to "the account's only worker in this cluster" for session
        tokens / unbound keys. Raises ForbiddenError if nothing matches.
        """
        account_id, _scope, key_id, bound_worker_id = _worker_credential(authorization)
        if bound_worker_id:
            w = store.get_worker(bound_worker_id)
            if w is not None and w.cluster_id == cluster_id:
                return w
        # Fallback: exactly one of this account's workers is in this cluster.
        matches = [
            w for w in store.list_workers()
            if w.account_id == account_id and w.cluster_id == cluster_id
        ]
        if len(matches) == 1:
            # Opportunistically bind the key so this is resolved via the key
            # from now on (covers the very first config fetch, before any
            # heartbeat had a chance to bind).
            if key_id:
                try:
                    store.bind_api_key_to_worker(key_id, matches[0].worker_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("failed to bind key %s to worker %s: %s", key_id, matches[0].worker_id, exc)
            return matches[0]
        raise ForbiddenError("This credential does not own a member of this cluster.")

    # ── accounts ─────────────────────────────────────────────────────────
    @app.post("/v1/accounts/register", response_model=Account, status_code=201)
    async def register_account(body: RegisterAccountRequest):
        rec = store.create_account(body.username, body.password)
        if rec is None:
            raise ConflictError("username_taken", "Username Taken", "That username is already registered.")
        return Account(
            account_id=rec.account_id,
            username=rec.username,
            created_at=_iso(rec.created_at),
            balance=rec.balance,
        )

    @app.post("/v1/accounts/login", response_model=Session)
    async def login(body: LoginRequest):
        rec = store.get_account_by_username(body.username)
        if rec is None or not verify_password(body.password, rec.password_hash):
            raise UnauthorizedError("Invalid credentials.")
        if rec.banned:
            raise ForbiddenError("This account is banned.")
        expires_at = int(time.time()) + settings.session_ttl_s
        token = sign_session(rec.account_id, settings.session_secret, expires_at)
        return Session(session_token=token, expires_at=_iso(expires_at))

    @app.post("/v1/accounts/{account_id}/password", status_code=204)
    async def change_password(
        account_id: str,
        body: ChangePasswordRequest,
        authorization: str | None = Header(None),
    ):
        session_account = _session_account(authorization)
        if session_account != account_id:
            raise ForbiddenError("Cannot change another account's password.")
        rec = store.get_account(account_id)
        if rec is None:
            raise NotFoundError("Account does not exist.")
        if not verify_password(body.current_password, rec.password_hash):
            raise UnauthorizedError("Current password is incorrect.")
        store.set_password(account_id, hash_password(body.new_password))
        return JSONResponse(status_code=204, content=None)

    @app.post("/v1/accounts/{account_id}/keys", response_model=ApiKey, status_code=201)
    async def create_key(account_id: str, body: CreateKeyRequest, authorization: str | None = Header(None)):
        session_account = _session_account(authorization)
        if session_account != account_id:
            raise ForbiddenError("Cannot manage another account's keys.")
        rec, secret = store.create_api_key(account_id, body.name, body.scope)
        return ApiKey(
            key_id=rec.key_id,
            name=rec.name,
            scope=rec.scope,
            api_key=secret,
            created_at=_iso(rec.created_at),
        )

    @app.get("/v1/accounts/{account_id}/keys", response_model=list[ApiKeySummary])
    async def list_keys(account_id: str, authorization: str | None = Header(None)):
        session_account = _session_account(authorization)
        if session_account != account_id:
            raise ForbiddenError("Cannot manage another account's keys.")
        return [
            ApiKeySummary(key_id=k.key_id, name=k.name, scope=k.scope, created_at=_iso(k.created_at))
            for k in store.list_api_keys(account_id)
        ]

    @app.delete("/v1/accounts/{account_id}/keys/{key_id}", status_code=204)
    async def revoke_key(account_id: str, key_id: str, authorization: str | None = Header(None)):
        session_account = _session_account(authorization)
        if session_account != account_id:
            raise ForbiddenError("Cannot manage another account's keys.")
        rec = store.get_api_key(key_id)
        if rec is None or rec.account_id != account_id:
            raise NotFoundError("Key does not exist.")
        store.revoke_api_key(key_id)
        return JSONResponse(status_code=204, content=None)

    # ── balance (account-scoped) ────────────────────────────────────────
    @app.get("/v1/accounts/{account_id}/balance", response_model=AccountBalance)
    async def account_balance(account_id: str, authorization: str | None = Header(None)):
        """Return the account's own balance.

        Auth: user-scoped API key OR the account session token.
        """
        if _user_credential(authorization) != account_id:
            raise ForbiddenError("Cannot view another account's balance.")
        balance = store.get_balance(account_id)
        if balance is None:
            raise NotFoundError("Account does not exist.")
        return AccountBalance(account_id=account_id, balance=balance)

    @app.get(
        "/v1/accounts/{account_id}/balance/events",
        response_model=list[BalanceEvent],
    )
    async def account_balance_events(
        account_id: str,
        limit: int = 100,
        authorization: str | None = Header(None),
    ):
        """Return the account's own balance events, newest first.

        Auth: user-scoped API key OR the account session token. The acting
        admin's identity is NOT exposed here (admin-only detail).
        """
        if _user_credential(authorization) != account_id:
            raise ForbiddenError("Cannot view another account's balance.")
        if store.get_account(account_id) is None:
            raise NotFoundError("Account does not exist.")
        if limit < 1:
            raise BadRequestError("'limit' must be at least 1.")
        return [
            BalanceEvent(
                event_id=rec.event_id,
                kind=rec.kind,
                delta=rec.delta,
                balance_after=rec.balance_after,
                reason=rec.reason,
                created_at=_iso(rec.created_at),
            )
            for rec in store.list_balance_events(account_id, limit=limit)
        ]

    # ── admin (account management, admin-gated) ──────────────────────────
    def _admin_account_view(a: AccountRecord) -> AdminAccount:
        """Build the admin view of an account, computing the EFFECTIVE
        capabilities with the same formula as the enforcement layer."""
        return AdminAccount(
            account_id=a.account_id,
            username=a.username,
            is_admin=a.is_admin,
            can_work=a.can_work,
            can_use=a.can_use,
            banned=a.banned,
            effective_can_work=_can_work(a.account_id),
            effective_can_use=_can_use(a.account_id),
            created_at=_iso(a.created_at),
            balance=a.balance,
        )

    @app.get("/v1/admin/permissions", response_model=PermissionState)
    async def admin_permission_state(authorization: str | None = Header(None)):
        """Current pool-wide permissionless switches (admin only)."""
        _require_admin(authorization)
        return PermissionState(
            work_permissionless=settings.work_permissionless,
            use_permissionless=settings.use_permissionless,
        )

    @app.get("/v1/admin/accounts", response_model=list[AdminAccount])
    async def admin_list_accounts(authorization: str | None = Header(None)):
        """List all accounts with their permission booleans (admin only)."""
        _require_admin(authorization)
        return [_admin_account_view(a) for a in store.list_accounts()]

    @app.patch("/v1/admin/accounts/{account_id}", response_model=AdminAccount)
    async def admin_update_account(
        account_id: str,
        body: UpdateAccountPermissionsRequest,
        authorization: str | None = Header(None),
    ):
        """Toggle one account's permissions (admin only).

        Each present field is set; absent fields are left untouched. Demoting
        the last remaining admin is rejected (the pool must always have one).
        """
        _require_admin(authorization)
        target = store.get_account(account_id)
        if target is None:
            raise NotFoundError("Account does not exist.")
        if body.is_admin is None and body.can_work is None and body.can_use is None and body.banned is None:
            raise BadRequestError("No permission field to update.")

        # "Cannot demote the last admin": if this removes admin from the last
        # admin, reject. (Self-demotion is allowed as long as another admin
        # remains — see design notes.)
        if body.is_admin is False and target.is_admin:
            if store.count_admins() <= 1:
                raise ConflictError(
                    "last_admin",
                    "Last Admin",
                    "Cannot demote the last remaining admin.",
                )

        store.update_account_permissions(
            account_id,
            is_admin=body.is_admin,
            can_work=body.can_work,
            can_use=body.can_use,
            banned=body.banned,
        )
        updated = store.get_account(account_id)
        return _admin_account_view(updated)

    # ── admin (balance management, admin-gated) ─────────────────────────
    def _balance_event_view(rec, admin_username: str | None = None) -> AdminBalanceEvent:
        """Build an admin balance-event response (adds the actor's identity).

        `admin_username` is None when the event has no recorded admin.
        """
        return AdminBalanceEvent(
            event_id=rec.event_id,
            kind=rec.kind,
            delta=rec.delta,
            balance_after=rec.balance_after,
            reason=rec.reason,
            created_at=_iso(rec.created_at),
            admin_username=admin_username,
        )

    @app.put("/v1/admin/accounts/{account_id}/balance", response_model=AccountBalance)
    async def admin_set_balance(
        account_id: str,
        body: SetBalanceRequest,
        authorization: str | None = Header(None),
    ):
        """Set an account's balance to an exact value (admin only)."""
        admin = _require_admin(authorization)
        if store.get_account(account_id) is None:
            raise NotFoundError("Account does not exist.")
        try:
            store.set_balance(
                account_id,
                body.balance,
                admin_account_id=admin.account_id,
                reason=body.reason,
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        return AccountBalance(account_id=account_id, balance=body.balance)

    @app.post(
        "/v1/admin/accounts/{account_id}/balance/adjust",
        response_model=AccountBalance,
    )
    async def admin_adjust_balance(
        account_id: str,
        body: AdjustBalanceRequest,
        authorization: str | None = Header(None),
    ):
        """Adjust an account's balance by a signed delta (admin only).

        No sign restriction: negative deltas (deductions) are allowed, and the
        balance may go negative.
        """
        admin = _require_admin(authorization)
        if store.get_account(account_id) is None:
            raise NotFoundError("Account does not exist.")
        try:
            new_balance = store.adjust_balance(
                account_id,
                body.delta,
                admin_account_id=admin.account_id,
                reason=body.reason,
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
        return AccountBalance(account_id=account_id, balance=new_balance)

    @app.get(
        "/v1/admin/accounts/{account_id}/balance/events",
        response_model=list[AdminBalanceEvent],
    )
    async def admin_balance_events(
        account_id: str,
        limit: int = 100,
        authorization: str | None = Header(None),
    ):
        """List an account's balance events, newest first (admin only)."""
        _require_admin(authorization)
        if store.get_account(account_id) is None:
            raise NotFoundError("Account does not exist.")
        if limit < 1:
            raise BadRequestError("'limit' must be at least 1.")
        # Resolve the acting admin's username for each event (may be gone).
        admin_names: dict[str, str] = {}
        events = store.list_balance_events(account_id, limit=limit)
        out: list[AdminBalanceEvent] = []
        for rec in events:
            if rec.admin_account_id:
                if rec.admin_account_id not in admin_names:
                    a = store.get_account(rec.admin_account_id)
                    admin_names[rec.admin_account_id] = a.username if a else rec.admin_account_id
                out.append(_balance_event_view(rec, admin_names[rec.admin_account_id]))
            else:
                out.append(_balance_event_view(rec, None))
        return out

    # ── workers ──────────────────────────────────────────────────────────
    @app.post("/v1/workers/register", response_model=Worker, status_code=201)
    async def register_worker(
        request: Request,
        body: RegisterWorkerRequest,
        authorization: str | None = Header(None),
    ):
        account_id, scope, _ = _api_key(authorization)
        if scope != "worker":
            raise ForbiddenError("A user key cannot register workers.")
        if not _can_work(account_id):
            raise ForbiddenError("This account is not permitted to provide workers.")

        # Which API key is registering? We bind it to the created worker so a
        # worker-scoped key later uniquely identifies its worker — required
        # once one account may run several workers (same or different models).
        rec_api_key = store.resolve_api_key(authorization.split(" ", 1)[1].strip())
        bound_key_id = rec_api_key.key_id if rec_api_key else None

        model_def = settings.models.get(body.model)
        if model_def is None:
            raise BadRequestError(f"Unknown model '{body.model}'.")

        # The advertised GGUF hash must match the registered one — a cluster
        # only ever groups workers with identical hashes, so reject mismatches
        # at registration. If the registry hash is unset (dev default
        # "<no-hash>"), integrity is NOT enforced here — but the scheduler
        # still groups workers per advertised hash, so mismatches never mix
        # within a cluster.
        if not model_def.gguf_sha256:
            logger.warning(
                "model '%s' has no registered GGUF hash; hash integrity is not "
                "enforced. Set PRIMA_POOL_MODELS with a real sha256 to enable it.",
                body.model,
            )
        elif body.gguf_sha256 != model_def.gguf_sha256:
            raise BadRequestError(
                f"GGUF hash for model '{body.model}' does not match the pool's "
                f"registered hash (advertised {body.gguf_sha256[:12]}…, expected "
                f"{model_def.gguf_sha256[:12]}…)."
            )

        # WG endpoint: prefer the client's self-reported (reachable) host, else
        # fall back to the IP the server observes on the registration connection.
        # This closes the "container IP advertised as endpoint" gap: a provider
        # behind a container (or any private IP) gets its public source IP used
        # as the WG endpoint host automatically.
        endpoint = body.endpoint
        if not _is_usable_endpoint_host(endpoint.host):
            observed = _client_ip(request)
            if observed:
                logger.info(
                    "worker '%s' advertised unusable endpoint host %r; using "
                    "observed source IP %s",
                    body.model,
                    endpoint.host,
                    observed,
                )
                endpoint.host = observed

        rec = WorkerRecord(
            worker_id=new_id("wrk"),
            account_id=account_id,
            model=body.model,
            gguf_sha256=body.gguf_sha256,
            memory_allocated_mb=body.memory_allocated_mb,
            wg_pubkey=body.wg_pubkey,
            endpoint=endpoint,
            hardware=body.hardware,
            status=WorkerStatus.waitlisted,
            online=False,
        )
        # Atomic check-and-create: enforces the per-account cap and the
        # one-device-one-worker rule (one worker per WG pubkey) without a race
        # between concurrent calls. Multiple workers may serve the same model.
        #
        # Re-registration with the SAME WG pubkey is NOT a conflict: it's the
        # same device re-advertising its endpoint (e.g. the operator set
        # PRIMA_POOL_WG_ENDPOINT_HOST, or the device moved networks). Update the
        # existing worker's endpoint instead of rejecting.
        if not store.create_worker_if_available(rec, settings.max_workers_per_account):
            existing = store.list_workers_for_account(account_id)
            if len(existing) >= settings.max_workers_per_account:
                raise ConflictError("worker_limit", "Worker Limit", "Too many workers for this account.")
            prev = next((w for w in existing if w.wg_pubkey == body.wg_pubkey), None)
            if prev is not None:
                # Same device re-registering → update its endpoint (and any
                # other changed fields) in place.
                prev.endpoint = endpoint
                prev.hardware = body.hardware
                prev.model = body.model
                prev.gguf_sha256 = body.gguf_sha256
                prev.memory_allocated_mb = body.memory_allocated_mb
                prev.status = WorkerStatus.waitlisted
                prev.online = False
                store.update_worker(prev)
                if bound_key_id:
                    store.bind_api_key_to_worker(bound_key_id, prev.worker_id)
                logger.info("worker %s re-registered (endpoint updated to %s)", prev.worker_id, endpoint.host)
                rec = prev
            else:
                raise ConflictError("worker_exists", "Worker Exists", "A worker for this device already exists.")
        else:
            # Bind the presenting key to this worker (enables unambiguous per-key
            # worker identity for cluster readiness/config later).
            if bound_key_id:
                try:
                    store.bind_api_key_to_worker(bound_key_id, rec.worker_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("failed to bind key %s to worker %s: %s", bound_key_id, rec.worker_id, exc)
        scheduler.check_and_form()
        # Re-read to reflect any immediate assignment.
        rec = store.get_worker(rec.worker_id)
        return rec.to_worker()

    @app.delete("/v1/workers/{worker_id}", status_code=204)
    async def revoke_worker(worker_id: str, authorization: str | None = Header(None)):
        w = _worker_from_key(authorization, worker_id)
        scheduler.on_worker_leave(w)
        store.delete_worker(worker_id)
        return JSONResponse(status_code=204, content=None)

    @app.get("/v1/workers/{worker_id}/state", response_model=WorkerState)
    async def get_worker_state(worker_id: str, authorization: str | None = Header(None)):
        w = _worker_from_key(authorization, worker_id)
        config_url = None
        if w.cluster_id:
            config_url = f"{settings.public_base_url}/v1/clusters/{w.cluster_id}/config"
        return w.to_state(config_url)

    @app.post("/v1/workers/{worker_id}/heartbeat", response_model=Worker)
    async def heartbeat(worker_id: str, authorization: str | None = Header(None)):
        w = _worker_from_key(authorization, worker_id)
        was_offline = not w.online
        w.online = True
        w.last_heartbeat = time.time()
        if was_offline:
            # Re-add to waitlist after the assignable grace period.
            w.assignable_at = time.time() + settings.assignable_grace_s
            if w.status == WorkerStatus.registered:
                w.status = WorkerStatus.waitlisted
        # Persist BEFORE the scheduler reads (SQLite reads from disk; the
        # scheduler must see online=True / assignable_at already written).
        store.update_worker(w)
        # Always re-check formation: a worker may have just become eligible
        # (online long enough that assignable_at <= now). The old code only
        # re-checked on offline→online transitions, so a pair of workers whose
        # grace periods aligned after the last check stayed waitlisted forever.
        scheduler.check_and_form()
        return w.to_worker()

    # ── clusters ─────────────────────────────────────────────────────────
    @app.get("/v1/clusters/{cluster_id}/config", response_model=ClusterConfig)
    async def get_cluster_config(cluster_id: str, authorization: str | None = Header(None)):
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            raise NotFoundError("Cluster does not exist.")
        # The presenting credential must own a member of this cluster. With
        # multiple same-account workers in one cluster, the key's bound worker
        # disambiguates which one this is.
        worker = _bound_worker(authorization, cluster_id)
        config = scheduler._build_cluster_config(cluster)
        config["interface"]["private_ip"] = worker.assigned_ip
        return ClusterConfig(**config)

    @app.post("/v1/clusters/{cluster_id}/ready", response_model=ClusterStatusResponse, status_code=202)
    async def report_ready(cluster_id: str, body: ReportReadyRequest, authorization: str | None = Header(None)):
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            raise NotFoundError("Cluster does not exist.")
        # Identify the reporting member precisely: with one account running
        # several workers in the same cluster, the old account-wide lookup
        # attributed every report to the SAME worker, so the cluster never
        # went live. The key's bound worker fixes this.
        worker = _bound_worker(authorization, cluster_id)
        # If the head carries its rank-keyed layer distribution in the body
        # (WS fallback / primary), record it BEFORE marking readiness so the
        # liveness gate can be satisfied atomically. Note: an EMPTY dict is a
        # valid "unknown" report (parse failure) and must still be recorded —
        # so we check `is not None`, not truthiness.
        if body.layer_windows is not None and worker.ring_position == 0:
            lw = {k: v for k, v in body.layer_windows.items() if isinstance(v, int) and v >= 0}
            try:
                # Map rank -> worker_id (members are in ring order).
                by_worker: dict[str, int] = {}
                for rank, count in lw.items():
                    try:
                        idx = int(rank)
                    except (TypeError, ValueError):
                        continue
                    if 0 <= idx < len(cluster.members):
                        by_worker[cluster.members[idx]] = count
                # NOTE: pass by_worker through even when empty — an empty dict
                # is the 'reported unknown' marker ({}), distinct from None
                # ('not reported'). Collapsing {} to None would block liveness
                # on a parse failure, which the design forbids.
                scheduler.on_layer_distribution(cluster_id, by_worker)
            except KeyError:
                pass
        status = scheduler.on_ready(cluster_id, worker.worker_id)
        # on_ready mutates its own in-memory ClusterRecord; re-read so the
        # response reflects the updated ready set (the first read above is a
        # different object).
        cluster = store.get_cluster(cluster_id) or cluster
        return ClusterStatusResponse(
            cluster_id=cluster_id,
            status=status,
            members_ready=len(cluster.ready),
            members_total=len(cluster.members),
        )

    # ── model discovery (unauthenticated overview) ────────────────────────
    @app.get("/v1/models", response_model=list[ModelInfo])
    async def list_models():
        """List the models the pool serves. Unauthenticated (public overview).

        Each entry includes the model slug, the exact GGUF SHA-256, the
        required memory, and whether a live cluster currently serves it.
        """
        live_models = {c.model for c in store.list_clusters() if c.status == ClusterStatus.live}
        return [
            ModelInfo(
                slug=md.slug,
                gguf_sha256=md.gguf_sha256,
                required_memory_mb=md.required_memory_mb,
                live=md.slug in live_models,
            )
            for md in settings.models.values()
        ]

    # ── public info (unauthenticated, deliberately minimal) ───────────────
    # Both endpoints expose ONLY anonymized, class-level data (worker ids are
    # opaque random ids; no account, endpoint, or availability info). This is
    # the "what kind of machines ran my prompt" amazement feature. Anything
    # instance-level (exact CPU model, OS version, live free RAM, WG IPs) must
    # NOT be added here — see WorkerInfo / ClusterInfo docstrings.

    @app.get("/v1/workers/{worker_id}/info", response_model=WorkerInfo)
    async def worker_info(worker_id: str):
        """Public worker info (unauthenticated).

        Returns the worker's id, model, and advertised RAM pool share.
        Deliberately minimal — see WorkerInfo.
        """
        w = store.get_worker(worker_id)
        if w is None:
            raise NotFoundError("Worker does not exist.")
        return WorkerInfo(
            worker_id=w.worker_id,
            model=w.model,
            memory_allocated_mb=w.memory_allocated_mb,
        )

    @app.get("/v1/clusters/{cluster_id}/info", response_model=ClusterInfo)
    async def cluster_info(cluster_id: str):
        """Public cluster info (unauthenticated).

        Returns the member list (in ring order, index 0 = head) with each
        worker's layer window — the "what kind of machines ran my prompt"
        view. Deliberately minimal — see ClusterInfo.
        """
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            raise NotFoundError("Cluster does not exist.")
        layer_windows = cluster.layer_windows or {}
        return ClusterInfo(
            cluster_id=cluster.cluster_id,
            model=cluster.model,
            status=cluster.status,
            members=[
                ClusterMemberInfo(
                    worker_id=wid,
                    layer_window=layer_windows.get(wid),
                )
                for wid in cluster.members
            ],
        )

    # ── inference proxy (option A: server joins WG, proxies to head) ─────
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, authorization: str | None = Header(None)):
        """Proxy an OpenAI-compatible chat completion to a live cluster's head.

        Auth: user-scoped API key (sk-user-...). The server finds a live cluster
        for the requested model and forwards the request to the head's
        llama-server over the WireGuard tunnel.
        """
        account_id, scope, key_id = _api_key(authorization)
        if scope != "user":
            raise ForbiddenError("Only a user key can send inference requests.")
        if not _can_use(account_id):
            raise ForbiddenError("This account is not permitted to use inference.")

        body = await request.json()
        model = body.get("model")
        if not model:
            raise BadRequestError("Missing 'model' in request body.")

        cluster = router.find_live_cluster(model)
        if cluster is None:
            raise NotFoundError(f"No live cluster available for model '{model}'.")
        head_url = router.head_url(cluster)
        if head_url is None:
            raise NotFoundError("Cluster has no head to route to.")

        import httpx

        target = f"{head_url}/v1/chat/completions"
        stream = bool(body.get("stream", False))
        request_id = new_id("req")

        def _log_usage(prompt_tokens: int, completion_tokens: int) -> None:
            """Persist a request record for accounting (best-effort)."""
            try:
                store.record_request(
                    RequestRecord(
                        request_id=request_id,
                        account_id=account_id,
                        key_id=key_id,
                        model=model,
                        cluster_id=cluster.cluster_id,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                    )
                )
            except Exception:  # noqa: BLE001 - accounting must never break inference
                logger.exception("failed to record usage for request %s", request_id)

        try:
            if stream:
                # Keep the httpx client alive for the WHOLE response body, and
                # tolerate a premature upstream close: llama-server may drop the
                # TCP connection after the last SSE chunk instead of sending a
                # clean `data: [DONE]` terminator. Treating a mid-stream
                # ReadError as end-of-stream is correct for SSE — the tokens
                # already delivered are valid.
                #
                # The status code / content-type are taken from the single
                # streaming request itself (no separate probe — probing would
                # run the generation twice). Success is 200, which is the
                # StreamingResponse default.
                async def proxy_stream():
                    # trust_env=False: the upstream is on the WG tunnel; a
                    # host-level HTTP(S)_PROXY must NOT intercept it.
                    async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
                        async with client.stream(
                            "POST", target, json=body, timeout=300
                        ) as resp:
                            # Buffer the SSE stream so we can parse the final
                            # `usage` chunk for token accounting while still
                            # forwarding every byte to the client.
                            buffer = bytearray()
                            try:
                                async for chunk in resp.aiter_bytes():
                                    buffer.extend(chunk)
                                    yield chunk
                            except (httpx.ReadError, httpx.ReadTimeout, httpx.RemoteProtocolError):
                                # Upstream dropped the connection after sending
                                # its final bytes. End the stream cleanly; the
                                # client got everything llama-server produced.
                                logger.warning("upstream stream ended prematurely: %s", target)
                            finally:
                                # Only record usage if the upstream actually
                                # sent a `usage` chunk. If it closed before
                                # that (abrupt close), there's no token count
                                # to log — recording (0,0) would be a false
                                # "zero-token request" entry.
                                parsed = _parse_sse_usage(bytes(buffer))
                                if parsed is not None:
                                    prompt_tokens, completion_tokens = parsed
                                    _log_usage(prompt_tokens, completion_tokens)

                return StreamingResponse(proxy_stream(), media_type="text/event-stream")
            async with httpx.AsyncClient(timeout=300, trust_env=False) as client:
                resp = await client.post(target, json=body)
                data = resp.json()
                usage = data.get("usage") or {}
                _log_usage(
                    int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)),
                )
                return JSONResponse(status_code=resp.status_code, content=data)
        except httpx.HTTPError as exc:
            logger.error("proxy to %s failed: %s", target, exc)
            raise ProblemError(
                502,
                "https://prima-pool.dev/errors/upstream_error",
                "Upstream Error",
                f"Failed to reach cluster head: {exc}",
            )

    # ── GUI (static) ──────────────────────────────────────────────────────
    # Serve the static dashboard, plus an account-scoped data endpoint.
    # The GUI page is static; all data comes from the API (session token).
    _ui_dir = Path(__file__).parent / "ui" / "static"
    app.mount("/ui/static", StaticFiles(directory=_ui_dir), name="ui-static")

    @app.get("/ui")
    async def ui_dashboard():
        return FileResponse(_ui_dir / "dashboard.html")

    @app.get("/v1/accounts/{account_id}/dashboard")
    async def account_dashboard(account_id: str, authorization: str | None = Header(None)):
        session_account = _session_account(authorization)
        if session_account != account_id:
            raise ForbiddenError("Cannot view another account's dashboard.")
        return build_account_overview(store, account_id)

    # ── usage / logs (account-scoped) ────────────────────────────────────
    @app.get("/v1/accounts/{account_id}/usage/logs", response_model=list[RequestLogEntry])
    async def account_usage_logs(
        account_id: str,
        begin: float,
        end: float,
        limit: int = 1000,
        authorization: str | None = Header(None),
    ):
        """Return the account's inference logs in [begin, end), newest first.

        Auth: user-scoped API key OR the account session token. begin/end are
        Unix timestamps (seconds). `limit` caps the number of entries returned
        (default 1000) so a large window doesn't silently truncate.
        """
        if _user_credential(authorization) != account_id:
            raise ForbiddenError("Cannot view another account's usage.")
        if begin >= end:
            raise BadRequestError("'begin' must be before 'end'.")
        if limit < 1:
            raise BadRequestError("'limit' must be at least 1.")
        return [
            RequestLogEntry(
                request_id=r.request_id,
                model=r.model,
                cluster_id=r.cluster_id,
                prompt_tokens=r.prompt_tokens,
                completion_tokens=r.completion_tokens,
                created_at=r.created_at,
            )
            for r in store.list_requests_in_range(account_id, begin, end, limit=limit)
        ]

    @app.get("/v1/accounts/{account_id}/usage/logs/latest", response_model=list[RequestLogEntry])
    async def account_usage_logs_latest(
        account_id: str,
        limit: int = 50,
        authorization: str | None = Header(None),
    ):
        """Return the account's most recent `limit` inference logs, newest first.

        Auth: user-scoped API key OR the account session token. `limit` is the
        maximum number of entries to return (default 50).
        """
        if _user_credential(authorization) != account_id:
            raise ForbiddenError("Cannot view another account's usage.")
        if limit < 1:
            raise BadRequestError("'limit' must be at least 1.")
        return [
            RequestLogEntry(
                request_id=r.request_id,
                model=r.model,
                cluster_id=r.cluster_id,
                prompt_tokens=r.prompt_tokens,
                completion_tokens=r.completion_tokens,
                created_at=r.created_at,
            )
            for r in store.list_requests_for_account(account_id, limit=limit)
        ]

    @app.post("/v1/accounts/{account_id}/usage/stats")
    async def account_usage_stats(
        account_id: str,
        body: UsageStatsRequest,
        authorization: str | None = Header(None),
    ):
        """Aggregate the account's usage over a list of (begin, end) windows.

        Auth: user-scoped API key OR the account session token. Returns one
        entry per window: {model: {requests, prompt_tokens, completion_tokens}}.
        """
        if _user_credential(authorization) != account_id:
            raise ForbiddenError("Cannot view another account's usage.")
        result = []
        for begin, end in body.windows:
            if begin >= end:
                raise BadRequestError("Each window's 'begin' must be before its 'end'.")
            stats = store.usage_stats_in_range(account_id, begin, end)
            result.append(
                {
                    model: {
                        "requests": reqs,
                        "prompt_tokens": prompt,
                        "completion_tokens": completion,
                    }
                    for model, (reqs, prompt, completion) in stats.items()
                }
            )
        return result

    # ── worker usage / logs (account-scoped, worker-attributed) ──────────
    @app.get("/v1/accounts/{account_id}/worker-logs", response_model=list[WorkerLogEntry])
    async def account_worker_logs(
        account_id: str,
        begin: float,
        end: float,
        limit: int = 1000,
        authorization: str | None = Header(None),
    ):
        """Return the account's worker-attributed inference logs in [begin, end),
        newest first.

        Auth: user-scoped API key OR the account session token. Each request
        served by a cluster appears once per worker the account owns in that
        cluster, with that worker's layer share and effective tokens.
        """
        if _user_credential(authorization) != account_id:
            raise ForbiddenError("Cannot view another account's usage.")
        if begin >= end:
            raise BadRequestError("'begin' must be before 'end'.")
        if limit < 1:
            raise BadRequestError("'limit' must be at least 1.")
        rows = store.worker_attribution(account_id, begin, end, limit=limit)
        return [_attribution_entry(r) for r in rows]

    @app.get("/v1/accounts/{account_id}/worker-logs/latest", response_model=list[WorkerLogEntry])
    async def account_worker_logs_latest(
        account_id: str,
        limit: int = 50,
        authorization: str | None = Header(None),
    ):
        """Return the account's most recent `limit` worker-attributed logs,
        newest first.

        Auth: user-scoped API key OR the account session token.
        """
        if _user_credential(authorization) != account_id:
            raise ForbiddenError("Cannot view another account's usage.")
        if limit < 1:
            raise BadRequestError("'limit' must be at least 1.")
        rows = store.worker_logs_latest(account_id, limit=limit)
        return [_attribution_entry(r) for r in rows]

    @app.post("/v1/accounts/{account_id}/worker-stats")
    async def account_worker_stats(
        account_id: str,
        body: WorkerStatsRequest,
        authorization: str | None = Header(None),
    ):
        """Aggregate the account's worker-attributed usage over a list of
        (begin, end) windows.

        Auth: user-scoped API key OR the account session token. Returns one
        entry per window: {model: {total_tokens: [prompt, completion],
        effective_tokens: [prompt, completion]}}. `total_tokens` sums the
        request token counts over the account's worker rows; `effective_tokens`
        sums the share-scaled counts. If `worker_ids` is given, only rows for
        (worker_ids ∩ owned workers) are included.
        """
        if _user_credential(authorization) != account_id:
            raise ForbiddenError("Cannot view another account's usage.")
        result = []
        for begin, end in body.windows:
            if begin >= end:
                raise BadRequestError("Each window's 'begin' must be before its 'end'.")
            rows = store.worker_attribution(account_id, begin, end, worker_ids=body.worker_ids)
            per_model: dict[str, dict] = {}
            for r in rows:
                entry = per_model.setdefault(
                    r["model"],
                    {"total_tokens": [0.0, 0.0], "effective_tokens": [0.0, 0.0]},
                )
                entry["total_tokens"][0] += r["prompt_tokens"]
                entry["total_tokens"][1] += r["completion_tokens"]
                lw = r["layer_window"]
                total = r["cluster_total"]
                if lw is not None and total:
                    share = lw / total
                    entry["effective_tokens"][0] += r["prompt_tokens"] * share
                    entry["effective_tokens"][1] += r["completion_tokens"] * share
            result.append(per_model)
        return result

    # ── WebSocket ────────────────────────────────────────────────────────
    async def _handle_ws_frame(frame: dict, worker_id: str) -> None:
        """Handle a client-originated WS frame.

        Only `layer_distribution` is currently supported: the head reports the
        per-worker layer windows (Halda) once its prima.cpp is ready. It is
        accepted ONLY from the head (ring_position 0) — workers cannot report
        a distribution.

        The client sends the distribution keyed by RANK (Device Index, which
        is the ring position). The server maps rank -> worker_id using the
        cluster's member order (members[rank] == worker_id), so it can store
        the distribution keyed by worker_id for accounting.
        """
        ftype = frame.get("type")
        if ftype != "layer_distribution":
            return
        w = store.get_worker(worker_id)
        if w is None or w.cluster_id is None:
            return
        if w.ring_position != 0:
            logger.warning("ignoring layer_distribution from non-head worker %s", worker_id)
            return
        cluster_id = frame.get("cluster_id")
        if cluster_id != w.cluster_id:
            return
        lw = frame.get("layer_windows")
        if lw is not None and not isinstance(lw, dict):
            logger.warning("malformed layer_windows from %s; recording unknown", worker_id)
            lw = None
        elif lw is not None:
            # Normalize: only ints, drop anything else.
            lw = {k: v for k, v in lw.items() if isinstance(k, str) and isinstance(v, int) and v >= 0}
        try:
            cluster = store.get_cluster(cluster_id)
            if cluster is None:
                return
            # Map rank -> worker_id (members are in ring order; rank == index).
            by_worker: dict[str, int] = {}
            for rank, count in (lw or {}).items():
                try:
                    idx = int(rank)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(cluster.members):
                    by_worker[cluster.members[idx]] = count
            # Pass by_worker through even when empty — {} is the 'reported
            # unknown' marker, distinct from None ('not reported').
            status = scheduler.on_layer_distribution(cluster_id, by_worker)
            logger.info(
                "recorded layer distribution for cluster %s from head %s (%s)",
                cluster_id, worker_id, by_worker,
            )
            if status == ClusterStatus.live:
                logger.info("cluster %s is now live", cluster_id)
        except KeyError:
            logger.warning("layer_distribution for unknown cluster %s from %s", cluster_id, worker_id)

    @app.websocket("/v1/workers/{worker_id}/events")
    async def worker_events(websocket: WebSocket, worker_id: str, api_key: str | None = None):
        # Auth via query param (per spec) or Authorization header.
        secret = api_key
        if secret is None:
            auth = websocket.headers.get("authorization")
            if auth and auth.lower().startswith("bearer "):
                secret = auth.split(" ", 1)[1].strip()
        if not secret:
            await websocket.close(code=4401)
            return
        # Accept a worker key OR an account session token.
        account_id: str | None = None
        if secret.startswith("sess_"):
            try:
                account_id = verify_session(secret, settings.session_secret)[0]
            except (TypeError, IndexError):
                account_id = None
        else:
            rec = store.resolve_api_key(secret)
            if rec is not None and rec.scope == "worker":
                account_id = rec.account_id
        if account_id is None:
            await websocket.close(code=4401)
            return
        acct = store.get_account(account_id)
        if acct is not None and acct.banned:
            await websocket.close(code=4403)
            return
        w = store.get_worker(worker_id)
        if w is None or w.account_id != account_id:
            await websocket.close(code=4403)
            return

        await hub.connect(worker_id, websocket)
        await hub.send_hello(worker_id)
        try:
            while True:
                msg = await websocket.receive_text()
                if msg == "ping":
                    await websocket.send_json({"type": "pong"})
                else:
                    try:
                        frame = json.loads(msg)
                    except ValueError:
                        continue
                    await _handle_ws_frame(frame, worker_id)
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(worker_id)

    return app


def settings_path_from_env() -> str | None:
    import os

    return os.environ.get("PRIMA_POOL_STORE_PATH")
