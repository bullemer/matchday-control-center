"""Supervised local media pipeline. All control mutations share one lock."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent
MEDIAMTX_VERSION = "1.21.2"
MEDIAMTX_BIN = str(BASE / "mediamtx")
MEDIAMTX_CONFIG = str(BASE / "data/mediamtx.yml")
MEDIAMTX_API = "http://127.0.0.1:9997"
RTMP_BASE = "rtmp://127.0.0.1:1935"
RTSP_BASE = "rtsp://127.0.0.1:8554"
YOUTUBE_RTMP_BASE = "rtmps://a.rtmps.youtube.com:443/live2"
SCOREBOARD_PATH = str(BASE / "static/scoreboard.txt")
CAMERA_IDS = ["cam1", "cam2", "cam3"]
_lock = threading.RLock()
# Serializes relay replacement. Slow work (probe, waiting for frames) happens under this
# lock only, never under _lock, so status, STOP and the supervisor stay responsive.
_switch_lock = threading.Lock()
_generation = 0          # bumped by each operator cut and by STOP; stale cut attempts discard their result
_pending_camera = None
_stop = threading.Event()
_thread = None
_mediamtx_proc = _youtube_proc = _relay_proc = None
_active_camera = None
_audio_source_mode = "cam1"
_relay_audio = None      # audio actually on program: "cam1", "usb:<id>", "camera" or "silence"
_audio_warning = ""
_relay_failures = 0
_relay_retry_at = 0.0
_restore_check_at = 0.0
_mediamtx_retry_at = 0.0
_youtube_stream_key = None
_want_stream = False
_state = "stopped"
_error = ""
_started_at = None
_retry_at = 0.0
_retries = 0
_camera_status = {c: False for c in CAMERA_IDS + ["program"]}
_profile = "720p"
PROFILES = {"720p": (1280, 720, "4500k"), "480p": (854, 480, "1800k")}


# Media processes run in their own process group (so a cut can kill ffmpeg cleanly) and
# would therefore outlive a crashed control app, still streaming to YouTube. This wrapper
# leads the group: it terminates the child when its stdin pipe from the app closes (the app
# exited, however it died), and the child gets SIGKILL if the wrapper itself dies.
_WATCHDOG = r"""
import ctypes, signal, subprocess, sys, threading
def setup():
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    ctypes.CDLL(None).prctl(1, signal.SIGKILL, 0, 0, 0)  # PR_SET_PDEATHSIG
signal.signal(signal.SIGTERM, signal.SIG_IGN)  # a group SIGTERM stops the child; then exit with it
child = subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL, preexec_fn=setup)
def orphaned():
    sys.stdin.buffer.read()
    child.terminate()
    try:
        child.wait(3)
    except subprocess.TimeoutExpired:
        child.kill()
