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

  # First attempt here; the app's watchdog (supsearch/tailscale.py) checks every minute
  # and repairs a failed or dropped connection, so a hiccup at boot doesn't stick.
  # --reset: clean preferences, so a saved exit node never makes `up` refuse.
  if ts up --reset --timeout=60s --auth-key="$TS_AUTHKEY" --hostname="${TS_HOSTNAME:-supplier-search}" \
     && [ -n "$TS_EXIT_NODE" ] && ts set --exit-node="$TS_EXIT_NODE"; then
    echo "tailscale up; supplier traffic exits via $TS_EXIT_NODE"
  else
    echo "tailscale not ready yet - the app will keep retrying" >&2
  fi
  # supplier traffic goes through Tailscale's proxy: out via the office once the exit node is set
  export OUTBOUND_PROXY="${OUTBOUND_PROXY:-http://localhost:1055}"
fi

exec python -m supsearch
