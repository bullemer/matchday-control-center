#!/usr/bin/env python3
"""
Matchday Live Control Center — Main Flask Server & REST API

Provides endpoints for:
  - Network management (USB tether, Wi-Fi hotspot)
  - MediaMTX / camera status
  - YouTube live stream start/stop
  - Seamless camera switching
  - Audio source selection
  - Live football scoreboard & match clock

Run: python app.py
"""

import logging
import os
import tempfile
import threading
import time
from functools import wraps

from flask import Flask, jsonify, render_template, request

# ---------------------------------------------------------------------------
# Graceful imports for companion modules (may not exist yet during early dev)
# ---------------------------------------------------------------------------
try:
    from network_manager import get_network_status, connect_usb_tether, start_hotspot, stop_hotspot
except ImportError:
    logging.warning("network_manager module not found — network endpoints will return stub data")

    def get_network_status():
        return {
            "wifi_ip": "10.42.0.1",
            "usb_iface": None,
            "usb_status": "Module not loaded",
            "hotspot_active": False,
            "internet_ok": False,
        }

    def connect_usb_tether():
        return False, "network_manager module not loaded"

    def start_hotspot(ssid, password):
        return False, "network_manager module not loaded"

    def stop_hotspot():
        return False, "network_manager module not loaded"

try:
    from media_engine import (
        init_mediamtx,
        get_camera_status,
        get_mediamtx_status,
        start_youtube_stream,
        stop_youtube_stream,
        switch_camera,
        set_audio_source,
        get_stream_status,
    )
except ImportError:
    logging.warning("media_engine module not found — media endpoints will return stub data")

    def init_mediamtx():
        logging.info("MediaMTX init skipped (media_engine not loaded)")

    def get_camera_status():
        return {"cam1": False, "cam2": False, "cam3": False}

    def get_mediamtx_status():
        return {"running": False}

    def start_youtube_stream(stream_key):
        return False, "media_engine module not loaded"

    def stop_youtube_stream():
        return False, "media_engine module not loaded"

    def switch_camera(cam_id):
        return False, "media_engine module not loaded"

    def set_audio_source(mode):
        return False, "media_engine module not loaded"

    def get_stream_status():
        return {"active": False, "active_camera": "cam1", "audio_source": "cam1"}

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("app")

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------
app = Flask(__name__)

# ---------------------------------------------------------------------------
# CORS — allow any origin on the local network
# ---------------------------------------------------------------------------

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

# ---------------------------------------------------------------------------
# Scoreboard & match clock state  (server-side, thread-safe)
# ---------------------------------------------------------------------------
SCOREBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SCOREBOARD_PATH = os.path.join(SCOREBOARD_DIR, "scoreboard.txt")

_scoreboard_lock = threading.Lock()
_scoreboard = {
    "home_name": "HOME",
    "home_score": 0,
    "away_name": "AWAY",
    "away_score": 0,
}

_clock_lock = threading.Lock()
_clock = {
    "running": False,
    "elapsed": 0.0,        # seconds elapsed when last paused / updated
    "start_ts": None,      # time.monotonic() when clock was started
    "half": 1,
}


def _clock_elapsed():
    """Return current elapsed seconds (accounts for running state)."""
    if _clock["running"] and _clock["start_ts"] is not None:
        return _clock["elapsed"] + (time.monotonic() - _clock["start_ts"])
    return _clock["elapsed"]


def _format_clock(total_seconds):
    """Format elapsed seconds as MM:SS."""
    total_seconds = max(0, int(total_seconds))
    minutes = total_seconds // 60
    seconds = total_seconds % 60
    return f"{minutes:02d}:{seconds:02d}"


def _write_scoreboard_file():
    """Atomically write scoreboard.txt so FFmpeg drawtext reload=1 never reads a partial file."""
    with _scoreboard_lock:
        home = _scoreboard["home_name"]
        hs = _scoreboard["home_score"]
        away = _scoreboard["away_name"]
        aws = _scoreboard["away_score"]
    with _clock_lock:
        clock_str = _format_clock(_clock_elapsed())

    text = f" {home}  {hs} - {aws}  {away}   |   {clock_str} "

    os.makedirs(SCOREBOARD_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=SCOREBOARD_DIR, suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_path, SCOREBOARD_PATH)
    except Exception:
        # Clean up temp file on error
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _get_scoreboard_state():
    """Return combined scoreboard + clock state as a dict."""
    with _scoreboard_lock:
        sb = dict(_scoreboard)
    with _clock_lock:
        elapsed = _clock_elapsed()
        sb["clock"] = _format_clock(elapsed)
        sb["clock_running"] = _clock["running"]
        sb["clock_elapsed_secs"] = round(elapsed, 1)
        sb["half"] = _clock["half"]
    return sb

