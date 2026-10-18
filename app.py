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
import math
import os
import tempfile
import threading
import time
from functools import wraps

from flask import Flask, jsonify, render_template, request, send_file

from browser_overlay import BrowserOverlay, normalize_url

browser_overlay = BrowserOverlay()

import atexit
import json
import secrets
import signal
from pathlib import Path
from urllib.parse import urlsplit
from werkzeug.exceptions import HTTPException
from network_manager import (get_network_status, connect_usb, start_hotspot, get_hotspot_config,
                             stop_hotspot, get_usb_audio_devices, get_firewall_status)
from media_engine import (ensure_mediamtx, start_mediamtx, get_camera_status,
                          get_mediamtx_running, get_stream_status, start_youtube_stream,
                          stop_youtube_stream, switch_camera, set_audio_source, shutdown_all,
                          get_audio_levels, set_mic_devices, get_mixer, set_mix, set_follow,
                          SUPERSEDED, set_layout, CAMERA_IDS)

def init_mediamtx():
    if not ensure_mediamtx():
        raise RuntimeError("MediaMTX installation failed; check prerequisites and logs")
    with _init_lock:
        if not _workers_stop.is_set() and not start_mediamtx():
            raise RuntimeError("MediaMTX could not start; check prerequisites and logs")

def get_mediamtx_status():
    return {"running": get_mediamtx_running()}

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

app.config["MAX_CONTENT_LENGTH"] = 16 * 1024
app.config["OPERATOR_PASSWORD"] = os.environ.get("MCC_OPERATOR_PASSWORD")
app.config["OPERATOR_USER"] = os.environ.get("MCC_OPERATOR_USER", "operator")

@app.before_request
def protect_controls():
    if (
        request.path.startswith("/static/")
        or request.path.startswith("/tally/")
        or request.path.startswith("/api/tally/")
    ):
        return None
    password = app.config.get("OPERATOR_PASSWORD")
    auth = request.authorization
    if not password:
        return jsonify(ok=False, error="Operator password has not been configured"), 503
    user_ok = secrets.compare_digest((auth.username or "").encode(), app.config["OPERATOR_USER"].encode()) if auth else False
    if not auth or not user_ok or not secrets.compare_digest((auth.password or "").encode(), password.encode()):
        return jsonify(ok=False, error="Operator login required"), 401, {
            "WWW-Authenticate": 'Basic realm="Matchday operator"', "Cache-Control": "no-store"}
    if request.method == "POST":
        origin = request.headers.get("Origin")
        if origin and origin != request.host_url.rstrip("/"):
            return jsonify(ok=False, error="Cross-origin control requests are forbidden"), 403
        if not request.is_json or not isinstance(request.get_json(silent=True), dict):
            return jsonify(ok=False, error="A JSON object is required"), 400

@app.after_request
def secure_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response

@app.errorhandler(HTTPException)
def http_error(error):
    return jsonify(ok=False, error=error.description), error.code

# ---------------------------------------------------------------------------
# Scoreboard & match clock state  (server-side, thread-safe)
# ---------------------------------------------------------------------------
SCOREBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
SCOREBOARD_PATH = os.path.join(SCOREBOARD_DIR, "scoreboard.txt")

_scoreboard_lock = threading.RLock()
_scoreboard = {
    "home_name": "HOME",
    "home_score": 0,
    "away_name": "AWAY",
    "away_score": 0,
    "visible": True,
    "source": "builtin",
    "overlay_url": "",
}

_clock_lock = _scoreboard_lock
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
    with _scoreboard_lock:
        _write_scoreboard_locked()

def _write_scoreboard_locked():
    """Atomically write scoreboard.txt so FFmpeg drawtext reload=1 never reads a partial file."""
    with _scoreboard_lock:
        home = _scoreboard["home_name"]
        hs = _scoreboard["home_score"]
        away = _scoreboard["away_name"]
        aws = _scoreboard["away_score"]
    with _clock_lock:
        clock_str = _format_clock(_clock_elapsed())

    text = f" {home}  {hs} - {aws}  {away}   |   {clock_str} " if _scoreboard["visible"] and _scoreboard["source"] == "builtin" else ""

    try:
        if Path(SCOREBOARD_PATH).read_text() == text:
            return
    except FileNotFoundError:
        pass
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
    sb["overlay_status"] = browser_overlay.status()
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
        except (ValueError, TypeError) as exc:
            return jsonify(ok=False, error=str(exc)), 400
        except Exception as exc:
            log.exception("Unhandled error in %s", f.__name__)
            return jsonify({"ok": False, "error": "Operation failed; inspect server logs"}), 500
    return wrapper

