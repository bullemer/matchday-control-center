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
if [ "${1:-}" = "--tensorrt" ]; then
  # TensorRT engines (FP16, up to 4 cameras per batch). Built for THIS GPU and
  # TensorRT version (rebuild after a driver/GPU change; the worker falls back to PyTorch).
  # Ultralytics would auto-install a newer numpy/onnx here that breaks the system torch build:
  # pin compatible versions and switch auto-install off.
  echo "[INFO] Installing TensorRT and building engines (about 10 minutes)"
  ai-venv/bin/pip install -q "tensorrt-cu12>=10.3,<10.14" "onnxslim==0.1.99"
  ai-venv/bin/pip install -q --no-deps "ml_dtypes==0.5.1"
  ai-venv/bin/python -c "import onnx" 2>/dev/null || ai-venv/bin/pip install -q "onnx==1.17.0"
  YOLO_AUTOINSTALL=False ai-venv/bin/python - <<'PYEOF'
from ultralytics import YOLO
# Rectangular 16:9 inputs and at most 4 match cameras at once keep the engines' GPU memory small.
for name, size in (("yolo26s", (544, 960)), ("box2boxcam-ball-v1.0", (736, 1280))):
    print("engine:", YOLO(f"models/{name}.pt").export(format="engine", half=True, dynamic=True, batch=4,
                                                     imgsz=size, device=0, workspace=1, verbose=False))
PYEOF
fi
echo "[OK] AI Director ready. Switch it on in the dashboard: 🤖 AI Director tab."
[ "${1:-}" = "--tensorrt" ] || echo "     Faster: scripts/setup-ai-director.sh --tensorrt (NVIDIA, builds TensorRT engines)"