threading.Thread(target=orphaned, daemon=True).start()
sys.exit(child.wait())
"""


class Process:
    """Drain both pipes continuously; retain only a bounded, redacted error tail."""
    def __init__(self, cmd, secret=None, cwd=None):
        if not shutil.which(cmd[0]):
            raise FileNotFoundError(cmd[0])
        self.proc = subprocess.Popen([sys.executable, "-I", "-c", _WATCHDOG, *cmd],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True,
                                     start_new_session=True, bufsize=1, cwd=cwd)
        self.last_progress = 0.0
        self.frame = 0
        self.error = ""
        self.launched = time.monotonic()
        self.secret = secret
        for pipe, progress in [(self.proc.stdout, True), (self.proc.stderr, False)]:
            threading.Thread(target=self._read, args=(pipe, progress), daemon=True).start()

    def _read(self, pipe, progress):
        try:
            for line in pipe:
                line = line.strip()
                if progress and line.startswith("frame="):
                    try:
                        frame = int(line.split("=", 1)[1])
                        if frame > self.frame:
                            self.last_progress = time.monotonic()
                        self.frame = frame
                    except ValueError:
                        pass
                elif not progress and line:
                    if self.secret:
                        line = line.replace(self.secret, "[REDACTED]")
                    line = re.sub(r"rtmps?://\S+", "[RTMP URL]", line)
                    self.error = line[-500:]
                    logger.debug("Media process: %s", self.error)
        finally:
            pipe.close()

    def poll(self):
        return self.proc.poll()

    def healthy(self):
        return self.poll() is None and self.last_progress > 0 and time.monotonic() - self.last_progress < 15


def _kill_process(proc, label="process"):
    if not proc:
        return
    try:
        if proc.poll() is not None:
            return
        os.killpg(proc.proc.pid, signal.SIGTERM)
        try:
            proc.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(proc.proc.pid, signal.SIGKILL)
            proc.proc.wait(timeout=3)
    except ProcessLookupError:
        pass
    finally:
        if proc.proc.stdin:
            proc.proc.stdin.close()


def ensure_mediamtx():
    """Install only the binary, with the release checksum; never extract arbitrary paths."""
    if os.path.isfile(MEDIAMTX_BIN) and os.access(MEDIAMTX_BIN, os.X_OK):
        try:
            result = subprocess.run([MEDIAMTX_BIN, "--version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0 and result.stdout.strip() == "v" + MEDIAMTX_VERSION:
                return True
            logger.error("Existing MediaMTX version differs from v%s; move it aside to provision the supported release", MEDIAMTX_VERSION)
        except (OSError, subprocess.TimeoutExpired):
            logger.error("Cannot execute the installed MediaMTX binary")
        return False
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if platform.system() != "Linux" or not arch:
        logger.error("Unsupported MediaMTX platform")
        return False
    name = f"mediamtx_v{MEDIAMTX_VERSION}_linux_{arch}.tar.gz"
    base = f"https://github.com/bluenviron/mediamtx/releases/download/v{MEDIAMTX_VERSION}"
    try:
        with tempfile.TemporaryDirectory(dir=BASE) as tmp:
            archive = Path(tmp) / name
            with requests.get(f"{base}/{name}", stream=True, timeout=60) as response:
                response.raise_for_status()
                with archive.open("wb") as output:
                    for chunk in response.iter_content(65536):
                        output.write(chunk)
            checks = requests.get(f"{base}/checksums.sha256", timeout=20)
            checks.raise_for_status()
            expected = next(line.split()[0] for line in checks.text.splitlines()
                            if line.split()[-1].lstrip("*") == name)
            if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
                raise ValueError("MediaMTX checksum mismatch")
            with tarfile.open(archive) as tar:
                member = tar.getmember("mediamtx")
                if not member.isfile():
                    raise ValueError("MediaMTX binary is not a regular file")
                with tar.extractfile(member) as source, open(Path(tmp) / "binary", "wb") as target:
                    shutil.copyfileobj(source, target)
            os.chmod(Path(tmp) / "binary", 0o755)
            os.replace(Path(tmp) / "binary", MEDIAMTX_BIN)
        return True
    except Exception as exc:
        logger.error("MediaMTX installation failed: %s", exc)
        return False


def generate_mediamtx_config():
    # A continuous program path supplies video + silence during cuts/source loss.
    # Program publishing is loopback-only; phones may publish only camera paths.
    config = {
        "logLevel": "warn", "api": True, "apiAddress": "127.0.0.1:9997",
        "rtsp": True, "rtspAddress": "127.0.0.1:8554", "rtspTransports": ["tcp"],
        "rtmp": True, "rtmpAddress": ":1935", "hls": True, "hlsAddress": ":8888",
        "webrtc": True, "webrtcAddress": ":8889", "srt": False,
        "authInternalUsers": [
            {"user": "any", "ips": [], "permissions": [
                {"action": "publish", "path": "~^cam[123]$"}, {"action": "read"}]},
            {"user": "any", "ips": ["127.0.0.1", "::1"], "permissions": [
                {"action": "api"}, {"action": "publish", "path": "program"}]},
        ],
        "paths": {c: {"overridePublisher": False} for c in CAMERA_IDS},
    }
    config["paths"]["program"] = {"alwaysAvailable": True, "alwaysAvailableTracks": [
        {"codec": "H264"}, {"codec": "MPEG4Audio", "sampleRate": 48000, "channelCount": 2}]}
    Path(MEDIAMTX_CONFIG).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(MEDIAMTX_CONFIG).write_text(json.dumps(config, indent=2))  # JSON is valid YAML
    return MEDIAMTX_CONFIG


def _paths():
    response = requests.get(f"{MEDIAMTX_API}/v3/paths/list", timeout=2)
    response.raise_for_status()
    return {p["name"]: bool(p.get("ready")) for p in response.json().get("items", [])}


def start_mediamtx():
    global _mediamtx_proc, _error, _thread
    with _lock:
        if get_mediamtx_running():
            return True
        try:
            try:
                _paths()
            except requests.RequestException:
                pass
            else:
                _error = "MediaMTX API port is already in use by another server"
                return False
            generate_mediamtx_config()
            _mediamtx_proc = Process([MEDIAMTX_BIN, MEDIAMTX_CONFIG], cwd=Path(MEDIAMTX_CONFIG).parent)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and _mediamtx_proc.poll() is None:
                try:
                    _paths()
                    if _thread is None or not _thread.is_alive():
                        _stop.clear()
                        _thread = threading.Thread(target=_supervise, daemon=True, name="media-supervisor")
                        _thread.start()
                    return True
                except requests.RequestException:
                    time.sleep(.1)
            raise RuntimeError(_mediamtx_proc.error or "MediaMTX did not become ready")
        except Exception as exc:
            _error = str(exc)
            _kill_process(_mediamtx_proc)
            return False


def get_mediamtx_running():
    return _mediamtx_proc is not None and _mediamtx_proc.poll() is None


def get_camera_status():
    return dict(_camera_status)


def get_active_camera():
    relay = _relay_proc
    return _active_camera if relay and relay.healthy() else None


def get_audio_source():
    return _audio_source_mode


def get_youtube_running():
    encoder = _youtube_proc
    return bool(encoder and encoder.healthy())


def get_stream_status():
    # Status must remain responsive while a control operation awaits readiness.
    active = get_youtube_running()
    state = "reconnecting" if _want_stream and _state == "live" and not active else _state
    return {"active": active, "state": state,
            "active_camera": get_active_camera(), "selected_camera": _active_camera,
            "pending_camera": _pending_camera,
            "audio_source": _audio_source_mode, "audio_actual": _relay_audio,
            "audio_warning": _audio_warning if _relay_proc else "",
            "started_at": _started_at,
            "error": _error, "profile": _profile, "fallback": get_active_camera() is None}


def _ffmpeg():
    return ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-nostats", "-progress", "pipe:1"]


def _input(path):
    return ["-rtsp_transport", "tcp", "-timeout", "5000000", "-i", f"{RTSP_BASE}/{path}"]


def _probe(cam):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                                 "-timeout", "3000000", "-show_streams", "-of", "json",
                                 f"{RTSP_BASE}/{cam}"], capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        raise ValueError(f"Timed out reading {cam}") from None
    if result.returncode:
        raise ValueError(f"Cannot read {cam}")
    return {s["codec_type"] for s in json.loads(result.stdout).get("streams", [])}


def _usb_device_available(device):
    from network_manager import get_usb_audio_devices
    return device in {d["id"] for d in get_usb_audio_devices()}


def _wanted_audio_available(cam, mode):
    """Whether the operator's chosen audio source can be used for cam right now."""
    if mode.startswith("usb:"):
        return _usb_device_available(mode[4:])
    if mode == "cam1":
        try:
            return "audio" in _probe("cam1")
        except ValueError:
            return False
    return True


