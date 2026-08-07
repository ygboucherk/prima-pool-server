"""FastAPI application: REST + WebSocket control plane endpoints."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings
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
    ClusterStatusResponse,
    CreateKeyRequest,
    LoginRequest,
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

    def _worker_from_key(authorization: str | None, worker_id: str) -> WorkerRecord:
        account_id, scope = _api_key(authorization)
        if scope != "worker":
            raise ForbiddenError("A user key cannot manage workers.")
        w = store.get_worker(worker_id)
        if w is None:
            raise NotFoundError("Worker does not exist.")
        if w.account_id != account_id:
            raise ForbiddenError("This key does not own this worker.")
        return w

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
    async def register_worker(body: RegisterWorkerRequest, authorization: str | None = Header(None)):
        account_id, scope = _api_key(authorization)
        if scope != "worker":
            raise ForbiddenError("A user key cannot register workers.")

        if body.model not in settings.models:
            raise BadRequestError(f"Unknown model '{body.model}'.")

        rec = WorkerRecord(
            worker_id=new_id("wrk"),
            account_id=account_id,
            model=body.model,
            memory_allocated_mb=body.memory_allocated_mb,
            wg_pubkey=body.wg_pubkey,
            endpoint=body.endpoint,
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
            scheduler.check_and_form()
        store.update_worker(w)
        return w.to_worker()

    # ── clusters ─────────────────────────────────────────────────────────
    @app.get("/v1/clusters/{cluster_id}/config", response_model=ClusterConfig)
    async def get_cluster_config(cluster_id: str, authorization: str | None = Header(None)):
        account_id, scope = _api_key(authorization)
        if scope != "worker":
            raise ForbiddenError("A user key cannot fetch cluster config.")
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            raise NotFoundError("Cluster does not exist.")
        # The requesting worker must be a member.
        worker = next((w for w in store.list_workers() if w.account_id == account_id and w.cluster_id == cluster_id), None)
        if worker is None:
            raise ForbiddenError("This key does not own this worker.")
        config = scheduler._build_cluster_config(cluster)
        config["interface"]["private_ip"] = worker.assigned_ip
        return ClusterConfig(**config)

    @app.post("/v1/clusters/{cluster_id}/ready", response_model=ClusterStatusResponse, status_code=202)
    async def report_ready(cluster_id: str, body: ReportReadyRequest, authorization: str | None = Header(None)):
        account_id, scope = _api_key(authorization)
        if scope != "worker":
            raise ForbiddenError("A user key cannot report readiness.")
        cluster = store.get_cluster(cluster_id)
        if cluster is None:
            raise NotFoundError("Cluster does not exist.")
        worker = next((w for w in store.list_workers() if w.account_id == account_id and w.cluster_id == cluster_id), None)
        if worker is None:
            raise ForbiddenError("This key does not own this worker.")
        status = scheduler.on_ready(cluster_id, worker.worker_id)
        return ClusterStatusResponse(
            cluster_id=cluster_id,
            status=status,
            members_ready=len(cluster.ready),
            members_total=len(cluster.members),
        )

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
        rec = store.resolve_api_key(secret)
        if rec is None or rec.scope != "worker":
            await websocket.close(code=4401)
            return
        w = store.get_worker(worker_id)
        if w is None or w.account_id != rec.account_id:
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
