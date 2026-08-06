# Tunnel — WireGuard bring-up, readiness, health

**Status:** v0 draft. Specifies how a worker device brings up its WireGuard interface and how
the pool server tracks tunnel health.

## 1. Worker-side bring-up

On receiving `cluster_config`, the worker:

1. Writes `/etc/wireguard/prima-pool.conf` from the config payload:
   - `[Interface]`: own private key (never leaves the machine), `Address = <assigned_ip>/24`,
     `MTU = <mtu>` (default 1280 — the safe IPv6 minimum, chosen to avoid fragmentation over
     NAT/relay links; the server may set a different value per cluster).
   - `[Peer]` per peer: `PublicKey`, `Endpoint` (best-known), `AllowedIPs`, `PersistentKeepalive`.
   - Optionally a relay `[Peer]` (see §3).
2. Brings the interface up (`wg-quick up` or equivalent).
3. Joins the prima.cpp cluster using the member addresses (`10.23.<cluster_id>.<n>`).
4. Calls `POST /v1/clusters/{id}/ready`.

The worker MUST NOT reuse a WireGuard key across clusters concurrently; each cluster assignment
gets a fresh keypair (or the same keypair is permitted only if the server supports one-interface
multiple-peer — v0 uses per-assignment keys).

## 2. Health tracking (server side)

The server considers a member **healthy** if any of:

- it receives a `POST /v1/workers/{id}/heartbeat` (default every 10 s), or
- it receives a WS `pong` within the heartbeat window.

A member is **offline** after 3 missed heartbeats (~30 s). Being offline is **transient and
orthogonal to registration**:

- The worker keeps its `worker_id` and its API key stays valid.
- It is **removed from the waitlist** — it can't be assigned work while offline.
- On the next heartbeat, it is **re-added to the waitlist** with the same identity. No
  re-registration.
- If the worker was serving in an active cluster, it is excluded from new assignments (v0).
  Eviction / rebalancing of active members is v1 (see [churn.md](churn.md)).

**Grace period**: a heartbeat re-adds a worker to the waitlist, but the worker is not immediately
assignable. The server waits a short **assignable grace period** (default 5 s) after the first
heartbeat before treating the worker as a valid cluster member — this closes the race where a
worker that re-adds and immediately dies could be assigned mid-flap. The grace period is
server-side; the worker sees no protocol change.

v0 does **not** auto-evict; it only records liveness.

### Heartbeat

`POST /v1/workers/{id}/heartbeat` — `200 OK`, body optional (server may return cadence
adjustments). Auth: the worker key that owns `{id}`; a key cannot heartbeat another worker. The
server may return `{ "status": "waitlisted" }` to signal the worker has been re-added to the
waitlist after being offline.

## 3. Direct-first, relay fallback (tunnel details)

**Direct path**

- Peers are configured with `PersistentKeepalive = 25` so NAT mappings stay alive.
- If both peers are reachable (their self-reported endpoints work), the handshake completes
  directly.

**Relay path**

- The relay is a dedicated, publicly reachable WG node with its own keypair and a stable endpoint.
- To fall back, the worker adds the relay as a peer (`AllowedIPs` covering the peer's IP) — or,
  simpler, the server includes the relay in the peer list from the start with a lower priority.
- The relay **forwards** traffic between members; it cannot decrypt it (WG end-to-end
  encryption), but it CAN observe metadata (who talks to whom, packet sizes, timing).

**Path selection**

| Signal                              | Action                                     |
| ----------------------------------- | ------------------------------------------ |
| Direct handshake OK                 | Stay direct                                |
| Direct handshake timeout (> 5 s)    | Switch to relay                            |
| Direct keepalive loss (> 3 missed)  | Switch to relay, re-probe direct in background |

## 4. Readiness timeout

After the last `cluster_assigned` push, the server waits up to **60 s** for all members to report
`ready`. Members that miss the deadline are marked `failed`; the server may retry cluster formation
(excluding failed members).

## 5. Leaving

A worker leaves by:

1. `DELETE /v1/workers/{worker_id}` (removes from waitlist / cluster).
2. Bringing down its WG interface.

v0 does not support server-initiated eviction; it is planned for v1
(see [churn.md](churn.md)).
