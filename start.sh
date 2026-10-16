#!/usr/bin/env bash
# Matchday Live Control Center - Startup Script
set -e

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

# Check ffmpeg
if command -v ffmpeg &>/dev/null; then
    echo "[OK] ffmpeg found: $(ffmpeg -version 2>&1 | head -1)"
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
pip install -q -r requirements.txt

echo ""
echo "[START] Launching on http://0.0.0.0:5000"
echo "        Local access: http://localhost:5000"
echo "        Hotspot access: http://10.42.0.1:5000"
echo ""

python3 app.py
