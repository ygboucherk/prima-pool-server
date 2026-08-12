# Setup guide — running a prima-pool

This guide walks through setting up a **fully working prima-pool**: installing
WireGuard, standing up the pool server (control plane), and joining provider
devices (clients) so that inference requests can be served by a prima.cpp
cluster.

It covers the whole process end-to-end. For reference documentation of the
individual pieces, see:

- [Server README](../../README.md)
- [Client README](../../../prima-pool-client/README.md)
- [Protocol docs](../protocol/overview.md)
- [OpenAPI spec](../openapi/prima-pool.yaml)

> **Already ran a v0 pool?** A two-machine (public VPS pool + NAT'd laptop)
> CPU-only deployment with hardware specs, memory limits, and measured
> performance lives in
> [Tested two-machine CPU cluster](../../../prima-pool-client/docs/tests/two-machine-cpu-cluster.md).

---

## Architecture recap

```
                    ┌─────────────────────────────┐
                    │  Pool server (operator)     │
                    │  • control plane (HTTPS/WSS)│
                    │  • model registry           │
                    │  • (option A) joins WG nets │
                    └─────────────┬───────────────┘
                                  │ HTTPS/WSS (control)
              ┌───────────────────┼───────────────────┐
              │                   │                   │
        ┌─────▼─────┐       ┌─────▼─────┐       ┌─────▼─────┐
        │ Provider A│       │ Provider B│       │ Provider C│
        │ client +  │       │ client +  │       │ client +  │
        │ prima.cpp │◄─────►│ prima.cpp │◄─────►│ prima.cpp │
        └───────────┘  WG   └───────────┘  WG   └───────────┘
              └─────────────── ring (WireGuard) ───────────────┘
```

- **Control plane**: providers talk to the server over HTTPS/WSS (register, heartbeat, WS push, config fetch).
- **Data plane**: cluster members talk to each other over a **WireGuard** ring.
- **Inference**: the server (option A) joins the WG network and proxies
  `POST /v1/chat/completions` to the cluster head.

---

## Part 0 — Prerequisites

- **Docker + Docker Compose** on every machine (server + each provider).
- **WireGuard** kernel support on every machine (see Part 1).
- A **publicly reachable** host for the pool server (providers must reach it).
- A **GGUF model** file, identical on every provider that will serve it.

---

## Part 1 — Install WireGuard

WireGuard is used for the cluster data plane. The containers already bundle the
`wireguard-tools` binaries, but the **host kernel** must support WireGuard and
expose `/dev/net/tun`.

### 1.1 Install the tools (host)

**Debian / Ubuntu**

```bash
sudo apt update
sudo apt install -y wireguard-tools
```

**Fedora / RHEL**

```bash
sudo dnf install -y wireguard-tools
```

**Arch**

```bash
sudo pacman -S wireguard-tools
```

### 1.2 Enable the kernel module

```bash
sudo modprobe wireguard
```

To make it persist across reboots, add `wireguard` to `/etc/modules-load.d/`:

```bash
echo "wireguard" | sudo tee /etc/modules-load.d/wireguard.conf
```

### 1.3 Verify `/dev/net/tun`

The containers map `/dev/net/tun` in. Confirm it exists on the host:

```bash
ls -l /dev/net/tun
```

If it's missing, create it:

```bash
sudo mkdir -p /dev/net
sudo mknod /dev/net/tun c 10 200
sudo chmod 600 /dev/net/tun
```

### 1.4 Verify the module is loaded

```bash
sudo wg show 2>&1 | head -1   # should not error with "not supported"
```

> **Note**: WireGuard is **orchestrated automatically** by the pool software —
> you do not create interfaces or keypairs by hand. The client generates its own
> keypair and brings up the interface on cluster assignment; the server does the
> same when option A is enabled. You only need the tools + kernel support above.

---

## Part 2 — Set up the pool server

### 2.1 Clone and configure

```bash
git clone <repo-url> && cd prima-pool-server
cp .env.example .env
```

### 2.2 Edit `.env`

The three settings you **must** set:

```bash
# Publicly reachable base URL (what providers fetch configs from)
PRIMA_POOL_PUBLIC_BASE_URL=https://pool.example.com

# ⚠️ Strong random secret for session tokens
PRIMA_POOL_SESSION_SECRET=$(openssl rand -hex 32)

# Model registry: name:gguf_sha256:required_memory_mb
PRIMA_POOL_MODELS=deepseek-v4-flash-0731:3f9c2a5e...:16384
```

**Getting the model hash.** The registry pins each model to the exact SHA-256 of
its GGUF. Compute it from the model file you expect providers to run:

```bash
sha256sum model.gguf
```

> For a local test without a real model, you can use `demo-model:<no-hash>:4096`
> (the default). This disables the hash-integrity check — fine for dev, but for a
> real pool set real hashes so mismatched GGUFs are rejected.

**If you want to serve inference requests** (the proxy), also set:

```bash
PRIMA_POOL_SERVER_JOIN_WG=true
PRIMA_POOL_SERVER_WG_ENDPOINT_HOST=<your-public-ip>
```

### 2.3 Start the server

```bash
docker compose up -d
```

### 2.4 State & persistence

All state (accounts, API keys, workers, clusters) is stored in a **SQLite
database** — a single file that survives restarts:

- **Default path**: `/data/store.db` inside the container, backed by a named
  Docker volume (`prima-pool-server_prima-pool-server-data`). Nothing is erased
  between startups.
- **Config**: set `PRIMA_POOL_STORE_PATH` to use a custom path. If unset (bare
  local run), the store is in-memory only — data is lost on restart.
- **Legacy migration**: if you previously ran the old JSON-snapshot store
  (`store.json`), it is **auto-migrated** into SQLite on first start. The JSON
  file is kept (not deleted).
- **Schema upgrades**: when the server starts against a DB created by an older
  version, it **auto-upgrades the schema in place** (adds new columns). In
  particular, upgrading to the layer-distribution version backfills an explicit
  "unknown" distribution (`{}`) onto any **live** cluster that predates layer
  accounting — so the invariant "a live cluster always carries a distribution
  field" holds even across upgrades. (`assembling`/`terminated` clusters are
  untouched.) No manual step needed; existing data is preserved.