def _audio_degraded(mode, actual):
    return (mode == "cam1" and actual != "cam1") or (mode.startswith("usb:") and actual != mode)


def _relay_command(cam, mode):
    """Return (command, audio actually used, warning). Only missing video is fatal: a
    missing commentary source degrades to the camera's own audio (or silence) so the
    picture never goes to fallback because of a microphone."""
    tracks = _probe(cam)
    if "video" not in tracks:
        raise ValueError(f"{cam} has no video")
    cmd = _ffmpeg() + _input(cam)
    warning = ""
    if mode.startswith("usb:"):
        device = mode[4:]
        if _usb_device_available(device):
            fmt = "alsa" if device.startswith(("hw:", "plughw:")) else "pulse"
            cmd += ["-thread_queue_size", "512", "-f", fmt, "-i", device]
            return _relay_output(cmd, "1:a:0"), mode, ""
        warning = "USB microphone unavailable"
    elif mode == "cam1" and cam != "cam1":
        try:
            commentary = "audio" in _probe("cam1")
        except ValueError:
            commentary = False
        if commentary:
            return _relay_output(cmd + _input("cam1"), "1:a:0"), "cam1", ""
        warning = "Cam 1 commentary unavailable"
    if "audio" in tracks:
        actual = "cam1" if cam == "cam1" else "camera"
        if warning:
            warning += f"; using {cam} audio"
        return _relay_output(cmd, "0:a:0"), actual, warning
    if mode == "cam1" and cam == "cam1":
        warning = "Cam 1 has no audio"
    cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    return _relay_output(cmd, "1:a:0"), "silence", f"{warning}; broadcasting silence" if warning else ""


