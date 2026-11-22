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
import recordings
import camera_control
import ai_director

from browser_overlay import BrowserOverlay, normalize_url

browser_overlay = BrowserOverlay()

import atexit
import json
import secrets
import subprocess
import signal
from pathlib import Path
from urllib.parse import quote, urlsplit
from werkzeug.exceptions import HTTPException
from network_manager import (get_network_status, connect_usb, start_hotspot, get_hotspot_config,
                             stop_hotspot, get_usb_audio_devices, get_audio_outputs, get_firewall_status,
                             UPLINK_CONFLICT)
from media_engine import (ensure_mediamtx, start_mediamtx, get_camera_status, CAMERA_USER, camera_password,
                          get_mediamtx_running, get_stream_status, start_stream, stop_stream,
                          make_target, DESTINATIONS, PROFILES, switch_camera, set_audio_source, shutdown_all,
                          get_audio_levels, set_mic_devices, get_mixer, set_mix, set_follow, set_mute,
                          SUPERSEDED, set_layout, CAMERA_IDS, start_recording, stop_recording,
                          get_recording_status, play_replay, stop_replay, get_replay_status,
                          add_replay_listener, REPLAY_MARGIN, set_hardware_encoding, load_encoder_setting,
                          resume_state, resume, set_mic_delay, load_audio_setting, set_gate, set_monitor,
                          start_monitor_worker, get_cameras, set_camera_info, load_camera_setting,
                          REPORT_CAMERAS, get_camera_inputs, QUALITY_LABELS, set_iso_match,
                          load_recording_setting, nvenc_available)

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
        or request.path.startswith("/api/camera-report/")  # phones; checked in the route
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
DEFAULT_TEAM_NAMES = {"home_name": "Home", "away_name": "Guest"}
_scoreboard = {
    "home_name": DEFAULT_TEAM_NAMES["home_name"],
    "home_score": 0,
    "away_name": DEFAULT_TEAM_NAMES["away_name"],
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
    "half_length": 45,     # minutes; the 2nd half starts at this clock time
    "added": 0,            # announced added time of the current half (minutes, 0 = none)
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


def _match_clock_text(elapsed, half, half_length, added=0):
    """Broadcast clock: 32:10; past the end of a half the clock stops at it and the
    stoppage time counts on, as on TV: "45:00 +1:23", with announced added time "(+3)"."""
    end = half_length * 60 * (2 if half == 2 else 1)
    text = _format_clock(elapsed)
    if elapsed >= end + 1:
        over = int(elapsed) - end
        text = f"{_format_clock(end)} +{over // 60}:{over % 60:02d}"
    return text + (f" (+{added})" if added else "")


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
        clock_str = _match_clock_text(_clock_elapsed(), _clock["half"], _clock["half_length"], _clock["added"])

    # Only live relays draw this file: a replay shows the recording's own scoreboard, and
    # the live one returns together with the live picture.
    shown = _scoreboard["visible"] and _scoreboard["source"] == "builtin"
    text = f" {home}  {hs} - {aws}  {away}   |   {clock_str} " if shown else ""

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
        sb["half_length"] = _clock["half_length"]
        sb["added"] = _clock["added"]
        sb["clock_text"] = _match_clock_text(elapsed, _clock["half"], _clock["half_length"], _clock["added"])
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

def _camera_query():
    password = camera_password()
    return f"?user={CAMERA_USER}&pass={quote(password, safe='')}" if password else ""


@app.route("/")
def index():
    """Serve the main dashboard UI."""
    host = urlsplit(request.host_url).hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    destinations = [(name, label, default()) for name, (label, default, _) in DESTINATIONS.items()]
    bitrates = {name: int(video[:-1]) + 128 for name, (_, _, video, _) in PROFILES.items()}
    return render_template("index.html", media_host=host, destinations=destinations, bitrates=bitrates,
                           camera_query=_camera_query(), cameras=get_cameras(),
                           qualities=list(QUALITY_LABELS.items()))


@app.route("/tally/<cam_id>")
def tally_page(cam_id):
    """Mobile operator tally screen."""
    if cam_id not in CAMERA_IDS:
        return jsonify(ok=False, error=f"Unknown camera '{cam_id}'"), 404
    host = urlsplit(request.host_url).hostname or "localhost"
    if ":" in host:
        host = f"[{host}]"
    cam = next(c for c in get_cameras() if c["id"] == cam_id)
    r = cam["recommended"]
    recommended = (f"{r['size']} · {r['fps']} fps · {r['kbps'] / 1000:g} Mbit/s · keyframe {r['keyframe_s']} s · "
                   + ("H.264 or H.265" if cam["protocol"] == "srt" else "H.264"))
    if cam["protocol"] == "srt":
        # Never the stream id with the camera password: this page needs no login.
        stream_url = (f"srt://{host}:{cam['srt_port']}?streamid={quote('publish:' + cam_id, safe='')}"
                      f"&latency={cam['latency_ms'] * 1000}")
    else:
        stream_url = f"rtmp://{host}:1935/{cam_id}"
    # The tally page needs no login: never show a link holding a password or passphrase.
    show_url = not camera_password() and not cam["encrypt"]
    return render_template("tally.html", cam_id=cam_id, title=cam["short"], media_host=host, show_url=show_url,
                           protocol=cam["protocol"], stream_url=stream_url, recommended=recommended)


@app.route("/api/camera-report/<cam_id>", methods=["POST"])
@api_endpoint
def api_camera_report(cam_id):
    """Status from the Chameleon Stream phone app (no operator login); answers with the zoom
    the operator wants. With a camera password set, the phone must send it."""
    if cam_id not in CAMERA_IDS:
        return jsonify(ok=False, error=f"Unknown camera '{cam_id}'"), 404
    password = camera_password()
    if password and not secrets.compare_digest(request.headers.get("X-Camera-Password", "").encode(), password.encode()):
        return jsonify(ok=False, error="Camera password required"), 401
    if request.content_length and request.content_length > 4096:
        return jsonify(ok=False, error="Report too large"), 413
    answer = camera_control.record_report(cam_id, request.get_json(silent=True), tally=lambda: tally_state(cam_id))
    return jsonify(ok=True, **answer)


# --- AI Director ---------------------------------------------------------

@app.route("/api/director")
@api_endpoint
def api_director_status():
    """Scores of the match cameras (AI Director), worker state and setup problems."""
    return jsonify(ok=True, **ai_director.status())


@app.route("/api/director", methods=["POST"])
@api_endpoint
def api_director_configure():
    """{"enabled": true/false, "fps": 2|5|10}"""
    data = request.get_json()
    result = ai_director.configure(enabled=data.get("enabled"), fps=data.get("fps"))
    return jsonify(ok=True, **result)


@app.route("/api/director/fence/<cam_id>", methods=["POST"])
@api_endpoint
def api_director_fence(cam_id):
    """Field fence of a match camera: {"points": [[x, y], ...]} relative 0..1; [] removes it."""
    points = ai_director.set_fence(cam_id, request.get_json().get("points"))
    return jsonify(ok=True, points=points)


@app.route("/api/director/snapshot/<cam_id>")
@api_endpoint
def api_director_snapshot(cam_id):
    """A fresh still of a match camera for drawing its field fence (works while the AI is off)."""
    return send_file(ai_director.snapshot(cam_id), mimetype="image/jpeg", max_age=0)


@app.route("/api/director/image/<cam_id>")
def api_director_image(cam_id):
    """Annotated thumbnail of one match camera (ball and players marked)."""
    path = ai_director.image_path(cam_id)
    if not path:
        return jsonify(ok=False, error="No picture yet"), 404
    return send_file(path, mimetype="image/jpeg", max_age=0)


@app.route("/api/cameras/<cam_id>/zoom", methods=["POST"])
@api_endpoint
def api_camera_zoom(cam_id):
    """Operator zoom for a phone camera: {"zoom": 2.0} | {"step": 1 / -1} | {"wide": true}."""
    if cam_id not in CAMERA_IDS:
        return jsonify(ok=False, error=f"Unknown camera '{cam_id}'"), 404
    data = request.get_json()
    target = camera_control.request_zoom(cam_id, zoom=data.get("zoom"), step=data.get("step"),
                                         wide=data.get("wide") is True)
    return jsonify(ok=True, zoom=target)


def _tally(cam_id):
    """Tally of one camera: what its operator needs to know (tally page and phone app)."""
    cameras = get_camera_status()
    stream = get_stream_status()
    publishing = bool(cameras.get(cam_id))
    # Only the camera actually in the program picture counts (not one merely selected while
    # the fallback card is on air), and only while the program goes out (stream or recording).
    active_cam = stream.get("active_camera")
    secondary_cam = stream.get("secondary_camera")
    replay = stream.get("replay", {}).get("active", False)
    # A short uplink drop is still a live broadcast: the operator keeps framing.
    stream_active = bool(stream.get("active") or stream.get("state") in ("live", "reconnecting"))
    going_out = stream_active or bool((stream.get("recording") or {}).get("active"))
    in_program = bool(publishing and active_cam == cam_id and not replay)
    on_air = in_program and going_out
    # During a replay the camera it returns to is seconds from being live again.
    returning = bool(replay and publishing and going_out and stream.get("replay", {}).get("camera") == cam_id)
    selected = in_program and not going_out
    secondary = bool(publishing and secondary_cam == cam_id and going_out)
    return {
        "cam_id": cam_id,
        "name": next(c["label"] for c in get_cameras() if c["id"] == cam_id),
        "role": "report" if cam_id in REPORT_CAMERAS else "match",
        "publishing": publishing,
        "on_air": on_air,
        "selected": selected,
        "secondary": secondary,
        "stream_active": stream_active,
        "stream_state": stream.get("state", "stopped"),
        "replay": replay,
        "returning": returning,
        "replay_hold": bool(replay and stream.get("replay", {}).get("hold")),
    }


def tally_state(cam_id):
    """One word for the phone app: live | returning | secondary | selected | "" (not in the picture)."""
    t = _tally(cam_id)
    for key, word in (("on_air", "live"), ("returning", "returning"), ("secondary", "secondary"), ("selected", "selected")):
        if t[key]:
            return word
    return ""


@app.route("/api/tally/<cam_id>")
@api_endpoint
def api_tally_status(cam_id):
    """Lightweight tally status for camera operators (no basic auth required)."""
    if cam_id not in CAMERA_IDS:
        return jsonify(ok=False, error=f"Unknown camera '{cam_id}'"), 404
    return jsonify(ok=True, **_tally(cam_id))


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
        "camera_list": get_cameras(),
        "camera_inputs": get_camera_inputs(),
        "camera_reports": camera_control.get_reports(),
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

    force = data.get("force", False)
    if type(force) is not bool:
        return jsonify({"ok": False, "error": "force must be true or false"}), 400
    log.info("Hotspot start requested: ssid=%s%s", ssid, " (taking over the internet Wi-Fi)" if force else "")
    success, msg = start_hotspot(ssid, password, force=force)
    if msg == UPLINK_CONFLICT:  # the dashboard asks the operator, then retries with force
        return jsonify({"ok": False, "confirm": True, "message": msg}), 409
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
    """GO LIVE: {"destinations": {"youtube"|"facebook"|"twitch"|"custom": {"key", "server"?}},
    "profile", "record"}. "stream_key" alone still means YouTube. While live, only the
    destinations that are not running yet are added."""
    data = request.get_json(force=True, silent=True) or {}
    destinations = data.get("destinations")
    if destinations is None and "stream_key" in data:
        destinations = {"youtube": {"key": data["stream_key"]}}
    if not isinstance(destinations, dict) or not destinations:
        return jsonify({"ok": False, "error": "Choose at least one streaming destination"}), 400
    targets = {}
    for name, settings in destinations.items():
        if not isinstance(settings, dict):
            raise ValueError("Each destination needs a key and optional server")
        targets[name] = make_target(name, text_field(settings, "key", 300), text_field(settings, "server", 300) or None)
    record, remember = data.get("record", False), data.get("remember")
    if type(record) is not bool:
        raise ValueError("record must be true or false")
    if remember is not None and type(remember) is not bool:
        raise ValueError("remember must be true or false")
    log.info("Stream start requested (%s, record=%s)", ", ".join(targets), record)
    success, msg = start_stream(targets, text_field(data, "profile", 10, "720p"),
                                record=record, title=_match_title())
    if success and remember:
        _remember_keys({name: settings["key"] for name, settings in destinations.items() if settings.get("key")})
    elif remember is False:
        _forget_keys()
    status_code = 200 if success else 502
    return jsonify({"ok": success, "message": msg}), status_code


# Stream keys are kept on this laptop (data/ is private, owner-only file) so the operator
# does not paste them again every match; only the logged-in dashboard can read them.
def _keys_path():
    return RUNTIME_DIR / "stream-keys.json"


def _stored_keys():
    try:
        keys = json.loads(_keys_path().read_text())
        return {k: v for k, v in keys.items() if isinstance(v, str)} if isinstance(keys, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_keys(keys):
    if not keys:
        return
    stored = {**_stored_keys(), **keys}
    path = _keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # created 0600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(stored, f)
        os.replace(tmp, path)
    except OSError:
        Path(tmp).unlink(missing_ok=True)
        log.exception("Cannot save the stream keys")


def _forget_keys():
    _keys_path().unlink(missing_ok=True)


@app.route("/api/stream/keys", methods=["GET"])
@api_endpoint
def api_stream_keys():
    keys = _stored_keys()
    return jsonify({"ok": True, "remembered": bool(keys), "keys": keys})


@app.route("/api/stream/keys/forget", methods=["POST"])
@api_endpoint
def api_stream_keys_forget():
    _forget_keys()
    log.info("Saved stream keys removed")
    return jsonify({"ok": True, "message": "Saved stream keys removed from this laptop"})


@app.route("/api/stream/stop", methods=["POST"])
@api_endpoint
def api_stream_stop():
    """Stop streaming everywhere, or one destination with {"destination": name}."""
    data = request.get_json(force=True, silent=True) or {}
    name = text_field(data, "destination", 20) or None
    log.info("Stream stop requested (%s)", name or "all destinations")
    success, msg = stop_stream(name)
    status_code = 200 if success else 409 if name else 502
    return jsonify({"ok": success, "message": msg}), status_code


# --- Recording & highlights --------------------------------------------

def _match_title():
    with _scoreboard_lock:
        return f"{_scoreboard['home_name']} vs {_scoreboard['away_name']}"


def _match_moment():
    """Match clock and score for a highlight, e.g. ("23:41", "Home 1-0 Guest")."""
    with _scoreboard_lock:
        sb = dict(_scoreboard)
        clock = _format_clock(_clock_elapsed())
    return clock, f"{sb['home_name']} {sb['home_score']}-{sb['away_score']} {sb['away_name']}"


def _auto_goal_marker(team, delta):
    session = get_recording_status()
    if not session["active"]:
        return
    try:
        if delta > 0:
            clock, score = _match_moment()
            recordings.add_marker(session["session"], "Goal", clock, score, auto=team)
        else:
            recordings.withdraw_auto_goal(session["session"], team)
    except (ValueError, OSError):
        log.exception("Cannot update the goal highlight")


@app.route("/api/recording/start", methods=["POST"])
@api_endpoint
def api_recording_start():
    """Record the finished program (with scoreboard) to this laptop; YouTube optional."""
    data = request.get_json(force=True, silent=True) or {}
    log.info("Recording start requested")
    success, msg = start_recording(text_field(data, "profile", 10, "720p"), title=_match_title())
    return jsonify({"ok": success, "message": msg}), 200 if success else 502


@app.route("/api/recording/stop", methods=["POST"])
@api_endpoint
def api_recording_stop():
    log.info("Recording stop requested")
    success, msg = stop_recording()
    return jsonify({"ok": success, "message": msg}), 200 if success else 409


@app.route("/api/markers", methods=["POST"])
@api_endpoint
def api_marker_add():
    """Mark a highlight now on the running recording: {"label": "Highlight"|"Goal"|...}."""
    data = request.get_json(force=True, silent=True) or {}
    session = get_recording_status()
    if not session["active"]:
        return jsonify({"ok": False, "error": "Start recording to mark highlights"}), 409
    clock, score = _match_moment()
    marker = recordings.add_marker(session["session"], text_field(data, "label", 20, "Highlight"), clock, score)
    return jsonify({"ok": True, "message": f"⭐ {marker['label']} marked at {clock}", "marker": marker})


@app.route("/api/recordings", methods=["GET"])
@api_endpoint
def api_recordings():
    status = get_recording_status()
    return jsonify({"ok": True, "recordings": recordings.list_sessions(live_wall=time.time()),
                    "free_bytes": recordings.free_bytes(), "labels": list(recordings.LABELS),
                    "recordable_bytes": recordings.room_left(status["session"] if status["active"] else None),
                    "recording": status})


@app.route("/api/recording/match-cameras", methods=["POST"])
@api_endpoint
def api_iso_match():
    """{"on": bool}: also record every match camera on its own (material for automatic cutting)."""
    data = request.get_json(force=True, silent=True) or {}
    if type(data.get("on")) is not bool:
        raise ValueError("on must be true or false")
    ok, message = set_iso_match(data["on"])
    return jsonify(ok=ok, message=message, recording=get_recording_status())


@app.route("/recordings/<sid>/cuts.csv", methods=["GET"])
def recording_cuts(sid):
    """The session's cut log: what was on air when, chosen by whom."""
    try:
        body = recordings.cuts_csv(sid)
    except ValueError:
        return jsonify({"ok": False, "error": "Unknown recording"}), 404
    return app.response_class(body, mimetype="text/csv",
                              headers={"Content-Disposition": f'attachment; filename="{sid}-cuts.csv"'})


@app.route("/api/recordings/<sid>/markers/<mid>", methods=["POST"])
@api_endpoint
def api_marker_update(sid, mid):
    data = request.get_json(force=True, silent=True) or {}
    if data.get("delete") is True:
        recordings.delete_marker(sid, mid)
        return jsonify({"ok": True, "message": "Highlight removed"})
    marker = recordings.update_marker(sid, mid, text_field(data, "label", 20))
    return jsonify({"ok": True, "message": "Highlight updated", "marker": marker})


@app.route("/api/recordings/<sid>/markers/<mid>/clip", methods=["POST"])
@api_endpoint
def api_marker_clip(sid, mid):
    name = recordings.export_clip(sid, mid)
    return jsonify({"ok": True, "message": "Clip ready", "clip": name})


@app.route("/api/recordings/<sid>/reel", methods=["POST"])
@api_endpoint
def api_recording_reel(sid):
    name = recordings.build_reel(sid)
    return jsonify({"ok": True, "message": "Highlight reel ready", "file": name})


@app.route("/api/recordings/<sid>/finalize", methods=["POST"])
@api_endpoint
def api_recording_finalize(sid):
    if get_recording_status()["session"] == sid:
        return jsonify({"ok": False, "error": "Stop the recording first"}), 409
    recordings.finalize_async(sid)
    return jsonify({"ok": True, "message": "Saving the recording…"})


@app.route("/api/recordings/<sid>/delete", methods=["POST"])
@api_endpoint
def api_recording_delete(sid):
    if get_recording_status()["session"] == sid:
        return jsonify({"ok": False, "error": "Stop the recording first"}), 409
    recordings.delete_session(sid)
    return jsonify({"ok": True, "message": "Recording deleted"})


@app.route("/recordings/<sid>/<path:name>", methods=["GET"])
def recording_file(sid, name):
    """Recorded files: played in the dashboard, or downloaded with ?download=1."""
    try:
        path = recordings.file_path(sid, name)
    except ValueError:
        return jsonify({"ok": False, "error": "Unknown file"}), 404
    return send_file(path, mimetype="video/mp2t" if path.suffix == ".ts" else "video/mp4",
                     as_attachment=request.args.get("download") == "1",
                     download_name=path.name, conditional=True, max_age=0)


@app.route("/api/replay", methods=["POST"])
@api_endpoint
def api_replay():
    """Instant replay from the running recording: {"seconds": 3-30, "speed": 1|0.5}
    for the last seconds, or {"marker": "<id>"} around a highlight (both return to live
    by themselves), or CAM REPLAY {"start": timeline seconds, "speed", "hold": true},
    on air until a cut to a camera."""
    data = request.get_json(force=True, silent=True) or {}
    seconds, speed, marker = data.get("seconds", 10), data.get("speed", 1), data.get("marker")
    start, hold = data.get("start"), data.get("hold", False)
    if type(seconds) not in (int, float) or type(speed) not in (int, float):
        raise ValueError("seconds and speed must be numbers")
    if start is not None and (type(start) not in (int, float) or not math.isfinite(start) or start < 0):
        raise ValueError("start must be a position in seconds")
    if marker is not None and type(marker) is not str:
        raise ValueError("marker must be a highlight id")
    if type(hold) is not bool:
        raise ValueError("hold must be true or false")
    log.info("Replay requested (seconds=%s, speed=%s, marker=%s, start=%s, hold=%s)", seconds, speed, marker, start, hold)
    success, msg = play_replay(float(seconds), float(speed), marker=marker,
                               start=None if start is None else float(start), hold=hold)
    if not success and msg == SUPERSEDED:
        return jsonify({"ok": True, "message": "Replaced by a newer action"})
    return jsonify({"ok": success, "message": msg}), 200 if success else 409


@app.route("/api/replay/timeline", methods=["GET"])
@api_endpoint
def api_replay_timeline():
    """CAM REPLAY timeline: how much of the running recording can be replayed, and its highlights."""
    session = get_recording_status()
    if not session["active"]:
        return jsonify({"ok": True, "available": False})
    info = recordings.get(session["session"])
    edge = max(0.0, recordings.written_duration(session["session"]) - REPLAY_MARGIN)
    return jsonify({"ok": True, "available": True, "session": session["session"], "edge": round(edge, 1),
                    "markers": [{k: m[k] for k in ("id", "label", "offset", "time", "clock")} for m in info["markers"]]})


@app.route("/api/replay/frame", methods=["GET"])
def api_replay_frame():
    """JPEG still of the running recording at ?t=<timeline seconds> (CAM REPLAY preview)."""
    session = get_recording_status()
    try:
        offset = float(request.args.get("t", ""))
        if not session["active"] or not math.isfinite(offset):
            raise ValueError
        image = recordings.frame_at(session["session"], offset)
    except (ValueError, OSError, subprocess.SubprocessError):
        return jsonify({"ok": False, "error": "No picture at that moment yet"}), 404
    return app.response_class(image, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})


@app.route("/api/replay/stop", methods=["POST"])
@api_endpoint
def api_replay_stop():
    success, msg = stop_replay()
    return jsonify({"ok": success, "message": msg}), 200 if success else 409


def _replay_changed(active):
    """Hide the Score67 overlay while a replay (with its own scoreboard) is on air. The
    built-in scoreboard needs nothing: replay relays do not draw it."""
    browser_overlay.suspend(active)


add_replay_listener(_replay_changed)


# --- Camera switching --------------------------------------------------

@app.route("/api/switch/<cam_id>", methods=["POST"])
@api_endpoint
def api_switch_camera(cam_id):
    """Switch the active (on-air) camera to cam_id. Optional JSON {"source": "auto"} marks
    a cut chosen by an auto-director (match cameras only; never right after a manual cut)."""
    if cam_id not in CAMERA_IDS:
        return jsonify({"ok": False, "error": f"Invalid camera id '{cam_id}'. Use one of {CAMERA_IDS}"}), 400
    data = request.get_json(force=True, silent=True) or {}
    source = text_field(data, "source", 10, "manual")

    log.info("Camera switch requested: %s (%s)", cam_id, source)
    success, msg = switch_camera(cam_id, source=source)
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
    secondary = text_field(data, "secondary", 10, REPORT_CAMERAS[0])
    position = text_field(data, "position", 20, "top-right")
    if mode not in ("single", "pip", "split") or main not in CAMERA_IDS or secondary not in CAMERA_IDS:
        raise ValueError("Invalid layout or camera")
    if mode != "single" and main == secondary:
        raise ValueError("Choose different main and second cameras")
    if mode != "single" and main not in REPORT_CAMERAS and secondary not in REPORT_CAMERAS:
        raise ValueError("Two pictures need a reporting camera: match cameras are shown one at a time")
    if position not in ("top-left", "top-right", "bottom-left", "bottom-right"):
        raise ValueError("Invalid picture-in-picture position")
    ok, message = set_layout(mode, main, secondary, position)
    return jsonify(ok=ok, message=message, stream=get_stream_status()), 200 if ok else 409 if message == SUPERSEDED else 502


@app.route("/api/cameras/<cam_id>", methods=["POST"])
@api_endpoint
def api_camera_info(cam_id):
    """Camera settings: label (name), position (e.g. "Left goal"), protocol (rtmp/srt),
    latency_ms (SRT delay), encrypt (SRT passphrase on/off), new_passphrase, quality."""
    if cam_id not in CAMERA_IDS:
        return jsonify(ok=False, error=f"Unknown camera '{cam_id}'"), 404
    data = request.get_json(force=True, silent=True) or {}
    fields = {}
    if "label" in data:
        fields["label"] = text_field(data, "label", 24)
    if "position" in data:
        fields["position"] = text_field(data, "position", 40)
    for name in ("protocol", "quality", "mount"):
        if name in data:
            fields[name] = text_field(data, name, 10)
    if "latency_ms" in data:
        if type(data["latency_ms"]) is not int:
            raise ValueError("latency_ms must be a whole number of milliseconds")
        fields["latency_ms"] = data["latency_ms"]
    for name in ("encrypt", "new_passphrase", "wide"):
        if name in data:
            if type(data[name]) is not bool:
                raise ValueError(f"{name} must be true or false")
            fields[name] = data[name]
    ok, message = set_camera_info(cam_id, **fields)
    return jsonify(ok=ok, message=message, cameras=get_cameras()), 200 if ok else 400


# --- Audio source ------------------------------------------------------

@app.route("/api/audio-source", methods=["POST"])
@api_endpoint
def api_audio_source():
    """Set the audio mode — 'off', 'cam1', 'active', or 'usb:<device_id>'."""
    data = request.get_json(force=True, silent=True) or {}
    mode = text_field(data, "mode", 200)

    if mode not in ("off", "cam1", "active") and not (mode.startswith("usb:") and len(mode) > 4):
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


@app.route("/api/audio/mute", methods=["POST"])
@api_endpoint
def api_audio_mute():
    """Master mute {"muted": bool}: silences every source live, keeping the mixer."""
    data = request.get_json(force=True, silent=True) or {}
    if type(data.get("muted")) is not bool:
        raise ValueError("muted must be true or false")
    log.info("Audio %s requested", "mute" if data["muted"] else "unmute")
    success, msg = set_mute(data["muted"])
    return jsonify({"ok": success, "message": msg, "mixer": get_mixer()}), 200 if success else 502


@app.route("/api/audio/mic-delay", methods=["POST"])
@api_endpoint
def api_audio_mic_delay():
    """{"ms": 0-3000}: delay USB microphones to match the camera picture."""
    data = request.get_json(force=True, silent=True) or {}
    log.info("Microphone delay %s ms requested", data.get("ms"))
    success, msg = set_mic_delay(data.get("ms"))
    return jsonify({"ok": success, "message": msg, "mixer": get_mixer()}), 200 if success else 400


@app.route("/api/audio/gate", methods=["POST"])
@api_endpoint
def api_audio_gate():
    """{"on": bool, "threshold_db": -70..-10}: noise gate on the microphones."""
    data = request.get_json(force=True, silent=True) or {}
    log.info("Noise gate %s requested", data)
    success, msg = set_gate(data.get("on"), data.get("threshold_db"))
    return jsonify({"ok": success, "message": msg, "mixer": get_mixer()}), 200 if success else 400


@app.route("/api/audio/outputs", methods=["GET"])
@api_endpoint
def api_audio_outputs():
    """Sound outputs (headsets, headphones, speakers) the monitor can play on."""
    return jsonify({"ok": True, "outputs": get_audio_outputs()})


@app.route("/api/audio/monitor", methods=["POST"])
@api_endpoint
def api_audio_monitor():
    """{"on": bool, "mode": "nomic"|"full", "output": sink}: listen in the headset."""
    data = request.get_json(force=True, silent=True) or {}
    log.info("Headset monitor %s requested", {k: data.get(k) for k in ("on", "mode", "output")})
    success, msg = set_monitor(data.get("on"), data.get("mode"), data.get("output"))
    return jsonify({"ok": success, "message": msg, "mixer": get_mixer()}), 200 if success else 400


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
            # Team names are shown exactly as typed; empty falls back to Home / Guest.
            updates[field] = text_field(data, field, 40) or DEFAULT_TEAM_NAMES[field]
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
    if "delta" in data:
        _auto_goal_marker(data["team"], data["delta"])
    log.info("Scoreboard updated (source=%s, visible=%s)", _scoreboard["source"], _scoreboard["visible"])
    return jsonify({"ok": True, "scoreboard": _get_scoreboard_state()})


@app.route("/api/scoreboard/clock", methods=["POST"])
@api_endpoint
def api_scoreboard_clock():
    """
    Control the match clock.

    Accepts JSON with "action": start | pause | reset | set_half | adjust | set
    For set_half, also accepts "half": 1 or 2 and optional "minutes" (half length, 10-60)
    set_half_length: "minutes" (10-60) without touching the clock.
    set_added: "minutes" (0-15) of announced added time for the current half (0 hides it).
    For adjust, "seconds" to add (negative to subtract); for set, "seconds" of match time.
    adjust and set keep the clock running or paused as it was.
    """
    data = request.get_json(force=True, silent=True) or {}
    action = text_field(data, "action", 20).lower()

    if action not in ("start", "pause", "reset", "set_half", "set_half_length", "set_added", "adjust", "set"):
        return jsonify({
            "ok": False,
            "error": "action must be one of: start, pause, reset, set_half, set_half_length, set_added, adjust, set",
        }), 400
    minutes = data.get("minutes")
    if action == "set_half_length" or (action == "set_half" and minutes is not None):
        if type(minutes) is not int or not 10 <= minutes <= 60:
            raise ValueError("Half length must be a whole number of minutes between 10 and 60")
    if action == "set_added" and (type(minutes) is not int or not 0 <= minutes <= 15):
        raise ValueError("Added time must be a whole number of minutes between 0 and 15")
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
            _clock["half"], _clock["added"] = 1, 0
            log.info("Match clock reset to 00:00")

        elif action in ("adjust", "set"):
            target = max(0.0, (_clock_elapsed() if action == "adjust" else 0.0) + data["seconds"])
            _clock["elapsed"] = target
            if _clock["running"]:
                _clock["start_ts"] = time.monotonic()
            log.info("Match clock %s to %s", "adjusted" if action == "adjust" else "set",
                     _format_clock(target))

        elif action == "set_half_length":
            _clock["half_length"] = minutes
            log.info("Half length set to %d min", minutes)

        elif action == "set_added":
            _clock["added"] = minutes
            log.info("Added time: %d min", minutes)

        elif action == "set_half":
            if minutes is not None:
                _clock["half_length"] = minutes
            half = data.get("half", 2)
            try:
                half = int(half)
            except (ValueError, TypeError):
                half = 2
            _clock["half"], _clock["added"] = half, 0
            if half == 2:
                _clock["elapsed"] = _clock["half_length"] * 60.0
                _clock["running"] = False
                _clock["start_ts"] = None
                log.info("Match clock set to %d:00 for 2nd half", _clock["half_length"])
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
@app.route("/api/encoder", methods=["POST"])
@api_endpoint
def api_encoder():
    """{"encoder": "software"|"intel"|"nvidia"} (or the older {"hardware": bool} = intel/software)."""
    data = request.get_json(force=True, silent=True) or {}
    if "encoder" in data:
        kind = text_field(data, "encoder", 10)
    elif type(data.get("hardware")) is bool:
        kind = "intel" if data["hardware"] else "software"
    else:
        raise ValueError("encoder must be software, intel or nvidia")
    log.info("Encoder requested: %s", kind)
    success, msg = set_hardware_encoding(kind)
    return jsonify({"ok": success, "message": msg, "encoder": get_stream_status()["encoder"]}), 200 if success else 409


RUNTIME_DIR = Path(__file__).resolve().parent / "data"
MATCH_PATH = RUNTIME_DIR / "match.json"
CLOCK_RESUME_MAX = 600  # seconds of downtime after which a running clock is not resumed
# A saved match older than this is a finished one: the next start begins with Home 0:0 Guest
# and a clock at 00:00. Within it (crash, laptop restart on match day) everything continues.
MATCH_RESTORE_MAX = 6 * 3600
_MATCH_FIELDS = ("home_name", "home_score", "away_name", "away_score", "half", "clock_elapsed_secs")
_match_changed = {"at": None, "content": None}  # last change of names, score or clock
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
        now = time.time()
        state["saved_at"] = now  # lets a restart continue a running clock
        # changed_at = when the match itself last changed (the file is rewritten every 5 s).
        content = tuple(state.get(k) for k in _MATCH_FIELDS)
        if _match_changed["at"] is None or content != _match_changed["content"] or state.get("clock_running"):
            _match_changed["at"] = now
        _match_changed["content"] = content
        state["changed_at"] = _match_changed["at"]
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
        changed_at = state.get("changed_at", state.get("saved_at"))  # files before changed_at: saved_at
        fresh = type(changed_at) in (int, float) and math.isfinite(changed_at) and 0 <= time.time() - changed_at <= MATCH_RESTORE_MAX
        half_length = state.get("half_length", 45)  # saved before half length existed
        if type(half_length) is not int or not 10 <= half_length <= 60:
            half_length = 45
        with _scoreboard_lock:
            if not fresh:
                # Old match: keep only the setup (scoreboard source, overlay, visibility, half length).
                _scoreboard.update({key: state[key] for key in ("source", "overlay_url", "visible")})
                _clock["half_length"] = half_length
                log.info("Saved match is older than %d h: starting with %s 0:0 %s", MATCH_RESTORE_MAX // 3600,
                         _scoreboard["home_name"], _scoreboard["away_name"])
                return
            _scoreboard.update({key: state[key] for key in _scoreboard})
            # Keep the restored age: an untouched match must not look new after each restart.
            _match_changed["at"] = changed_at
            _match_changed["content"] = tuple(state.get(k) for k in _MATCH_FIELDS)
            # A clock that was running keeps running through a crash or restart: the match
            # went on. After a long gap (the laptop was put away) it stays paused instead.
            saved_at, gap = state.get("saved_at"), None
            if state.get("clock_running") is True and type(saved_at) in (int, float) and math.isfinite(saved_at):
                gap = time.time() - saved_at
            resume = gap is not None and 0 <= gap <= CLOCK_RESUME_MAX
            _clock.update(running=resume, elapsed=elapsed + (gap if resume else 0.0),
                          start_ts=time.monotonic() if resume else None,
                          half=state["half"], half_length=half_length,
                          added=state["added"] if type(state.get("added")) is int and 0 <= state["added"] <= 15 else 0)
            if resume:
                log.info("Match clock resumed at %s after a %.0f s restart", _format_clock(_clock["elapsed"]), gap)
    except (ValueError, KeyError, OSError, TypeError):
        log.exception("Cannot restore saved match")

def _scoreboard_wait():
    """Seconds until the scoreboard text is written again. A running clock is written just
    after each of its seconds begins, so every second stays on screen equally long."""
    with _clock_lock:
        if not _clock["running"]:
            return .5
        return min(.5, 1 - _clock_elapsed() % 1 + .02)


def _overlay_worker():
    saved = time.monotonic()
    while not _workers_stop.wait(_scoreboard_wait()):
        try:
            _write_scoreboard_file()
            if time.monotonic() - saved >= 5:
                saved = time.monotonic()
                _save_match()
                _save_live_state()
        except OSError:
            log.exception("Cannot update scoreboard")


# --- Resume after a crash or restart -------------------------------------
# While live or recording, what is needed to continue is saved every 5 s. STOP (nothing
# wanted any more) removes it; a crash, kill or logout leaves it, and the next start
# within RESUME_MAX continues the broadcast by itself.
RESUME_MAX = 600
_resume_pending = None


def _live_state_path():
    return RUNTIME_DIR / "live-state.json"


def _save_live_state():
    state = resume_state()
    path = _live_state_path()
    if state is None:
        if _resume_pending is None:  # not while a resume is still to come
            path.unlink(missing_ok=True)
        return
    state["saved_at"] = time.time()
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")  # 0600: holds stream keys
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(state, output)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_resume():
    """The broadcast to continue, if the app went down while live within RESUME_MAX."""
    try:
        state = json.loads(_live_state_path().read_text())
        age = time.time() - float(state["saved_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not 0 <= age <= RESUME_MAX or not isinstance(state, dict):
        log.info("Not resuming the broadcast: it ended %.0f s ago", age)
        _live_state_path().unlink(missing_ok=True)
        return None
    return state


def _resume_broadcast(state, wait=90):
    """Wait for MediaMTX and a camera, then go live again."""
    global _resume_pending
    try:
        deadline = time.monotonic() + wait
        while not _workers_stop.is_set() and time.monotonic() < deadline:
            cameras = get_camera_status() if get_mediamtx_running() else {}
            if any(cameras.get(c) for c in CAMERA_IDS):
                break
            _workers_stop.wait(1)
        if _workers_stop.is_set():
            return
        ok, message = resume(state, title=_match_title())
        log.log(logging.INFO if ok else logging.WARNING, "Resuming the broadcast after a restart: %s", message)
        if not ok and state.get("session"):
            recordings.finalize(state["session"])
    except Exception:
        log.exception("Resuming the broadcast failed")
    finally:
        _resume_pending = None

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

def _recover_recordings():
    """A recording interrupted by a crash or restart is joined into its MP4 now, unless
    the broadcast resumes: then it continues in the same session."""
    resuming = (_resume_pending or {}).get("session") if (_resume_pending or {}).get("record") else None
    try:
        recordings.recover()
        for item in recordings.list_sessions():
            if item["state"] == "unfinished" and item["id"] != resuming:
                recordings.finalize(item["id"])
    except Exception:
        log.exception("Recording recovery failed")
    if _resume_pending:
        _resume_broadcast(_resume_pending)

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
    global _resume_pending
    _resume_pending = _load_resume()
    if _resume_pending:
        log.info("The app stopped while live; resuming the broadcast once a camera is back")
    load_audio_setting()
    load_camera_setting()
    load_recording_setting()
    start_monitor_worker()  # plays in the headset once MediaMTX runs, if listening was on
    ai_director.autostart()  # rates the match cameras if it was switched on last time
    threading.Thread(target=load_encoder_setting, daemon=True).start()  # test-encodes: not on the start path
    threading.Thread(target=_recover_recordings, daemon=True).start()
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
    ai_director.stop()
    browser_overlay.stop()
    _save_match()

if __name__ == "__main__":
    from waitress import serve
    initialize()
    atexit.register(shutdown)
    def terminate(signum, frame):
        if signum == signal.SIGINT:  # Ctrl+C is a deliberate quit: do not resume next start
            _live_state_path().unlink(missing_ok=True)
            globals()["_resume_pending"] = None
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    # Logging out closes the terminal: SIGHUP used to kill the app without saving anything.
    signal.signal(signal.SIGHUP, terminate)
    serve(app, host=os.environ.get("MCC_BIND", "0.0.0.0"), port=5000, threads=16)