- **Backup**: the DB is a single file — back it up by copying it, e.g.
  `docker compose exec server cp /data/store.db /data/store.db.bak` (or back up
  the Docker volume).

### 2.5 Verify

```bash
# OpenAPI docs
curl http://<server>:8000/docs

# Model discovery (unauthenticated)
curl http://<server>:8000/v1/models

# Web GUI (account dashboard)
open http://<server>:8000/ui

# Register an account
curl -X POST http://<server>:8000/v1/accounts/register \
  -H "Content-Type: application/json" \
  -d '{"username":"operator","password":"hunter2hunter2"}'
```

---

## Part 3 — Join a provider device (client)

Each provider runs the client container, which bundles the agent **and**
prima.cpp in one image (same network namespace, so the WG tunnel is directly
visible to prima.cpp).

### 3.1 Clone and configure

```bash
git clone <repo-url> && cd prima-pool-client
cp .env.example .env
```

### 3.2 Bootstrap an account + worker key

Against the operator's server:

```bash
prima-pool-client bootstrap --pool-url https://pool.example.com
# prompts for username + password
# → prints account_id + worker key (sk-worker-...)
```

> The worker key is shown **once** — save it.

### 3.3 Edit `.env`

```bash
# URL of the operator's server
PRIMA_POOL_URL=https://pool.example.com

# Worker key from bootstrap
PRIMA_POOL_API_KEY=sk-worker-...

# Model to serve — must match a model in the server's registry
PRIMA_POOL_MODEL=deepseek-v4-flash-0731

# Self-declared memory to allocate (MB)
PRIMA_POOL_MEMORY_MB=16384

# WireGuard (leave private key empty to auto-generate)
PRIMA_POOL_WG_LISTEN_PORT=51820
PRIMA_POOL_WG_INTERFACE=prima-pool
# Optional: explicit WG endpoint host (public IP / Tailscale IP / hostname).
# If empty, the server uses the IP it observes on the registration connection —
# recommended when the client is behind NAT or in a container.
PRIMA_POOL_WG_ENDPOINT_HOST=

# Model file (mounted from host)
PRIMA_POOL_MODEL_PATH=/models/model.gguf
```

### 3.4 Provide the model

Put the GGUF in `./models/` on the host (mounted read-only into the container):

```bash
mkdir -p models
cp /path/to/model.gguf models/model.gguf
```

> The model must be the **same file** (same hash) as the one the server's
> registry expects. The client computes the SHA-256 at registration and the
> server rejects a mismatch.

### 3.5 Start the client

```bash
docker compose up -d
```

The agent will:
1. Register the worker (advertising the GGUF hash).
2. Heartbeat and listen on the WebSocket channel.
3. When enough matching workers are online, the server forms a cluster and
   pushes `cluster_assigned`.
4. The client brings up WireGuard, launches prima.cpp in-container, and reports
   ready. The head additionally parses prima.cpp's layer distribution (Halda)
   from its stdout and reports it over WS + in its ready body.

---

## Part 4 — Use the pool

Once a cluster is **live** — all members reported ready AND the head reported
the layer distribution — a user with a `sk-user-...` key can send requests:

```bash
curl http://<server>:8000/v1/chat/completions \
  -H "Authorization: Bearer sk-user-..." \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-v4-flash-0731","messages":[{"role":"user","content":"Hello!"}]}'
```

Streaming is supported (`"stream": true` → SSE).

---

## Part 5 — Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Failed to parse total physical memory` | No `MEM_LIMIT` / cgroup bug | Set `PRIMA_POOL_MEM_LIMIT` ≥ model size + 2 GB |
| Worker never assigned | Not enough matching memory, or hash mismatch | Check `GET /v1/models`; ensure `PRIMA_POOL_MODEL` + hash match; add more workers |
| `400 ... does not match` at registration | Wrong GGUF hash | Use the exact model file the registry pins |
| WG interface won't come up | No `/dev/net/tun` or kernel module | See Part 1 |
| Cluster stays `assembling`, never `live` | Head hasn't reported the layer distribution (WS dropped AND REST body lost, or `PRIMA_POOL_PRIMA_READY_TIMEOUT_S` exceeded) | Check head logs for "sent layer distribution" / "readiness reported". The cluster goes live only when ALL members report ready AND the head reports a distribution — an empty one (`{}`, unknown) still counts. Ensure the head's WS is up; the REST `ready` body carries the same distribution as a fallback |
| Peers can't reach each other | Endpoint is a container IP / NAT | Set `PRIMA_POOL_WG_ENDPOINT_HOST` to a reachable IP (or rely on the server's observed source IP). For hard NAT, deploy a relay (Part 6) |
| `502 Upstream Error` on `/v1/chat/completions` | Server can't reach the head over WG | Ensure `PRIMA_POOL_SERVER_JOIN_WG=true` + `PRIMA_POOL_SERVER_WG_ENDPOINT_HOST` set, the server image has `wireguard-tools`, and the server joined the cluster (check server logs for `server joined cluster`) |
| `no service selected` (client) | `COMPOSE_PROFILES` missing | Not applicable — client uses `same-container` mode |

---

## Part 6 — Relay fallback (optional)

For providers behind **symmetric NAT / CGNAT** where direct WireGuard peering
fails, deploy a **relay** — a publicly reachable WG forwarding node (like
Tailscale's DERP). The relay cannot decrypt anything (WG is end-to-end
encrypted); it only forwards encrypted packets between members.

### 6.1 Run the relay (operator)

```bash
cd prima-pool-server/relay
cp .env.example .env
# ⚠️ Set WG_PRIVATE_KEY to a FIXED key (wg genkey) so the pubkey is stable
docker compose up -d
docker logs prima-pool-relay    # shows the relay PUBLIC KEY
```

The relay listens on UDP `51822` and hot-reloads a peers file
(`./relay-peers`, one `pubkey allowedips` per line).

### 6.2 Point the server at the relay

```bash
# server .env
PRIMA_POOL_RELAY_ENABLED=true
PRIMA_POOL_RELAY_PUBKEY=<relay public key>
PRIMA_POOL_RELAY_ENDPOINT=relay.pool.example.com:51822
```

When no peer needs relaying, the client removes the relay route entirely; when
a direct path recovers, it stops routing that peer via the relay.

### 6.3 Add members to the relay (required)

For the relay to forward between two members, **both must appear as peers on the
relay** with the cluster IPs they can reach. Add each worker's public key + IP
to the `./relay-peers` file (hot-reloaded every `PEER_RELOAD_S` seconds):

```bash
# ./relay-peers — one per line: "<worker_wg_pubkey> <allowed_ips>"
# e.g. the two members of a 10.23.1.0/24 cluster:
<worker_A_pubkey> 10.23.1.1/32,10.23.1.2/32
<worker_B_pubkey> 10.23.1.1/32,10.23.1.2/32
```

> The pool server knows every worker's pubkey + assigned IP from cluster
> formation, but v0 does **not** auto-configure the relay — the operator (or a
> small script that reads cluster configs) maintains `./relay-peers`. A future
> version may hand member list to the relay automatically.

---

## Known limitations (v0)

- **NAT traversal**: direct connections work when clients are reachable — the
  server automatically uses each worker's observed source IP as its WG endpoint
  when the advertised one is a container/private IP (or the client can set
  `PRIMA_POOL_WG_ENDPOINT_HOST` explicitly). For symmetric NAT / CGNAT where
  direct peering fails, a **relay** provides fallback (see
  [Part 6 — Relay](#part-6--relay-fallback-optional)).
- **The web GUI is account-scoped** — it shows the logged-in account's own
  workers/keys, not a global operator view.
- **No usage/billing** yet (v1).
