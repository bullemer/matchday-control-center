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
_switch_lock = threading.RLock()
_start_lock = threading.Lock()
_stop_generation = 0  # cancels the entire START operation, including readiness queries
_generation = 0          # bumped by each operator cut and by STOP; stale cut attempts discard their result
_pending_camera = None
_stop = threading.Event()
_thread = None
_encoder_thread = None
_mediamtx_proc = _youtube_proc = _relay_proc = None
_active_camera = None
# Audio mixer. Channels are the cameras and detected microphones ("usb:<device id>");
# each has on/off and a gain in dB. With _follow, only the on-air camera's channel is on
# among the cameras (its sound follows the cut). Gains change live over ZMQ; switching a
# channel on that the running relay does not include rebuilds the relay.
_mix = {c: {"on": c == "cam1", "gain": 0.0} for c in CAMERA_IDS}
_follow = False
_mic_ids = set()         # detected microphone ids, set by the app
_relay_channels = {}     # channel -> volume filter name in the running relay
_relay_muted = set()     # installed channels successfully muted in the running relay
_relay_missing = set()   # channels wanted by the running relay but unavailable at build
_audio_warning = ""
ZMQ_ADDRESS = "tcp://127.0.0.1:28765"
GAIN_MIN, GAIN_MAX = -60.0, 12.0
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
_relay_profile = None    # profile the running relay was built for
# width, height, YouTube bitrate, camera-relay bitrate (relay output is re-encoded, so it
# gets headroom). The relay runs at the stream resolution so 1080p is never upscaled.
PROFILES = {"1080p": (1920, 1080, "6000k", "8000k"),
            "720p": (1280, 720, "4500k", "4500k"),
            "480p": (854, 480, "1800k", "2500k")}


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


class Meter(Process):
    """Audio level meter: decodes one audio source and reports peak/RMS in dBFS ~10x/s."""
    def __init__(self, cmd):
        self.peak = self.rms = None
        self.updated = 0.0
        super().__init__(cmd)

    def _read(self, pipe, progress):
        if not progress:
            return super()._read(pipe, progress)
        try:
            for line in pipe:
                key, _, value = line.strip().partition("=")
                if key.endswith(".Peak_level"):
                    self.peak = _db(value)
                elif key.endswith(".RMS_level"):
                    self.rms = _db(value)
                    self.updated = time.monotonic()
        finally:
            pipe.close()


def _db(value):
    try:
        return max(-120.0, min(0.0, round(float(value), 1)))  # "-inf" is digital silence
    except ValueError:
        return -120.0


# 100 ms blocks at 48 kHz; only overall peak/RMS, printed as key=value lines on stdout.
METER_FILTER = ("aresample=48000,asetnsamples=n=4800:p=0,astats=metadata=1:reset=1:"
                "measure_perchannel=none:measure_overall=Peak_level+RMS_level,"
                "ametadata=print:file=/dev/stdout:direct=1")  # direct: unbuffered, one line per block
_meters = {}            # source key ("cam1", "program", "usb:<pulse source>") -> Meter
_meter_retry_at = {}
_meter_no_audio = set()  # publishing cameras whose stream has no audio track
_mic_sources = []        # pulse sources of detected microphones, set by the app


def set_mic_devices(devices):
    """Meter every detected microphone that PulseAudio/PipeWire can share. ALSA-only
    devices are opened exclusively by the relay, so they cannot be metered alongside it."""
    global _mic_sources, _mic_ids
    _mic_sources = [d["pulse_source"] for d in devices if d.get("pulse_source")]
    with _lock:
        _mic_ids = {d["id"] for d in devices}
        for device in _mic_ids:
            _mix.setdefault("usb:" + device, {"on": False, "gain": 0.0})


def _meter_command(key):
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error"]
    if key.startswith("usb:"):
        cmd += ["-f", "pulse", "-i", key[4:]]
    else:
        cmd += ["-rtsp_transport", "tcp", "-allowed_media_types", "audio",
                "-timeout", "5000000", "-i", f"{RTSP_BASE}/{key}"]
    return cmd + ["-vn", "-af", METER_FILTER, "-f", "null", "-"]


