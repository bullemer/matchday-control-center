#!/usr/bin/env bash
# Matchday Live Control Center - Startup Script
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo " Matchday Live Control Center"
echo "========================================"
echo ""

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python 3 is required but not found."
    exit 1
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else "Python 3.10+ is required")'

# Check ffmpeg
if command -v ffmpeg &>/dev/null; then
    echo "[OK] ffmpeg found: $(ffmpeg -version 2>&1 | sed -n '1p')"
else
    echo "[WARN] ffmpeg not found - streaming features will be unavailable"
fi

# Check nmcli
if command -v nmcli &>/dev/null; then
    echo "[OK] nmcli found"
else
    echo "[WARN] nmcli not found - network management features will be unavailable"
fi

# Install dependencies
if [ ! -d "venv" ]; then
    echo "[SETUP] Creating virtual environment..."
    python3 -m venv venv
fi

source venv/bin/activate
# Install on first run or when the pinned dependency file changes.
if [ ! -f venv/.requirements-installed ] || ! cmp -s requirements.txt venv/.requirements-installed; then
    python -m pip install -q -r requirements.txt
    cp requirements.txt venv/.requirements-installed
fi

echo ""
echo "[START] Launching on http://0.0.0.0:5000"
echo "        Local access: http://localhost:5000"
echo "        Login details are printed on startup"
echo ""

exec python3 app.py
