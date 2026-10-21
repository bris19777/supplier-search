#!/bin/sh
# Container entry point. With TS_AUTHKEY set, joins the Tailscale network and sends
# all supplier traffic out through the office PC (the exit node TS_EXIT_NODE), so
# the supplier sites see the office's Israeli IP instead of a cloud address.
set -e

if [ -n "$TS_AUTHKEY" ]; then
  mkdir -p /data/tailscale
  # no TUN device on Fly: userspace networking, exposed as a local HTTP/SOCKS5 proxy
  tailscaled --tun=userspace-networking --statedir=/data/tailscale \
    --socket=/tmp/tailscaled.sock \
    --socks5-server=localhost:1055 --outbound-http-proxy-listen=localhost:1055 &
  ts() { tailscale --socket=/tmp/tailscaled.sock "$@"; }

  # The exit node is set in a second step: its name can't be resolved until the
  # node is up. If either step fails the app still starts, going out directly
  # (EGRESS tells the UI), rather than leaving the container restarting.
  if ts up --timeout=60s --authkey="$TS_AUTHKEY" --hostname="${TS_HOSTNAME:-supplier-search}" \
     && [ -n "$TS_EXIT_NODE" ] && ts set --exit-node="$TS_EXIT_NODE"; then
    export OUTBOUND_PROXY="${OUTBOUND_PROXY:-http://localhost:1055}"
    export EGRESS="office:$TS_EXIT_NODE"
    echo "tailscale up; supplier traffic exits via $TS_EXIT_NODE"
  else
    export EGRESS="direct:tailscale-failed"
    echo "tailscale/exit node setup FAILED (check TS_AUTHKEY, TS_EXIT_NODE) - going out directly" >&2
  fi
fi

exec python -m supsearch
