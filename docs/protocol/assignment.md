# Assignment — WebSocket events, cluster config, WireGuard bring-up

**Status:** v0 draft. Defines the WebSocket push channel and the cluster/WireGuard configuration
handed to workers after cluster formation.

## 1. The WebSocket channel

After registering, the worker device opens a WebSocket to the server, authenticated with its
**worker-scoped key**:

```
wss://pool.example.com/v1/workers/{worker_id}/events?api_key=...
```

On connect, the server sends a `hello` frame carrying the negotiated cadence and any pending state
(the worker should treat this as authoritative):

```json
{ "type": "hello", "state": { "status": "waitlisted" }, "cadence": { "heartbeat_s": 10, "ws_reconnect_backoff_s": [1, 30] } }
```

### Frames (v0)

| Frame                | Direction       | Payload (key fields)                                    |
| -------------------- | --------------- | ------------------------------------------------------- |
| `hello`              | server → client | state, cadence                                           |
| `cluster_assigned`   | server → client | `cluster_id`, `worker_id`, `model`                       |
| `cluster_config`     | server → client | full WireGuard/peer config (below)                       |
| `cluster_evicted`    | server → client | `reason` (v1; worker-initiated leave uses REST instead)  |
| `ping` / `pong`      | both            | keepalive                                                |

### Recovery rule

**Every WS event is recoverable via REST.** A worker that misses a push (disconnect, restart)
must be able to reconstruct its full state from `GET /v1/workers/{worker_id}/state`. The WS is an
accelerator, not the source of truth.

### Authorization

All worker-scoped endpoints (`GET /state`, `POST /heartbeat`, `DELETE /workers/{worker_id}`, the
WS channel, and `POST /clusters/{id}/ready`) require the **worker key that owns the target
`worker_id`**. A worker key cannot access another worker's state, heartbeat, or cluster config.

## 2. Cluster assignment

On cluster formation, the server sends each member:

```json
{
  "type": "cluster_assigned",
  "cluster_id": "clu_01HZ4...",
  "worker_id": "wrk_01HZ3...",
  "model": "llama-3.1-8b-instruct",
  "assigned_ip": "10.23.7.2",
  "subnet": "10.23.7.0/24",
  "config_url": "https://pool.example.com/v1/clusters/clu_01HZ4.../config"
}
```

`config_url` lets the worker **re-pull** the full config (idempotent), and is the recovery path
for missed `cluster_config` pushes.

## 3. Cluster config (WireGuard)

`GET /v1/clusters/{id}/config` (auth: any member worker's key) returns:

```json
{
  "cluster_id": "clu_01HZ4...",
  "interface": {
    "private_ip": "10.23.7.2",
    "subnet": "10.23.7.0/24",
    "mtu": 1280
  },
  "relay": {
    "pubkey": "relay_public_key",
    "endpoint": "relay1.pool.example.com:51820",
    "enabled": true
  },
  "peers": [
    {
      "pubkey": "peer_a_pubkey",
      "endpoint": "203.0.113.20:51820",        // best-known endpoint (STUN self-report)
      "allowed_ips": ["10.23.7.1/32"],
      "persistent_keepalive": 25,               // keep NAT mapping alive
      "preferred": "direct"                     // "direct" | "relay"
    }
  ]
}
```

Notes:

- The worker **already has** its own WG keypair; it only needs `interface` + `peers` to bring up
  the tunnel.
- `relay.enabled` tells the worker that a relay path exists and should be used as fallback.
- `preferred: direct` is the initial preference; switching to relay is a runtime decision (see §4).

### Error

If the requesting worker is not a member of the cluster: `403 not_a_member`.

## 4. Direct-first, relay fallback

The initial preference is **direct** peering. If a direct path cannot be established (or dies
later), the worker falls back to the relay.

| Signal                               | Action                                   |
| ------------------------------------ | ---------------------------------------- |
| Direct WG handshake succeeds         | Stay direct                              |
| Direct handshake fails / times out   | Bring up relay path (add relay as peer)  |
| Direct path dies (WG keepalive loss) | Switch to relay, re-probe direct in background |

How liveness is detected (WG `PersistentKeepalive` + periodic handshake checks) is specified in
[tunnel.md](tunnel.md). The relay itself is a dedicated, publicly reachable node; the pool server
only tells workers *which* relay to use.

> **Transparency**: workers MUST be able to tell whether they are on relay or direct
> (the relay appears in their peer list). This matters for trust and, later, for payment
> (relayed hops cost the operator bandwidth).

## 5. Readiness handshake

After bringing up WireGuard and joining the prima.cpp cluster, each member calls:

### `POST /v1/clusters/{id}/ready`

The request body is empty; the caller is identified by the worker-scoped API key (there is no
`worker_id` in the body to avoid a mismatch surface between the key and the payload).

**Success — `202 Accepted`** (cluster is not live until all members are ready)

```json
{ "cluster_id": "clu_01HZ4...", "status": "assembling", "members_ready": 2, "members_total": 3 }
```

**Errors** — `409 not_member` if the worker is not in the cluster.

The cluster becomes **live** (routable) when the server has received `ready` from every member, or
after a readiness timeout (default 60 s — members that miss it are marked failed and the cluster
formation may be retried).

## 6. State recovery

`GET /v1/workers/{worker_id}/state` returns the worker's full current state, so a daemon can
reconstruct everything after a restart:

```json
{
  "worker_id": "wrk_01HZ3...",
  "account_id": "acc_01HZ2...",
  "status": "assigned",                          // "registered" | "waitlisted" | "assigned" | "ready"
  "online": true,                                // false → removed from waitlist until next heartbeat
  "model": "llama-3.1-8b-instruct",
  "cluster": { "cluster_id": "clu_01HZ4...", "assigned_ip": "10.23.7.2", "config_url": "..." }
}
```

`online` is the liveness flag, orthogonal to `status`: a worker can be `waitlisted` but `online:
false` (currently down, or freshly registered before its first heartbeat). While offline, the
worker is out of the waitlist and the scheduler; it is re-added on the next heartbeat (see
[tunnel.md](tunnel.md#2-health-tracking-server-side)).
