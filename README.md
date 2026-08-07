# prima-pool-server

A control plane for an AI inference pool based on prima.cpp. This is a **working v0**
implementation of the negotiation protocol defined in `docs/protocol/*.md` and
`docs/openapi/prima-pool.yaml`.

## What it does

- **Accounts** — register/login, scoped API keys (`worker` / `user`)
- **Workers** — device registration, per-model waitlists, heartbeat-driven liveness
- **Clusters** — forms a prima.cpp ring when a model's waitlist has enough memory,
  hands out WireGuard configs (ring order = peer order), readiness handshake
- **WebSocket push** — `cluster_assigned` / `cluster_dissolved` frames (REST is the
  source of truth; WS is an accelerator)

Out of scope for v0: usage/accounting, eviction/rebalance (churn), the user-facing
OpenAI request API, and the relay node.

## Quick start (local)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
prima-pool-server --reload
```

The server listens on `http://127.0.0.1:8000` by default. OpenAPI docs at
`/docs`.

## Quick start (docker)

Deployed by the **pool operator** on their own host:

```bash
cp .env.example .env   # edit PRIMA_POOL_PUBLIC_BASE_URL, PRIMA_POOL_SESSION_SECRET, PRIMA_POOL_MODELS
docker compose up -d
```

Providers do **not** run this — they run `prima-pool-client` on their own
devices, pointed at this server's URL.

## Configuration

All settings are read from `PRIMA_POOL_*` environment variables. See
`src/prima_pool_server/config.py` for defaults.

| Variable | Default | Description |
|---|---|---|
| `PRIMA_POOL_HOST` | `0.0.0.0` | Bind host |
| `PRIMA_POOL_PORT` | `8000` | Bind port |
| `PRIMA_POOL_DEV_INSECURE` | `true` | Return secrets in plaintext (dev only) |
| `PRIMA_POOL_PUBLIC_BASE_URL` | `http://127.0.0.1:8000` | Base URL advertised in config URLs |
| `PRIMA_POOL_SESSION_SECRET` | dev value | HMAC secret for session tokens (**change in prod**) |
| `PRIMA_POOL_MODELS` | `demo-model:4096` | Model registry `name:required_memory_mb[,..]` |
| `PRIMA_POOL_HEARTBEAT_TIMEOUT_S` | `30` | Missed-heartbeat offline threshold |
| `PRIMA_POOL_HEARTBEAT_INTERVAL_S` | `10` | Suggested heartbeat cadence |
| `PRIMA_POOL_ASSIGNABLE_GRACE_S` | `5` | Grace period before a re-added worker is assignable |
| `PRIMA_POOL_READINESS_TIMEOUT_S` | `60` | Cluster readiness timeout |
| `PRIMA_POOL_MAX_WORKERS_PER_ACCOUNT` | `5` | Worker cap per account |
| `PRIMA_POOL_WG_MTU` | `1280` | WireGuard MTU |
| `PRIMA_POOL_RELAY_ENABLED` | `false` | Relay support (v0: config only) |
| `PRIMA_POOL_STORE_PATH` | unset | JSON persistence path (in-memory if unset) |

## Tests

```bash
pip install -e ".[dev]"
pytest
```

## Layout

```
src/prima_pool_server/
├── app.py        # FastAPI app (REST + WS endpoints)
├── config.py     # Settings
├── errors.py     # RFC 7807 problem details
├── liveness.py   # Background heartbeat monitor
├── models.py     # Pydantic schemas + domain dataclasses
├── scheduler.py  # Cluster formation / dissolution
├── security.py   # Password hashing, tokens, key generation
├── store.py      # In-memory store with optional JSON persistence
└── ws_hub.py     # WebSocket push hub
```
Large Language Models are often too large to run on a single machine, leading people to attempt to spread models across multiple machines. One of these attempts led to the creation of prima.cpp, which spreads a model across multiple machines connected to a local network.
Since prima.cpp can lead to usable performance through the internet (I personally experienced ~10-15 tokens/s on two machines connected through Tailscale with ~50ms latency), this repo aims to explore a broader use of prima.cpp, allowing to pool compute from crowdsourced machines.

The goal of this repo will be the creation of a control plane above prima.cpp, overseeing multiple distributed clusters and accounting for token usage. This would enable the creation of an accounting model similar to the one of cryptocurrency mining pools (e.g. a rate per token*layer) for operators with a billing similar to standard API billing (deposit money => make API call => it works).

## Strengths

This repository aims to decentralize AI compute, allowing people with smaller hardware to participate and provide computing power.

Additionally, while allowing AI accounting, it would make decentralized AI economically viable: while decentralized open-source options like Petals exist, their growth is limited by the fact that hardware is expensive and electricity doesn't fall from the sky (I mean, it kinda does, but not in a practical nor consistent way).

## Downsides

While prima.cpp has yielded to an usable performance with two devices through a tailscale link, a lower performance is to be expected with more devices, as clusters become more network-bound than compute-bound.

Additionally, decentralizing inference leads to privacy challenges, since multiple third-party providers end up processing one user's prompt.

# Target workflows

## For compute providers

- provider installs the prima-pool software on their computer (which would expose the required prima ports through a WireGuard tunnel)
- they configure it for a specific model
- pool adds it to a model-specific waitlist (noted model.waitlist here)
- if sum(computer.memory_allocated for computer in model.waitlist) >= model.required_memory, it takes all the waitlisted computers and groups them in a prima.cpp cluster, which becomes available for jobs
- when a request is procesed by the cluster, the provider's balance accrues

## For users

- user sends a request through the OpenAI api
- it's routed to a cluster, with a routing algorithm yet to specify