# ===================================================================
#  ROUTES
# ===================================================================

# --- UI ---------------------------------------------------------------

@app.route("/")
def index():
    """Serve the main dashboard UI."""
    host = urlsplit(request.host_url).hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    return render_template("index.html", media_host=host)


@app.route("/tally/<cam_id>")
def tally_page(cam_id):
    """Mobile operator tally screen."""
    if cam_id not in CAMERA_IDS:
        return jsonify(ok=False, error=f"Unknown camera '{cam_id}'"), 404
    host = urlsplit(request.host_url).hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    return render_template("tally.html", cam_id=cam_id, media_host=host)


@app.route("/api/tally/<cam_id>")
@api_endpoint
def api_tally_status(cam_id):
    """Lightweight tally status for camera operators (no basic auth required)."""
    if cam_id not in CAMERA_IDS:
        return jsonify(ok=False, error=f"Unknown camera '{cam_id}'"), 404
    cameras = get_camera_status()
    stream = get_stream_status()
    publishing = bool(cameras.get(cam_id))
    active_cam = stream.get("active_camera") or stream.get("selected_camera")
    secondary_cam = stream.get("secondary_camera")
    on_air = bool(publishing and active_cam == cam_id)
    secondary = bool(publishing and secondary_cam == cam_id)
    stream_active = bool(stream.get("active") or stream.get("state") == "live")
    return jsonify({
        "ok": True,
        "cam_id": cam_id,
        "name": f"Camera {cam_id[3:]}" if cam_id.startswith("cam") else cam_id,
        "publishing": publishing,
        "on_air": on_air,
        "secondary": secondary,
        "stream_active": stream_active,
        "stream_state": stream.get("state", "stopped"),
    })


# --- Combined status (polled by UI every ~2 s) ------------------------

@app.route("/api/status")
@api_endpoint
def api_status():
    """Return combined network + camera + stream + scoreboard status."""
    network = dict(_diagnostics.get("network", {}))
    cameras = get_camera_status()
    mediamtx = get_mediamtx_status()
    stream = get_stream_status()
    scoreboard = _get_scoreboard_state()
    audio_devices = list(_diagnostics.get("audio_devices", []))

    return jsonify({
        "ok": True,
        "network": network,
        "cameras": cameras,
        "mediamtx": mediamtx,
        "stream": stream,
        "scoreboard": scoreboard,
        "audio_devices": audio_devices,
        "firewall": _diagnostics.get("firewall", {}),
        "diagnostics_updated_at": _diagnostics.get("updated_at"),
        "initialization_error": _initialization_error,
    })


# --- Network -----------------------------------------------------------

@app.route("/api/network/usb-connect", methods=["POST"])
@api_endpoint
def api_usb_connect():
    """Connect the USB tethering interface."""
    log.info("USB connect requested")
    # Auto-detect USB interface from network status
    net = get_network_status(check_internet=False)
    iface = net.get("usb_iface")
    if not iface:
        return jsonify({"ok": False, "message": "No USB tethering interface detected"}), 404
    success, msg = connect_usb(iface)
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg}), status_code


@app.route("/api/network/hotspot-start", methods=["POST"])
@api_endpoint
def api_hotspot_start():
    """Start the Wi-Fi hotspot with the given SSID and password."""
    data = request.get_json(force=True, silent=True) or {}
    ssid = data.get("ssid", "MatchdayLive")
    password = data.get("password", "")

    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
        raise ValueError("SSID must be 1–32 bytes")
    # Empty password: reuse the one saved with the hotspot profile.
    if not isinstance(password, str) or (password and not 8 <= len(password) <= 63):
        return jsonify({"ok": False, "error": "Password must be 8–63 characters"}), 400

    log.info("Hotspot start requested: ssid=%s", ssid)
    success, msg = start_hotspot(ssid, password)
    _refresh_network()
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg}), status_code


@app.route("/api/network/hotspot-config", methods=["GET"])
@api_endpoint
def api_hotspot_config():
    """Saved hotspot SSID and password, to prefill the dashboard form."""
    return jsonify({"ok": True, **get_hotspot_config()})


def _refresh_network():
    """Update cached network diagnostics now, so the dashboard shows the result of a
    hotspot change on its next poll instead of after the next 5 s probe."""
    try:
        network = get_network_status(check_internet=False)
        _diagnostics.update(network=network, firewall=get_firewall_status(network))
    except Exception:
        log.exception("Network refresh failed")


