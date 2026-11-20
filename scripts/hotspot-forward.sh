#!/bin/sh
# Let camera phones on the app's hotspot (NetworkManager "shared", 10.42.0.0/24) reach the
# internet although Docker sets the FORWARD policy to DROP. Idempotent; run as root at boot
# (matchday-hotspot-forward.service) or by hand.
set -eu
NET="${MATCHDAY_HOTSPOT_NET:-10.42.0.0/24}"
iptables -N DOCKER-USER 2>/dev/null || true
add() { iptables -C DOCKER-USER "$@" 2>/dev/null || iptables -I DOCKER-USER "$@"; }
add -s "$NET" -j ACCEPT
add -d "$NET" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
echo "Hotspot $NET may use the internet (DOCKER-USER)"
