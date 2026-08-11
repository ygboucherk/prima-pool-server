# Usage & accounting

**Status:** The **user-side** half is implemented in v0 (see §0). The **worker-side**
crediting model below remains design-only for v1+; this document records the intended
model so the negotiation protocol doesn't paint us into a corner.

## 0. Implemented in v0 (user-side accounting)

The server logs every inference request proxied through `POST /v1/chat/completions`
into a `requests` table (SQLite store):

| Column | Meaning |
| ------ | ------- |
| `request_id` | unique id (`req_<hex>`) |
| `account_id` | source user (FK → accounts) |
| `key_id` | originating API key (FK → api_keys) |
| `model` | model used |
| `cluster_id` | which cluster processed it (FK → clusters) |
| `prompt_tokens` / `completion_tokens` | input/output tokens |
| `created_at` | timestamp |

Token counts come from the upstream llama-server `usage` object; for streaming
requests they are parsed from the final SSE chunk before `data: [DONE]`. If the
upstream closes before sending `usage`, no record is written (no false
zero-token entries). Accounting is best-effort and never breaks inference.

Clusters are **soft-deleted** (marked `terminated`, not removed), so the
`requests.cluster_id` reference stays valid and usage history survives the
cluster lifecycle — this is what will let the worker-crediting side later join
`requests → clusters → members` to attribute work.

Two account-scoped endpoints expose this (auth: user key OR session token):

- `GET /v1/accounts/{id}/usage/logs?begin=&end=` — the account's logs in
  `[begin, end)` (Unix seconds), newest first.
- `POST /v1/accounts/{id}/usage/stats` with `{"windows": [[begin, end], ...]}`
  — per-window `{model: {requests, prompt_tokens, completion_tokens}}`.

Both reject other accounts and worker-scoped keys (403).

## 1. The unit: token·layer

The economic unit proposed in the README is **token·layer**: a request that generates $T$ tokens
across $L$ layers credits each participating worker proportionally to the layers they hosted.

- `worker_credit = base_rate × (layers_hosted / L) × T`
- Summed over all requests in a billing period.

This is a draft. Whether the base rate is per-token, per-token·layer, or something else, and how
layers are attributed to workers, is unresolved.

## 2. What must be reported (for the negotiation protocol to be compatible)

Even though accounting endpoints are v1+, the negotiation protocol already carries the information
accounting will need:

| Signal in v0                    | Why accounting needs it                              |
| ------------------------------- | ---------------------------------------------------- |
| `worker.memory_allocated_mb`    | Layer attribution: a worker hosts layers proportional to its allocated memory. |
| `cluster.members + IPs`         | Which workers actually participated in a request.    |
| `heartbeat` / liveness          | Workers must be live to serve; dead workers can't accrue. |
| `relay.enabled` (per member)    | Relayed hops cost the operator bandwidth; payment may differ. |

## 3. Open questions (design only)

- **Who reports usage?** The pool server sees request metadata (via the OpenAI-compatible API); the
  cluster members see actual tokens. Both must agree.
- **How is it verified?** Plain reports are forgeable. Options: cluster members sign per-request
  summaries (aggregate signature), or the server samples/inspects a fraction of traffic.
- **Sybil resistance.** Fake workers that claim work without doing it. Mitigations (reputation,
  staking, occasional verification jobs) are future work.
- **Attribution.** Does `layers_hosted` track *hosted layers* (static: proportional to memory) or
  *actively computed layers* (dynamic: depends on routing)? The README says the latter; the
  protocol currently only carries the former.

## 4. Compatibility constraints for v0

- The user-side usage endpoints are implemented (`/v1/accounts/{id}/usage/logs`,
  `/v1/accounts/{id}/usage/stats`). The worker-side report endpoint
  `POST /v1/clusters/{id}/usage` (tentative) remains design-only for v1+.
- Keep `worker.memory_allocated_mb` and cluster membership stable so accounting can attribute work.
- The `relay.enabled` transparency requirement stays: workers must know when they're relaying,
  because relayed work may be paid differently.

## 5. Trust: self-declared memory

`memory_allocated_mb` is **self-declared** in v0, and the entire token·layer attribution model
rests on it. This is trivially gameable: a worker can declare a huge allocation, get assigned, and
produce nothing. The docs treat this as an open problem; concrete mitigations for v1+:

- **Scheduling sanity**: the server can cross-check the declared allocation against the hardware
  reported at registration (e.g. a worker declaring 256 GB on a machine with 64 GB RAM is
  suspicious).
- **Verification jobs**: occasionally send a synthetic request to a cluster and verify the output,
  to detect workers that accept work but don't compute.
- **Reputation / stake**: tie assignment priority to historical honest work, or require a deposit.

These are design notes, not v0 commitments.