def _supervise_meters(now):
    wanted = [c for c in CAMERA_IDS + ["program"] if _camera_status.get(c)]
    wanted += ["usb:" + source for source in _mic_sources]
    _meter_no_audio.intersection_update(wanted)
    for key, meter in list(_meters.items()):
        exited = meter.poll() is not None
        if key in wanted and not exited:
            continue
        if exited and key in wanted:
            # Exiting before any reading means the source has no audio track.
            if not meter.updated and not key.startswith("usb:"):
                _meter_no_audio.add(key)
            _meter_retry_at[key] = now + (15 if not meter.updated else 2)
        _kill_process(meter)
        del _meters[key]
    for key in wanted:
        if key not in _meters and now >= _meter_retry_at.get(key, 0):
            try:
                _meters[key] = Meter(_meter_command(key))
            except OSError:
                _meter_retry_at[key] = now + 30


def get_audio_levels():
    """Latest levels per source: {"peak", "rms"} in dBFS, "no audio", or None (no data)."""
    now = time.monotonic()
    levels = {key: "no audio" for key in _meter_no_audio}
    for key, meter in list(_meters.items()):
        if meter.updated and now - meter.updated < 1.5:
            levels[key] = {"peak": meter.peak, "rms": meter.rms}
        else:
            levels.setdefault(key, None)
    return levels


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
        "webrtc": True, "webrtcAddress": ":8889", "srt": False, "moq": False,
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
    global _mediamtx_proc, _error, _thread, _encoder_thread
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
                    if _encoder_thread is None or not _encoder_thread.is_alive():
                        _encoder_thread = threading.Thread(target=_watch_encoder, daemon=True, name="encoder-supervisor")
                        _encoder_thread.start()
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
    """The preset matching the mixer ("cam1", "active", "usb:<id>"), else "mix"."""
    cams_on = [c for c in CAMERA_IDS if _mix[c]["on"]]
    mics_on = [c for c in _mix if c.startswith("usb:") and _mix[c]["on"]]
    if _follow:
        return "active" if not mics_on else "mix"
    if cams_on == ["cam1"] and not mics_on:
        return "cam1"
    if not cams_on and len(mics_on) == 1:
        return mics_on[0]
    return "mix"


def get_mixer():
    return {"follow": _follow, "preset": get_audio_source(),
            "channels": {c: {**setting, "available": c in CAMERA_IDS or c[4:] in _mic_ids}
                         for c, setting in _mix.items()}}


def _effective_mix(cam):
    """Channels that should be on air when cam is cut, with their gain in dB."""
    wanted = {}
    for channel, setting in _mix.items():
        on = channel == cam if _follow and channel in CAMERA_IDS else setting["on"]
        if on:
            wanted[channel] = setting["gain"]
    return wanted


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
            "audio_source": get_audio_source(),
            "audio_on_air": [c for c in _relay_channels if c not in _relay_muted] if get_active_camera() else [],
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


def _channel_available(channel):
    if channel.startswith("usb:"):
        return _usb_device_available(channel[4:])
    try:
        return "audio" in _probe(channel)
    except ValueError:
        return False


def _db_to_volume(db):
    return 10 ** (db / 20)


