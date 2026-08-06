# Churn — membership changes (v1+)

**Status:** Design notes only; not part of v0.

v0 supports only worker-initiated leave (`DELETE /v1/workers/{worker_id}`) and liveness
tracking. This document records the intended behavior so the protocol stays compatible.

## v0 behavior (what exists today)

- **Worker leaves** → `DELETE /v1/workers/{worker_id}`. If the worker was in a cluster, the cluster
  is dissolved and all members return to the waitlist (see
  [assignment.md](assignment.md#cluster-dissolution)).
- **Worker goes offline** → same dissolution. Offline members rejoin on heartbeat; online members
  are immediately re-eligible.
- There is **no** `cluster_updated` or per-member eviction in v0.

## Planned events (v1+)

| Event    | Trigger                                  | Protocol impact                            |
| -------- | ---------------------------------------- | ------------------------------------------ |
| Eviction | Server-initiated (unhealthy, policy)     | `cluster_evicted` WS frame (v1).           |
| Rebalance| Cluster under/over capacity              | `cluster_updated` with new peer list (v1). |

The core v1 goal is eviction + rebalance **without** full dissolution — so a cluster survives a
member loss instead of dissolving. `cluster_updated` (a new WS frame) tells remaining members to
diff their WG config against a new peer list. This is deliberately deferred from v0.

## Known v0 gap: mid-request failure

A cluster is **dissolved when any member goes offline** (see
[negotiation.md](negotiation.md#5-waitlist-and-cluster-formation)), so an active member that drops
mid-request triggers dissolution:

- The whole cluster is returned to the waitlist; a request in flight may fail.
- The client-side retry is the only safety net for the failed request.
- Offline members rejoin on heartbeat; online members are immediately re-eligible.

This is an accepted v0 limitation, deliberately deferred so negotiation v0 stays small.

## Design principles

- **REST recovery always wins**: every churn event is reflected in `GET /state` and re-pullable via
  `config_url`.
- **Config diffing**: on `cluster_updated`, workers should diff their current WG config against
  the new one and only touch changed peers (avoid WG restarts on every churn).
- **Payment continuity**: a worker that was evicted mid-request should still be credited for work
  done (ties into usage accounting, v1).