def _relay_output(cmd, audio):
    return cmd + ["-map", "0:v:0", "-map", audio,
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-g", "30", "-b:v", "4500k",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-af", "aresample=async=1",
        "-shortest", "-f", "rtsp", "-rtsp_transport", "tcp", f"{RTSP_BASE}/program"]


def _wait_progress(proc, timeout=12, aborted=lambda: False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None and not aborted():
        if proc.healthy():
            return True
        time.sleep(.1)
    return False


SUPERSEDED = "Superseded by a newer request"
BUSY = "A cut is already in progress"


def switch_camera(cam_id, automatic=False):
    """Operator cuts supersede any cut in flight. Automatic (supervisor) cuts never wait
    and never supersede an operator."""
    global _generation, _pending_camera
    if cam_id not in CAMERA_IDS:
        return False, "Invalid camera"
    with _lock:
        if not get_mediamtx_running():
            return False, "MediaMTX is not ready"
        if not automatic:
            _generation += 1
            _pending_camera = cam_id
        generation = _generation
    if not _switch_lock.acquire(blocking=not automatic):
        return False, BUSY
    try:
        return _replace_relay(cam_id, generation)
    finally:
        _switch_lock.release()
        with _lock:
            if generation == _generation and not automatic:
                _pending_camera = None


def _replace_relay(cam_id, generation):
    global _relay_proc, _active_camera, _error, _relay_audio, _audio_warning, _relay_failures
    current = lambda: generation == _generation
    if not current():
        return False, SUPERSEDED
    mode = _audio_source_mode
    try:
        # Probe and validate before disturbing the current publisher.
        cmd, audio, warning = _relay_command(cam_id, mode)
    except Exception as exc:
        if current():
            _error = str(exc)
        return False, str(exc)
    with _lock:
        if not current():
            return False, SUPERSEDED
        old, _relay_proc = _relay_proc, None
    _kill_process(old)
    try:
        candidate = Process(cmd)
    except OSError as exc:
        _error = f"Cannot start camera relay: {exc}"
        return False, _error
    ok = _wait_progress(candidate, aborted=lambda: not current())
    with _lock:
        installed = ok and current()
        if installed:
            _relay_proc, _active_camera = candidate, cam_id
            _relay_audio, _audio_warning = audio, warning
            _error, _relay_failures = "", 0
    if installed:
        if warning:
            logger.warning("Relay %s: %s", cam_id, warning)
        return True, f"Camera {cam_id} selected" + (f" ({warning})" if warning else "")
    message = candidate.error or "Camera relay did not produce frames"
    _kill_process(candidate)
    if not current():
        return False, SUPERSEDED
    # _active_camera keeps the prior selection: the supervisor restores it when its
    # camera is publishing, and program shows fallback meanwhile.
    _error = message
    return False, message


def set_audio_source(mode):
    global _audio_source_mode
    if mode not in ("cam1", "active") and not (mode.startswith("usb:") and mode[4:]):
        return False, "Invalid audio source"
    if mode.startswith("usb:") and not _usb_device_available(mode[4:]):
        return False, "Unknown audio device"
    with _lock:
        previous, camera = _audio_source_mode, _active_camera
        _audio_source_mode = mode
    if previous != mode and camera:
        ok, message = switch_camera(camera)
        if not ok and message != SUPERSEDED:
            with _lock:
                if _audio_source_mode == mode:
                    _audio_source_mode = previous
            return False, message
        if ok and _audio_warning:
            return True, f"Audio source updated ({_audio_warning})"
    return True, "Audio source updated"


def _launch_encoder():
    global _youtube_proc
    width, height, bitrate = PROFILES[_profile]
    # Disable drawtext expansion: team names are literal text, including percent signs.
    escaped = SCOREBOARD_PATH.replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''")
    vf = (f"scale={width}:{height},drawtext=textfile='{escaped}':reload=1:expansion=none:"
          "fontcolor=white:fontsize=28:box=1:boxcolor=black@0.6:boxborderw=8:x=20:y=h-th-20")
    cmd = _ffmpeg() + _input("program") + [
        "-map", "0:v:0", "-map", "0:a:0", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", "9000k", "-g", "60",
        "-r", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-ar", "48000", "-ac", "2", "-rw_timeout", "10000000", "-f", "flv",
        f"{YOUTUBE_RTMP_BASE}/{_youtube_stream_key}"]
    _youtube_proc = Process(cmd, secret=_youtube_stream_key)


def start_youtube_stream(stream_key, profile="720p"):
    global _want_stream, _youtube_stream_key, _state, _error, _profile, _retry_at, _retries
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", stream_key):
        return False, "Invalid stream key"
    if profile not in PROFILES:
        return False, "Invalid quality profile"
    if _want_stream:
        return False, "Stream is already starting or running; stop before changing settings"
    if not all(shutil.which(binary) for binary in ("ffmpeg", "ffprobe")):
        return False, "Install ffmpeg and ffprobe before streaming"
    if not get_mediamtx_running():
        return False, "MediaMTX is not ready"
    if not Path(SCOREBOARD_PATH).is_file():
        return False, "Scoreboard is not initialized"
    if not get_active_camera():
        # Selecting a camera can take seconds; do it without holding _lock.
        try:
            live = _paths()
        except requests.RequestException:
            return False, "Cannot query camera readiness"
        preferred = [_active_camera] if _active_camera else []
        camera = next((c for c in preferred + CAMERA_IDS if live.get(c)), None)
        if camera is None:
            return False, "No camera is publishing"
        ok, message = switch_camera(camera)
        if not ok:
            return False, message
    with _lock:
        if _want_stream:
            return False, "Stream is already starting or running; stop before changing settings"
        _youtube_stream_key, _profile = stream_key, profile
        try:
            _launch_encoder()
        except OSError:
            _youtube_stream_key = None
            return False, "Unable to launch encoder"
        _want_stream, _state, _error = True, "starting", ""
        _retry_at, _retries = 0, 0
        return True, "Encoder starting; watch the live status for confirmation"


def stop_youtube_stream():
    global _want_stream, _youtube_proc, _relay_proc, _active_camera, _generation
    global _youtube_stream_key, _state, _error, _started_at, _pending_camera
    with _lock:
        _want_stream = False
        _generation += 1  # abort any cut in flight
        encoder, relay = _youtube_proc, _relay_proc
        _youtube_proc = _relay_proc = None
        _active_camera = _youtube_stream_key = _started_at = _pending_camera = None
        _state, _error = "stopped", ""
    _kill_process(encoder)
    _kill_process(relay)
    return True, "Streaming stopped"


def _supervise_relay(now):
    """Restart a dead relay with backoff, and restore the chosen commentary source after
    a degraded relay once that source is available again. Never holds _lock while slow."""
    global _relay_proc, _relay_failures, _relay_retry_at, _restore_check_at
    with _lock:
        relay, camera, mode = _relay_proc, _active_camera, _audio_source_mode
        dead = relay and (relay.poll() is not None or
                          (not relay.healthy() and now - relay.launched > 15))
        if dead:
            _relay_proc = None
    if dead:
        _kill_process(relay)
        relay = None
    if not camera or _switch_lock.locked():
        return
    if relay is None:
        if _camera_status.get(camera) and now >= _relay_retry_at:
            ok, message = switch_camera(camera, automatic=True)
            if not ok and message not in (BUSY, SUPERSEDED):
                _relay_failures += 1
                _relay_retry_at = now + min(30, 2 ** min(_relay_failures, 5))
    elif _audio_degraded(mode, _relay_audio) and now >= _restore_check_at and relay.healthy():
        _restore_check_at = now + 10
        if _wanted_audio_available(camera, mode):
            logger.info("Restoring %s audio on %s", mode, camera)
            switch_camera(camera, automatic=True)


def _supervise_once():
    global _camera_status, _state, _error, _started_at, _retry_at, _retries, _mediamtx_retry_at
    try:
        paths = _paths()
        _camera_status = {c: paths.get(c, False) for c in CAMERA_IDS + ["program"]}
    except (requests.RequestException, ValueError, KeyError):
        _camera_status = {c: False for c in CAMERA_IDS + ["program"]}
    now = time.monotonic()
    # Cameras need MediaMTX whether or not we are live.
    if not get_mediamtx_running():
        if now >= _mediamtx_retry_at:
            _mediamtx_retry_at = now + 5
            logger.warning("MediaMTX is not running; restarting")
            start_mediamtx()
        if not get_mediamtx_running():
            return
    _supervise_relay(now)
    with _lock:
        if not _want_stream:
            return
        if _youtube_proc and _youtube_proc.healthy():
            _state = "live"
            _error = ""
            _retries = 0
            if _started_at is None:
                _started_at = datetime.now(timezone.utc).isoformat()
            return
        if _youtube_proc and _youtube_proc.poll() is None and now - _youtube_proc.launched < 20:
            return
        _state = "reconnecting"
        _error = (_youtube_proc.error if _youtube_proc else "") or "Encoder stopped producing frames"
        _kill_process(_youtube_proc)
        if now >= _retry_at:
            _retries += 1
            _retry_at = now + min(30, 2 ** min(_retries, 5))
            try:
                _launch_encoder()
            except OSError:
                _state, _error = "failed", "Cannot restart encoder"


def _supervise():
    while not _stop.wait(2):
        try:
            _supervise_once()
        except Exception:
            logger.exception("Media supervisor failed")


def shutdown_all():
    global _mediamtx_proc
    _stop.set()
    if _thread and _thread is not threading.current_thread():
        _thread.join(timeout=20)
    with _lock:
        stop_youtube_stream()
        _kill_process(_mediamtx_proc)
        _mediamtx_proc = None