def _relay_command(cam, wanted):
    """Return (command, {channel: volume filter}, missing channels, warning) for cutting
    to cam with the wanted {channel: gain dB} mix. Only missing video is fatal: if no
    wanted audio source is available, the camera's own sound (or silence) is used so the
    picture never goes to fallback because of a microphone."""
    tracks = _probe(cam)
    if "video" not in tracks:
        raise ValueError(f"{cam} has no video")
    cmd = _ffmpeg() + _input(cam)
    sources, missing, notes, next_input = [], set(), [], 1
    for channel, gain in wanted.items():
        if channel == cam:
            if "audio" in tracks:
                sources.append((channel, "0:a:0", gain))
            else:
                missing.add(channel)
                notes.append(f"{cam} has no audio")
        elif channel in CAMERA_IDS:
            if _channel_available(channel):
                sources.append((channel, f"{next_input}:a:0", gain))
                cmd += _input(channel)
                next_input += 1
            else:
                missing.add(channel)
                notes.append("Cam 1 commentary unavailable" if channel == "cam1" else f"{channel} audio unavailable")
        elif _usb_device_available(channel[4:]):
            device = channel[4:]
            fmt = "alsa" if device.startswith(("hw:", "plughw:")) else "pulse"
            sources.append((channel, f"{next_input}:a:0", gain))
            cmd += ["-thread_queue_size", "512", "-f", fmt, "-i", device]
            next_input += 1
        else:
            missing.add(channel)
            notes.append("USB microphone unavailable")
    warning = "; ".join(notes)
    if not sources and missing and "audio" in tracks and cam not in wanted:
        sources.append((cam, "0:a:0", 0.0))
        warning += f"; using {cam} audio"
    elif not sources and missing:
        warning += "; broadcasting silence"
    # Generated silence is the first mix input, so the mix never ends when a source does.
    inputs = next_input
    cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    parts, labels, channels = [], [], {}
    for index, (channel, pad, gain) in enumerate(sources):
        name = f"m{index}"
        parts.append(f"[{pad}]aresample=48000:async=1,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"volume@{name}={_db_to_volume(gain):.4f}[{name}]")
        labels.append(f"[{name}]")
        channels[channel] = name
    address = ZMQ_ADDRESS.replace(":", "\\\\:")  # escaped twice: filter option, then graph
    parts.append(f"[{inputs}:a:0]{''.join(labels)}amix=inputs={len(labels) + 1}:duration=first:"
                 f"normalize=0:dropout_transition=0,alimiter=limit=0.891:level=0,"
                 f"azmq=bind_address={address}[aout]")
    width, height, _, bitrate = PROFILES[_profile]
    return cmd + ["-filter_complex", ";".join(parts), "-map", "0:v:0", "-map", "[aout]",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-g", "30", "-b:v", bitrate,
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-shortest", "-f", "rtsp", "-rtsp_transport", "tcp", f"{RTSP_BASE}/program"], channels, missing, warning


def _send_volume(name, volume):
    """Set one channel's volume in the running relay without restarting it."""
    import zmq
    sock = zmq.Context.instance().socket(zmq.REQ)
    for option, value in ((zmq.LINGER, 0), (zmq.RCVTIMEO, 1000), (zmq.SNDTIMEO, 1000)):
        sock.setsockopt(option, value)
    try:
        sock.connect(ZMQ_ADDRESS)
        sock.send_string(f"volume@{name} volume {volume:.4f}")
        return sock.recv_string().startswith("0 ")
    except zmq.ZMQError:
        return False
    finally:
        sock.close()


def _wait_progress(proc, timeout=12, aborted=lambda: False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None and not aborted():
        if proc.healthy():
            return True
        time.sleep(.1)
    return False


SUPERSEDED = "Superseded by a newer request"
BUSY = "A cut is already in progress"


def switch_camera(cam_id, automatic=False, expected_stop=None):
    """Operator cuts supersede any cut in flight. Automatic (supervisor) cuts never wait
    and never supersede an operator."""
    global _generation, _pending_camera
    if cam_id not in CAMERA_IDS:
        return False, "Invalid camera"
    with _lock:
        if expected_stop is not None and expected_stop != _stop_generation:
            return False, SUPERSEDED
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
    global _relay_proc, _active_camera, _error, _relay_channels, _relay_missing, _audio_warning
    global _relay_failures, _relay_profile, _relay_muted
    current = lambda: generation == _generation
    if not current():
        return False, SUPERSEDED
    wanted, profile = _effective_mix(cam_id), _profile
    try:
        # Probe and validate before disturbing the current publisher.
        cmd, channels, missing, warning = _relay_command(cam_id, wanted)
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
            _relay_proc, _active_camera, _relay_profile = candidate, cam_id, profile
            _relay_channels, _relay_missing, _audio_warning = channels, missing, warning
            _relay_muted = set()
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


def _apply_mix():
    """Bring the running relay in line with the mixer: live volume changes when it already
    has every available wanted source, otherwise rebuild it (a short audio/picture gap)."""
    with _lock:
        stop_generation = _stop_generation
        camera, relay, channels, missing = _active_camera, _relay_proc, dict(_relay_channels), set(_relay_missing)
        wanted = _effective_mix(camera) if camera else {}
    if not camera:
        return True, "Mixer updated"
    if relay is not None and relay.poll() is None and set(wanted) - missing <= set(channels):
        applied = True
        for channel, name in channels.items():
            volume = _db_to_volume(wanted[channel]) if channel in wanted else 0.0
            if not _send_volume(name, volume):
                applied = False
                break
            with _lock:
                if _relay_proc is not relay:
                    return False, SUPERSEDED
                if volume == 0:
                    _relay_muted.add(channel)
                else:
                    _relay_muted.discard(channel)
        if applied:
            return True, "Mixer updated"
    # A mixer rebuild must not supersede a newer operator cut already queued.
    ok, message = switch_camera(camera, automatic=True, expected_stop=stop_generation)
    if ok or (message == SUPERSEDED and stop_generation == _stop_generation):
        return True, "Mixer updated" + (f" ({_audio_warning})" if ok and _audio_warning else "")
    return False, message


def _set_mixer(change):
    """Apply change() to the mixer state; roll it back if the relay cannot follow."""
    global _mix, _follow
    stop_generation = _stop_generation
    # Serialize mixer mutations with relay replacement, including live ZMQ commands.
    # STOP remains independent and can cancel work waiting for this lock.
    with _switch_lock:
        with _lock:
            if stop_generation != _stop_generation:
                return False, SUPERSEDED
            previous = ({c: dict(v) for c, v in _mix.items()}, _follow)
            change()
        ok, message = _apply_mix()
        if not ok:
            with _lock:
                _mix, _follow = previous
        return ok, message


def set_audio_source(mode):
    """Presets: "cam1" (Cam 1 mic only), "active" (camera sound follows the cut),
    "usb:<id>" (that microphone only)."""
    if mode not in ("cam1", "active") and not (mode.startswith("usb:") and mode[4:]):
        return False, "Invalid audio source"
    if mode.startswith("usb:") and not _usb_device_available(mode[4:]):
        return False, "Unknown audio device"

    def change():
        global _follow
        if mode.startswith("usb:"):
            _mix.setdefault(mode, {"on": False, "gain": 0.0})
        for channel, setting in _mix.items():
            setting["on"] = channel == mode
        _follow = mode == "active"
    ok, message = _set_mixer(change)
    return ok, message.replace("Mixer updated", "Audio source updated")


def set_mix(channel, on=None, gain=None):
    """Switch one mixer channel on/off and/or set its gain in dB."""
    if channel not in _mix:
        return False, "Unknown audio channel"
    if gain is not None and not -1000 < gain < 1000:
        return False, "Invalid gain"

    def change():
        if gain is not None:
            _mix[channel]["gain"] = round(max(GAIN_MIN, min(GAIN_MAX, float(gain))), 1)
        if on is not None:
            _mix[channel]["on"] = bool(on)
    return _set_mixer(change)


def set_follow(follow):
    """Camera sound follows the cut (only the on-air camera's channel is on)."""
    def change():
        global _follow
        _follow = bool(follow)
    return _set_mixer(change)


def _launch_encoder():
    global _youtube_proc
    width, height, bitrate, _ = PROFILES[_profile]
    font = round(height / 26)  # same on-screen size at every resolution
    # Disable drawtext expansion: team names are literal text, including percent signs.
    escaped = SCOREBOARD_PATH.replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''")
    vf = (f"scale={width}:{height},drawtext=textfile='{escaped}':reload=1:expansion=none:"
          f"fontcolor=white:fontsize={font}:box=1:boxcolor=black@0.6:boxborderw={font // 3}:"
          f"x={font * 2 // 3}:y=h-th-{font * 2 // 3}")
    cmd = _ffmpeg() + _input("program") + [
        "-map", "0:v:0", "-map", "0:a:0", "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{int(bitrate[:-1]) * 2}k", "-g", "60",
        "-r", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
        "-ar", "48000", "-ac", "2", "-rw_timeout", "10000000", "-f", "flv",
        f"{YOUTUBE_RTMP_BASE}/{_youtube_stream_key}"]
    _youtube_proc = Process(cmd, secret=_youtube_stream_key)


def start_youtube_stream(stream_key, profile="720p"):
    global _state, _profile
    if not _start_lock.acquire(blocking=False):
        return False, "Stream is already starting"
    try:
        with _lock:
            token = _stop_generation
            if _want_stream:
                return False, "Stream is already starting or running; stop before changing settings"
            if profile not in PROFILES:
                return False, "Invalid quality profile"
            _profile, _state = profile, "starting"
        try:
            return _start_youtube_stream(stream_key, profile, token)
        finally:
            with _lock:
                if token == _stop_generation and not _want_stream:
                    _state = "stopped"
    finally:
        _start_lock.release()


def _start_youtube_stream(stream_key, profile, token):
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
    if get_active_camera() and _relay_profile != profile:
        # Rebuild the program relay at the stream resolution before going live.
        ok, message = switch_camera(_active_camera, expected_stop=token)
        if not ok:
            return False, message
    if not get_active_camera():
        # Selecting a camera can take seconds; do it without holding _lock.
        try:
            live = _paths()
        except requests.RequestException:
            return False, "Cannot query camera readiness"
        if token != _stop_generation:
            return False, SUPERSEDED
        preferred = [_active_camera] if _active_camera else []
        camera = next((c for c in preferred + CAMERA_IDS if live.get(c)), None)
        if camera is None:
            return False, "No camera is publishing"
        ok, message = switch_camera(camera, expected_stop=token)
        if not ok:
            return False, message
    with _lock:
        if token != _stop_generation:
            return False, SUPERSEDED
        if _want_stream:
            return False, "Stream is already starting or running; stop before changing settings"
        _youtube_stream_key = stream_key
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
    global _relay_channels, _relay_missing, _relay_muted, _stop_generation
    with _lock:
        _want_stream = False
        _stop_generation += 1
        _generation += 1  # abort any cut in flight
        encoder, relay = _youtube_proc, _relay_proc
        _youtube_proc = _relay_proc = None
        _active_camera = _youtube_stream_key = _started_at = _pending_camera = None
        _relay_channels, _relay_missing, _relay_muted = {}, set(), set()
        _state, _error = "stopped", ""
    _kill_process(encoder)
    _kill_process(relay)
    return True, "Streaming stopped"


def _supervise_relay(now):
    """Restart a dead relay with backoff, and restore the chosen commentary source after
    a degraded relay once that source is available again. Never holds _lock while slow."""
    global _relay_proc, _relay_failures, _relay_retry_at, _restore_check_at
    with _lock:
        relay, camera = _relay_proc, _active_camera
        channels, missing = dict(_relay_channels), set(_relay_missing)
        dead = relay and (relay.poll() is not None or
                          (not relay.healthy() and now - relay.launched > 15))
        if dead:
            _relay_proc = None
    if dead:
        _kill_process(relay)
        relay = None
    if not camera:
        return
    if relay is None:
        if _camera_status.get(camera) and now >= _relay_retry_at:
            ok, message = switch_camera(camera, automatic=True)
            if not ok and message not in (BUSY, SUPERSEDED):
                _relay_failures += 1
                _relay_retry_at = now + min(30, 2 ** min(_relay_failures, 5))
    elif relay.healthy() and now >= _restore_check_at:
        # A mixed-in source that stopped publishing is silent in the mix: rebuild without it
        # (falls back to the camera's own sound). A missing source that is back: restore it.
        lost = [c for c in channels if c != camera and (
            (c in CAMERA_IDS and not _camera_status.get(c)) or (c.startswith("usb:") and c[4:] not in _mic_ids))]
        returned = [c for c in missing if c != camera and c in _mix and _channel_available(c)] if missing else []
        if lost or returned:
            _restore_check_at = now + 10
            logger.info("Rebuilding audio on %s (lost %s, returned %s)", camera, lost, returned)
            switch_camera(camera, automatic=True)
        elif missing:
            _restore_check_at = now + 10


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
    _supervise_meters(now)


def _supervise_encoder():
    global _youtube_proc, _state, _error, _started_at, _retry_at, _retries
    now = time.monotonic()
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
        token = _stop_generation
        old, _youtube_proc = _youtube_proc, None
    _kill_process(old)
    with _lock:
        if not _want_stream or token != _stop_generation:
            return
        if now >= _retry_at:
            _retries += 1
            _retry_at = now + min(30, 2 ** min(_retries, 5))
            try:
                _launch_encoder()
            except OSError:
                _state, _error = "failed", "Cannot restart encoder"


def _watch_encoder():
    while not _stop.wait(.5):
        try:
            _supervise_encoder()
        except Exception:
            logger.exception("Encoder supervisor failed")


def _supervise():
    while not _stop.wait(2):
        try:
            _supervise_once()
        except Exception:
            logger.exception("Media supervisor failed")


def shutdown_all():
    global _mediamtx_proc
    _stop.set()
    for worker in (_thread, _encoder_thread):
        if worker and worker is not threading.current_thread():
            worker.join(timeout=20)
    with _lock:
        stop_youtube_stream()
        for key in list(_meters):
            _kill_process(_meters.pop(key))
        _kill_process(_mediamtx_proc)
        _mediamtx_proc = None
