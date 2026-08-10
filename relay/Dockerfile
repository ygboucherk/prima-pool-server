# ─────────────────────────────────────────────────────────────
# prima-pool relay — WireGuard forwarding node (DERP-equivalent)
# ─────────────────────────────────────────────────────────────
# A publicly reachable WireGuard node that forwards encrypted packets
# between cluster members that cannot reach each other directly
# (symmetric NAT / CGNAT). It CANNOT decrypt anything — WG is
# end-to-end encrypted; the relay only sees metadata.
#
# Build/run:
#   docker compose up -d
#   docker logs prima-pool-relay   # prints the relay's PUBLIC KEY
# Then set PRIMA_POOL_RELAY_PUBKEY + PRIMA_POOL_RELAY_ENDPOINT on the
# pool server and enable PRIMA_POOL_RELAY_ENABLED=true.
# ─────────────────────────────────────────────────────────────
FROM ubuntu:22.04 AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
        wireguard-tools \
        iproute2 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The relay forwards packets between members.
ENV NET_IPV4_FORWARD=1 \
    WG_INTERFACE=prima-relay \
    WG_LISTEN_PORT=51822

COPY relay.sh /usr/local/bin/relay.sh
RUN chmod +x /usr/local/bin/relay.sh

# NET_ADMIN to create the WG interface; the kernel needs /dev/net/tun.
EXPOSE 51822/udp
ENTRYPOINT ["/usr/local/bin/relay.sh"]
