"""AI Director: runs ai_director_worker.py (rates the match cameras) when enabled.

For now it only rates and recommends (display); it does not cut. The worker runs in its own
Python environment (ai-venv with torch + ultralytics) so a crash or GPU trouble there never
touches the broadcast. It reports through files in data/ai-director/ (state.json + one
annotated JPEG per camera); this module supervises it (restart with back-off) and serves
the state to the dashboard.

Setup once: scripts/setup-ai-director.sh (creates ai-venv, downloads the two models).
"""
import json
import logging
import math
import subprocess
import threading
import time
from pathlib import Path

import media_engine

log = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent
VENV_PYTHON = BASE / "ai-venv" / "bin" / "python"
WORKER = BASE / "ai_director_worker.py"
MODELS = BASE / "models"
MODEL_FILES = ("yolo26s.pt", "box2boxcam-ball-v1.0.pt")
OUT = BASE / "data" / "ai-director"
SETTINGS = BASE / "data" / "ai-director.json"
FENCES = BASE / "data" / "ai-director-fences.json"   # all fences the operator drew
FENCE_MAX_POINTS = 16
SNAPSHOT_W, SNAPSHOT_H = 1280, 720                  # same framing as the worker's analysis frames
FPS_CHOICES = (2, 5, 10)
STALE_S = 6.0            # worker state older than this = not running
RESTART_MAX_S = 60.0

_lock = threading.Lock()
_settings = {"enabled": False, "fps": 5}
_proc = None
_supervisor = None
_stop = threading.Event()
_last_error = ""
_restarts = 0
_fences = {}


def match_cameras():
    return list(media_engine.MATCH_CAMERAS)


def setup_problem():
    """None when the AI environment is ready, else what is missing."""
    if not VENV_PYTHON.exists():
        return "AI environment missing: run scripts/setup-ai-director.sh"
    missing = [m for m in MODEL_FILES if not (MODELS / m).exists()]
    if missing:
        return "Model files missing (" + ", ".join(missing) + "): run scripts/setup-ai-director.sh"
    return None


def load_settings():
    try:
        data = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        data = {}
    with _lock:
        if isinstance(data.get("enabled"), bool):
            _settings["enabled"] = data["enabled"]
        if data.get("fps") in FPS_CHOICES:
            _settings["fps"] = data["fps"]
    return dict(_settings)


def _save_settings():
    SETTINGS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(_settings))
    tmp.replace(SETTINGS)


def configure(enabled=None, fps=None):
    """Switch on/off and set the analysis rate (frames per second per camera)."""
    if enabled is not None and type(enabled) is not bool:
        raise ValueError("enabled must be true or false")
    if fps is not None and fps not in FPS_CHOICES:
        raise ValueError(f"fps must be one of {FPS_CHOICES}")
    if enabled and setup_problem():
        raise ValueError(setup_problem())
    with _lock:
        restart = fps is not None and fps != _settings["fps"]
        if enabled is not None:
            _settings["enabled"] = enabled
        if fps is not None:
            _settings["fps"] = fps
        _save_settings()
        want = _settings["enabled"]
    if want:
        if restart:
            _stop_worker()
        start()
    else:
        stop()
    return status()


def _command():
    return [str(VENV_PYTHON), str(WORKER), "--cams", ",".join(match_cameras()),
            "--fps", str(_settings["fps"]), "--out", str(OUT), "--models", str(MODELS),
            "--fences", str(OUT / "fences.json")]


# --- Field fences ----------------------------------------------------------------------
# A polygon around the pitch per match camera (relative 0..1 coordinates of the analysis
# frame). Only fixed cameras use it: on a gimbal or in the hand the picture moves.

def load_fences():
    try:
        data = json.loads(FENCES.read_text())
    except (OSError, ValueError):
        data = {}
    clean = {}
    for cam, pts in (data.items() if isinstance(data, dict) else []):
        try:
            clean[cam] = _clean_points(pts)
        except ValueError:
            pass
    with _lock:
        _fences.clear()
        _fences.update({c: p for c, p in clean.items() if c in match_cameras() and p})
    return dict(_fences)


def _clean_points(points):
    if not isinstance(points, list) or (points and not 3 <= len(points) <= FENCE_MAX_POINTS):
        raise ValueError(f"A field fence needs 3 to {FENCE_MAX_POINTS} points (or none to remove it)")
    clean = []
    for pt in points:
        if not (isinstance(pt, (list, tuple)) and len(pt) == 2 and all(
                type(v) in (int, float) and math.isfinite(v) and -0.05 <= v <= 1.05 for v in pt)):
            raise ValueError("Points must be [x, y] between 0 and 1")
        clean.append([round(min(max(float(pt[0]), 0.0), 1.0), 4), round(min(max(float(pt[1]), 0.0), 1.0), 4)])
    return clean


def set_fence(cam, points):
    """Store (or with [] remove) the field fence of one match camera."""
    if cam not in match_cameras():
        raise ValueError("Field fences are for match cameras (Cam 1-4)")
    clean = _clean_points(points)
    with _lock:
        if clean:
            _fences[cam] = clean
        else:
            _fences.pop(cam, None)
        FENCES.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = FENCES.with_suffix(".tmp")
        tmp.write_text(json.dumps(_fences))
        tmp.replace(FENCES)
    sync_fences()
    return clean