@app.route("/api/network/hotspot-stop", methods=["POST"])
@api_endpoint
def api_hotspot_stop():
    """Stop the Wi-Fi hotspot."""
    log.info("Hotspot stop requested")
    success, msg = stop_hotspot()
    _refresh_network()
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg}), status_code


# --- Stream (YouTube) -------------------------------------------------

@app.route("/api/stream/start", methods=["POST"])
@api_endpoint
def api_stream_start():
    """Start the YouTube live stream encoder pipeline."""
    data = request.get_json(force=True, silent=True) or {}
    stream_key = text_field(data, "stream_key", 200)

    if not stream_key:
        return jsonify({"ok": False, "error": "stream_key is required"}), 400

    log.info("Stream start requested")
    success, msg = start_youtube_stream(stream_key, text_field(data, "profile", 10, "720p"))
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
    if not success and msg == SUPERSEDED:
        # A newer cut (or STOP) replaced this one; the client follows the latest request.
        return jsonify({"ok": False, "superseded": True, "message": msg}), 409
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg, "active_camera": cam_id}), status_code


@app.route("/api/layout", methods=["POST"])
@api_endpoint
def api_layout():
    data = request.get_json()
    mode = text_field(data, "mode", 10)
    main = text_field(data, "main", 10)
    secondary = text_field(data, "secondary", 10, "cam2")
    position = text_field(data, "position", 20, "top-right")
    if mode not in ("single", "pip", "split") or main not in ("cam1", "cam2", "cam3") or secondary not in ("cam1", "cam2", "cam3"):
        raise ValueError("Invalid layout or camera")
    if mode != "single" and main == secondary:
        raise ValueError("Choose different main and second cameras")
    if position not in ("top-left", "top-right", "bottom-left", "bottom-right"):
        raise ValueError("Invalid picture-in-picture position")
    ok, message = set_layout(mode, main, secondary, position)
    return jsonify(ok=ok, message=message, stream=get_stream_status()), 200 if ok else 409 if message == SUPERSEDED else 502


# --- Audio source ------------------------------------------------------

@app.route("/api/audio-source", methods=["POST"])
@api_endpoint
def api_audio_source():
    """Set the audio mode — 'cam1', 'active', or 'usb:<device_id>'."""
    data = request.get_json(force=True, silent=True) or {}
    mode = text_field(data, "mode", 200)

    if mode not in ("cam1", "active") and not (mode.startswith("usb:") and len(mode) > 4):
        return jsonify({
            "ok": False,
            "error": "mode must be 'cam1', 'active', or 'usb:<device_id>'",
        }), 400

    log.info("Audio source change requested: %s", mode)
    success, msg = set_audio_source(mode)
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg, "audio_source": mode}), status_code


@app.route("/api/audio/levels", methods=["GET"])
@api_endpoint
def api_audio_levels():
    """Peak/RMS levels in dBFS for each input (cameras, microphones) and the program
    output. Polled several times a second by the meters, so it only reads cached values."""
    stream = get_stream_status()
    return jsonify({"ok": True, "levels": get_audio_levels(), "mixer": get_mixer(),
                    "audio_on_air": stream.get("audio_on_air", []),
                    "active_camera": stream.get("active_camera"), "live": stream.get("active", False)})


@app.route("/api/audio/mix", methods=["POST"])
@api_endpoint
def api_audio_mix():
    """Mixer: {"channel": "cam1"|"usb:<id>", "on": bool?, "gain": dB?} or {"follow": bool}.
    Gain changes apply live; switching on a source the relay lacks rebuilds it briefly."""
    data = request.get_json(force=True, silent=True) or {}
    if "follow" in data:
        if type(data["follow"]) is not bool:
            raise ValueError("follow must be true or false")
        success, msg = set_follow(data["follow"])
    else:
        channel = text_field(data, "channel", 200)
        on, gain = data.get("on"), data.get("gain")
        if on is not None and type(on) is not bool:
            raise ValueError("on must be true or false")
        if gain is not None and (type(gain) not in (int, float) or not math.isfinite(gain)):
            raise ValueError("gain must be a number of dB")
        if on is None and gain is None:
            raise ValueError("Provide on and/or gain")
        success, msg = set_mix(channel, on=on, gain=gain)
    return jsonify({"ok": success, "message": msg, "mixer": get_mixer()}), 200 if success else 502


