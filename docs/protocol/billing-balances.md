# Billing balances

Status: **implemented (storage + admin control + visibility only)**.

This document records the design decisions behind the per-account balance
system, so a future contributor doesn't misread the unit or the rationale.
The implemented scope is the *state + controls* layer. Nothing yet debits a
balance on inference or credits a balance for serving work — those are the
follow-ups this design anticipates.

## 1. Unit

A balance is a plain **integer**, in units of **10⁻¹⁸ token** — the ERC20
"minor units" pattern (a token's `decimals()` = 18).

- `balance_minor` is the exact value: `balance_minor = tokens × 10¹⁸`.
- A balance of `1500000000000000000` means `1.5` tokens.

Why integer minor units rather than a float or a decimal column:

- **Exactness** (money arithmetic must not accumulate float error).
- **Model-agnostic**: pricing tiers (per-model rates, token·layer vs
  per-token) are undecided, and a fixed monetary/token·layer unit would force
  awkward cross-model conversions. A plain token count keeps the conversion
  rate a *billing-time policy*, not a schema concern.
- **Simplest**: Python `int` is arbitrary precision; set/adjust are integer
  `+`/`-` under the store lock. No `Decimal`, no rounding, no string math
  server-side.

### The wire format is a decimal string

`balance` is serialized as a **JSON string** (`"1500000000000000000"`), not a
JSON number. JSON numbers are float64, which can only represent integers
exactly up to 2⁵³ ≈ 9.007×10¹⁵ — above that (≈ 0.009 tokens) a balance would
silently round. The dashboard likewise does `BigInt` string math for
formatting and input parsing; never `parseFloat(x) * 1e18`.

Request fields (`SetBalanceRequest.balance`, `AdjustBalanceRequest.delta`)
accept **either** a JSON integer **or** a numeric string (Pydantic coerces), so
a client can send a large value as a string without loss.

### 64-bit ceiling

`balance_minor` is a SQLite `INTEGER` (64-bit signed), so a single balance is
capped at 2⁶³⁻¹ minor units ≈ **9.2 tokens**. Requests outside that range are
rejected with 422 (the request models bound the field to `[-(2⁶³), 2⁶³⁻¹]` to
avoid an `OverflowError`/500).

Two computed values can overflow **even when the inputs are in range**, and the
store re-checks both against the same bounds (raising `ValueError`, surfaced as
400 by the endpoints) rather than letting SQLite raise `OverflowError`:

- **`adjust_balance`**: `balance + delta` (e.g. INT64_MAX + 1).
- **`set_balance`'s recorded delta**: `balance - before` (e.g. INT64_MIN →
  INT64_MAX has delta 2⁶⁴−1, which overflows the `delta` column).

This is fine for a v0 ledger and far above any real per-request debit, but it
is a real limit. The escape hatches, when real billing lands:

1. **Re-denominate** — re-base to 10⁻²⁴ (or higher) units; a one-time
   migration multiplies existing balances.
2. **Store as `TEXT`** — a decimal string column removes the ceiling while
   staying exact (the store already does all math in Python under the RLock).

## 2. Schema

```
accounts.balance_minor    INTEGER NOT NULL DEFAULT 0     -- v0.8 migration

balance_events(
    event_id        TEXT PRIMARY KEY,
    account_id      TEXT NOT NULL,      -- NO FK (history survives deletion)
    admin_account_id TEXT,              -- NO FK (actor may also be deleted)
    kind            TEXT CHECK(kind IN ('set','adjust')),
    delta           INTEGER NOT NULL,   -- signed change
    balance_before  INTEGER NOT NULL,
    balance_after   INTEGER NOT NULL,
    reason          TEXT,               -- optional operator memo
    created_at      REAL NOT NULL
)
```

Design points:

- **No foreign keys** on `balance_events` — mirrors `requests.cluster_id` and
  `cluster_members.worker_id`. Balance history must survive account (and admin)
  deletion; it is an append-only audit trail.
- **Atomic**: the `UPDATE accounts` + event insert happen in one transaction
  under the store `RLock`, so state and audit trail never drift.
- **Migration**: `ALTER TABLE accounts ADD COLUMN balance_minor INTEGER NOT
  NULL DEFAULT 0` — existing (legacy) accounts backfill to 0 via the default;
  the `balance_events` table is created by `_SCHEMA` (`CREATE TABLE IF NOT
  EXISTS`), so no separate migration step for it.
- **`kind`** is redundant with `delta`/`before`/`after` (a "set" is just a
  delta computed as `after - before`), but makes admin-UI filtering trivial.

## 3. API

### Admin (session-auth, `_require_admin`)

- `PUT /v1/admin/accounts/{id}/balance` — set to an absolute value.
- `POST /v1/admin/accounts/{id}/balance/adjust` — add a signed `delta`.
  **No sign restriction** — negative deltas (deductions) and negative balances
  are allowed.
- `GET /v1/admin/accounts/{id}/balance/events` — history, newest first, with
  the acting admin's `admin_username` resolved (falls back to the raw id if the
  admin account was deleted).

### Account (user key OR session token, `_user_credential`)

- `GET /v1/accounts/{id}/balance` — own balance.
- `GET /v1/accounts/{id}/balance/events` — own history. The acting admin's
  identity is **not** exposed here (admin-only detail).

## 4. Open questions (future work)

These are the deliberate follow-ups the current layer is designed to serve:

- **Debit-on-use.** A per-request deduction would need to happen when token
  counts are known — for streaming, only at the end of the SSE stream. It must
  also handle multi-account clusters (attribute the request across accounts
  consistently with the worker-attribution logic in
  `usage-and-accounting.md` §0.1) and decide what happens when a balance goes
  negative (block vs. allow into debt).
- **Worker crediting.** Credits for serving work (purpose (c)) need the
  token·layer → token conversion decided (see `usage-and-accounting.md` §1)
  and a base rate per model tier.
- **Reason/audit semantics.** Whether the owner's event view should expose the
  acting admin's identity for transparency (currently admin-only).
- **Redenomination vs. TEXT**, when balances approach the 64-bit ceiling.
