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

Three account-scoped endpoints expose this (auth: user key OR session token):

- `GET /v1/accounts/{id}/usage/logs?begin=&end=&limit=` — the account's logs in
  `[begin, end)` (Unix seconds), newest first; `limit` caps the count (default 1000).
- `GET /v1/accounts/{id}/usage/logs/latest?limit=N` — the account's most
  recent `N` logs, newest first (default 50).
- `POST /v1/accounts/{id}/usage/stats` with `{"windows": [[begin, end], ...]}`
  — per-window `{model: {requests, prompt_tokens, completion_tokens}}`.

All three reject other accounts and worker-scoped keys (403).

### 0.1 Worker-attributed usage (v0)

The user-side endpoints above attribute the **full** token count of each request
to the account that made the call. A complementary set of **worker-attributed**
endpoints answers the other question: *what did this account's machines
contribute to serving requests?* Because a cluster can contain workers from
several accounts, a single request is attributed to each worker the account
owns in that cluster, scaled by that worker's layer share.

For a request served by cluster $C$, a worker $w \in C$ owned by the account has:

$$\text{share}(w) = \frac{\text{layer\_window}(w)}{\sum_{m \in C} \text{layer\_window}(m)}$$

$$\text{effective\_prompt} = \text{prompt\_tokens} \times \text{share}(w), \qquad
\text{effective\_completion} = \text{completion\_tokens} \times \text{share}(w)$$

The denominator sums over **all** members of $C$ (including other accounts'
workers), so the shares of all members sum to 1 and the effective tokens across
all accounts sum to the request's true totals. `share`/`effective_*` are `null`
when the cluster's layer distribution is unknown (no window reported); a
forwarder (`layer_window = 0`) gets `share = 0.0` and `effective = 0.0`.

Three endpoints expose this (auth: user key OR session token):

- `GET /v1/accounts/{id}/worker-logs?begin=&end=&limit=` — per-request-per-worker
  entries `{request_id, worker_id, model, prompt_tokens, completion_tokens,
  share, effective_prompt, effective_completion, created_at}` in `[begin, end)`,
  newest first. A request appears once per owned worker in its cluster.
- `GET /v1/accounts/{id}/worker-logs/latest?limit=N` — the account's most recent
  `N` worker-attributed entries, newest first (default 50).
- `POST /v1/accounts/{id}/worker-stats` with
  `{"windows": [[begin, end], ...], "worker_ids": [...]?}` — per-window
  `{model: {total_tokens: [prompt, completion], effective_tokens: [prompt,
  completion]}}`. `total_tokens` sums the request token counts over the
  account's worker rows; `effective_tokens` sums the share-scaled counts. If
  `worker_ids` is given, only rows for `(worker_ids ∩ owned workers)` are
  included; omitted/empty means all owned workers.

All three reject other accounts and worker-scoped keys (403). No schema change
was needed: the data comes from joining `requests → cluster_members → workers`
(the junction table already carries `layer_window` per member, and clusters are
soft-deleted so history survives).

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
| `cluster.layer_windows`         | **Actual** per-worker layer counts (Halda/HiGHS), reported by the head at cluster formation. This is the authoritative `layers_hosted` source — better than the memory-proportional estimate. An empty dict `{}` means "unknown" (head's stdout parse failed) and the memory-based estimate is the fallback. |
| `worker.memory_allocated_mb`    | Fallback layer attribution when `layer_windows` is unknown: a worker hosts layers proportional to its allocated memory. |
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
- **Attribution.** Does `layers_hosted` track *hosted layers* (static: the window Halda assigned) or
  *actively computed layers* (dynamic: depends on routing)? v0 now captures the former precisely via
  `cluster.layer_windows` (the head reports Halda's assignment at formation). Actively computed
  layers per request remain unresolved — that would require per-request telemetry from the ring.

## 4. Compatibility constraints for v0

- The user-side usage endpoints are implemented (`/v1/accounts/{id}/usage/logs`,
  `/v1/accounts/{id}/usage/stats`). The worker-side report endpoint
  `POST /v1/clusters/{id}/usage` (tentative) remains design-only for v1+.
- Keep `worker.memory_allocated_mb` and cluster membership stable so accounting can attribute work.
- The `relay.enabled` transparency requirement stays: workers must know when they're relaying,
  because relayed work may be paid differently.

## 5. Trust: self-declared memory

`memory_allocated_mb` is **self-declared** in v0. The good news: with `cluster.layer_windows` now
captured, attribution no longer *rests solely* on the declared allocation — Halda's assignment is
computed from actual profiled device capabilities (GFLOPS, memory bandwidth, disk speed), which is
much harder to game than a self-declared number. `memory_allocated_mb` remains the fallback only
when the distribution is unknown (`{}`). Still, nothing stops a worker from declaring a large
allocation to *enter* a cluster while contributing little. Mitigations for v1+:

- **Scheduling sanity**: the server can cross-check the declared allocation against the hardware
  reported at registration (e.g. a worker declaring 256 GB on a machine with 64 GB RAM is
  suspicious).
- **Verification jobs**: occasionally send a synthetic request to a cluster and verify the output,
  to detect workers that accept work but don't compute.
- **Reputation / stake**: tie assignment priority to historical honest work, or require a deposit.

These are design notes, not v0 commitments.
