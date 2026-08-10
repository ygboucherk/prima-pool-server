"""FastAPI application: REST + WebSocket control plane endpoints."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
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
    ApiKey,
    ApiKeySummary,
    ClusterConfig,
    ClusterStatus,
    ClusterStatusResponse,
    CreateKeyRequest,
    LoginRequest,
    ModelInfo,
    RegisterAccountRequest,
    RegisterWorkerRequest,
    ReportReadyRequest,
    Session,
    Worker,
    WorkerRecord,
    WorkerState,
    WorkerStatus,
)
from .router import ClusterRouter
from .scheduler import Scheduler
from .security import new_id, sign_session, verify_password, verify_session
from .store import Store
from .wg_server import ServerWireGuard
from .ws_hub import WsHub

logger = logging.getLogger(__name__)


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


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

    hub = WsHub(store, settings)
    wg_server = ServerWireGuard(settings)
    scheduler = Scheduler(store, settings, hub, wg_server)
    router = ClusterRouter(store, settings)
    monitor = LivenessMonitor(store, settings, scheduler)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
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
        if store.get_account(account_id) is None:
            raise UnauthorizedError()
        return account_id

    def _api_key(authorization: str | None) -> tuple[str, str]:
        """Return (account_id, scope) for a scoped API key."""
        if not authorization or not authorization.lower().startswith("bearer "):
            raise UnauthorizedError()
        secret = authorization.split(" ", 1)[1].strip()
        rec = store.resolve_api_key(secret)
        if rec is None:
            raise UnauthorizedError("The provided API key is not valid.")
        return rec.account_id, rec.scope

    def _worker_credential(authorization: str | None) -> tuple[str, str]:
        """Resolve (account_id, scope) from a worker-scoped credential.

        Accepts EITHER:
        - a worker-scoped API key (device path), or
        - the account session token (owner path).

        User-scoped API keys are rejected. Raises UnauthorizedError if no valid
        credential, or ForbiddenError for a user-scoped key.
        """
        account_id: str | None = None
        scope: str | None = None
        if authorization and authorization.lower().startswith("bearer "):
            secret = authorization.split(" ", 1)[1].strip()
            rec = store.resolve_api_key(secret)
            if rec is not None:
                account_id, scope = rec.account_id, rec.scope
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
        return account_id, scope

    def _worker_from_key(authorization: str | None, worker_id: str) -> WorkerRecord:
        """Resolve a worker, authenticating with EITHER:
        - a worker-scoped API key (device path), or
        - the account session token that owns the worker (owner path).

        User-scoped API keys are rejected in both paths.
        """
        account_id, _scope = _worker_credential(authorization)
        w = store.get_worker(worker_id)
        if w is None:
            raise NotFoundError("Worker does not exist.")
        if w.account_id != account_id:
            raise ForbiddenError("This credential does not own this worker.")
        return w

    def _worker_account(authorization: str | None) -> str:
        """Resolve the account_id from a worker-scoped credential (worker key
        or account session). User-scoped API keys are rejected."""
        account_id, _scope = _worker_credential(authorization)
        return account_id

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
        )

    @app.post("/v1/accounts/login", response_model=Session)
    async def login(body: LoginRequest):
        rec = store.get_account_by_username(body.username)
        if rec is None or not verify_password(body.password, rec.password_hash):
            raise UnauthorizedError("Invalid credentials.")
        expires_at = int(time.time()) + settings.session_ttl_s
        token = sign_session(rec.account_id, settings.session_secret, expires_at)
        return Session(session_token=token, expires_at=_iso(expires_at))

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

    # ── workers ──────────────────────────────────────────────────────────
    @app.post("/v1/workers/register", response_model=Worker, status_code=201)
    async def register_worker(
        request: Request,
        body: RegisterWorkerRequest,
        authorization: str | None = Header(None),
    ):
        account_id, scope = _api_key(authorization)
        if scope != "worker":
            raise ForbiddenError("A user key cannot register workers.")

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
        # one-device-one-worker rule without a race between concurrent calls.
        if not store.create_worker_if_available(rec, settings.max_workers_per_account):
            existing = store.list_workers_for_account(account_id)
            if len(existing) >= settings.max_workers_per_account:
                raise ConflictError("worker_limit", "Worker Limit", "Too many workers for this account.")
            raise ConflictError("worker_exists", "Worker Exists", "A worker for this model already exists.")
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
        if was_offline:
            scheduler.check_and_form()
        return w.to_worker()

    # ── clusters ─────────────────────────────────────────────────────────
    @app.get("/v1/clusters/{cluster_id}/config", response_model=ClusterConfig)
    async def get_cluster_config(cluster_id: str, authorization: str | None = Header(None)):
        account_id = _worker_account(authorization)
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            raise NotFoundError("Cluster does not exist.")
        # The requesting account must own a worker that is a member.
        worker = next((w for w in store.list_workers() if w.account_id == account_id and w.cluster_id == cluster_id), None)
        if worker is None:
            raise ForbiddenError("This credential does not own a member of this cluster.")
        config = scheduler._build_cluster_config(cluster)
        config["interface"]["private_ip"] = worker.assigned_ip
        return ClusterConfig(**config)

    @app.post("/v1/clusters/{cluster_id}/ready", response_model=ClusterStatusResponse, status_code=202)
    async def report_ready(cluster_id: str, body: ReportReadyRequest, authorization: str | None = Header(None)):
        account_id = _worker_account(authorization)
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            raise NotFoundError("Cluster does not exist.")
        worker = next((w for w in store.list_workers() if w.account_id == account_id and w.cluster_id == cluster_id), None)
        if worker is None:
            raise ForbiddenError("This credential does not own a member of this cluster.")
        status = scheduler.on_ready(cluster_id, worker.worker_id)
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

    # ── inference proxy (option A: server joins WG, proxies to head) ─────
    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request, authorization: str | None = Header(None)):
        """Proxy an OpenAI-compatible chat completion to a live cluster's head.

        Auth: user-scoped API key (sk-user-...). The server finds a live cluster
        for the requested model and forwards the request to the head's
        llama-server over the WireGuard tunnel.
        """
        account_id, scope = _api_key(authorization)
        if scope != "user":
            raise ForbiddenError("Only a user key can send inference requests.")

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
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                if stream:
                    req = client.build_request("POST", target, json=body)
                    resp = await client.send(req, stream=True)
                    return StreamingResponse(
                        resp.aiter_bytes(),
                        status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "text/event-stream"),
                    )
                resp = await client.post(target, json=body)
                return JSONResponse(status_code=resp.status_code, content=resp.json())
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

    # ── WebSocket ────────────────────────────────────────────────────────
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
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(worker_id)

    return app


def settings_path_from_env() -> str | None:
    import os

    return os.environ.get("PRIMA_POOL_STORE_PATH")
