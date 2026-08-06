# Churn — membership changes (v1+)

**Status:** Design notes only; not part of v0.

v0 supports only worker-initiated leave (`DELETE /v1/workers/{worker_id}`) and liveness
tracking. This document records the intended behavior so the protocol stays compatible.

## Planned events

| Event            | Trigger                                   | Protocol impact                              |
| ---------------- | ----------------------------------------- | -------------------------------------------- |
| Worker leaves    | `DELETE /v1/workers/{worker_id}`          | Remove from cluster; remaining members get `cluster_updated` + re-pull config. |
| Eviction         | Server-initiated (unhealthy, policy)      | `cluster_evicted` WS frame (v1).             |
| Rebalance        | Cluster under/over capacity               | `cluster_updated` with new peer list.        |
| Failure          | Member offline mid-request                | Client-side retry; server rebalances (v1).   |

## Known v0 gap: mid-request failure

A cluster is **persistent until failure** (see [negotiation.md](negotiation.md#5-waitlist-and-cluster-formation)),
so an active member that goes offline mid-request has **no defined recovery in v0**:

- The cluster is not auto-evicted or rebalanced (that's v1).
- A request in flight when a member drops may fail; the client-side retry is the only safety net.
- The offline member is excluded from *new* assignments, but the cluster is not torn down.

This is an accepted v0 limitation, deliberately deferred so negotiation v0 stays small. It is the
first thing v1 must address (eviction + rebalance + mid-request failover).

## Design principles

- **REST recovery always wins**: every churn event is reflected in `GET /state` and re-pullable via
  `config_url`.
- **Config diffing**: on `cluster_updated`, workers should diff their current WG config against
  the new one and only touch changed peers (avoid WG restarts on every churn).
- **Payment continuity**: a worker that was evicted mid-request should still be credited for work
  done (ties into usage accounting, v1).
