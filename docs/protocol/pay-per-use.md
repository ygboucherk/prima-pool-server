# Pay-per-use (usage debit + worker crediting)

Status: **implemented** (v0.9).

This document records the decisions behind pay-per-use inference and worker
retribution, so the implementation and its follow-ups stay coherent. It
builds on the per-account balance system (`billing-balances.md`) and the
request/attribution accounting (`usage-and-accounting.md`).

## 1. Pricing model

Each model the pool serves carries two integer prices, expressed in
**balance-minor-units per token** (the same 10⁻¹² unit as `balance_minor`):

- `input_price`  — balance units charged per **prompt** token.
- `output_price` — balance units charged per **completion** token.

Prices are defined by the operator in the `PRIMA_POOL_MODELS` registry, which
grows two trailing fields:

```
slug:gguf_sha256:required_memory_mb:input_price:output_price
```

- **Legacy 3-field entries default to `input_price = 0`, `output_price = 0`**
  (backwards compatible — an existing pool stays free until the operator opts
  into pricing). The parser accepts both 3-field and 5-field entries.
- **One slug per quantization.** A model's price keys off the full
  `(slug, gguf_sha256)` pair, because a lower quantization (e.g. fp4) costs
  fewer resources than a higher one (fp8) and should be able to price
  differently. Allowed slugs are operator-defined; a distinct quantization is
  simply registered as a distinct slug. This matches the current registry
  shape (`dict[slug → ModelDef]`, one hash per slug) — no registry re-key is
  needed.
- Prices are **static config** — changing a price requires a server restart.
  (No admin price-mutation endpoint in v0.)

A model is **free** iff both prices are 0.

## 2. Unit & precision

`input_price` / `output_price` are plain integers in minor units, so a request's
cost is an exact integer — **no float ever enters the money path**:

```
cost = input_price × prompt_tokens + output_price × completion_tokens
```

- `cost` is computed from the token counts reported by the upstream
  llama-server `usage` object (already captured in the `requests` table).
- Because `cost` is a *computed product* of two in-range integers, the store
  re-checks it against the int64 ceiling before persisting (same overflow
  lesson already applied to `adjust_balance`/`set_balance` in
  `billing-balances.md`).
- The 2⁶³⁻¹ minor-unit ceiling (≈ 9.2M tokens) now applies on the **credit**
  side too: a busy worker account accumulates over time. Escape hatches are
  already documented (re-denominate, or a `TEXT` column).

## 3. Cost is recorded on the request (immutable)

`requests` gains two columns:

```
input_cost_minor    INTEGER NOT NULL DEFAULT 0
output_cost_minor   INTEGER NOT NULL DEFAULT 0
```

`input_cost_minor = input_price × prompt_tokens`, and
`output_cost_minor = output_price × completion_tokens`, both computed at
record time and **frozen** into the row. Rationale:

- **Price changes never rewrite history.** A request's cost stays what it was
  when it ran, regardless of later registry edits.
- **Single source of truth for both sides.** Users read their usage history
  (with per-request prompt/completion cost) and workers derive their credit
  from the *same* recorded cost — never from the live price, so the two sides
  can never disagree.

## 4. Debit (user side)

When a request's token counts are known (end of stream, or the non-stream
response), the requesting account's balance is debited `cost` in minor units.
This happens in the **same atomic settlement** that credits the workers (§6).

The insufficient-balance gate is a **start-of-request** check (see §7). No
reservation/hold is taken: a request that passes the gate may still drive the
account negative (accepted — see §10).

## 5. Worker credit (settlement)

Workers are paid **proportionally to their layer share**, using the same weight
that the worker dashboard already displays:

```
share(w) = layer_window(w) / Σ_members layer_window(m)
```

The weights are read from the **settled history** — `cluster_members.layer_window`
(the junction table written at cluster formation) — not from the head's live
claim. "Whatever counts in the history" is the rule: the dashboard's `share`
*is* the payment weight.

Settlement uses **exact integer division**, not the float `share`:

```
credit(w) = (cost × layer_window(w)) // cluster_total     # floor
remainder  = cost − Σ credit(w)                           # < N minor units
credit(head) += remainder                                  # give leftover to the head
```

where `cluster_total = Σ_members layer_window(m)`. Consequences:

- **Conservation holds exactly**: Σ credits = debit, no float error.
- Floor-per-worker + remainder-to-head is exactly what `//` yields; the leftover
  is rounding loss (< `N` minor units), not a fee.
- A **forwarder** (`layer_window = 0`) earns 0 under weighted settlement.
- **Unknown distribution** (`layer_windows` is `None`/`{}`, or a member is
  missing from the report) → **equal split** across all cluster members. This is
  the documented fallback: when there is no weight to pay by, split evenly
  (a cluster with an unknown distribution simply has no layer information to
  weight on). This is a *rare* path (head stdout parse failure) and is
  deliberately simple.
- `cluster_total = 0` (all members forwarders / no windows) is treated like an
  unknown distribution (equal split) — division by zero is impossible.

The float `share`/`effective_*` (from `usage-and-accounting.md` §0.1) remain a
**display/analytics** feature. The settled integer credit is the source of truth
for money; a sub-1-minor-unit gap between the dashboard's "expected credit" and
the settled amount is cosmetic and documented.

## 6. Atomic settlement

Debiting the user and crediting the workers is one store operation under the
`RLock`, so state can never drift:

```
settle_request(request_id, account_id, cluster_id,
               prompt_tokens, completion_tokens,
               input_cost, output_cost)
    → INSERT requests row (with frozen costs)
    → adjust_balance(user_account,  −cost,  reason=request_id)
    → for each member w in cluster:
        adjust_balance(w.account_id, +credit(w), reason=request_id)
```

- **One transaction.** A mid-crash cannot leave a debited user with uncredited
  workers (or vice versa). This is the only *new* store primitive the feature
  needs — `set_balance`/`adjust_balance` already exist per-account.
- **Cross-account.** Clusters are multi-account, so the credits legitimately
  flow to other accounts' workers. When one account owns every member of a
  cluster, debit and credits net to zero → **self-serve is free** (a direct,
  intended consequence of zero margin — see §10).
- **Settlement timing.** For streaming, settlement runs only when a `usage`
  chunk is seen (the existing `_parse_sse_usage` accounting point). No `usage`
  chunk → no record, no debit, no credit (see §10).

## 7. Insufficient balance gate

Before proxying a request to a priced model, the server checks the calling
account's balance:

- `balance > 0` → proceed.
- `balance <= 0` and the model is **free** (`input_price == output_price == 0`)
  → proceed.
- `balance <= 0` and the model is **priced** → reject with `402 Payment
  Required` and a new RFC 7807 problem `insufficient_balance`.

Negative balances are blocked exactly like zero balances. This is a *second*,
orthogonal gate on top of `can_use` — a banned/no-can-use account still gets
`403` before the balance check.

## 8. Schema changes

- `requests` gains `input_cost_minor`, `output_cost_minor` (migration ALTERs
  them in with `DEFAULT 0`; legacy rows backfill to 0).
- `balance_events.kind` CHECK extends from `('set','adjust')` to include
  `'debit'` and `'credit'` (a request's settlement writes one `debit` event for
  the user and one `credit` event per worker account). `reason` carries the
  `request_id`, and `admin_account_id` stays `NULL` (settlement has no human
  actor).

## 9. API changes

- `GET /v1/models` gains `input_price` / `output_price` (public, plain
  integers). Both users and workers can discover what they pay / earn.
- `GET /v1/accounts/{id}/usage/logs` (and `usage/stats`) expose the recorded
  `input_cost_minor` / `output_cost_minor` per request (and per-model sums).
- Worker-side views of earned credit reuse the existing `worker-logs` /
  `worker-stats` shape, with `effective_*` computed against the **recorded
  cost** (share × recorded cost) rather than raw tokens.

## 10. Documented v0 limitations & consequences

These are accepted-by-decision, and each must be stated in the final docs:

- **Allow-into-debt.** A request that passes the gate may drive a balance
  negative; the pool absorbs the deficit, and the account is blocked from
  priced models until it refills. Overshoot (a 1-token balance covering a
  1000-token generation) and the concurrent-requests race (two requests both
  pass the start-of-request check) are both accepted — no hold/reservation in
  v0.
- **Zero margin.** Σ credits = debit exactly; there is no operator fee, head
  premium, relay fee, or forwarder payment in v0. Self-serve (an account owning
  all members of a cluster) is therefore free. These are future work, not
  bugs.
- **Trust on the head.** The layer weights are the head's self-report (captured
  in history at formation). A weak/malicious head could misreport its
  distribution to skew pay. Accepted for a **permissioned** pool; sybil/honest
  head-hardening is future work.
- **Mid-request dissolve → no credit.** If a cluster dissolves mid-request and
  no `usage` chunk is produced, workers did real work but earn nothing, and the
  user is not charged (paying only for complete output). Accepted v0
  limitation.
- **No `usage` chunk → free inference.** An abrupt upstream close with no
  `usage` object records nothing (existing behavior), so a priced request that
  never reports usage is effectively free. Accepted for v0; consistent with
  "no charge for truncated output."
- **Key revocation now has monetary weight.** `requests.key_id` is `ON DELETE
  CASCADE`, so revoking a user key deletes that key's request/cost history (the
  settlement `balance_events` survive — no FK). Pre-existing behavior that is
  now about money, not just tokens; a soft-delete is a future fix.
- **Revoked worker mid-settlement → its credit is dropped.** Settlement maps
  each cluster member to its owning account by reading the `workers` table at
  settlement time. If a worker was revoked (deleted) between cluster formation
  and the request completing, `get_worker` returns `None`, that member's credit
  is silently skipped, and the requester still pays the full cost — the pool
  absorbs the difference. Niche (a worker must be revoked in the same window as
  an in-flight request, and settlement is immediate at end-of-stream), and
  consistent with "deleting a worker erases its worker-attributed history"
  (usage-and-accounting.md §0.1). Not a crash — the atomic transaction still
  commits the requester debit and any other workers' credits.
- **Cosmetic rounding.** The dashboard's float `share × cost` may differ from
  the settled integer credit by < 1 minor unit. Cosmetic only.

## 11. Future work (explicitly out of v0)

- Operator/platform fee, head premium, relay/forwarder payment.
- Honest-head verification (signing, sampling, reputation/stake) to reduce the
  trust on the head's self-reported distribution.
- Reservations/holds to close the concurrent-request overshoot race.
- Worker soft-delete (preserve attribution history across worker revocation).
- Runtime price mutation (vs. static config + restart).