def _mounts():
    try:
        return {c["id"]: c.get("mount", "fixed") for c in media_engine.get_cameras()}
    except Exception:  # camera settings unreadable: assume fixed
        return {}


def effective_fences():
    """The fences the worker uses: fixed cameras only."""
    mounts = _mounts()
    with _lock:
        return {c: p for c, p in _fences.items() if mounts.get(c, "fixed") == "fixed"}


def sync_fences():
    """Write the worker's fence file when it differs (fence edited or camera mount changed)."""
    path = OUT / "fences.json"
    text = json.dumps(effective_fences(), sort_keys=True)
    try:
        if path.read_text() == text:
            return
    except OSError:
        pass
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def snapshot(cam):
    """A fresh still of one match camera in the worker's framing (for drawing the fence)."""
    if cam not in match_cameras():
        raise ValueError("Unknown match camera")
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = OUT / f"{cam}-still.jpg"
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-rtsp_transport", "tcp",
           "-timeout", "5000000", "-i", f"rtsp://127.0.0.1:8554/{cam}", "-an", "-frames:v", "1",
           "-vf", f"scale={SNAPSHOT_W}:{SNAPSHOT_H}:force_original_aspect_ratio=decrease,"
                  f"pad={SNAPSHOT_W}:{SNAPSHOT_H}:(ow-iw)/2:(oh-ih)/2", "-q:v", "4", "-y", str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=12)
    except subprocess.TimeoutExpired:
        raise ValueError("The camera did not deliver a picture in time")
    if result.returncode != 0 or not path.is_file():
        raise ValueError("Camera is not publishing")
    return path


def _start_worker():
    global _proc
    OUT.mkdir(mode=0o700, parents=True, exist_ok=True)
    _proc = media_engine.Process(_command(), cwd=str(BASE))
    log.info("AI Director started (%s fps per camera)", _settings["fps"])


def _stop_worker():
    global _proc
    proc, _proc = _proc, None
    if proc:
        media_engine._kill_process(proc, "AI Director")


def _supervise():
    """Keep the worker running while enabled; restart it with back-off after a crash."""
    global _last_error, _restarts
    delay = 2.0
    while not _stop.is_set():
        with _lock:
            enabled = _settings["enabled"]
            proc = _proc
        if not enabled:
            break
        if proc is None or proc.poll() is not None:
            if proc is not None:
                _last_error = (proc.error or f"worker exited with code {proc.poll()}").strip()[-400:]
                _restarts += 1
                log.warning("AI Director stopped (%s); restarting in %.0f s", _last_error[-120:], delay)
                if _stop.wait(delay):
                    break
                delay = min(delay * 2, RESTART_MAX_S)
            with _lock:
                if not _settings["enabled"]:
                    break
                try:
                    _start_worker()
                except OSError as exc:
                    _last_error = str(exc)
        elif _state_age() < STALE_S:
            delay = 2.0  # running fine: next crash restarts quickly again
        try:
            sync_fences()  # a camera switched to/from "fixed" mount
        except OSError:
            pass
        _stop.wait(1.0)


def start():
    global _supervisor
    problem = setup_problem()
    if problem:
        raise ValueError(problem)
    with _lock:
        if _supervisor and _supervisor.is_alive():
            return
        _stop.clear()
        _supervisor = threading.Thread(target=_supervise, daemon=True, name="ai-director")
        _supervisor.start()


def stop():
    global _supervisor
    _stop.set()
    if _supervisor:
        _supervisor.join(timeout=3)
        _supervisor = None
    with _lock:
        _stop_worker()


def autostart():
    """At app start: run if it was enabled last time (and the environment is ready)."""
    load_settings()
    load_fences()
    try:
        sync_fences()
    except OSError:
        pass
    if _settings["enabled"] and not setup_problem():
        try:
            start()
        except ValueError as exc:
            log.warning("AI Director not started: %s", exc)


def _read_state():
    try:
        return json.loads((OUT / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def _state_age():
    state = _read_state()
    updated = state.get("updated")
    return time.time() - updated if isinstance(updated, (int, float)) else 1e9


def status():
    state = _read_state()
    updated = state.get("updated")
    age = time.time() - updated if isinstance(updated, (int, float)) else None
    with _lock:
        enabled = _settings["enabled"]
        fps = _settings["fps"]
        running = bool(_proc and _proc.poll() is None)
    fresh = running and age is not None and age < STALE_S
    phase = state.get("phase") if fresh else ("starting" if running else ("crashed" if enabled and _last_error else "off"))
    return {
        "enabled": enabled, "fps": fps, "fps_choices": list(FPS_CHOICES), "phase": phase,
        "setup_problem": setup_problem(), "gpu": state.get("gpu") if fresh else None,
        "busy": state.get("busy") if fresh else None, "recommended": state.get("recommended") if fresh else None,
        "cameras": match_cameras(), "cams": state.get("cams", {}) if fresh else {},
        "error": _last_error if enabled else "", "restarts": _restarts,
        "fences": dict(_fences), "mounts": _mounts(),
    }


def image_path(cam):
    if cam not in match_cameras():
        return None
    path = OUT / f"{cam}.jpg"
    return path if path.is_file() else None
