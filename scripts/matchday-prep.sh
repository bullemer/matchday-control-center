#!/usr/bin/env bash
# Before a match: free the laptop's CPU and check what the broadcast needs.
#   scripts/matchday-prep.sh          stop dev servers, report the rest
#   scripts/matchday-prep.sh --check  only report
set -u
export LC_ALL=C
CHECK_ONLY=0; [ "${1:-}" = "--check" ] && CHECK_ONLY=1
cd "$(dirname "$0")/.."
warn=0
say()  { printf '  %-4s %s\n' "$1" "$2"; }

echo "Matchday prep"
# 1. Development servers with auto-reload burn whole cores (measured ~1.3 cores for two).
pids=$(pgrep -f "[u]vicorn .*--reload|[u]vicorn.*reload=True|[w]atchfiles|[n]ext dev|[v]ite( |$)" || true)
if [ -n "$pids" ]; then
  if [ $CHECK_ONLY = 1 ]; then
    say WARN "development servers running (stop them: scripts/matchday-prep.sh):"; ps -o pid=,pcpu=,args= -p $(echo $pids) | cut -c1-120 | sed 's/^/         /'; warn=1
  else
    ps -o pid=,pcpu=,args= -p $(echo $pids) | cut -c1-120 | sed 's/^/         stopping /'
    kill $pids 2>/dev/null; sleep 2; kill -9 $pids 2>/dev/null || true
    say OK "development servers stopped"
  fi
else
  say OK "no development servers running"
fi
# 2. Other heavy processes.
top=$(ps -eo pcpu=,comm= --sort=-pcpu | awk '$1 > 30 && $2 !~ /ffmpeg|mediamtx|python3|chrom/ {print}' | head -3)
[ -n "$top" ] && { say WARN "busy processes:"; echo "$top" | sed 's/^/         /'; warn=1; }
# 3. Hotspot phones need the Docker forwarding rules (installed once, survive reboots).
if systemctl is-active --quiet docker; then
  if systemctl is-active --quiet matchday-hotspot-forward.service; then
    say OK "hotspot phones may reach the internet (matchday-hotspot-forward)"
  else
    say WARN "Docker blocks hotspot phones' internet: pkexec sh scripts/install-hotspot-forward.sh"; warn=1
  fi
fi
# 4. Disk for the recording (about 2.1 GB per hour at 720p, needed twice until saved).
free_gb=$(df -BG --output=avail "${MCC_RECORDINGS_DIR:-recordings}" 2>/dev/null | tail -1 | tr -dc 0-9)
if [ -n "$free_gb" ] && [ "$free_gb" -lt 10 ]; then say WARN "only ${free_gb} GB free for recordings (want 10+)"; warn=1
else say OK "${free_gb:-?} GB free for recordings"; fi
# 5. Power: on battery the CPU may throttle.
if ls /sys/class/power_supply/*/online >/dev/null 2>&1 && ! grep -qsx 1 $(grep -lsx Mains /sys/class/power_supply/*/type | sed "s/type$/online/"); then
  say WARN "laptop runs on battery"; warn=1
fi
[ $warn = 0 ] && echo "Ready." || echo "Check the warnings above."