@app.route("/api/audio/devices", methods=["GET"])
@api_endpoint
def api_audio_devices():
    """Return a list of detected USB audio devices."""
    devices = get_usb_audio_devices()
    return jsonify({"ok": True, "devices": devices})


# --- Scoreboard --------------------------------------------------------

@app.route("/api/scoreboard/update", methods=["POST"])
@api_endpoint
def api_scoreboard_update():
    """Update team names and/or scores, then rewrite the overlay file."""
    data = request.get_json(force=True, silent=True) or {}

    updates = {}
    if "source" in data:
        if data["source"] not in ("builtin", "score67"):
            raise ValueError("Choose built-in or Score67 scoreboard")
        updates["source"] = data["source"]
    if "overlay_url" in data:
        updates["overlay_url"] = normalize_url(data["overlay_url"])
    if "visible" in data:
        if type(data["visible"]) is not bool:
            raise ValueError("visible must be true or false")
        updates["visible"] = data["visible"]
    for field in ("home_name", "away_name"):
        if field in data:
            # Team names are shown exactly as typed; empty falls back to HOME / AWAY.
            updates[field] = text_field(data, field, 40) or field.split("_")[0].upper()
    for field in ("home_score", "away_score"):
        if field in data:
            value = data[field]
            if type(value) is not int or not 0 <= value <= 999:
                raise ValueError("Scores must be integers between 0 and 999")
            updates[field] = value
    with _scoreboard_lock:
        if "delta" in data:
            if data.get("team") not in ("home", "away") or type(data["delta"]) is not int or data["delta"] not in (-1, 1):
                raise ValueError("Score adjustment requires team and delta (+1 or -1)")
            key = data["team"] + "_score"
            updates[key] = max(0, min(999, _scoreboard[key] + data["delta"]))
        candidate = dict(_scoreboard, **updates)
        if candidate["source"] == "score67" and not candidate["overlay_url"]:
            raise ValueError("Paste the Score67 OBS overlay link first")
        _scoreboard.update(updates)
        browser_overlay.configure(_scoreboard["source"], _scoreboard["visible"], _scoreboard["overlay_url"])

    _write_scoreboard_file()
    _save_match()
    log.info("Scoreboard updated (source=%s, visible=%s)", _scoreboard["source"], _scoreboard["visible"])
    return jsonify({"ok": True, "scoreboard": _get_scoreboard_state()})


@app.route("/api/scoreboard/clock", methods=["POST"])
@api_endpoint
def api_scoreboard_clock():
    """
    Control the match clock.

    Accepts JSON with "action": start | pause | reset | set_half | adjust | set
    For set_half, also accepts "half": 1 or 2
    For adjust, "seconds" to add (negative to subtract); for set, "seconds" of match time.
    adjust and set keep the clock running or paused as it was.
    """
    data = request.get_json(force=True, silent=True) or {}
    action = text_field(data, "action", 20).lower()

    if action not in ("start", "pause", "reset", "set_half", "adjust", "set"):
        return jsonify({
            "ok": False,
            "error": "action must be one of: start, pause, reset, set_half, adjust, set",
        }), 400
    if action in ("adjust", "set"):
        seconds = data.get("seconds")
        low = -3600 if action == "adjust" else 0
        if type(seconds) is not int or not low <= seconds <= 7200:
            raise ValueError(f"seconds must be a whole number between {low} and 7200")

    if action == "set_half" and (type(data.get("half")) is not int or data["half"] not in (1, 2)):
        raise ValueError("Half must be 1 or 2")

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

        elif action in ("adjust", "set"):
            target = max(0.0, (_clock_elapsed() if action == "adjust" else 0.0) + data["seconds"])
            _clock["elapsed"] = target
            if _clock["running"]:
                _clock["start_ts"] = time.monotonic()
            log.info("Match clock %s to %s", "adjusted" if action == "adjust" else "set",
                     _format_clock(target))

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
    _save_match()
    return jsonify({"ok": True, "scoreboard": _get_scoreboard_state()})


@app.route("/api/scoreboard/preview", methods=["GET"])
@api_endpoint
def api_scoreboard_preview():
    return send_file(browser_overlay.path, mimetype="image/png", conditional=False)


@app.route("/api/scoreboard", methods=["GET"])
@api_endpoint
def api_scoreboard_get():
    """Return current scoreboard + clock state."""
    return jsonify({"ok": True, "scoreboard": _get_scoreboard_state()})


