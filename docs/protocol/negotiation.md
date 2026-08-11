# Negotiation — accounts, keys, workers, cluster formation

**Status:** v0 draft. Covers account registration, scoped API keys, worker device registration,
per-model waitlists, and cluster formation. This is the core of the negotiation protocol.

## Model

```
Account (username + password)  ── owns ──▶  scoped API keys
                                              ├─ worker key  → worker device → waitlist → cluster
                                              └─ user key    → sends requests via /chat/completions
```

- **Account** is a pure identity: a username and a password. It is **not tied to hardware**. It
  owns scoped API keys and (in the future) the billing balance.
- **Scoped API key** is a credential restricted to one scope. A **worker key** lets a device
  register as a worker and provide compute. A **user key** lets a client send inference requests
  through `POST /v1/chat/completions` (proxied to a live cluster's head).
- **Worker** is a distinct device entity that logs in with a worker key. Because prima.cpp shards a
  model across nodes and **every node must hold a copy of the model weights**, a device must pick a
  model before it can join anything. So the model is a property of the **worker**, not of the
  account.

## 1. Account registration

### `POST /v1/accounts/register`

**Request**

```json
{
  "username": "alice",
  "password": "hunter2"
}
```

**Success — `201 Created`**

```json
{
  "account_id": "acc_01HZ2...",
  "username": "alice",
  "created_at": "2026-08-06T12:00:00Z"
}
```

**Errors**

| Status | Problem type                        | When                                            |
| ------ | ----------------------------------- | ----------------------------------------------- |
| 400    | `invalid_request`                   | Missing/invalid fields                          |
| 409    | `username_taken`                    | Username already registered                     |

The password is stored hashed (e.g. bcrypt/argon2); it is never returned.

## 2. Login (session token)

To manage API keys, the account logs in and receives a short-lived session token.

### `POST /v1/accounts/login`

**Request**

```json
{ "username": "alice", "password": "hunter2" }
```

**Success — `200 OK`**

```json
{ "session_token": "sess_<token>.<expiry>.<hmac>", "expires_at": "2026-08-06T13:00:00Z" }
```

**Errors** — `401 invalid_credentials`.

The session token is used to manage API keys (below) **and** — with the worker endpoints'
dual-auth — to access workers the session's account owns (see
[assignment.md](assignment.md#authorization)). Session tokens embed the `account_id` and are
short-lived (default 1 h). Worker devices use worker-scoped API keys, not session tokens.

## 3. Scoped API keys

### `POST /v1/accounts/{account_id}/keys`  (auth: session token)

**Request**

```json
{
  "name": "home-lab-worker",
  "scope": "worker"          // "worker" | "user"
}
```

**Success — `201 Created`**

```json
{
  "key_id": "key_01HZ6...",
  "name": "home-lab-worker",
  "scope": "worker",
  "api_key": "sk-worker-abc123...",   // shown once, at creation
  "created_at": "2026-08-06T12:30:00Z"
}
```

The `api_key` value is returned **only once** at creation. The server stores only a hash.

### `GET /v1/accounts/{account_id}/keys`  (auth: session token)

Lists the account's keys (id, name, scope, created_at — **not** the secret).

### `DELETE /v1/accounts/{account_id}/keys/{key_id}`  (auth: session token)

Revokes a key. Revoking a worker key permanently invalidates that worker's credential and removes
it from any waitlist/cluster (churn is v1+; see [churn.md](churn.md)). This is **distinct from
going offline**: an offline worker keeps its key and can return; a revoked key cannot be reused
(the worker must be re-registered with a new key).

**Success — `204 No Content`**

## 4. Worker device registration

A device that wants to provide compute logs in with a **worker-scoped key**. This creates a worker
entity and adds it to `model.waitlist`.

> **Authorization scope**: a worker key may only register a worker, and may only operate *the
> worker it created*. It cannot list/manage other workers, cannot manage the account, and cannot
> send requests. All worker-scoped endpoints (`state`, `heartbeat`, `DELETE /workers/{id}`, the
> WS channel) accept EITHER the worker-scoped key OR the owning account's session token
> (dual-auth); both must point at the same `worker_id` owned by the account. User-scoped API keys
> are rejected.

### `POST /v1/workers/register`  (auth: worker key)

**Request**

```json
{
  "model": "llama-3.1-8b-instruct",
  "gguf_sha256": "3f9c2a5e...64_hex_chars",
  "memory_allocated_mb": 16384,
  "wg_pubkey": "base64_encoded_curve25519_public_key",
  "endpoint": {
    "host": "203.0.113.10",
    "port": 51820,
    "behind_nat": true,
    "nat_type": "cone"               // "none" | "cone" | "symmetric" | "unknown"
  },
  "hardware": {
    "cpu": "AMD Ryzen 9 5950X",
    "gpu": "NVIDIA RTX 4090 24GB",
    "ram_gb": 64,
    "os": "linux",
    "prima_version": "0.1.0"
  }
}
```

Notes:

- `gguf_sha256` is the SHA-256 of the worker's GGUF file. The server only groups
  workers whose hash matches the registered model's hash — a mismatched GGUF is
  **rejected at registration** (400), so a cluster always runs the same model with
  the same quantization. Use `GET /v1/models` to see the pool's registered hashes.
- `memory_allocated_mb` is **self-declared** in v0. Verifying it against hardware is an open
  problem.
- `wg_pubkey` and `endpoint` are the device's **own WireGuard interface**, generated locally. The
  private key never leaves the device.
- `behind_nat` / `nat_type` are self-reported (ideally from a STUN probe). The server uses them to
  decide whether to prefer direct peering or relay.
- One account may run **multiple workers** (e.g. the same model on several machines). The server
  enforces a per-account worker cap and rejects a second worker for the **same device** (same WG
  pubkey). Each worker-scoped key is bound to the worker it registered, so cluster readiness and
  config are attributed to the correct device even within one account.

**Success — `201 Created`**

```json
{
  "worker_id": "wrk_01HZ3...",
  "account_id": "acc_01HZ2...",
  "status": "waitlisted",
  "model": "llama-3.1-8b-instruct",
  "waitlist_position": 2
}
```

**Liveness**: a worker is `online: false` until it sends its first heartbeat after registering
(registration is a one-shot API call, not a liveness signal). The server marks it `offline` and
removes it from the waitlist if it stops heartbeating — its key and `worker_id` stay valid (see
[tunnel.md](tunnel.md#2-health-tracking-server-side)). On the next heartbeat it is re-added to the
waitlist with the same identity; no re-registration needed.

### `DELETE /v1/workers/{worker_id}`  (auth: worker key)

Removes the worker. The device leaves the waitlist. If the worker was serving in a cluster, this
also removes it from the cluster (churn is v1+; see [churn.md](churn.md)).

**Success — `204 No Content`**

**Errors** — `404 not_found` if the worker doesn't exist; `409 worker_in_use` if the worker is
currently part of an active cluster (v1+).

## 5. Waitlist and cluster formation

- The server maintains a **per-(model, gguf_sha256) waitlist** of workers with
  `status: waitlisted`. Because every node must hold a copy of the exact same model weights,
  workers are only grouped with others that advertise the **same GGUF hash** (same model, same
  quantization) — a mismatched file can never join a cluster.
- The server groups workers into a **cluster** when
  `sum(worker.memory_allocated_mb for worker in model.waitlist) >= model.required_memory_mb`.
- Exactly how many workers get grouped, and in what order, is the **scheduler's** job
  (see [scheduler.md](../../design/scheduler.md)). The protocol only defines the trigger and the
  resulting assignment.
- The pool's registered models (slug + authoritative GGUF hash + required memory) are
  discoverable via the unauthenticated `GET /v1/models`. A worker that advertises a hash
  different from the registered one is rejected at registration (400).

When the server forms a cluster, it:

1. Creates a `cluster` record.
2. Assigns a per-cluster subnet (e.g. `10.23.<cluster_id>.0/24`) and a private IP to each member.
3. Builds each member's **peer list** (see [assignment.md](assignment.md)).
4. Pushes `cluster_assigned` to every member over its WebSocket.

**Cluster lifecycle**: a cluster exists only while it has **all its members online** — a cluster
with a missing member can't serve (the model no longer fits in the remaining memory). So when a
member goes offline or leaves, the server **dissolves the cluster** and returns **all** members to
the waitlist (see [assignment.md](assignment.md#cluster-dissolution)). A cluster does **not**
dissolve on idle.

The waitlist condition is the only trigger for cluster formation in v0. Rebalance without full
dissolution is v1+ (see [churn.md](churn.md)).

## 6. Cadence and timeouts (suggested defaults)

| Item                      | Default    | Notes                                        |
| ------------------------- | ---------- | -------------------------------------------- |
| Heartbeat interval        | 10 s       | `POST /v1/workers/{id}/heartbeat`            |
| Heartbeat timeout         | 30 s       | 3 missed heartbeats → worker marked offline  |
| WS reconnect backoff      | 1 s → 30 s | exponential, capped                         |
| Assignment poll fallback  | 30 s       | if WS unavailable, poll `GET /state`         |

These are defaults; the server may push its preferred cadence in a `hello` frame after the WS
handshake.

## 7. Abuse model & rate limiting

- **Login brute-force**: `POST /accounts/login` must be rate-limited and/or throttle on repeated
  failures (e.g. per-IP + per-username backoff). A `429 too_many_requests` problem response is
  expected.
- **Worker proliferation**: a single account could create unbounded workers. v0 should cap workers
  per account (default suggestion: a modest limit, e.g. 5) and rate-limit `POST /workers/register`.
- **Heartbeat flood**: heartbeats are cheap but unauthenticated rate-limiting is not needed (they
  are key-authenticated); still, the server may enforce a per-worker minimum heartbeat interval.
- These are enforcement notes for the server implementation; the protocol itself only needs the
  `429` error response defined (RFC 7807, as in §8).

## 8. Errors

All errors are `application/problem+json` (RFC 7807):

```json
{
  "type": "https://prima-pool.dev/errors/unauthorized",
  "title": "Unauthorized",
  "status": 401,
  "detail": "The provided API key is not valid.",
  "instance": "/v1/workers/register"
}
```
