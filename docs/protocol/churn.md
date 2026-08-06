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

A cluster is **dissolved when any member goes offline** (see
[negotiation.md](negotiation.md#5-waitlist-and-cluster-formation)), so an active member that drops
mid-request triggers dissolution:

- The whole cluster is returned to the waitlist; a request in flight may fail.
- The client-side retry is the only safety net for the failed request.
- Offline members rejoin on heartbeat; online members are immediately re-eligible.

This is an accepted v0 limitation, deliberately deferred so negotiation v0 stays small. The first
thing v1 must address is eviction + rebalance *without* full dissolution (see "Planned events").

## Design principles

- **REST recovery always wins**: every churn event is reflected in `GET /state` and re-pullable via
  `config_url`.
- **Config diffing**: on `cluster_updated`, workers should diff their current WG config against
  the new one and only touch changed peers (avoid WG restarts on every churn).
- **Payment continuity**: a worker that was evicted mid-request should still be credited for work
  done (ties into usage accounting, v1).
