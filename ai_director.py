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
            "--fps", str(_settings["fps"]), "--out", str(OUT), "--models", str(MODELS)]


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
    }


def image_path(cam):
    if cam not in match_cameras():
        return None
    path = OUT / f"{cam}.jpg"
    return path if path.is_file() else None
