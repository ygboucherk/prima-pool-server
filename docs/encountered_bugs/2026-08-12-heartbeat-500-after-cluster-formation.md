# Bug report — recurring 500 on `POST /v1/workers/{id}/heartbeat` after cluster formation

**Date observed:** 2026-08-12
**Status:** ⚠️ **OPEN — not reproduced, root cause unknown.** Transient: self-resolved later the same day (server/worker restart), so it could recur.
**Severity:** High while active — heartbeats fail for **all** members of a just-formed cluster, which stalls readiness and can eventually trip liveness (30 s timeout) and dissolve the cluster. No data loss.

---

## 1. Summary

Immediately after the scheduler formed a 2-member cluster (`clu_2706c83cfbec13e3`, model `qwen2.5-coder-3b-instruct-q5_k_m`) and the server joined its WireGuard network, **every subsequent heartbeat from BOTH workers returned `500 Internal Server Error`**. The server logged an unhandled exception in the ASGI application, but the traceback is truncated in every captured log — the actual exception type/message and the app-frame below `starlette/_exception_handler.py` were never captured, so the root cause is unknown.

## 2. Environment

| | |
|---|---|
| **Server** | pool VPS, docker compose (uvicorn/FastAPI, Python 3.13) |
| **Server WG join** | `PRIMA_POOL_SERVER_JOIN_WG=true` (server joined the cluster's WG network) |
| **Model** | `qwen2.5-coder-3b-instruct-q5_k_m`, **8 GB** required (`required_memory_mb=8192`) |
| **Worker A (gbook)** | laptop, 8 GB machine **advertising 4096 MB** (rank 0 / head, `llama-server`) |
| **Worker B (24fire)** | 24 GB machine **advertising 6144 MB** (rank 1, `llama-cli`; VM kernel, so it reports ~6 GB) |
| **Cluster trigger** | 4096 + 6144 = 10240 ≥ 8192 → 2-member cluster forms |
| **Account** | both workers on the same account, each with its own worker-scoped key |
| **Deployed server code** | current `worker-statistics` branch (public `/v1/clusters/{id}/info` endpoint present in the logs) |

## 3. Observed timeline (server log)

```
scheduler:formed cluster clu_2706c83cfbec13e3 for model qwen2.5-coder-3b-instruct-q5_k_m (2 members)
wg_server:server joined cluster clu_2706c83cfbec13e3
POST /v1/workers/wrk_d16e73a55ae4e8c9/heartbeat           200   ← gbook (the heartbeat that formed the cluster)
GET  /v1/clusters/clu_2706c83cfbec13e3/config             200   ← 24fire (same-host docker network)
GET  /v1/clusters/clu_2706c83cfbec13e3/config             200   ← gbook
POST /v1/clusters/clu_2706c83cfbec13e3/ready             202   ← 24fire (rank 1) → "assembling (1/2)"
POST /v1/workers/wrk_9ec2e8f1c9e7994e/heartbeat           500   ← 24fire — FIRST failure
ERROR: Exception in ASGI application (traceback truncated in all captures)
```

Client side (both workers): `WARNING:prima_pool_client.agent:heartbeat failed: Internal Server Error` repeated every 10 s. The head (gbook) never got far enough to report its layer distribution / readiness during the window — the cluster stayed `assembling (1/2)`.

## 4. What was ruled out

- **Test suite regression:** all 99 server tests pass; nothing in the recent `worker-statistics` diff (public info endpoints, dashboard cluster view) touches the heartbeat path.
- **Faithful local reproduction:** reproduced the exact production conditions with `TestClient` — 2-member cluster from a 4096 MB + 6144 MB pair against an 8192 MB model, same account with two worker keys, `server_join_wg=True` (stub WG), config fetch, rank-1 ready report → the subsequent heartbeat returned **200**, not 500. Not reproducible.
- **`wg_server.up` as the direct 500 source:** it **succeeded** at formation (log line present) and is **not re-invoked afterwards** — once both members are `assigned`, `check_and_form()` finds an empty waitlist and returns before any WG work. So the recurring 500 is elsewhere in the heartbeat path.
- **Auth path:** a bad key/worker/ownership yields 401/403/404 (specific problem-details codes), not 500.

## 5. Root-cause candidates (unverified, ordered by likelihood)

1. **Blocking `subprocess.run([wg-quick, "up", iface])` inside an async handler.** `wg_server.up()` runs synchronously *inside* the heartbeat request (via `check_and_form → _form_cluster`), blocking the entire event loop for the duration of the WG bring-up. While blocked, all other heartbeats/WS frames/ready reports pile up; whatever state interaction happens once the loop drains (SQLite `update_worker`/`check_and_form` re-entrancy, liveness monitor starvation, client timeouts) is the best lead. Even if this is not *the* 500, it is a genuine defect on its own.
2. **SQLite error surfaced as 500** (e.g. `database is locked` from a stuck writer, or an FK/constraint failure on the cluster-FK'd worker row). Less likely on a single event loop with WAL + `busy_timeout=15 s` + a global RLock, but the 500s were deterministic once started.
3. **`_next_subnet` exhaustion:** `RuntimeError("no free cluster subnets")` → 500 if ≥255 subnets are held by non-`terminated` clusters (persisted DB + repeated testing can accumulate stuck `assembling` clusters). Cheap to verify by counting rows in the deployed `store.db`.
4. **A formation-adjacent race** between the WS `layer_distribution`, ready reports, and heartbeats — could not be reproduced with `TestClient`.

## 6. Why diagnosis stalled

The single biggest blocker: **the exception itself was never captured**. Uvicorn does log the full traceback on 500, but in every paste it is cut off at `starlette/_exception_handler.py:53 in wrapped_app` — the frames below (the route handler and the final `ExceptionType: message` line) were never shared. Without the last few lines, the root cause is a guessing game.

## 7. Recommended hardening (so the next occurrence is diagnosable + two real defects fixed)

1. **Capture the exception (blocker for future diagnosis).** Wrap the `heartbeat` handler body so any exception is logged with full context before re-raising:
   ```python
   try:
       ...existing handler...
   except Exception:
       logger.exception("heartbeat failed for worker %s (status=%s, cluster=%s)",
                        worker_id, w.status, w.cluster_id)
       raise
   ```
   (Or, quicker: next time it happens, grab the tail of the server log — `docker compose logs --tail=500` — the full traceback is there; the exception type + message are the last lines.)
2. **Never block the event loop with `wg-quick`.** Move `wg_server.up(cluster, chosen)` off the request path (schedule it as a background task via the existing `_schedule(...)` helper, or run it in a thread). Also log `CalledProcessError.stderr` (currently swallowed by `capture_output=True`), and treat a failed `up()` as non-fatal for the response.
3. **Verify subnet pool health** in the deployed DB (`SELECT status, COUNT(*) FROM clusters GROUP BY status;`) — if many `assembling` rows accumulated, that is both a cleanup item and candidate #3.

## 8. How to re-diagnose if it recurs

1. Immediately: `docker compose logs --tail=500` on the server and keep the **last 10 lines** (exception type + message) — that alone identifies the bug.
2. Note the worker status/`cluster_id` at failure time (client state file + `/v1/workers/{id}/state`).
3. Check whether the 500 is a single request or repeats on the *next* heartbeat (deterministic vs transient).
4. `SELECT status, COUNT(*), subnet FROM clusters GROUP BY status;` on the store DB.
