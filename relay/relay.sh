#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# prima-pool relay entrypoint.
#
# A WireGuard forwarding node: terminates tunnels with cluster members
# and forwards encrypted packets between them (like Tailscale DERP).
# WG is end-to-end encrypted, so the relay cannot decrypt traffic.
#
# Members are configured as peers via:
#   - $RELAY_PEERS : "pubkey=allowedips[,pubkey=allowedips...]" (allowedips
#     default "0.0.0.0/0,::/0"), or
#   - a peers file at $RELAY_PEERS_FILE ("pubkey allowedips" per line),
#     which is watched and hot-reloaded via `wg syncconf`.
# ─────────────────────────────────────────────────────────────
set -euo pipefail

WG_INTERFACE="${WG_INTERFACE:-prima-relay}"
WG_LISTEN_PORT="${WG_LISTEN_PORT:-51822}"
WG_ADDRESS="${WG_ADDRESS:-10.23.255.254/24}"
WG_MTU="${WG_MTU:-1280}"
WG_PRIVATE_KEY="${WG_PRIVATE_KEY:-}"   # optional; auto-generated if empty
RELAY_PEERS="${RELAY_PEERS:-}"
RELAY_PEERS_FILE="${RELAY_PEERS_FILE:-/etc/wireguard/relay-peers}"
PEER_RELOAD_S="${PEER_RELOAD_S:-15}"   # how often to reload the peers file

# Enable IP forwarding (host sysctl may be read-only in a container, so we
# also set it at runtime — this works when the container has NET_ADMIN).
sysctl -w net.ipv4.ip_forward=1 >/dev/null 2>&1 || true

# Generate a keypair if none provided.
if [ -z "$WG_PRIVATE_KEY" ]; then
  WG_PRIVATE_KEY="$(wg genkey)"
fi
WG_PUBLIC_KEY="$(echo "$WG_PRIVATE_KEY" | wg pubkey)"
echo "════════════════════════════════════════════════"
echo "prima-pool relay"
echo "  interface : $WG_INTERFACE"
echo "  listen    : 0.0.0.0:$WG_LISTEN_PORT/udp"
echo "  address   : $WG_ADDRESS"
echo "  PUBLIC KEY: $WG_PUBLIC_KEY"
echo ""
echo "  Set on the pool server:"
echo "    PRIMA_POOL_RELAY_ENABLED=true"
echo "    PRIMA_POOL_RELAY_PUBKEY=$WG_PUBLIC_KEY"
echo "    PRIMA_POOL_RELAY_ENDPOINT=<this-host>:$WG_LISTEN_PORT"
echo "════════════════════════════════════════════════"

# ── Peer management ────────────────────────────────────────
# Build the full config including peers from RELAY_PEERS and the peers file.
# Members are added as peers with broad AllowedIPs so the relay can forward
# any cluster IP between them.
build_config() {
  {
    cat <<EOF
[Interface]
PrivateKey = $WG_PRIVATE_KEY
Address = $WG_ADDRESS
ListenPort = $WG_LISTEN_PORT
MTU = $WG_MTU
EOF
    # From RELAY_PEERS env: "pubkey=allowedips[,pubkey=allowedips...]"
    if [ -n "$RELAY_PEERS" ]; then
      IFS=',' read -ra ENTRIES <<< "$RELAY_PEERS"
      for e in "${ENTRIES[@]}"; do
        pub="${e%%=*}"
        ips="${e#*=}"
        [ -z "$ips" ] && ips="0.0.0.0/0,::/0"
        [ -z "$pub" ] && continue
        echo ""
        echo "[Peer]"
        echo "PublicKey = $pub"
        echo "AllowedIPs = $ips"
        echo "PersistentKeepalive = 25"
      done
    fi
    # From RELAY_PEERS_FILE: "pubkey allowedips" per line
    if [ -f "$RELAY_PEERS_FILE" ]; then
      while IFS= read -r line; do
        [ -z "$line" ] && continue
        pub="${line%% *}"
        ips="${line#* }"
        [ -z "$ips" ] && ips="0.0.0.0/0,::/0"
        echo ""
        echo "[Peer]"
        echo "PublicKey = $pub"
        echo "AllowedIPs = $ips"
        echo "PersistentKeepalive = 25"
      done < "$RELAY_PEERS_FILE"
    fi
  } > /etc/wireguard/$WG_INTERFACE.conf
  chmod 600 /etc/wireguard/$WG_INTERFACE.conf
}

build_config
wg-quick up "$WG_INTERFACE"
echo "relay up. Waiting for members to connect..."
echo "(the relay must be reachable on UDP $WG_LISTEN_PORT)"

# Hot-reload the peers file if it changes.
last_mtime=""
while true; do
  mtime="$(stat -c %Y "$RELAY_PEERS_FILE" 2>/dev/null || echo 0)"
  if [ "$mtime" != "$last_mtime" ]; then
    last_mtime="$mtime"
    build_config
    wg syncconf "$WG_INTERFACE" <(wg-quick strip "$WG_INTERFACE") 2>/dev/null || {
      wg-quick down "$WG_INTERFACE" && wg-quick up "$WG_INTERFACE"
    }
  fi
  sleep "$PEER_RELOAD_S"
done
