#!/usr/bin/env bash
# One-time setup of the AI Director: a Python environment with torch + ultralytics (ai-venv)
# and the two models (players: stock YOLO26s; ball: box2boxcam v1.0, AGPL-3.0).
# Re-running is safe. Needs an NVIDIA GPU with CUDA for real-time analysis.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
if ! "$PY" -c "import torch" 2>/dev/null; then
  echo "[INFO] torch is not installed for $PY: installing it into ai-venv (large download)"
  SYSTEM_SITE=""
else
  SYSTEM_SITE="--system-site-packages"   # reuse the installed torch (CUDA build)
fi
[ -x ai-venv/bin/python ] || "$PY" -m venv $SYSTEM_SITE ai-venv
ai-venv/bin/python -c "import torch" 2>/dev/null || ai-venv/bin/pip install -q torch --index-url https://download.pytorch.org/whl/cu124
ai-venv/bin/pip install -q "ultralytics==8.4.153" huggingface_hub
mkdir -p models
ai-venv/bin/python - <<'PYEOF'
import shutil
from pathlib import Path
from huggingface_hub import hf_hub_download
from ultralytics import YOLO
models = Path("models")
ball = models / "box2boxcam-ball-v1.0.pt"
if not ball.exists():
    shutil.copy(hf_hub_download("Totorotibo84/box2boxcam-football-ball-detection", "box2boxcam-ball-v1.0.pt"), ball)
person = models / "yolo26s.pt"
if not person.exists():
    YOLO("yolo26s.pt")              # downloads into the current directory
    shutil.move("yolo26s.pt", person)
import torch
print("models ok;", "CUDA:", torch.cuda.get_device_name(0) if torch.cuda.is_available() else "not available (CPU: slow)")
PYEOF
echo "[OK] AI Director ready. Switch it on in the dashboard: 🤖 AI Director tab."