# ---------------------------------------------------------------------------
# Helper: wrap endpoint in try/except → always return JSON
# ---------------------------------------------------------------------------

def api_endpoint(f):
    """Decorator that catches exceptions and returns a JSON error response."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as exc:
            log.exception("Unhandled error in %s", f.__name__)
            return jsonify({"ok": False, "error": str(exc)}), 500
    return wrapper

# ===================================================================
#  ROUTES
# ===================================================================

# --- UI ---------------------------------------------------------------

@app.route("/")
def index():
    """Serve the main dashboard UI."""
    return render_template("index.html")


# --- Combined status (polled by UI every ~2 s) ------------------------

@app.route("/api/status")
@api_endpoint
def api_status():
    """Return combined network + camera + stream + scoreboard status."""
    network = get_network_status()
    cameras = get_camera_status()
    mediamtx = get_mediamtx_status()
    stream = get_stream_status()
    scoreboard = _get_scoreboard_state()

    return jsonify({
        "ok": True,
        "network": network,
        "cameras": cameras,
        "mediamtx": mediamtx,
        "stream": stream,
        "scoreboard": scoreboard,
    })


# --- Network -----------------------------------------------------------

@app.route("/api/network/usb-connect", methods=["POST"])
@api_endpoint
def api_usb_connect():
    """Connect the USB tethering interface."""
    log.info("USB connect requested")
    success, msg = connect_usb_tether()
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg}), status_code


@app.route("/api/network/hotspot-start", methods=["POST"])
@api_endpoint
def api_hotspot_start():
    """Start the Wi-Fi hotspot with the given SSID and password."""
    data = request.get_json(force=True, silent=True) or {}
    ssid = data.get("ssid", "MatchdayLive")
    password = data.get("password", "matchday123")

    if len(password) < 8:
        return jsonify({"ok": False, "error": "Password must be at least 8 characters"}), 400

    log.info("Hotspot start requested: ssid=%s", ssid)
    success, msg = start_hotspot(ssid, password)
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg}), status_code


@app.route("/api/network/hotspot-stop", methods=["POST"])
@api_endpoint
def api_hotspot_stop():
    """Stop the Wi-Fi hotspot."""
    log.info("Hotspot stop requested")
    success, msg = stop_hotspot()
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg}), status_code


# --- Stream (YouTube) -------------------------------------------------

@app.route("/api/stream/start", methods=["POST"])
@api_endpoint
def api_stream_start():
    """Start the YouTube live stream encoder pipeline."""
    data = request.get_json(force=True, silent=True) or {}
    stream_key = data.get("stream_key", "").strip()

    if not stream_key:
        return jsonify({"ok": False, "error": "stream_key is required"}), 400

    log.info("Stream start requested")
    success, msg = start_youtube_stream(stream_key)
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg}), status_code


@app.route("/api/stream/stop", methods=["POST"])
@api_endpoint
def api_stream_stop():
    """Stop the YouTube live stream encoder pipeline."""
    log.info("Stream stop requested")
    success, msg = stop_youtube_stream()
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg}), status_code


# --- Camera switching --------------------------------------------------

@app.route("/api/switch/<cam_id>", methods=["POST"])
@api_endpoint
def api_switch_camera(cam_id):
    """Switch the active (on-air) camera to cam_id (cam1, cam2, cam3)."""
    valid_cams = ("cam1", "cam2", "cam3")
    if cam_id not in valid_cams:
        return jsonify({"ok": False, "error": f"Invalid camera id '{cam_id}'. Use one of {valid_cams}"}), 400

    log.info("Camera switch requested: %s", cam_id)
    success, msg = switch_camera(cam_id)
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg, "active_camera": cam_id}), status_code


# --- Audio source ------------------------------------------------------

@app.route("/api/audio-source", methods=["POST"])
@api_endpoint
def api_audio_source():
    """Set the audio mode — 'cam1' (lock to main mic) or 'active' (follow active camera)."""
    data = request.get_json(force=True, silent=True) or {}
    mode = data.get("mode", "").strip().lower()

    if mode not in ("cam1", "active"):
        return jsonify({"ok": False, "error": "mode must be 'cam1' or 'active'"}), 400

    log.info("Audio source change requested: %s", mode)
    success, msg = set_audio_source(mode)
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg, "audio_source": mode}), status_code


# --- Scoreboard --------------------------------------------------------

@app.route("/api/scoreboard/update", methods=["POST"])
@api_endpoint
def api_scoreboard_update():
    """Update team names and/or scores, then rewrite the overlay file."""
    data = request.get_json(force=True, silent=True) or {}

    with _scoreboard_lock:
        if "home_name" in data:
            _scoreboard["home_name"] = str(data["home_name"]).strip().upper() or "HOME"
        if "away_name" in data:
            _scoreboard["away_name"] = str(data["away_name"]).strip().upper() or "AWAY"
        if "home_score" in data:
            try:
                _scoreboard["home_score"] = max(0, int(data["home_score"]))
            except (ValueError, TypeError):
                pass
        if "away_score" in data:
            try:
                _scoreboard["away_score"] = max(0, int(data["away_score"]))
            except (ValueError, TypeError):
                pass

    _write_scoreboard_file()
    log.info("Scoreboard updated: %s", _get_scoreboard_state())
    return jsonify({"ok": True, "scoreboard": _get_scoreboard_state()})


@app.route("/api/scoreboard/clock", methods=["POST"])
@api_endpoint
def api_scoreboard_clock():
    """
    Control the match clock.

    Accepts JSON with "action": start | pause | reset | set_half
    For set_half, also accepts "half": 1 or 2
    """
    data = request.get_json(force=True, silent=True) or {}
    action = data.get("action", "").strip().lower()

    if action not in ("start", "pause", "reset", "set_half"):
        return jsonify({
            "ok": False,
            "error": "action must be one of: start, pause, reset, set_half",
        }), 400

    with _clock_lock:
        if action == "start":
            if not _clock["running"]:
                _clock["start_ts"] = time.monotonic()
                _clock["running"] = True
                log.info("Match clock started (elapsed so far: %.1fs)", _clock["elapsed"])

        elif action == "pause":
            if _clock["running"]:
                _clock["elapsed"] += time.monotonic() - _clock["start_ts"]
                _clock["start_ts"] = None
                _clock["running"] = False
                log.info("Match clock paused at %.1fs", _clock["elapsed"])

        elif action == "reset":
            _clock["running"] = False
            _clock["elapsed"] = 0.0
            _clock["start_ts"] = None
            _clock["half"] = 1
            log.info("Match clock reset to 00:00")

        elif action == "set_half":
            half = data.get("half", 2)
            try:
                half = int(half)
            except (ValueError, TypeError):
                half = 2
            _clock["half"] = half
            if half == 2:
                _clock["elapsed"] = 45 * 60.0  # 45:00
                _clock["running"] = False
                _clock["start_ts"] = None
                log.info("Match clock set to 45:00 for 2nd half")
            else:
                _clock["elapsed"] = 0.0
                _clock["running"] = False
                _clock["start_ts"] = None
                log.info("Match clock set to 00:00 for 1st half")

    _write_scoreboard_file()
    return jsonify({"ok": True, "scoreboard": _get_scoreboard_state()})


@app.route("/api/scoreboard", methods=["GET"])
@api_endpoint
def api_scoreboard_get():
    """Return current scoreboard + clock state."""
    return jsonify({"ok": True, "scoreboard": _get_scoreboard_state()})


# ===================================================================
#  Start-up helpers
# ===================================================================

def _background_init():
    """Initialise MediaMTX in a background thread so Flask starts immediately."""
    try:
        log.info("Background init: starting MediaMTX provisioning…")
        init_mediamtx()
        log.info("Background init: MediaMTX ready")
    except Exception:
        log.exception("Background init: MediaMTX failed to start")


# ===================================================================
#  Main entry point
# ===================================================================

if __name__ == "__main__":
    # Write initial scoreboard file
    _write_scoreboard_file()
    log.info("Initial scoreboard written to %s", SCOREBOARD_PATH)

    # Start MediaMTX in background so Flask binds immediately
    init_thread = threading.Thread(target=_background_init, daemon=True, name="mediamtx-init")
    init_thread.start()

    log.info("Starting Matchday Live Control Center on 0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