# Runtime services are explicit: importing the module starts no processes.
RUNTIME_DIR = Path(__file__).resolve().parent / "data"
MATCH_PATH = RUNTIME_DIR / "match.json"
_workers_stop = threading.Event()
_init_lock = threading.Lock()
_workers = []
_diagnostics = {}
_initialization_error = None

def text_field(data, field, limit, default=""):
    value = data.get(field, default)
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(f"{field} must be text of at most {limit} characters without control characters")
    return value.strip()

def _save_match():
    with _scoreboard_lock:
        state = _get_scoreboard_state()
        MATCH_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=MATCH_PATH.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(state, output)
            os.replace(temporary, MATCH_PATH)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

def _restore_match():
    if not MATCH_PATH.exists():
        return
    try:
        state = json.loads(MATCH_PATH.read_text())
        for field in ("home_name", "away_name"):
            text_field(state, field, 40)
        for field in ("home_score", "away_score"):
            if type(state[field]) is not int or not 0 <= state[field] <= 999:
                raise ValueError("Invalid persisted score")
        elapsed = float(state["clock_elapsed_secs"])
        if not math.isfinite(elapsed) or elapsed < 0 or state["half"] not in (1, 2):
            raise ValueError("Invalid persisted clock")
        state.setdefault("source", "builtin")
        state["overlay_url"] = normalize_url(state.get("overlay_url", ""))
        if state["source"] not in ("builtin", "score67") or (state["source"] == "score67" and not state["overlay_url"]):
            raise ValueError("Invalid persisted scoreboard source")
        state.setdefault("visible", True)  # saved matches from before the visibility control
        if type(state["visible"]) is not bool:
            raise ValueError("Invalid persisted scoreboard visibility")
        with _scoreboard_lock:
            _scoreboard.update({key: state[key] for key in _scoreboard})
            _clock.update(running=False, elapsed=float(state["clock_elapsed_secs"]),
                          start_ts=None, half=state["half"])
    except (ValueError, KeyError, OSError, TypeError):
        log.exception("Cannot restore saved match")

def _overlay_worker():
    ticks = 0
    while not _workers_stop.wait(.5):
        try:
            _write_scoreboard_file()
            ticks += 1
            if ticks % 10 == 0:
                _save_match()
        except OSError:
            log.exception("Cannot update scoreboard")

def _diagnostics_worker():
    global _diagnostics
    while not _workers_stop.is_set():
        try:
            network = get_network_status()
            audio_devices = get_usb_audio_devices()
            set_mic_devices(audio_devices)
            _diagnostics = {"network": network,
                            "audio_devices": audio_devices,
                            "firewall": get_firewall_status(network), "updated_at": time.time()}
        except Exception:
            log.exception("Diagnostics failed")
        _workers_stop.wait(5)

def _background_init():
    global _initialization_error
    try:
        init_mediamtx()
    except Exception as exc:
        _initialization_error = str(exc)
        log.exception("MediaMTX initialization failed")

def initialize():
    RUNTIME_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    password_file = RUNTIME_DIR / "operator-password"
    user_file = RUNTIME_DIR / "operator-user"
    if "MCC_OPERATOR_USER" not in os.environ and user_file.exists():
        app.config["OPERATOR_USER"] = user_file.read_text().strip() or "operator"
    if not app.config["OPERATOR_PASSWORD"]:
        if not password_file.exists():
            fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as output:
                output.write(secrets.token_urlsafe(24))
        app.config["OPERATOR_PASSWORD"] = password_file.read_text().strip()
        log.info("Login as %s; password is in %s", app.config["OPERATOR_USER"], password_file)
    _restore_match()
    browser_overlay.start()
    browser_overlay.configure(_scoreboard["source"], _scoreboard["visible"], _scoreboard["overlay_url"])
    _write_scoreboard_file()
    for target in (_overlay_worker, _diagnostics_worker, _background_init):
        worker = threading.Thread(target=target, daemon=True)
        _workers.append(worker)
        worker.start()

def shutdown():
    _workers_stop.set()
    # Join initialization before cleaning up a process it may be starting.
    for worker in _workers:
        worker.join(timeout=2)
    with _init_lock:
        shutdown_all()
    browser_overlay.stop()
    _save_match()

if __name__ == "__main__":
    from waitress import serve
    initialize()
    atexit.register(shutdown)
    def terminate(signum, frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    serve(app, host=os.environ.get("MCC_BIND", "0.0.0.0"), port=5000, threads=16)
