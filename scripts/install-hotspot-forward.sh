#!/bin/sh
# Install the hotspot internet rules permanently (once per laptop):
#   pkexec sh scripts/install-hotspot-forward.sh     (or sudo)
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
install -m 0755 "$HERE/hotspot-forward.sh" /usr/local/sbin/matchday-hotspot-forward
install -m 0644 "$HERE/matchday-hotspot-forward.service" /etc/systemd/system/matchday-hotspot-forward.service
systemctl daemon-reload
systemctl enable --now matchday-hotspot-forward.service
systemctl restart matchday-hotspot-forward.service
iptables -S DOCKER-USER
