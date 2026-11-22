"""Supervised local media pipeline. All control mutations share one lock."""
from __future__ import annotations

from browser_overlay import FRAME_PATH, ensure_frame
import recordings
import settings_store

import hashlib
import collections
import json
import logging
import os
from pathlib import Path
import platform
import re
import secrets
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
# Default ingest servers (editable per destination: Facebook can issue per-broadcast
# servers, Twitch has regional ones). Twitch: https://ingest.twitch.tv/ingests "Default".
DESTINATIONS = {
    "youtube": ("YouTube", lambda: YOUTUBE_RTMP_BASE, r"[A-Za-z0-9_-]{1,200}"),
    "facebook": ("Facebook", lambda: "rtmps://live-api-s.facebook.com:443/rtmp", r"[A-Za-z0-9_.?=&-]{1,300}"),
    "twitch": ("Twitch", lambda: "rtmps://ingest.global-contribute.live-video.net/app", r"[A-Za-z0-9_-]{1,200}"),
    "custom": ("Custom RTMP", lambda: "", r"[A-Za-z0-9_.?=&:/~%+-]{0,300}"),
}
SERVER_PATTERN = r"rtmps?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9_.~%+/-]*)?"
SCOREBOARD_PATH = str(BASE / "static/scoreboard.txt")
# Match cameras film the game; reporting cameras are free cameras (interviews, crowd,
# surroundings). Reporting cameras are heard whenever they are in the picture, may share
# the picture with a match camera (picture-in-picture, side by side) and are recorded
# on their own for the whole session, on air or not.
MATCH_CAMERAS = ["cam1", "cam2", "cam3", "cam4"]
REPORT_CAMERAS = ["cam5", "cam6"]
CAMERA_IDS = MATCH_CAMERAS + REPORT_CAMERAS
CAMERA_SETTING = BASE / "data/cameras.json"
_camera_info = {}         # camera -> {"label", "position"} (operator's names, data/cameras.json)
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
# Output stage: the encoder burns in the scoreboard and publishes the finished picture
# to the local "final" path; the YouTube pusher and the recorder only copy it. A YouTube
# outage therefore restarts just the pusher and never interrupts the recording.
_mediamtx_proc = _encoder_proc = _recorder_proc = _relay_proc = None
_active_camera = None
_last_camera = None       # on air at the last STOP: the next GO LIVE starts there again
_layout = {"mode": "single", "main": "cam1", "secondary": REPORT_CAMERAS[0], "position": "top-right"}
_cut_source = "manual"    # who made the last cut: "manual" (operator), "auto" (a future auto-director)
_manual_cut_at = -1e9     # when the operator last cut; an auto-director holds off for MANUAL_HOLD
MANUAL_HOLD = 10.0
_relay_secondary = None
_layout_warning = ""
# Audio mixer. Channels are the cameras and detected microphones ("usb:<device id>");
# each has on/off and a gain in dB. With _follow, only the on-air camera's channel is on
# among the cameras (its sound follows the cut). Gains change live over ZMQ; switching a
# channel on that the running relay does not include rebuilds the relay.
# Match camera sound starts off: the broadcast is silent until the operator picks a source
# (open phone mics caught crowd and stadium music, and YouTube blocked the stream).
# A reporting camera's "on" means "heard while it is in the picture" and starts on.
_mix = {c: {"on": c in REPORT_CAMERAS, "gain": 0.0} for c in CAMERA_IDS}
_follow = False
_muted = False           # master mute: every channel at zero volume, mixer settings kept
_mic_ids = set()         # detected microphone ids, set by the app
_relay_channels = {}     # channel -> volume filter name in the running relay
_relay_muted = set()     # installed channels successfully muted in the running relay
_relay_missing = set()   # channels wanted by the running relay but unavailable at build
_audio_warning = ""
ZMQ_ADDRESS = "tcp://127.0.0.1:28765"   # relays alternate between this port and port + 1
_relay_zmq = None        # live-volume control address of the running relay
# A cut starts the new relay while the current one stays on air, and MediaMTX hands
# program over when the new one publishes; the two relays need different control ports.
_relay_slot = 0
# Shown by MediaMTX while program has no publisher (and for a frame or two at a handover)
# instead of its "STREAM IS OFFLINE" segment.
# v2: without B-frames. WebRTC cannot play B-frames, so the browser preview of the program
# spun forever until the first cut. A new name makes existing installs create the new card.
OFFLINE_CARD = str(BASE / "data/offline-v2.mp4")
CAMERA_USER = "camera"
GAIN_MIN, GAIN_MAX = -60.0, 12.0
_relay_failures = 0
_relay_retry_at = 0.0
_restore_check_at = 0.0
_mediamtx_retry_at = 0.0
_status_failures = 0     # consecutive failed camera-status queries
# Streaming destinations: name -> {"label", "url", "secret", "proc", "state", "error",
# "failures", "retry_at", "started_at"}. Each is a copy-only reader of "final" with its own
# reconnect backoff, so one platform dropping out never touches the others.
_targets = {}
_want_stream = False      # output stage (relay + encoder) wanted: streaming and/or recording
_want_record = False
_recording = None         # active recording session id
_rec_retry_at = _disk_check_at = 0.0
_rec_failures = 0
_record_warning = ""
_disk_warning = ""        # recording room is running out
LOW_ROOM_MINUTES = 15
# Instant replay: a temporary relay plays part of the running recording into program.
# _active_camera keeps the live camera to return to; any cut or STOP ends the replay.
_replay = None
_replay_listeners = []    # called with True/False when a replay goes on/off air
REPLAY_MARGIN = 1.5       # seconds behind the recording's write position
REPLAY_SPEEDS = (1.0, 0.5)
REPLAY_LEAD = 1.5         # start the return-to-live relay at least this long before the clip ends
_cut_seconds = 3.0        # how long a cut takes here (moving average); the return starts that early
# While a replay is on air the recording holds the replay: the live camera is recorded
# (copy only) next to it, from the replay request until LIVE_TAIL after live is back.
_live_capture = None      # {"proc", "camera", "session", "path", "aired", "idle_since"}
LIVE_TAIL = 3.0
SHUTDOWN_SAVE_SECONDS = 60
_state = "stopped"
# One message per part of the pipeline, so one part's error neither hides nor clears another's.
ERROR_PARTS = ("server", "relay", "encoder", "destinations")
_errors = {}
_error_since = {}
_started_at = None
_retry_at = 0.0
_retries = 0
_camera_status = {c: False for c in CAMERA_IDS + ["program"]}
_profile = "720p"
_relay_profile = None    # profile the running relay was built for
# width, height, YouTube bitrate, camera-relay bitrate (relay output is re-encoded, so it
# gets headroom). The relay runs at the stream resolution so 1080p is never upscaled.
PROFILES = {"1080p": (1920, 1080, "6000k", "8000k"),
            "720p": (1280, 720, "4000k", "4500k"),  # 4 Mbps: top of Facebook/YouTube 720p30 range
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
    def __init__(self, cmd, secret=None, cwd=None, copy=False):
        if not shutil.which(cmd[0]):
            raise FileNotFoundError(cmd[0])
        self.proc = subprocess.Popen([sys.executable, "-I", "-c", _WATCHDOG, *cmd],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True,
                                     start_new_session=True, bufsize=1, cwd=cwd)
        self.last_progress = 0.0
        self.first_progress = None  # (time.time(), media seconds) of the first progress report
        self.frame = 0
        self.out_time = 0
        # Stream copy reports frame=0, so its progress is advancing output time. Encoders
        # keep frame counting: a relay can emit generated silence with no picture.
        self.copy = copy
        self.total_size = 0
        self._samples = collections.deque(maxlen=60)  # (monotonic, out_time_us, total_size)
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
                            if self.first_progress is None:
                                self.first_progress = (time.time(), frame / 30)
                        self.frame = frame
                    except ValueError:
                        pass
                elif progress and self.copy and line.startswith("total_size="):
                    try:
                        self.total_size = int(line.split("=", 1)[1])
                    except ValueError:
                        pass
                elif progress and self.copy and line.startswith("progress="):
                    # End of one progress report: one sample for the upload rate.
                    self._samples.append((time.monotonic(), self.out_time, self.total_size))
                elif progress and self.copy and line.startswith("out_time_us="):
                    # Stream copy (YouTube pusher, recorder) reports frame=0: output time advances.
                    try:
                        out_time = int(line.split("=", 1)[1])
                        if out_time > self.out_time:
                            self.last_progress = time.monotonic()
                            if self.first_progress is None:
                                self.first_progress = (time.time(), out_time / 1e6)
                        self.out_time = out_time
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

    def rates(self, window=6.0):
        """(kbit/s sent, speed) over the last seconds; speed 1.0 = keeping up with real time.
        ffmpeg's own speed= averages the whole run and hides an uplink getting slow."""
        samples = list(self._samples)
        if len(samples) < 2:
            return None, None
        now = samples[-1]
        if time.monotonic() - now[0] > 3:  # no report for a while: nothing is going out
            return 0.0, 0.0
        first = next((x for x in samples if now[0] - x[0] <= window), samples[0])
        elapsed = now[0] - first[0]
        if elapsed < 1.5:
            return None, None
        return (round((now[2] - first[2]) * 8 / elapsed / 1000, 0),
                round((now[1] - first[1]) / 1e6 / elapsed, 2))


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
                "-timeout", "5000000", "-i", f"{RTSP_BASE}/{program_path() if key == 'program' else key}"]
    return cmd + ["-vn", "-af", METER_FILTER, "-f", "null", "-"]


def _supervise_meters(now):
    wanted = [c for c in CAMERA_IDS + ["program"] if _camera_status.get(c)]
    wanted += ["usb:" + source for source in _mic_sources]
    _meter_no_audio.intersection_update(wanted)
    for key, meter in list(_meters.items()):
        exited = meter.poll() is not None
        moved = key == "program" and getattr(meter, "path", None) != program_path()
        if key in wanted and not exited and not moved:
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
                _meters[key].path = program_path() if key == "program" else key
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


def _ensure_offline_card():
    """A 0.5 s "back in a moment" card MediaMTX loops on a feed without a publisher."""
    if Path(OFFLINE_CARD).is_file():
        return True
    try:
        Path(OFFLINE_CARD).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x101418:s=1280x720:r=30:d=0.5",
                        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-vf",
                        "drawtext=text='Back in a moment':fontcolor=white:fontsize=48:x=(w-tw)/2:y=(h-th)/2",
                        "-t", "0.5", "-c:v", "libx264", "-preset", "veryfast", "-bf", "0", "-pix_fmt", "yuv420p", "-g", "15",
                        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-shortest", OFFLINE_CARD + ".tmp.mp4"],
                       check=True, capture_output=True, timeout=30)
        os.replace(OFFLINE_CARD + ".tmp.mp4", OFFLINE_CARD)
        return True
    except (OSError, subprocess.SubprocessError):
        logger.warning("Cannot create the offline card; MediaMTX's default is used")
        return False


def _spare_slot():
    return 1 - _relay_slot


def _slot_zmq(slot):
    host, port = ZMQ_ADDRESS.rsplit(":", 1)
    return f"{host}:{int(port) + slot}"


def program_path():
    """The path the dashboard's program preview and meter read."""
    return "program"


def camera_password():
    """Password for publishing a camera; empty (the default) means no password.
    MCC_CAMERA_PASSWORD wins over the Settings tab."""
    if "MCC_CAMERA_PASSWORD" in os.environ:
        return os.environ["MCC_CAMERA_PASSWORD"]
    return settings_store.get("camera_password")


def camera_role(cam):
    return "report" if cam in REPORT_CAMERAS else "match"


def _default_label(cam):
    if cam in REPORT_CAMERAS:
        return f"Reporter {REPORT_CAMERAS.index(cam) + 1}"
    return f"Cam {cam[3:]}"


SRT_PORT = 8890
PROTOCOLS = ("rtmp", "srt")
SRT_LATENCY_MIN, SRT_LATENCY_MAX = 80, 2000   # ms; SRT resends lost packets within this delay
# What to set in the camera app: resolution, frame rate, video bitrate (kbit/s). Keyframe
# every second (our cuts start on one). Reporting cameras default lower: they move away
# from the router, and six cameras share one Wi-Fi.
QUALITIES = {"1080p": ("1920x1080", 30, 6000), "720p": ("1280x720", 30, 3000),
             "720p-low": ("1280x720", 30, 1800), "480p": ("854x480", 30, 1000)}
QUALITY_LABELS = {"1080p": "1080p · 6 Mbit/s", "720p": "720p · 3 Mbit/s",
                  "720p-low": "720p · 1.8 Mbit/s (weak Wi-Fi)", "480p": "480p · 1 Mbit/s (very weak link)"}


MOUNTS = ("fixed", "tracking", "handheld")  # tracking: XbotGo, motorised gimbal


def _default_camera(cam):
    # mount and wide matter for the future auto-director: a fixed camera covers a known part
    # of the pitch; a tracking camera follows play; the wide camera is the fallback shot.
    # SRT by default: steadier on weak Wi-Fi, H.265, encryption (RTMP stays accepted too).
    return {"label": _default_label(cam), "position": "", "protocol": "srt", "latency_ms": 300,
            "encrypt": False, "passphrase": "", "quality": "720p" if cam in REPORT_CAMERAS else "1080p",
            "mount": "handheld" if cam in REPORT_CAMERAS else "fixed", "wide": False}


def _clean_camera(cam, entry):
    """A camera's settings with every field valid (bad or missing fields get the default)."""
    clean = _default_camera(cam)
    entry = entry if isinstance(entry, dict) else {}
    label = str(entry.get("label") or "").strip()[:24]
    clean["label"] = label or clean["label"]
    clean["position"] = str(entry.get("position") or "").strip()[:40]
    if entry.get("protocol") in PROTOCOLS:
        clean["protocol"] = entry["protocol"]
    if type(entry.get("latency_ms")) is int and SRT_LATENCY_MIN <= entry["latency_ms"] <= SRT_LATENCY_MAX:
        clean["latency_ms"] = entry["latency_ms"]
    passphrase = entry.get("passphrase")
    if isinstance(passphrase, str) and re.fullmatch(r"[A-Za-z0-9_-]{10,79}", passphrase):
        clean["passphrase"] = passphrase
    clean["encrypt"] = entry.get("encrypt") is True and bool(clean["passphrase"])
    if entry.get("quality") in QUALITIES:
        clean["quality"] = entry["quality"]
    if entry.get("mount") in MOUNTS:
        clean["mount"] = entry["mount"]
    clean["wide"] = entry.get("wide") is True and cam in MATCH_CAMERAS
    return clean


def load_camera_setting():
    """The operator's camera settings: name, field position (e.g. "Left goal"), how the
    camera sends (RTMP or SRT, SRT delay and encryption) and the quality to set in its app."""
    try:
        saved = json.loads(CAMERA_SETTING.read_text())
    except (OSError, ValueError):
        saved = {}
    saved = saved if isinstance(saved, dict) else {}
    for cam in CAMERA_IDS:
        _camera_info[cam] = _clean_camera(cam, saved.get(cam))


def _srt_stream_id(cam):
    password = camera_password()
    return f"publish:{cam}:{CAMERA_USER}:{password}" if password else f"publish:{cam}"


def get_cameras():
    """Every camera with its role and settings, in display order. Holds SRT passphrases
    and the camera password: for the logged-in dashboard only."""
    if not _camera_info:
        load_camera_setting()
    cams = []
    for i, c in enumerate(CAMERA_IDS):
        info = _camera_info[c]
        size, fps, kbps = QUALITIES[info["quality"]]
        cams.append({"id": c, "role": camera_role(c), "key": str(i + 1), **info,
                     "short": f"REP {REPORT_CAMERAS.index(c) + 1}" if c in REPORT_CAMERAS else f"CAM {c[3:]}",
                     "srt_port": SRT_PORT, "srt_stream_id": _srt_stream_id(c),
                     "recommended": {"size": size, "fps": fps, "kbps": kbps, "keyframe_s": 1,
                                     "label": QUALITY_LABELS[info["quality"]]}})
    return cams


def set_camera_info(cam, label=None, position=None, protocol=None, latency_ms=None, encrypt=None,
                    quality=None, new_passphrase=False, mount=None, wide=None):
    """Change one camera's settings. Switching encryption (or a new passphrase) changes what
    MediaMTX accepts for that camera: the running server reloads it, only that camera reconnects."""
    if cam not in CAMERA_IDS:
        return False, "Invalid camera"
    if protocol is not None and protocol not in PROTOCOLS:
        return False, "Protocol must be rtmp or srt"
    if latency_ms is not None and (type(latency_ms) is not int or not SRT_LATENCY_MIN <= latency_ms <= SRT_LATENCY_MAX):
        return False, f"SRT delay must be {SRT_LATENCY_MIN}-{SRT_LATENCY_MAX} ms"
    if quality is not None and quality not in QUALITIES:
        return False, "Unknown quality"
    if mount is not None and mount not in MOUNTS:
        return False, "Mount must be fixed, tracking or handheld"
    if wide and cam not in MATCH_CAMERAS:
        return False, "Only a match camera can be the wide camera"
    if not _camera_info:
        load_camera_setting()
    with _lock:
        info = _camera_info[cam]
        before = (info["encrypt"], info["passphrase"])
        if label is not None:
            info["label"] = str(label).strip()[:24] or _default_label(cam)
        if position is not None:
            info["position"] = str(position).strip()[:40]
        if protocol is not None:
            info["protocol"] = protocol
        if latency_ms is not None:
            info["latency_ms"] = latency_ms
        if quality is not None:
            info["quality"] = quality
        if mount is not None:
            info["mount"] = mount
        if wide is not None:
            if wide:  # one wide camera: the shot when nobody sees the ball
                for other in _camera_info.values():
                    other["wide"] = False
            info["wide"] = bool(wide)
        if new_passphrase or (encrypt and not info["passphrase"]):
            info["passphrase"] = secrets.token_urlsafe(12)  # 16 characters, SRT allows 10-79
        if encrypt is not None:
            info["encrypt"] = bool(encrypt)
        reload = (info["encrypt"], info["passphrase"]) != before and (info["encrypt"] or before[0])
        data = {c: dict(v) for c, v in _camera_info.items()}
    CAMERA_SETTING.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = CAMERA_SETTING.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds passphrases
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, CAMERA_SETTING)
    if reload and get_mediamtx_running():
        generate_mediamtx_config()  # MediaMTX reloads a changed file by itself
        return True, "Camera saved; an SRT camera reconnects with the new setting"
    return True, "Camera saved"


_inputs = {}        # camera -> what arrives: protocol, codec, size, kbps, SRT loss/RTT
_input_bytes = {}   # camera -> (monotonic, bytes received) for the bitrate
_srt_stats = {}     # camera -> (packets received, packets lost) at the last look
SOURCE_PROTOCOLS = {"rtmpConn": "rtmp", "rtmpSource": "rtmp", "srtConn": "srt", "srtSource": "srt",
                    "rtspSession": "rtsp", "rtspsSession": "rtsp", "webRTCSession": "webrtc"}


def _note_input(p, now):
    """Record what a ready camera path receives (from the MediaMTX path list)."""
    name = p["name"]
    video = next((t for t in p.get("tracks2") or [] if t.get("codec") in VIDEO_CODECS), None)
    props = (video or {}).get("codecProps") or {}
    received = p.get("inboundBytes") or p.get("bytesReceived") or 0
    entry = _inputs.setdefault(name, {})
    entry.update(protocol=SOURCE_PROTOCOLS.get((p.get("source") or {}).get("type"), "other"),
                 codec=(video or {}).get("codec") or "", width=props.get("width"), height=props.get("height"),
                 audio=_camera_audio.get(name, False))
    last = _input_bytes.get(name)
    if last and now > last[0] and received >= last[1]:
        kbps = (received - last[1]) * 8 / 1000 / (now - last[0])
        entry["kbps"] = round(kbps if entry.get("kbps") is None else 0.5 * entry["kbps"] + 0.5 * kbps)
    _input_bytes[name] = (now, received)


def _update_srt_stats():
    """Packet loss (before SRT's resends repaired it), round trip and buffer of SRT cameras."""
    if not any(i.get("protocol") == "srt" for i in _inputs.values()):
        return
    try:
        response = requests.get(f"{MEDIAMTX_API}/v3/srtconns/list", timeout=2)
        response.raise_for_status()
        items = response.json().get("items", [])
    except (requests.RequestException, ValueError):
        return
    for conn in items:
        cam = conn.get("path")
        if conn.get("state") != "publish" or cam not in _inputs:
            continue
        received, lost = conn.get("packetsReceived") or 0, conn.get("packetsReceivedLoss") or 0
        before = _srt_stats.get(cam)
        if before and received > before[0] and lost >= before[1]:
            fresh, missing = received - before[0], lost - before[1]
            _inputs[cam]["loss_pct"] = round(100 * missing / (fresh + missing), 1)
        _srt_stats[cam] = (received, lost)
        _inputs[cam]["rtt_ms"] = round(conn.get("msRTT") or 0)
        _inputs[cam]["drops"] = conn.get("packetsReceivedDrop") or 0  # arrived too late: lost for good


def get_camera_inputs():
    """Per publishing camera: protocol, codec, picture size, bitrate, SRT loss and round trip,
    and a hint when it differs from what its settings recommend."""
    if not _camera_info:
        load_camera_setting()
    result = {}
    for cam, entry in list(_inputs.items()):
        if not _camera_status.get(cam):
            continue
        info = dict(entry)
        size, _, kbps = QUALITIES[_camera_info[cam]["quality"]]
        notes = []
        if info.get("kbps") is not None and info["kbps"] < kbps * 0.5:
            notes.append(f"only {info['kbps'] / 1000:.1f} of {kbps / 1000:g} Mbit/s arrive")
        if (info.get("loss_pct") or 0) >= 2:
            notes.append(f"{info['loss_pct']:g} % packets lost on the way (weak Wi-Fi)")
        if info.get("width") and f"{info['width']}x{info['height']}" != size \
                and info["width"] < int(size.split("x")[0]):
            notes.append(f"sends {info['width']}x{info['height']}, set {size} in the app")
        info["warning"] = "; ".join(notes)
        result[cam] = info
    return result


PREVIEW_HEIGHT = 360
PREVIEW_KBPS = 800


def preview_command(cam):
    """ffmpeg for the dashboard preview of one camera: H.264 360p, no sound (WebRTC previews are muted)."""
    return (f"ffmpeg -hide_banner -nostdin -loglevel error -rtsp_transport tcp -i rtsp://127.0.0.1:8554/{cam} "
            f"-an -vf scale=-2:{PREVIEW_HEIGHT} -c:v libx264 -preset ultrafast -tune zerolatency "
            f"-b:v {PREVIEW_KBPS}k -maxrate {PREVIEW_KBPS}k -bufsize {PREVIEW_KBPS}k -g 30 -pix_fmt yuv420p "
            f"-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8554/{cam}_preview")


def generate_mediamtx_config():
    # A continuous program path supplies video + silence during cuts/source loss.
    # Program publishing is loopback-only; phones may publish only camera paths.
    password = camera_password()
    cameras = "|".join(CAMERA_IDS)
    camera_publish = [{"action": "publish", "path": f"~^({cameras})$"}]
    config = {
        "logLevel": "warn", "api": True, "apiAddress": "127.0.0.1:9997",
        "rtsp": True, "rtspAddress": "127.0.0.1:8554", "rtspTransports": ["tcp"],
        "rtmp": True, "rtmpAddress": ":1935", "hls": True, "hlsAddress": ":8888",
        "webrtc": True, "webrtcAddress": ":8889", "srt": True, "srtAddress": f":{SRT_PORT}", "moq": False,
        "authInternalUsers": [
            {"user": CAMERA_USER, "pass": password, "ips": [], "permissions": camera_publish}
            if password else {"user": "any", "ips": [], "permissions": camera_publish},
            {"user": "any", "ips": [], "permissions": [{"action": "read"}]},
            {"user": "any", "ips": ["127.0.0.1", "::1"], "permissions": [
                {"action": "api"}, {"action": "publish", "path": f"~^(program|final|monitor|{cameras})(_preview)?$"}]},
        ],
        # A camera reconnecting after a Wi-Fi drop replaces its own stale session at once
        # (otherwise it is refused until MediaMTX times the dead one out).
        "paths": {c: {"overridePublisher": True} for c in CAMERA_IDS},
    }
    # Encrypted SRT: the camera must know its passphrase (RTMP is not affected).
    if not _camera_info:
        load_camera_setting()
    for cam in CAMERA_IDS:
        if _camera_info[cam]["encrypt"]:
            config["paths"][cam]["srtPublishPassphrase"] = _camera_info[cam]["passphrase"]
    # Browsers often cannot play H.265 over WebRTC ("codecs not supported by client"), so the
    # dashboard shows H.265 cameras via camN_preview: a small H.264 copy made by ffmpeg only
    # while someone watches it (MediaMTX starts it on the first viewer, stops it 10 s after the last).
    for cam in CAMERA_IDS:
        config["paths"][f"{cam}_preview"] = {
            "runOnDemand": preview_command(cam), "runOnDemandRestart": True,
            "runOnDemandStartTimeout": "15s", "runOnDemandCloseAfter": "10s"}
    program = {"alwaysAvailable": True}
    if _ensure_offline_card():
        program["alwaysAvailableFile"] = OFFLINE_CARD
    else:
        program["alwaysAvailableTracks"] = [
            {"codec": "H264"}, {"codec": "MPEG4Audio", "sampleRate": 48000, "channelCount": 2}]
    config["paths"]["program"] = program
    config["paths"]["final"] = {"overridePublisher": True}  # the encoder's finished picture
    # The live mix without the microphones, for the operator's headset (no own-voice echo).
    # Always available: the headset player stays connected through cuts and while off air.
    config["paths"]["monitor"] = {"alwaysAvailable": True, "alwaysAvailableTracks": [
        {"codec": "MPEG4Audio", "sampleRate": 48000, "channelCount": 2}]}
    Path(MEDIAMTX_CONFIG).parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    Path(MEDIAMTX_CONFIG).write_text(json.dumps(config, indent=2))  # JSON is valid YAML
    return MEDIAMTX_CONFIG


def _paths():
    response = requests.get(f"{MEDIAMTX_API}/v3/paths/list", timeout=2)
    response.raise_for_status()
    items = response.json().get("items", [])
    now = time.monotonic()
    for p in items:
        if p.get("name") in CAMERA_IDS:
            if p.get("ready"):
                _camera_audio[p["name"]] = any(t in AUDIO_CODECS for t in p.get("tracks") or [])
                source = (p.get("source") or {}).get("id")
                if _camera_source.get(p["name"]) != source:  # a new connection: fresh counters
                    _inputs.pop(p["name"], None); _input_bytes.pop(p["name"], None); _srt_stats.pop(p["name"], None)
                _camera_source[p["name"]] = source
                _note_input(p, now)
            else:
                _camera_audio.pop(p["name"], None)
                _camera_source.pop(p["name"], None)
                _inputs.pop(p["name"], None); _input_bytes.pop(p["name"], None); _srt_stats.pop(p["name"], None)
    for name in set(_camera_audio) - {p.get("name") for p in items}:
        _camera_audio.pop(name, None)
        _camera_source.pop(name, None)
        _inputs.pop(name, None); _input_bytes.pop(name, None); _srt_stats.pop(name, None)
    return {p["name"]: bool(p.get("ready")) for p in items}


def start_mediamtx():
    global _mediamtx_proc, _thread, _encoder_thread
    with _lock:
        if get_mediamtx_running():
            return True
        try:
            try:
                _paths()
            except requests.RequestException:
                pass
            else:
                _set_error("server", "MediaMTX API port is already in use by another server")
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
                    if _monitor["on"]:
                        start_monitor_worker()
                    _set_error("server")
                    return True
                except requests.RequestException:
                    time.sleep(.1)
            raise RuntimeError(_mediamtx_proc.error or "MediaMTX did not become ready")
        except Exception as exc:
            _set_error("server", str(exc))
            _kill_process(_mediamtx_proc)
            return False


def get_mediamtx_running():
    return _mediamtx_proc is not None and _mediamtx_proc.poll() is None


def get_camera_status():
    return dict(_camera_status)


def get_active_camera():
    relay = _relay_proc
    return _active_camera if relay and relay.healthy() and not _replay else None


def get_audio_source():
    """The preset matching the mixer ("off", "cam1", "active", "usb:<id>"), else "mix"."""
    cams_on = [c for c in MATCH_CAMERAS if _mix[c]["on"]]
    mics_on = [c for c in _mix if c.startswith("usb:") and _mix[c]["on"]]
    if _follow:
        return "active" if not mics_on else "mix"
    if not cams_on and not mics_on:
        return "off"
    if cams_on == ["cam1"] and not mics_on:
        return "cam1"
    if not cams_on and len(mics_on) == 1:
        return mics_on[0]
    return "mix"


AUDIO_SETTING = BASE / "data/audio.json"
MIC_DELAY_MAX = 3000
_mic_delay_ms = 0
# Noise gate on the microphones: below the threshold (crowd, PA music in the pauses) the
# mic is turned down by about 30 dB; speech opens it within milliseconds.
GATE_MIN_DB, GATE_MAX_DB = -70.0, -10.0
GATE_RANGE = 0.03          # gain while closed (about -30 dB)
_gate = {"on": True, "threshold_db": -45.0}
# Headset monitor: "nomic" = the live mix without the microphones (the operator hears their
# own voice through the headset's sidetone, not 1-3 s late); "full" = exactly what goes out.
MONITOR_MODES = ("nomic", "full")
_monitor = {"on": False, "mode": "nomic", "output": ""}


def load_audio_setting():
    global _mic_delay_ms, _gate, _monitor
    try:
        data = json.loads(AUDIO_SETTING.read_text())
        if not isinstance(data, dict):
            raise ValueError
    except (OSError, ValueError):
        data = {}
    value = data.get("mic_delay_ms", 0)
    _mic_delay_ms = value if type(value) is int and 0 <= value <= MIC_DELAY_MAX else 0
    gate = data.get("gate") if isinstance(data.get("gate"), dict) else {}
    threshold = gate.get("threshold_db", -45.0)
    _gate = {"on": gate.get("on", True) is not False,
             "threshold_db": float(threshold) if type(threshold) in (int, float)
             and GATE_MIN_DB <= threshold <= GATE_MAX_DB else -45.0}
    monitor = data.get("monitor") if isinstance(data.get("monitor"), dict) else {}
    output = monitor.get("output")
    _monitor = {"on": monitor.get("on") is True and isinstance(output, str) and bool(output),
                "mode": monitor.get("mode") if monitor.get("mode") in MONITOR_MODES else "nomic",
                "output": output if isinstance(output, str) and len(output) <= 300 else ""}


def _save_audio_setting():
    try:
        AUDIO_SETTING.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        AUDIO_SETTING.write_text(json.dumps({"mic_delay_ms": _mic_delay_ms, "gate": dict(_gate),
                                             "monitor": dict(_monitor)}))
    except OSError:
        logger.exception("Cannot save the audio settings")


def set_mic_delay(ms):
    """Delay USB microphones by ms so the commentary matches the (later) camera picture.
    A relay that plays a microphone is rebuilt with it (a seamless cut)."""
    global _mic_delay_ms
    if type(ms) is not int or not 0 <= ms <= MIC_DELAY_MAX:
        return False, f"Delay must be 0 to {MIC_DELAY_MAX} ms"
    with _lock:
        _mic_delay_ms = ms
        camera, uses_mic, replay = _active_camera, any(c.startswith("usb:") for c in _relay_channels), _replay
    _save_audio_setting()
    message = f"Microphone delay {ms / 1000:.1f} s"
    if camera and uses_mic and not replay and get_active_camera() == camera:
        ok, result = switch_camera(camera, force=True)
        if not ok:
            return False, result
    return True, message


def _gate_name(volume_name):
    """The gate filter of a microphone channel whose volume filter is volume_name (m3 -> g3)."""
    return "g" + volume_name[1:]


def _gate_levels():
    """(threshold, range) for agate: a fully open range (1.0) switches the gate off."""
    return 10 ** (_gate["threshold_db"] / 20), (GATE_RANGE if _gate["on"] else 1.0)


def _gate_options():
    threshold, gate_range = _gate_levels()
    return (f"threshold={threshold:.6f}:range={gate_range:g}:ratio=8:attack=5:release=300:"
            f"detection=rms")


def set_gate(on=None, threshold_db=None):
    """Microphone noise gate: on/off and threshold in dBFS. On/off is switched live in the
    running relay (agate's range). ffmpeg 6.1 accepts a live threshold command but ignores
    it, so a new threshold rebuilds a relay that plays a microphone (a seamless cut)."""
    global _gate
    if on is not None and type(on) is not bool:
        return False, "on must be true or false"
    if threshold_db is not None and (type(threshold_db) not in (int, float)
                                     or not GATE_MIN_DB <= threshold_db <= GATE_MAX_DB):
        return False, f"Threshold must be {GATE_MIN_DB:g} to {GATE_MAX_DB:g} dB"
    with _lock:
        previous = _gate["threshold_db"]
        _gate = {"on": _gate["on"] if on is None else on,
                 "threshold_db": _gate["threshold_db"] if threshold_db is None else round(float(threshold_db), 1)}
        rebuild = _gate["threshold_db"] != previous
        gates = [_gate_name(name) for channel, name in _relay_channels.items() if channel.startswith("usb:")]
        camera, replay = _active_camera, _replay
    _save_audio_setting()
    message = (f"Noise gate on at {_gate['threshold_db']:g} dB" if _gate["on"] else "Noise gate off")
    _, gate_range = _gate_levels()
    applied = not rebuild and all(_send_command(_relay_zmq or ZMQ_ADDRESS, f"agate@{gate} range {gate_range:g}")
                                  for gate in gates)
    # During a replay the new setting comes with the return to live.
    if gates and not applied and camera and not replay and get_active_camera() == camera:
        ok, result = switch_camera(camera, force=True)
        if not ok:
            return False, result
    return True, message


# ---- Headset monitor ------------------------------------------------------------------
_monitor_proc = None
_monitor_key = None        # (mode, output) the running player was started with
_monitor_retry_at = 0.0
_monitor_error = ""
_monitor_thread = None
MONITOR_RETRY = 2.0


def _monitor_command(mode, output):
    """Play the monitor mix (or exactly what goes out) on a PulseAudio/PipeWire output."""
    path = "monitor" if mode == "nomic" else "final"
    return _ffmpeg() + ["-fflags", "nobuffer", "-flags", "low_delay", "-probesize", "32768",
                        "-analyzeduration", "500000"] + _input(path) + [
        "-map", "0:a:0", "-ar", "48000", "-ac", "2",
        "-f", "pulse", "-buffer_duration", "120", "-device", output, "-name", "Matchday Control Center",
        "Headset monitor"]


def get_monitor():
    proc, setting = _monitor_proc, dict(_monitor)
    if proc is not None and _monitor_key != (setting["mode"], setting["output"]):
        proc = None  # still the player of the previous choice: about to be replaced
    if not setting["on"]:
        state = "off"
    elif proc is None and _monitor_proc is not None:
        state = "starting"
    elif proc is not None and proc.healthy():
        state = "playing"
    elif proc is not None and proc.poll() is None:
        state = "starting"
    else:
        state = "waiting"
    error = _monitor_error if state == "waiting" else ""
    if state == "waiting" and setting["mode"] == "full" and not _want_stream:
        error = "Nothing is going out yet — starts when you go live or record"
    return {**setting, "state": state, "error": error}


def set_monitor(on=None, mode=None, output=None):
    """Headset monitor on/off, mode ("nomic" or "full") and output device (a sink name)."""
    global _monitor
    if on is not None and type(on) is not bool:
        return False, "on must be true or false"
    if mode is not None and mode not in MONITOR_MODES:
        return False, "Invalid monitor mode"
    if output is not None and (not isinstance(output, str) or len(output) > 300
                               or any(c in output for c in "\n\r\0")):
        return False, "Invalid output device"
    with _lock:
        new = {"on": _monitor["on"] if on is None else on,
               "mode": mode or _monitor["mode"],
               "output": _monitor["output"] if output is None else output}
        if new["on"] and not new["output"]:
            return False, "Choose the headset first"
        _monitor = new
    _save_audio_setting()
    start_monitor_worker()
    if not new["on"]:
        return True, "Listening off"
    return True, "Listening: " + ("everything except the microphones" if new["mode"] == "nomic"
                                  else "exactly what goes out (incl. your mic, 1-3 s late)")


def _monitor_tick():
    """Keep the headset player in line with the setting; restart it when it stops."""
    global _monitor_proc, _monitor_key, _monitor_retry_at, _monitor_error
    with _lock:
        setting = dict(_monitor)
    key = (setting["mode"], setting["output"]) if setting["on"] else None
    proc = _monitor_proc
    if proc is not None and (key != _monitor_key or proc.poll() is not None):
        if proc.poll() is not None and proc.error:
            _monitor_error = proc.error
        _kill_process(proc, "monitor")
        _monitor_proc = None
        if key == _monitor_key:
            _monitor_retry_at = time.monotonic() + MONITOR_RETRY
        else:
            _monitor_retry_at = 0.0
    if key is None or _monitor_proc is not None or time.monotonic() < _monitor_retry_at:
        return
    if not get_mediamtx_running():
        return
    try:
        _monitor_proc = Process(_monitor_command(*key), copy=True)
        _monitor_key = key
        _monitor_error = ""
    except OSError as exc:
        _monitor_error = str(exc)
        _monitor_retry_at = time.monotonic() + MONITOR_RETRY


def _monitor_worker():
    while not _stop.wait(0.5):
        try:
            _monitor_tick()
        except Exception:
            logger.exception("Headset monitor")


def start_monitor_worker():
    global _monitor_thread
    with _lock:
        if _monitor_thread is None or not _monitor_thread.is_alive():
            _monitor_thread = threading.Thread(target=_monitor_worker, daemon=True, name="monitor")
            _monitor_thread.start()


def get_mixer():
    return {"follow": _follow, "muted": _muted, "preset": get_audio_source(), "mic_delay_ms": _mic_delay_ms,
            "gate": dict(_gate), "monitor": get_monitor(),
            "channels": {c: {**setting, "available": c in CAMERA_IDS or c[4:] in _mic_ids}
                         for c, setting in _mix.items()}}


_camera_audio = {}     # camera -> True/False: publishes an audio track (from the MediaMTX path list)
_camera_source = {}    # camera -> MediaMTX publisher session id; changes when a camera reconnects
_relay_sources = {}    # camera -> publisher session of every camera the running relay reads
_follow_last = None    # last camera with sound that "sound follows the cut" put on air


def _follow_channel(cam):
    """The camera whose sound plays with cam on air in follow mode. A camera without
    sound (e.g. the Raspberry Pi) keeps the last camera's sound instead of silence."""
    global _follow_last
    if _camera_audio.get(cam, True):
        _follow_last = cam
        return cam
    if _follow_last in MATCH_CAMERAS and _follow_last != cam and _camera_audio.get(_follow_last):
        return _follow_last
    return next((c for c in MATCH_CAMERAS if c != cam and _camera_audio.get(c)), cam)


def _effective_layout(cam, layout=None):
    """The layout a cut to cam really gets. Two pictures need a reporting camera among
    them (two match cameras are never combined), and cutting to the camera in the inset
    shows it full screen."""
    layout = dict(layout or _layout)
    if layout["mode"] != "single":
        second = layout["secondary"]
        if second == cam or not (cam in REPORT_CAMERAS or second in REPORT_CAMERAS):
            layout["mode"] = "single"
    return layout


def _effective_mix(cam):
    """Channels that should be on air when cam is cut, with their gain in dB. A reporting
    camera is heard while it is in the picture (full screen or inset), never otherwise."""
    wanted = {}
    layout = _effective_layout(cam)
    visible = {cam} | ({layout["secondary"]} if layout["mode"] != "single" else set())
    follow = _follow_channel(cam) if _follow and cam in MATCH_CAMERAS else None
    for channel, setting in _mix.items():
        if channel in REPORT_CAMERAS:
            on = setting["on"] and channel in visible
        elif channel in MATCH_CAMERAS and follow:
            on = channel == follow
        else:
            on = setting["on"]
        if on:
            wanted[channel] = setting["gain"]
    return wanted


def get_youtube_running():
    """True while at least one streaming destination is live."""
    return any(t["proc"] and t["proc"].healthy() for t in list(_targets.values()))


UPLOAD_SLOW, UPLOAD_FAILING = 0.97, 0.9   # speed below which an upload falls behind


def _upload_health(target):
    """Upload rate and whether this connection keeps up (speed ~1.0) or falls behind."""
    proc = target.get("proc")
    try:
        kbps, speed = proc.rates() if proc is not None and target["state"] == "live" else (None, None)
    except (TypeError, ValueError, AttributeError):
        kbps, speed = None, None
    if speed is None:
        health = None
    elif speed >= UPLOAD_SLOW:
        health = "good"
    elif speed >= UPLOAD_FAILING:
        health = "slow"
    else:
        health = "failing"
    return {"kbps": kbps, "speed": speed, "health": health}


def get_destinations():
    """Per-destination status for the dashboard (never the key or URL)."""
    return {name: {"label": t["label"], "state": t["state"], "error": t["error"], "started_at": t["started_at"],
                   **_upload_health(t)}
            for name, t in list(_targets.items())}


def _set_error(part, message=""):
    if message:
        if _errors.get(part) != message:
            _error_since[part] = time.monotonic()
        _errors[part] = message
    else:
        _errors.pop(part, None)
        _error_since.pop(part, None)


def _clear_errors(*parts):
    for part in parts or ERROR_PARTS:
        _set_error(part)


def _error_text():
    return "; ".join(_errors[part] for part in ERROR_PARTS if _errors.get(part))


def get_stream_status():
    # Status must remain responsive while a control operation awaits readiness.
    active = get_youtube_running()
    state = "reconnecting" if _want_stream and _state == "live" and not active else _state
    return {"active": active, "state": state,
            "active_camera": get_active_camera(), "selected_camera": _active_camera,
            "pending_camera": _pending_camera,
            "layout": dict(_layout), "cut_source": _cut_source,
            "secondary_camera": _relay_secondary if get_active_camera() else None,
            "layout_warning": _layout_warning if _relay_proc else "",
            "audio_source": get_audio_source(), "muted": _muted,
            "audio_on_air": [c for c in _relay_channels if c not in _relay_muted] if get_active_camera() else [],
            "audio_warning": _audio_warning if _relay_proc else "",
            "started_at": _started_at, "destinations": get_destinations(), "program_path": program_path(),
            "error": _error_text(), "encoder": get_encoder_status(), "profile": _profile, "fallback": get_active_camera() is None and not _replay,
            "recording": get_recording_status(), "replay": get_replay_status()}


def get_replay_status():
    replay = _replay
    status = {"active": bool(replay), "available": bool(_want_stream and _want_record and _recording)}
    if replay:
        status.update(label=replay["label"], speed=replay["speed"], ends_at=replay["ends_at"],
                      camera=replay["camera"], hold=replay.get("hold", False))
        if "position" in replay:  # where the playhead is on the recording timeline (CAM REPLAY bar)
            status["position"] = round(min(replay["position"] + replay["length"],
                                           replay["position"] + (time.time() - replay["on_air_at"]) * replay["speed"]), 1)
    return status


def add_replay_listener(listener):
    _replay_listeners.append(listener)


def _notify_replay(active):
    for listener in list(_replay_listeners):
        try:
            listener(active)
        except Exception:
            logger.exception("Replay listener failed")


def get_recording_status():
    session, recorder = _recording, _recorder_proc
    status = {"active": bool(_want_record and session), "session": session,
              "writing": bool(recorder and recorder.healthy()),
              "warning": _record_warning or (_disk_warning if _want_record and session else ""),
              "iso": get_iso_status(), "iso_match": _iso_match}
    if session:
        try:
            info = recordings.get(session, live_wall=time.time())
            status.update(duration=info["duration"], bytes=info["bytes"], markers=len(info["markers"]))
        except (ValueError, OSError):
            pass
    return status


def _ffmpeg():
    return ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "warning",
            "-nostats", "-progress", "pipe:1"]


def _relay_ffmpeg():
    """A relay reports progress ten times a second: a cut waits for its first frames."""
    return _ffmpeg() + ["-stats_period", "0.1"]


def _input(path):
    return ["-rtsp_transport", "tcp", "-timeout", "5000000", "-i", f"{RTSP_BASE}/{path}"]


def _camera_input(path, audio_only=False):
    """A camera input of a relay. Deep queue: with several cameras (picture-in-picture,
    commentary from another camera) the default 8 packets blocked the start of a cut.
    A camera that only contributes sound is fetched without its video."""
    # MediaMTX announces the tracks, so ffmpeg's default stream probing only delayed every
    # cut by about a second (measured: new picture on air after 2.4 s, 1.2 s with this).
    options = ["-thread_queue_size", "512", "-probesize", "500000", "-analyzeduration", "500000"]
    if audio_only:
        options += ["-allowed_media_types", "audio"]
    elif _relay_uses_nvenc() and _NVDEC:
        options += ["-hwaccel", "cuda"]  # decode on the GPU; frames come back for the filters
    return options + _input(path)


VIDEO_CODECS = {"H264", "H265", "AV1", "VP8", "VP9", "MPEG-4 Video", "MPEG-1/2 Video", "M-JPEG"}
AUDIO_CODECS = {"MPEG-4 Audio", "MPEG-4 Audio LATM", "Opus", "G711", "G722", "MPEG-1/2 Audio", "AC-3", "LPCM", "Speex"}


def _probe(cam):
    """Track kinds of a published path, from MediaMTX in milliseconds (ffprobe took 1-2 s
    per camera and slowed every cut); ffprobe only if the API cannot be reached."""
    try:
        response = requests.get(f"{MEDIAMTX_API}/v3/paths/get/{cam}", timeout=2)
        if response.status_code == 404:
            raise ValueError(f"Cannot read {cam}")
        response.raise_for_status()
        info = response.json()
    except (requests.RequestException, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc) == f"Cannot read {cam}":
            raise
        return _ffprobe(cam)
    if not info.get("ready"):
        raise ValueError(f"Cannot read {cam}")
    tracks = info.get("tracks") or []
    return {"video" for t in tracks if t in VIDEO_CODECS} | {"audio" for t in tracks if t in AUDIO_CODECS}


def _ffprobe(cam):
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


def _video_filters(width, height, secondary_index, layout):
    def fit(pad, w, h, label):
        return (f"[{pad}]setpts=PTS-STARTPTS,scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[{label}]")
    if secondary_index is None or layout["mode"] == "single":
        return [fit("0:v:0", width, height, "vout")]
    if layout["mode"] == "split":
        left = width // 4 * 2  # keep both halves even at 854px width
        return [fit("0:v:0", left, height, "left"),
                fit(f"{secondary_index}:v:0", width-left, height, "right"),
                "[left][right]hstack=inputs=2:shortest=0[vout]"]
    inset_w, inset_h = width // 4 * 2, height // 4 * 2
    x = "W-w-16" if layout["position"].endswith("right") else "16"
    y = "H-h-16" if layout["position"].startswith("bottom") else "16"
    return [fit("0:v:0", width, height, "main"),
            fit(f"{secondary_index}:v:0", inset_w, inset_h, "inset"),
            f"[main][inset]overlay=x={x}:y={y}:eof_action=pass:repeatlast=0[vout]"]


def _scoreboard_filter(width, height):
    """The built-in scoreboard, drawn by the live relay so it travels with the live picture:
    drawn in the encoder, it switched off (and back on) seconds before (after) a replay's
    picture, so live play aired without a score and replay frames got two scoreboards."""
    font = round(height / 26)  # same on-screen size at every resolution
    # Disable drawtext expansion: team names are literal text, including percent signs.
    escaped = SCOREBOARD_PATH.replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''")
    return (f"drawtext=textfile='{escaped}':reload=1:expansion=none:"
            f"fontcolor=white:fontsize={font}:box=1:boxcolor=black@0.6:boxborderw={font // 3}:"
            f"x={font * 2 // 3}:y=h-th-{font * 2 // 3}")


def _replay_filter(width, height, replay):
    """Replay picture, slowed for slow motion, with a REPLAY label (top left: the
    scoreboard burned into the recording stays visible at the bottom)."""
    font = round(height / 22)
    label = "SLOW-MO REPLAY" if replay["speed"] < 1 else "REPLAY"
    return (f"[0:v:0]setpts=(PTS-STARTPTS)/{replay['speed']:g},scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,"
            # Hold the last frame until the live relay takes over: never a black gap. Paced to
            # real time: unpaced held frames ran ahead and cost the recording time at handover.
            f"tpad=stop=-1:stop_mode=clone,realtime,"
            f"drawtext=text='{label}':expansion=none:fontcolor=white:fontsize={font}:box=1:"
            f"boxcolor=0xdc2626@0.95:boxborderw={font // 3}:x={font}:y={font}[vout]")


def _relay_command(cam, wanted, replay=None):
    """Return command, channel filters, missing audio, audio warning, second camera,
    and video warning for cutting
    to cam with the wanted {channel: gain dB} mix. Only missing video is fatal: if no
    wanted audio source is available, the camera's own sound (or silence) is used so the
    picture never goes to fallback because of a microphone.
    With replay, the picture comes from the recording and the live mix plays over it."""
    if replay:
        cam, tracks = None, {"video"}
        cmd = _relay_ffmpeg() + ["-readrate", f"{replay['speed']:g}", "-ss", f"{replay['start']:.2f}",
                           "-t", f"{replay['length']:.2f}", "-i", replay["source"]]
        layout = {"mode": "single"}
        inputs_by_camera = {}
    else:
        tracks = _probe(cam)
        if "video" not in tracks:
            raise ValueError(f"{cam} has no video")
        cmd = _relay_ffmpeg() + _camera_input(cam)
        layout = _effective_layout(cam)
        inputs_by_camera = {cam: (0, tracks)}
    sources, missing, notes, next_input = [], set(), [], 1
    secondary, secondary_index, video_warning = None, None, ""
    if layout["mode"] != "single":
        selected = layout["secondary"]
        if selected == cam:
            raise ValueError("Choose different main and second cameras")
        try:
            secondary_tracks = _probe(selected)
        except ValueError:
            secondary_tracks = set()
        if "video" in secondary_tracks:
            secondary, secondary_index = selected, next_input
            inputs_by_camera[selected] = (next_input, secondary_tracks)
            cmd += _camera_input(selected)
            next_input += 1
        else:
            video_warning = f"{selected} is offline — showing main camera only"
    for channel, gain in wanted.items():
        if channel == cam:
            if "audio" in tracks:
                sources.append((channel, "0:a:0", gain))
            else:
                missing.add(channel)
                notes.append(f"{cam} has no audio")
        elif channel in inputs_by_camera:
            index, channel_tracks = inputs_by_camera[channel]
            if "audio" in channel_tracks:
                sources.append((channel, f"{index}:a:0", gain))
            else:
                missing.add(channel)
                notes.append(f"{channel} has no audio")
        elif channel in CAMERA_IDS:
            # A replay reuses what the live relay already plays instead of probing again.
            if (replay and channel in _relay_channels) or _channel_available(channel):
                sources.append((channel, f"{next_input}:a:0", gain))
                cmd += _camera_input(channel, audio_only=True)
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
    # Paced to real time: once the camera picture ends, unpaced silence would flood program
    # (and the recording) with minutes of audio per second until the relay is replaced.
    cmd += ["-re", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    parts, labels, monitor_labels, channels = [], [], [], {}
    for index, (channel, pad, gain) in enumerate(sources):
        name = f"m{index}"
        mic = channel.startswith("usb:")
        # A USB microphone is heard at once, the camera picture arrives a second or two
        # later: the delay lines the commentary up with the picture.
        delay = f"adelay=delays={_mic_delay_ms}:all=1," if mic and _mic_delay_ms else ""
        # The gate works on the mic's own level, before its fader.
        gate = f"agate@{_gate_name(name)}={_gate_options()}," if mic else ""
        # The headset monitor gets every source except the microphones.
        split = f",asplit=2[{name}][{name}x]" if not mic else f"[{name}]"
        parts.append(f"[{pad}]{delay}aresample=48000:async=1,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                     f"{gate}volume@{name}={0.0 if _muted else _db_to_volume(gain):.4f}{split}")
        labels.append(f"[{name}]")
        if not mic:
            monitor_labels.append(f"[{name}x]")
        channels[channel] = name
    address = _slot_zmq(_spare_slot()).replace(":", "\\\\:")  # escaped twice: filter option, then graph
    parts.append(f"[{inputs}:a:0]asplit=2[silence][silencex]")
    parts.append(f"[silence]{''.join(labels)}amix=inputs={len(labels) + 1}:duration=first:"
                 f"normalize=0:dropout_transition=0,alimiter=limit=0.891:level=0,"
                 f"azmq=bind_address={address}[aout]")
    parts.append(f"[silencex]{''.join(monitor_labels)}amix=inputs={len(monitor_labels) + 1}:duration=first:"
                 f"normalize=0:dropout_transition=0,alimiter=limit=0.891:level=0[mon]")
    width, height, _, bitrate = PROFILES[_profile]
    if replay:  # the recording's own scoreboard is in the replay picture
        parts.append(_replay_filter(width, height, replay))
    else:
        video = _video_filters(width, height, secondary_index, layout)
        video[-1] = video[-1][:-len("[vout]")] + f"[live];[live]{_scoreboard_filter(width, height)}[vout]"
        parts += video
    return cmd + ["-filter_complex", ";".join(parts), "-map", "[vout]", "-map", "[aout]",
        *_relay_video_codec(), "-pix_fmt", "yuv420p", "-g", "30", "-b:v", bitrate,
        "-c:a", "aac", "-ar", "48000", "-ac", "2",
        "-shortest", "-f", "rtsp", "-rtsp_transport", "tcp", f"{RTSP_BASE}/program",
        # The headset monitor: a failure here must never touch the program output.
        "-map", "[mon]", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
        "-f", "tee", f"[f=rtsp:rtsp_transport=tcp:onfail=ignore]{RTSP_BASE}/monitor"], \
        channels, missing, warning, secondary, video_warning


def _relay_video_codec():
    """The relay re-encodes every cut; on the NVIDIA GPU that costs almost no CPU."""
    if _relay_uses_nvenc():
        return ["-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-bf", "0", "-rc", "cbr"]
    return ["-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency"]


def _relay_nvenc_failed(problem):
    """A relay on the NVIDIA encoder did not start: continue in software (until the next start)."""
    global _relay_hw_failed
    if problem == SUPERSEDED or not _relay_uses_nvenc():
        return False
    _relay_hw_failed = True
    logger.warning("NVIDIA encoder failed in a relay (%s); using the software encoder", problem)
    return True


def _send_volume(name, volume):
    """Set one channel's volume in the running relay without restarting it."""
    return _send_command(_relay_zmq or ZMQ_ADDRESS, f"volume@{name} volume {volume:.4f}")


def _send_command(address, command):
    """One filter command over ZMQ (a relay's volumes, the encoder's feed switch)."""
    import zmq
    sock = zmq.Context.instance().socket(zmq.REQ)
    for option, value in ((zmq.LINGER, 0), (zmq.RCVTIMEO, 1000), (zmq.SNDTIMEO, 1000)):
        sock.setsockopt(option, value)
    try:
        sock.connect(address)
        sock.send_string(command)
        return sock.recv_string().startswith("0 ")
    except zmq.ZMQError:
        return False
    finally:
        sock.close()


_retiring = None  # thread stopping the relay that was just replaced


def _retire(proc):
    """Stop a replaced relay in the background. It is already off air; waiting for it to
    exit (about a second) delayed the answer to the cut and blocked the next one."""
    global _retiring
    if not proc:
        return
    _retiring = threading.Thread(target=_kill_process, args=(proc,), daemon=True, name="relay-retire")
    _retiring.start()


def _await_retired():
    """Before another relay starts: the previous one must have released its control port."""
    worker = _retiring
    if worker and worker is not threading.current_thread():
        worker.join(timeout=7)


def _take_over(cmd, current):
    """Start a relay while the current one stays on air; MediaMTX hands program over to it
    when it publishes. Returns (process, None) or (None, error message)."""
    try:
        candidate = Process(cmd)
    except OSError as exc:
        return None, f"Cannot start relay: {exc}"
    ok = _wait_progress(candidate, aborted=lambda: not current())
    if ok and current():
        return candidate, None
    message = SUPERSEDED if not current() else candidate.error or "Relay did not produce frames"
    _kill_process(candidate)
    return None, message


def _wait_progress(proc, timeout=12, aborted=lambda: False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and proc.poll() is None and not aborted():
        if proc.healthy():
            return True
        time.sleep(.1)
    return False


SUPERSEDED = "Superseded by a newer request"
BUSY = "A cut is already in progress"


CUT_SOURCES = ("manual", "auto")


def switch_camera(cam_id, automatic=False, expected_stop=None, force=False, source="manual"):
    """The one entry point for cuts. Operator cuts supersede any cut in flight. Automatic
    (supervisor) cuts never wait and never supersede an operator. An operator cut to the
    camera that is already on air changes nothing (a double tap must not rebuild the
    relay), unless force. source "auto" is a cut chosen by an auto-director: it may only
    pick match cameras, and it never overrides a manual cut made in the last few seconds."""
    global _generation, _pending_camera, _cut_source, _manual_cut_at
    if cam_id not in CAMERA_IDS:
        return False, "Invalid camera"
    if source not in CUT_SOURCES:
        return False, "Invalid cut source"
    if source == "auto" and cam_id not in MATCH_CAMERAS:
        return False, "An automatic cut may only choose a match camera"
    if source == "auto" and time.monotonic() - _manual_cut_at < MANUAL_HOLD:
        return False, "The operator cut a moment ago"
    with _lock:
        if expected_stop is not None and expected_stop != _stop_generation:
            return False, SUPERSEDED
        if not get_mediamtx_running():
            return False, "MediaMTX is not ready"
        if (not automatic and not force and _pending_camera is None
                and cam_id == _active_camera and get_active_camera() == cam_id):
            return True, f"Camera {cam_id} is already on air"
        if not automatic:
            _generation += 1
            _pending_camera = cam_id
            _cut_source = source
            if source == "manual":
                _manual_cut_at = time.monotonic()
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
    global _relay_proc, _active_camera, _relay_channels, _relay_missing, _audio_warning
    global _relay_failures, _relay_profile, _relay_muted, _relay_secondary, _layout_warning, _layout, _replay
    global _relay_sources
    global _relay_zmq, _relay_slot
    global _cut_seconds
    current = lambda: generation == _generation
    if not current():
        return False, SUPERSEDED
    began = time.monotonic()
    wanted, profile = _effective_mix(cam_id), _profile
    sessions = dict(_camera_source)
    try:
        # Probe and validate before disturbing the current publisher.
        cmd, channels, missing, warning, secondary, video_warning = _relay_command(cam_id, wanted)
    except Exception as exc:
        if current():
            _set_error("relay", str(exc))
        return False, str(exc)
    with _lock:
        if not current():
            return False, SUPERSEDED
        old, slot = _relay_proc, _spare_slot()
    # The current relay stays on air while the new one starts.
    _await_retired()
    candidate, problem = _take_over(cmd, current)
    if candidate is None and _relay_nvenc_failed(problem):
        return _replace_relay(cam_id, generation)
    if candidate is None:
        if problem != SUPERSEDED:
            _set_error("relay", problem)
        return False, problem
    with _lock:
        installed = current()
        ended_replay = installed and _replay is not None
        if installed:
            _replay, _relay_slot, _relay_zmq = None, slot, _slot_zmq(slot)
            if _relay_proc is old:
                _relay_proc = None
            _relay_proc, _active_camera, _relay_profile = candidate, cam_id, profile
            used = {cam_id, secondary, *(c for c in channels if c in CAMERA_IDS)} - {None}
            _relay_sources = {c: sessions.get(c) for c in used}
            _relay_channels, _relay_missing, _audio_warning = channels, missing, warning
            _relay_muted = set(channels) if _muted else set()
            _relay_secondary, _layout_warning = secondary, video_warning
            # A cut that cannot keep two pictures (see _effective_layout) goes full screen.
            _layout = {**_layout, "main": cam_id, "mode": _effective_layout(cam_id)["mode"]}
            _relay_failures = 0
            _set_error("relay")
    if not installed:
        _kill_process(candidate)
        return False, SUPERSEDED
    if installed:
        _retire(old)
        _cut_seconds = round(0.5 * _cut_seconds + 0.5 * (time.monotonic() - began), 2)
        _log_cut()
        _save_program(cam_id)
    if ended_replay:
        _notify_replay(False)
    if installed:
        if warning:
            logger.warning("Relay %s: %s", cam_id, warning)
        return True, f"Camera {cam_id} selected" + (f" ({warning})" if warning else "")


def play_replay(seconds=10, speed=1.0, marker=None, pre=8.0, post=4.0, start=None, hold=False):
    """Put a replay on air: the last `seconds`, around a highlight marker, or (CAM REPLAY)
    from `start` on the recording timeline. Returns to the live camera when it ends,
    unless `hold`: then it plays on (holding its last frame at the end of what is
    recorded) until the operator cuts to a camera."""
    global _generation, _pending_camera
    if speed not in REPLAY_SPEEDS:
        return False, "Choose normal speed or slow motion"
    if marker is None and start is None and not 3 <= seconds <= 30:
        return False, "Replay length must be 3 to 30 seconds"
    with _lock:
        if not _want_stream or not get_mediamtx_running():
            return False, "Go live or start recording first"
        if not (_want_record and _recording):
            return False, "Instant replay needs the recording on"
        session = _recording
        camera = _replay["camera"] if _replay else _active_camera
    info = recordings.get(session)
    recorded = recordings.written_duration(session) - REPLAY_MARGIN
    if start is not None:
        start = min(float(start), recorded - 2)
        length, label = recorded - max(0.0, start), "Replay"
    elif marker is not None:
        found = next((m for m in info["markers"] if m["id"] == marker), None)
        if found is None:
            return False, "Unknown highlight"
        start, label = found["offset"] - pre, found["label"]
        length = pre + post
    else:
        start, length, label = recorded - seconds, seconds, "Replay"
    start = max(0.0, start)
    length = min(length, recorded - start)
    if length < 2:
        return False, "Not enough has been recorded yet"
    try:
        source, offset = recordings.source_at(session, start)
    except ValueError as exc:
        return False, str(exc)
    _start_live_capture(camera, session)  # before the replay covers the live game
    with _lock:
        _generation += 1
        _pending_camera = None
        generation = _generation
    with _switch_lock:
        return _install_replay(generation, camera, {"source": str(source), "start": offset, "length": length,
                                                    "speed": speed, "label": label, "hold": bool(hold),
                                                    "position": start})


def _install_replay(generation, camera, replay):
    global _relay_proc, _relay_channels, _relay_missing, _relay_muted, _audio_warning
    global _relay_secondary, _replay, _relay_zmq, _relay_slot
    current = lambda: generation == _generation
    if not current():
        return False, SUPERSEDED
    wanted = _effective_mix(camera) if camera else {}
    try:
        cmd, channels, missing, warning, _, _ = _relay_command(camera, wanted, replay=replay)
    except Exception as exc:
        return False, str(exc)
    with _lock:
        if not current():
            return False, SUPERSEDED
        old, slot = _relay_proc, _spare_slot()
    _await_retired()
    candidate, problem = _take_over(cmd, current)  # live stays on air until the replay is ready
    if candidate is None and _relay_nvenc_failed(problem):
        return _install_replay(generation, camera, replay)
    if candidate is None:
        return False, problem
    airtime = replay["length"] / replay["speed"]
    with _lock:
        installed = current()
        if installed:
            _relay_proc, _relay_channels, _relay_missing = candidate, channels, missing
            _relay_slot, _relay_zmq = slot, _slot_zmq(slot)
            _relay_muted = set(channels) if _muted else set()
            _audio_warning, _relay_secondary = warning, None
            _replay = {**replay, "camera": camera, "generation": generation, "ends_at": time.time() + airtime,
                       "on_air_at": time.time()}
    if not installed:
        _kill_process(candidate)
        return False, SUPERSEDED
    _retire(old)
    _log_cut("replay")
    _notify_replay(True)
    threading.Thread(target=_watch_replay, args=(candidate, generation, camera, airtime, replay["hold"]),
                     daemon=True, name="replay-watcher").start()
    kind = "Slow-motion replay" if replay["speed"] < 1 else "Replay"
    if replay["hold"]:
        return True, f"CAM REPLAY on air ({kind.lower()}) until you cut to a camera"
    return True, f"{kind} on air ({airtime:.0f} s)"


def _watch_replay(proc, generation, camera, airtime, hold=False):
    """Shortly before the clip ends, start the live relay; it takes over from the replay's
    held last frame as soon as it is ready. An operator cut or STOP ends this early.
    CAM REPLAY (hold) stays on air until a cut; this only cleans up if it crashes."""
    global _replay, _relay_proc
    # The live relay takes over as soon as it is ready: start it as long before the end
    # as a cut takes here, so the replay's last frame is held as briefly as possible.
    returning = float("inf") if hold else time.monotonic() + max(0.0, airtime - max(REPLAY_LEAD, _cut_seconds + 0.5))
    def owns_replay():
        replay = _replay
        return replay is not None and replay["generation"] == generation

    # A request generation changes before its replacement is ready. Keep the replay
    # until a successful handover (or STOP) actually removes its ownership.
    while proc.poll() is None and owns_replay() and time.monotonic() < returning:
        time.sleep(.1)
    while camera and not hold and proc.poll() is None and owns_replay():
        if not _switch_lock.acquire(blocking=False):
            time.sleep(.1)
            continue
        try:
            with _lock:
                if not owns_replay():
                    return
                token = _stop_generation
                pending = _pending_camera is not None
            if pending:
                ok, message = False, BUSY
            else:
                ok, message = switch_camera(camera, automatic=True, expected_stop=token)
        finally:
            _switch_lock.release()
        if ok:
            return
        if message not in (BUSY, SUPERSEDED):
            logger.warning("Back to live after replay: %s", message)
            break
        time.sleep(.1)
    with _lock:
        ended = _replay is not None and _replay["generation"] == generation
        if ended:
            # The live camera could not be restored: leave it to the supervisor.
            _replay = None
            if _relay_proc is proc:
                _relay_proc = None
    if ended:
        _kill_process(proc)
        _notify_replay(False)


def _start_live_capture(camera, session):
    """Record the live camera while a replay covers it on the program (and therefore in
    the recording). Copy only: no measurable CPU. A replay over a replay keeps it."""
    global _live_capture
    with _lock:
        capture = _live_capture
        if (capture and capture["camera"] == camera and capture["session"] == session
                and capture["proc"].poll() is None):
            capture["idle_since"] = None
            return
    if capture:
        _end_live_capture()
    if not camera or not _camera_status.get(camera):
        return
    try:
        path = recordings.new_live_window(session, camera)
        proc = Process(_ffmpeg() + _camera_input(camera) + [
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-flush_packets", "1", "-f", "mpegts", path], copy=True)
    except (OSError, ValueError) as exc:
        logger.warning("Cannot record the live camera during the replay: %s", exc)
        return
    with _lock:
        _live_capture = {"proc": proc, "camera": camera, "session": session, "path": path,
                         "aired": False, "idle_since": None}


def _end_live_capture(background=True):
    global _live_capture
    with _lock:
        capture, _live_capture = _live_capture, None
    if not capture:
        return
    _kill_process(capture["proc"])
    ended = time.time()
    close = lambda: recordings.close_live_window(capture["session"], capture["path"], capture["aired"], ended)
    if background:
        threading.Thread(target=close, daemon=True, name="live-window").start()
    else:
        close()


def _supervise_live_capture(now):
    """End the live camera recording a little after the replay; restart it if it died."""
    with _lock:
        capture, replay = _live_capture, _replay
        if not capture:
            return
        if capture["session"] != _recording:
            finished = True
        elif replay:
            capture["aired"], capture["idle_since"] = True, None
            finished = False
        else:
            capture["idle_since"] = capture["idle_since"] or now
            finished = now - capture["idle_since"] >= LIVE_TAIL
        died = not finished and capture["proc"].poll() is not None
    if finished:
        _end_live_capture()
    elif died:
        logger.warning("Live camera recording during the replay stopped: %s", capture["proc"].error)
        aired = capture["aired"]
        _end_live_capture()
        if replay:
            _start_live_capture(capture["camera"], capture["session"])
            with _lock:
                if _live_capture:
                    _live_capture["aired"] = aired


_iso = {}                 # camera -> {"proc", "session", "path", "noted"}
RECORDING_SETTING = BASE / "data/recording.json"

# The camera on air survives a restart of the app: saved after every cut, put back at start
# (the relay supervisor builds the program as soon as that camera publishes). None = off:
# the app switches it on in initialize(), so tests never write the operator's real file.
PROGRAM_SETTING = None
_saved_program = None


def _save_program(cam):
    global _saved_program
    if PROGRAM_SETTING is None or cam == _saved_program:
        return
    try:
        PROGRAM_SETTING.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = PROGRAM_SETTING.with_suffix(".tmp")
        tmp.write_text(json.dumps({"camera": cam, "saved_at": time.time()}))
        os.replace(tmp, PROGRAM_SETTING)
        _saved_program = cam
    except OSError:
        logger.exception("Cannot save the camera on air")


def load_program_setting(path):
    """Enable saving the camera on air and select the saved one again (at app start)."""
    global PROGRAM_SETTING, _active_camera, _last_camera, _saved_program
    PROGRAM_SETTING = Path(path)
    try:
        cam = json.loads(PROGRAM_SETTING.read_text()).get("camera")
    except (OSError, ValueError, AttributeError):
        return None
    if cam not in CAMERA_IDS:
        return None
    with _lock:
        if _active_camera is None:
            _active_camera, _saved_program = cam, cam
            _last_camera = _last_camera or cam
            logger.info("Camera on air before the restart: %s (on air again once it publishes)", cam)
    return cam
_iso_match = False        # also record every match camera on its own (material for the auto-director)
_cut_logged = None        # (session, camera, mode, secondary) last written to the session's cut log
_iso_retry = {}           # reporting camera -> monotonic time of the next start attempt
_iso_lock = threading.Lock()
ISO_RETRY = 3.0


def _iso_command(cam, path):
    return _ffmpeg() + _camera_input(cam) + [
        "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-flush_packets", "1", "-f", "mpegts", path]


def load_recording_setting():
    global _iso_match
    try:
        _iso_match = json.loads(RECORDING_SETTING.read_text()).get("match_cameras") is True
    except (OSError, ValueError, AttributeError):
        _iso_match = False


def set_iso_match(on):
    """Also record the match cameras on their own (copy only: disk, not CPU)."""
    global _iso_match
    _iso_match = bool(on)
    try:
        RECORDING_SETTING.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        RECORDING_SETTING.write_text(json.dumps({"match_cameras": _iso_match}))
    except OSError:
        logger.exception("Cannot save the recording setting")
    return True, "Match cameras are recorded separately too" if _iso_match else "Only reporting cameras are recorded separately"


def _iso_cameras():
    return REPORT_CAMERAS + (MATCH_CAMERAS if _iso_match else [])


def _iso_bytes_per_hour():
    """What the separately recorded cameras add to the disk per hour (measured, else recommended)."""
    if not _camera_info:
        load_camera_setting()
    total = 0
    for cam in list(_iso):
        kbps = (_inputs.get(cam) or {}).get("kbps") or QUALITIES[_camera_info[cam]["quality"]][2]
        total += kbps * 1000 / 8 * 3600
    return total


def _log_cut(source=None):
    """Write what is on air to the session's cut log when it changed: the operator's cuts
    are the reference the auto-director is tested against."""
    global _cut_logged
    with _lock:
        session = _recording if _want_record else None
        if not session:
            return
        if _replay:
            camera, mode, secondary = "replay", "single", None
        else:
            camera = get_active_camera()
            if not camera:
                return
            mode = _layout["mode"] if _relay_secondary else "single"
            secondary = _relay_secondary
        state = (session, camera, mode, secondary)
        if state == _cut_logged:
            return
        first = _cut_logged is None or _cut_logged[0] != session
        _cut_logged = state
        source = "start" if first else source or ("replay" if camera == "replay" else _cut_source)
    try:
        recordings.add_cut(session, camera, source, mode, secondary)
    except (ValueError, OSError) as exc:
        logger.warning("Cannot log the cut: %s", exc)


def _supervise_iso(now):
    """Record every publishing reporting camera (and, when chosen, every match camera) on its
    own while a recording runs (copy only, on air or not). A camera that drops out ends its
    part; it continues in a new part when it is back. Serialized with _end_iso_all so STOP
    never leaves one running."""
    _log_cut()
    with _iso_lock:
        with _lock:
            session = _recording if _want_record else None
        wanted_cams = _iso_cameras()
        for cam in [c for c in _iso if c not in wanted_cams]:
            _kill_process(_iso.pop(cam)["proc"])
        for cam in wanted_cams:
            entry = _iso.get(cam)
            wanted = bool(session and _camera_status.get(cam))
            if entry:
                proc = entry["proc"]
                died = proc.poll() is not None
                if not wanted or entry["session"] != session or died:
                    if died and wanted:
                        logger.warning("Recording of %s stopped: %s", cam, proc.error)
                        _iso_retry[cam] = now + ISO_RETRY
                    _kill_process(_iso.pop(cam)["proc"])
                    entry = None
                elif not entry["noted"] and proc.first_progress:
                    wall, seconds = proc.first_progress
                    recordings.iso_started(session, entry["path"], wall - seconds)
                    entry["noted"] = True
            if wanted and not entry and now >= _iso_retry.get(cam, 0.0):
                try:
                    path = recordings.new_iso_part(session, cam)
                    _iso[cam] = {"proc": Process(_iso_command(cam, path), copy=True),
                                 "session": session, "path": path, "noted": False}
                except (OSError, ValueError) as exc:
                    logger.warning("Cannot record %s: %s", cam, exc)
                    _iso_retry[cam] = now + ISO_RETRY


def _end_iso_all():
    """Stop every reporting camera recording (before the session is saved)."""
    with _iso_lock:
        entries = list(_iso.values())
        _iso.clear()
        _iso_retry.clear()
    for entry in entries:
        _kill_process(entry["proc"])


def get_iso_status():
    return {cam: bool(entry["proc"].poll() is None) for cam, entry in list(_iso.items())}


def stop_replay():
    """Back to live now."""
    global _replay, _relay_proc
    with _lock:
        if not _replay:
            return False, "No replay on air"
        camera = _replay["camera"]
    if camera:
        ok, message = switch_camera(camera)
        return ok, "Back to live" if ok else message
    with _lock:
        relay, _relay_proc, _replay = _relay_proc, None, None
    _kill_process(relay)
    _notify_replay(False)
    return True, "Back to live"


def set_layout(mode, main, secondary=REPORT_CAMERAS[0], position="top-right"):
    """Configure composition; replace only the relay, keeping the output encoder alive."""
    global _layout, _generation, _pending_camera, _active_camera
    if mode not in ("single", "pip", "split"):
        return False, "Choose full-screen, picture-in-picture, or side-by-side"
    if main not in CAMERA_IDS or secondary not in CAMERA_IDS:
        return False, "Invalid camera"
    if mode != "single" and main == secondary:
        return False, "Choose different main and second cameras"
    if mode != "single" and main not in REPORT_CAMERAS and secondary not in REPORT_CAMERAS:
        return False, "Two pictures need a reporting camera: match cameras are shown one at a time"
    if position not in ("top-left", "top-right", "bottom-left", "bottom-right"):
        return False, "Invalid picture-in-picture position"
    with _lock:
        token = _stop_generation
        _generation += 1
        generation = _generation
        _pending_camera = main
    with _switch_lock:
        with _lock:
            if token != _stop_generation or generation != _generation:
                return False, SUPERSEDED
            previous = _layout
            _layout = dict(mode=mode, main=main, secondary=secondary, position=position)
            if _relay_proc is None and not _camera_status.get(main):
                _active_camera = main
                _pending_camera = None
                return True, "Layout saved; waiting for the main camera"
        try:
            ok, message = _replace_relay(main, generation)
            with _lock:
                if not ok and generation == _generation:
                    _layout = previous
            return (True, "Layout applied") if ok else (False, message)
        finally:
            with _lock:
                if generation == _generation:
                    _pending_camera = None


def _apply_mix():
    """Bring the running relay in line with the mixer: live volume changes when it already
    has every available wanted source, otherwise rebuild it (a short audio/picture gap)."""
    with _lock:
        stop_generation = _stop_generation
        camera, relay, channels, missing = _active_camera, _relay_proc, dict(_relay_channels), set(_relay_missing)
        wanted = _effective_mix(camera) if camera else {}
        muted = _muted
    if not camera:
        return True, "Mixer updated"
    # The relay uses camera audio at unity gain when every requested source is
    # missing. Preserve that same fallback when unmuting or adjusting the mixer.
    if wanted and set(wanted) <= missing and camera in channels and camera not in wanted:
        wanted = {camera: 0.0}
    # Muting never rebuilds: silencing the channels the relay has is enough.
    if relay is not None and relay.poll() is None and (muted or set(wanted) - missing <= set(channels)):
        applied = True
        for channel, name in channels.items():
            volume = _db_to_volume(wanted[channel]) if channel in wanted and not muted else 0.0
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
    global _mix, _follow, _muted
    stop_generation = _stop_generation
    # Serialize mixer mutations with relay replacement, including live ZMQ commands.
    # STOP remains independent and can cancel work waiting for this lock.
    with _switch_lock:
        with _lock:
            if stop_generation != _stop_generation:
                return False, SUPERSEDED
            previous = ({c: dict(v) for c, v in _mix.items()}, _follow, _muted)
            change()
        ok, message = _apply_mix()
        if not ok:
            with _lock:
                _mix, _follow, _muted = previous
        return ok, message


def set_audio_source(mode):
    """Presets: "off" (no sound), "cam1" (Cam 1 mic only), "active" (camera sound
    follows the cut), "usb:<id>" (that microphone only)."""
    if mode not in ("off", "cam1", "active") and not (mode.startswith("usb:") and mode[4:]):
        return False, "Invalid audio source"
    if mode.startswith("usb:") and not _usb_device_available(mode[4:]):
        return False, "Unknown audio device"

    def change():
        global _follow
        if mode.startswith("usb:"):
            _mix.setdefault(mode, {"on": False, "gain": 0.0})
        for channel, setting in _mix.items():
            if channel not in REPORT_CAMERAS:  # those follow the picture, not the preset
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


def set_mute(muted):
    """Master mute: silence every audio source on air without touching the mixer,
    the picture or the YouTube connection; unmuting restores the previous mix."""
    def change():
        global _muted
        _muted = bool(muted)
    ok, message = _set_mixer(change)
    return ok, message.replace("Mixer updated", "Audio muted" if muted else "Audio unmuted")


# Hardware (VAAPI) encoding of the final picture is an option; x264 stays the default and
# the fallback. The choice applies when the encoder starts.
ENCODER_SETTING = BASE / "data/encoder.json"
ENCODERS = ("software", "intel", "nvidia")
_hw_wanted = "software"  # the operator's choice
_hw_device = None        # Intel render node that passed the test encode
_hw_warning = ""         # why the software encoder runs although hardware is chosen
_encoder_hw = False      # the running encoder uses a graphics chip
_relay_hw_failed = False  # the NVIDIA encoder failed in a relay: relays use software until the next start
_nvenc_ok = None         # cached result of the NVIDIA test encode
_NVDEC = os.environ.get("MCC_NVDEC", "1") != "0"


def _test_vaapi(device):
    try:
        return subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-vaapi_device", device,
             "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30", "-t", "0.5", "-vf", "format=nv12,hwupload",
             "-c:v", "h264_vaapi", "-bf", "0", "-b:v", "6000k", "-maxrate", "6000k", "-f", "null", "-"],
            capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _test_nvenc():
    try:
        return subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
             "testsrc2=s=1920x1080:r=30", "-t", "0.5", "-c:v", "h264_nvenc", "-preset", "p1",
             "-tune", "ull", "-bf", "0", "-b:v", "6000k", "-f", "null", "-"],
            capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _find_hw_device():
    return next((str(d) for d in sorted(Path("/dev/dri").glob("renderD*")) if _test_vaapi(str(d))), None)


def nvenc_available():
    global _nvenc_ok
    if _nvenc_ok is None:
        _nvenc_ok = _test_nvenc()
    return _nvenc_ok


def set_hardware_encoding(kind, save=True):
    """Encode on the CPU ("software"), the Intel graphics chip (final picture) or the NVIDIA
    GPU (final picture and every camera relay: frees the most CPU). True means "intel"
    (the setting before the NVIDIA option existed)."""
    global _hw_wanted, _hw_device, _hw_warning, _relay_hw_failed
    kind = {True: "intel", False: "software"}.get(kind, kind)
    if kind not in ENCODERS:
        return False, "Choose software, intel or nvidia"
    device = _find_hw_device() if kind == "intel" else None
    if kind == "intel" and not device:
        return False, "The Intel graphics chip cannot encode the stream; staying on the current encoder"
    if kind == "nvidia" and not nvenc_available():
        return False, "No working NVIDIA encoder found; staying on the current encoder"
    with _lock:
        _hw_wanted, _hw_device, _hw_warning, _relay_hw_failed = kind, device, "", False
        running = _want_stream
    if save:
        try:
            ENCODER_SETTING.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            ENCODER_SETTING.write_text(json.dumps({"encoder": kind}))
        except OSError:
            logger.exception("Cannot save the encoder setting")
    names = {"software": "Software encoding", "intel": "Intel graphics encoding", "nvidia": "NVIDIA GPU encoding"}
    note = ""
    if running:
        note = "; relays use it from the next cut, the final encoder from the next start" if kind == "nvidia" or \
            _relay_uses_nvenc() else "; it applies from the next start"
    return True, names[kind] + " on" + note


def load_encoder_setting():
    """At startup: restore the saved choice if that encoder still passes the test."""
    try:
        saved = json.loads(ENCODER_SETTING.read_text())
        kind = saved.get("encoder") or ("intel" if saved.get("hardware") else "software")
    except (OSError, ValueError, AttributeError):
        return
    if kind != "software":
        ok, message = set_hardware_encoding(kind, save=False)
        if not ok:
            logger.warning(message)


def _relay_uses_nvenc():
    return _hw_wanted == "nvidia" and not _relay_hw_failed and bool(_nvenc_ok)


def get_encoder_status():
    active = None
    if _encoder_proc:
        active = (_hw_wanted if _encoder_hw else "software")
    warning = _hw_warning or ("NVIDIA encoder failed in a camera relay; relays use the software encoder"
                              if _hw_wanted == "nvidia" and _relay_hw_failed else "")
    return {"hardware": _hw_wanted != "software", "kind": _hw_wanted, "warning": warning,
            "active": active, "relay": "nvidia" if _relay_uses_nvenc() else "software",
            "nvidia": bool(_nvenc_ok), "intel_device": _hw_device}


def _launch_encoder():
    global _encoder_proc, _encoder_hw
    width, height, bitrate, _ = PROFILES[_profile]
    vf = f"scale={width}:{height}"  # the live relay draws the built-in scoreboard
    ensure_frame(FRAME_PATH)
    # Force image2: auto-detected png_pipe would keep reading the old inode after a live update.
    # Keep composition in the encoder's YUV420 format and bound worker counts:
    # auto-threading added substantial CPU overhead in real-time 1080p/PiP tests.
    intel = _hw_wanted == "intel" and bool(_hw_device) and not _hw_warning
    nvidia = _hw_wanted == "nvidia" and bool(_nvenc_ok) and not _hw_warning
    hardware = intel or nvidia
    upload = ",format=nv12,hwupload" if intel else ""
    vf = f"[0:v]{vf}[base];[1:v]scale={width}:{height},format=rgba[score67];[base][score67]overlay=0:0:format=yuv420{upload}[vout]"
    if intel:  # no B-frames, like x264's zerolatency
        codec = ["-c:v", "h264_vaapi", "-bf", "0"]
    elif nvidia:
        codec = ["-c:v", "h264_nvenc", "-preset", "p4", "-tune", "ll", "-bf", "0", "-rc", "cbr", "-pix_fmt", "yuv420p"]
    else:
        codec = ["-c:v", "libx264", "-threads:v", "6", "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p"]
    # A deep input queue: the reader must keep draining while the graph is busy.
    cmd = _ffmpeg() + (["-vaapi_device", _hw_device] if intel else []) + [
        "-filter_complex_threads", "2", "-thread_queue_size", "1024"] + _input("program") + [
        "-re", "-f", "image2", "-loop", "1", "-framerate", "2", "-i", str(FRAME_PATH),
        "-filter_complex", vf, "-map", "[vout]", "-map", "0:a:0", *codec,
        "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{int(bitrate[:-1]) * 2}k", "-g", "60",
        "-r", "30", "-c:a", "aac", "-b:a", "128k",
        "-ar", "48000", "-ac", "2", "-f", "rtsp", "-rtsp_transport", "tcp", f"{RTSP_BASE}/final"]
    _encoder_proc, _encoder_hw = Process(cmd), hardware


def _launch_pusher(target):
    """Forward the finished picture to one destination without re-encoding."""
    cmd = _ffmpeg() + _input("final") + [
        "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
        "-rw_timeout", "10000000", "-f", "flv", target["url"]]
    target["proc"] = Process(cmd, secret=target["secret"], copy=True)


def make_target(name, key="", server=None):
    """Validate one destination; ValueError names the problem without echoing secrets."""
    if name not in DESTINATIONS:
        raise ValueError("Unknown streaming destination")
    label, default_server, key_pattern = DESTINATIONS[name]
    server = (server or default_server()).strip().rstrip("/")
    key = (key or "").strip()
    if not server:
        raise ValueError(f"{label}: enter the RTMP server URL")
    if len(server) > 300 or not re.fullmatch(SERVER_PATTERN, server):
        raise ValueError(f"{label}: the server must be an rtmp:// or rtmps:// address")
    if not re.fullmatch(key_pattern, key) or (name != "custom" and not key):
        raise ValueError(f"{label}: invalid stream key")
    return {"label": label, "url": f"{server}/{key}" if key else server, "secret": key or None, "server": server,
            "proc": None, "state": "starting", "error": "", "failures": 0, "retry_at": 0.0, "started_at": None}


def _summarize_targets():
    """Under _lock: the overall stream state shown in the header (any platform live = live)."""
    global _state, _started_at
    states = [t["state"] for t in _targets.values()]
    for state in ("live", "reconnecting", "starting", "failed"):
        if state in states:
            _state = state
            break
    else:
        _state = "stopped"
    _set_error("destinations", "; ".join(f"{t['label']}: {t['error']}" for t in _targets.values() if t["error"]))
    started = [t["started_at"] for t in _targets.values() if t["started_at"]]
    _started_at = min(started) if started else None


def _launch_recorder():
    """Write the finished picture to a new crash-safe MPEG-TS part, without re-encoding."""
    global _recorder_proc
    path = recordings.new_part(_recording)
    # Every packet goes to disk at once: marks and replays read up to the live edge, and a
    # crash loses nothing buffered (a quiet scene would otherwise sit in 256 KB chunks).
    cmd = _ffmpeg() + _input("final") + ["-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
                                         "-flush_packets", "1", "-f", "mpegts", path]
    _recorder_proc = Process(cmd, copy=True)


def start_stream(destinations, profile="720p", record=False, title=""):
    """GO LIVE to {name: target} destinations (see make_target); with record, also record
    to this laptop. While the output already runs, this only adds what is not running."""
    return _start(dict(destinations), profile, record, title)


def start_youtube_stream(stream_key, profile="720p", record=False, title=""):
    try:
        target = make_target("youtube", stream_key)
    except ValueError:
        return False, "Invalid stream key"
    return start_stream({"youtube": target}, profile, record, title)


def start_recording(profile="720p", title=""):
    """Record to this laptop; starts the output stage without streaming if needed."""
    return _start({}, profile, True, title)


def _check_disk():
    free = recordings.free_bytes()
    if free < recordings.MIN_FREE_START:
        return f"Only {free / recordings.GB:.1f} GB free on this laptop; free up at least {recordings.MIN_FREE_START // recordings.GB} GB to record"
    return ""


def _room_note(profile):
    """Said when recording starts with less room than a whole match needs."""
    minutes = recordings.minutes_left(recordings.room_left(), profile)
    if minutes >= 120:
        return ""
    return f" — disk space for only about {minutes} min of recording (saving the file needs the same space again)"


def _begin_recording(title, session=None):
    """Under _lock: open a new session (or continue an interrupted one after a crash);
    the supervisor launches the recorder."""
    global _want_record, _recording, _rec_failures, _rec_retry_at, _record_warning, _disk_warning, _disk_check_at
    _recording = session if session and recordings.reopen(session) else recordings.create_session(_profile, title)
    _want_record, _rec_failures, _rec_retry_at, _record_warning = True, 0, 0.0, ""
    _disk_warning, _disk_check_at = "", 0.0


def _attach(targets, record, title):
    """Under _lock, output already running: add destinations and/or recording."""
    new = {name: t for name, t in targets.items() if name not in _targets}
    if targets and not new:
        return False, "Stream is already starting or running; stop before changing settings"
    if not targets and _want_record:
        return False, "Already recording"
    messages = []
    if record and not _want_record:
        problem = _check_disk()
        if problem:
            return False, problem
        _begin_recording(title)
        messages.append("Recording started" + _room_note(_profile))
    if new:
        _targets.update(new)
        _summarize_targets()
        messages.append(f"Connecting to {', '.join(t['label'] for t in new.values())}; watch the live status for confirmation")
    return True, "; ".join(messages)


def _start(targets, profile, record, title, session=None):
    global _state, _profile
    if not targets and not record:
        return False, "Choose at least one streaming destination"
    if profile not in PROFILES:
        return False, "Invalid quality profile"
    with _lock:
        if _want_stream:
            return _attach(targets, record, title)
    if not _start_lock.acquire(blocking=False):
        return False, "Stream is already starting"
    try:
        with _lock:
            token = _stop_generation
            if _want_stream:
                return False, "Stream is already starting or running; stop before changing settings"
            _profile = profile
            if targets:
                _state = "starting"
        try:
            return _start_output(targets, profile, token, record, title, session)
        finally:
            with _lock:
                if token == _stop_generation and not _want_stream:
                    _state = "stopped"
    finally:
        _start_lock.release()


def _start_output(targets, profile, token, record=False, title="", session=None):
    """Start relay + encoder (and the requested destinations / recording) from cold."""
    global _want_stream, _state, _profile, _retry_at, _retries, _hw_warning, _relay_hw_failed
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
    if record:
        problem = _check_disk()
        if problem:
            return False, problem
    if get_active_camera() and _relay_profile != profile:
        # Rebuild the program relay at the stream resolution before going live.
        ok, message = switch_camera(_active_camera, expected_stop=token, force=True)
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
        preferred = [c for c in (_active_camera, _last_camera) if c]
        if _layout["mode"] != "single":
            preferred = [_layout["main"]]
        # GO LIVE opens on the game: a reporting camera only if nothing else publishes.
        candidates = preferred if _layout["mode"] != "single" else preferred + MATCH_CAMERAS + REPORT_CAMERAS
        camera = next((c for c in candidates if live.get(c)), None)
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
        _hw_warning = ""  # every broadcast tries the chosen encoder afresh
        _relay_hw_failed = False
        try:
            _launch_encoder()
        except OSError:
            return False, "Unable to launch encoder"
        _want_stream = True
        _clear_errors("relay", "encoder", "destinations")
        _targets.clear()
        _targets.update(targets)  # the supervisor connects them once the encoder runs
        _summarize_targets()
        _retry_at, _retries = 0, 0
        if record:
            _begin_recording(title, session)
        note = _room_note(_profile) if record else ""
        if not targets:
            return True, "Recording starting" + note
        names = ", ".join(t["label"] for t in targets.values())
        return True, f"Going live on {names}" + (" and recording" if record else "") + "; watch the live status for confirmation" + note


def resume_state():
    """What a restart needs to continue this broadcast (None when nothing is wanted).
    Holds stream keys: the app keeps it in its private data folder."""
    with _lock:
        if not _want_stream:
            return None
        return {"targets": {name: {"key": t.get("secret") or "", "server": t.get("server")} for name, t in _targets.items()},
                "profile": _profile, "record": bool(_want_record and _recording), "session": _recording,
                "camera": (_replay or {}).get("camera") or _active_camera or _last_camera,
                "layout": dict(_layout),
                "audio": {"mix": {c: dict(v) for c, v in _mix.items()}, "follow": _follow, "muted": _muted}}


def resume(state, title=""):
    """Continue a broadcast interrupted by a crash or restart: same platforms, quality,
    camera, layout and audio mix; the recording continues in the same session when it is unsaved."""
    global _last_camera, _layout, _follow, _muted
    targets = {}
    for name, settings in (state.get("targets") or {}).items():
        try:
            targets[name] = make_target(name, settings.get("key", ""), settings.get("server"))
        except (ValueError, AttributeError, TypeError) as exc:
            logger.warning("Not resuming %s: %s", name, exc)
    record = bool(state.get("record"))
    if not targets and not record:
        return False, "Nothing to resume"
    with _lock:
        _last_camera = state.get("camera") if state.get("camera") in CAMERA_IDS else _last_camera
        layout = state.get("layout")
        if isinstance(layout, dict) and layout.get("mode") in ("single", "pip", "split") \
                and layout.get("main") in CAMERA_IDS and layout.get("secondary") in CAMERA_IDS:
            _layout = {**_layout, **{k: layout[k] for k in ("mode", "main", "secondary", "position") if k in layout}}
        # Camera sound starts off; a crash must not silence the commentary that was on air.
        audio = state.get("audio")
        if isinstance(audio, dict):
            for channel, setting in (audio.get("mix") or {}).items():
                if (channel in CAMERA_IDS or (isinstance(channel, str) and channel.startswith("usb:") and channel[4:])) \
                        and isinstance(setting, dict) and type(setting.get("on")) is bool \
                        and type(setting.get("gain")) in (int, float):
                    _mix[channel] = {"on": setting["on"],
                                     "gain": round(max(GAIN_MIN, min(GAIN_MAX, float(setting["gain"]))), 1)}
            _follow = audio.get("follow") is True
            _muted = audio.get("muted") is True
    return _start(targets, state.get("profile", "720p"), record, title, session=state.get("session") if record else None)


def stop_stream(name=None):
    """STOP STREAM (all destinations) or stop one destination. A running recording, and
    the other destinations, keep going; with nothing left the output stage stops."""
    with _lock:
        if name is not None and name not in _targets:
            return False, "That destination is not live"
        names = [name] if name else list(_targets)
        remaining = [n for n in _targets if n not in names]
        stop_everything = not (_want_stream and (_want_record or remaining))
        if not stop_everything:
            stopped = [_targets.pop(n) for n in names]
            _summarize_targets()
    if stop_everything:
        return _stop_all()
    for target in stopped:
        _kill_process(target["proc"])
    labels = ", ".join(t["label"] for t in stopped) or "Streaming"
    return True, f"{labels} stopped" + ("; recording continues" if _want_record else "")


def stop_youtube_stream():
    return stop_stream()


def stop_recording():
    """Stop recording and save the file. A YouTube broadcast keeps going."""
    global _recorder_proc, _want_record, _recording, _record_warning
    with _lock:
        if not (_want_record and _recording):
            return False, "Not recording"
        if _want_stream and _targets:
            recorder, session = _recorder_proc, _recording
            _recorder_proc, _recording, _want_record, _record_warning = None, None, False, ""
        else:
            recorder = False
    if recorder is False:
        _stop_all()
        return True, "Recording stopped; saving the file"
    _kill_process(recorder)
    _end_live_capture(background=False)
    _end_iso_all()
    recordings.close_part(session)
    recordings.finalize_async(session)
    return True, "Recording stopped; saving the file"


def _stop_all(finalize=True):
    global _want_stream, _relay_proc, _active_camera, _generation
    global _state, _started_at, _pending_camera, _encoder_proc
    global _relay_channels, _relay_missing, _relay_muted, _stop_generation, _relay_secondary, _layout_warning
    global _recorder_proc, _want_record, _recording, _record_warning, _replay, _relay_zmq, _last_camera
    with _lock:
        _last_camera = (_replay or {}).get("camera") or _active_camera or _last_camera
        ended_replay, _replay = _replay is not None, None
        _want_stream = False
        _stop_generation += 1
        _generation += 1  # abort any cut in flight
        processes = tuple(t["proc"] for t in _targets.values()) + (_recorder_proc, _encoder_proc, _relay_proc)
        _targets.clear()
        _recorder_proc = _encoder_proc = _relay_proc = None
        _active_camera = _started_at = _pending_camera = None
        _relay_channels, _relay_missing, _relay_muted = {}, set(), set()
        _state = "stopped"
        _clear_errors("relay", "encoder", "destinations")
        _relay_secondary, _layout_warning = None, ""
        session, _recording, _want_record, _record_warning = _recording, None, False, ""
        _relay_zmq = None
    for proc in processes:
        _kill_process(proc)
    _end_live_capture(background=False)  # placed before the session is joined
    _end_iso_all()
    if ended_replay:
        _notify_replay(False)
    if session:
        recordings.close_part(session)
        if finalize:
            recordings.finalize_async(session)
        return True, "Streaming stopped; saving the recording"
    return True, "Streaming stopped"


def _supervise_relay(now):
    """Restart a dead relay with backoff, and restore the chosen commentary source after
    a degraded relay once that source is available again. Never holds _lock while slow."""
    global _relay_proc, _relay_failures, _relay_retry_at, _restore_check_at
    with _lock:
        if _replay:
            return  # the replay watcher returns to the live camera
        relay, camera = _relay_proc, _active_camera
        channels, missing = dict(_relay_channels), set(_relay_missing)
        sources = dict(_relay_sources)
        # A relay whose picture stalled while its camera stopped publishing is finished:
        # replace it with fallback now instead of after the general 15 s health window.
        stalled = relay and relay.last_progress and now - relay.last_progress > 3 and not _camera_status.get(camera)
        dead = relay and (relay.poll() is not None or stalled or
                          (not relay.healthy() and now - relay.launched > 15))
        if dead:
            _relay_proc = None
    if dead:
        _kill_process(relay)
        relay = None
    if not camera:
        return
    if (relay is not None and relay.healthy() and _pending_camera is None
            and now - _error_since.get("relay", now) > 10):
        _set_error("relay")  # a cut that failed a while ago; the picture on air is fine
    reconnected = sorted(c for c, session in sources.items() if session and _camera_status.get(c)
                         and _camera_source.get(c) and _camera_source[c] != session)
    if relay is None:
        if _camera_status.get(camera) and now >= _relay_retry_at:
            ok, message = switch_camera(camera, automatic=True)
            if not ok and message not in (BUSY, SUPERSEDED):
                _relay_failures += 1
                _relay_retry_at = now + min(5, 2 ** min(_relay_failures, 3))  # 2, 4, 5, 5 … s
    elif reconnected:
        # A camera the relay reads reconnected (new publisher session): the relay still waits
        # on the old one, so the picture or sound is frozen until it is rebuilt.
        if now >= _relay_retry_at:
            logger.info("%s reconnected; rebuilding the relay", ", ".join(reconnected))
            ok, message = switch_camera(camera, automatic=True)
            if not ok and message not in (BUSY, SUPERSEDED):
                _relay_failures += 1
                _relay_retry_at = now + min(5, 2 ** min(_relay_failures, 3))
    elif relay.healthy() and now >= _restore_check_at:
        # A mixed-in source that stopped publishing is silent in the mix: rebuild without it
        # (falls back to the camera's own sound). A missing source that is back: restore it.
        lost = [c for c in channels if c != camera and (
            (c in CAMERA_IDS and not _camera_status.get(c)) or (c.startswith("usb:") and c[4:] not in _mic_ids))]
        returned = [c for c in missing if c != camera and c in _mix and _channel_available(c)] if missing else []
        secondary = _layout["secondary"] if _layout["mode"] != "single" else None
        video_changed = secondary and bool(_camera_status.get(secondary)) != bool(_relay_secondary)
        if lost or returned or video_changed:
            _restore_check_at = now + 10
            logger.info("Rebuilding audio on %s (lost %s, returned %s)", camera, lost, returned)
            switch_camera(camera, automatic=True)
        elif missing:
            _restore_check_at = now + 10


def _supervise_once():
    global _camera_status, _state, _started_at, _retry_at, _retries, _mediamtx_retry_at
    global _relay_failures, _relay_retry_at, _status_failures
    before = _camera_status
    try:
        paths = _paths()
        _camera_status = {c: paths.get(c, False) for c in CAMERA_IDS}
        _camera_status["program"] = paths.get(program_path(), False)
        _status_failures = 0
        _update_srt_stats()
    except (requests.RequestException, ValueError, KeyError):
        # One missed answer from a running MediaMTX is not a camera outage: keep the last
        # status (marking everything offline stopped the meters and rebuilt the relay on air).
        _status_failures += 1
        if _status_failures >= 2 or not get_mediamtx_running():
            _camera_status = {c: False for c in CAMERA_IDS + ["program"]}
    if any(_camera_status.get(c) and not before.get(c) for c in CAMERA_IDS):
        # A camera (re)connected: put it back on air right away, not after an old backoff.
        _relay_failures, _relay_retry_at = 0, 0.0
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
    global _encoder_proc, _state, _retry_at, _retries, _hw_warning
    now = time.monotonic()
    with _lock:
        if not _want_stream:
            return
        encoder = _encoder_proc
        if encoder and encoder.healthy():
            _retries = 0
            healthy = True
            _set_error("encoder")
        elif encoder and encoder.poll() is None and now - encoder.launched < 20:
            return
        else:
            healthy = False
            _set_error("encoder", (encoder.error if encoder else "") or "Encoder stopped producing frames")
            if _encoder_hw and not _hw_warning:
                # Whatever the reason, continue with the encoder that is known to work.
                _hw_warning = "Hardware encoder failed; using the software encoder"
                logger.warning("%s: %s", _hw_warning, encoder.error if encoder else "")
                _retry_at = 0
            if _targets:
                _state = "reconnecting"
            token = _stop_generation
            _encoder_proc = None
    if healthy:
        # The pusher and recorder read the encoder's output, so they start once it flows.
        _supervise_pusher(now)
        _supervise_recorder(now)
        return
    _kill_process(encoder)
    with _lock:
        if not _want_stream or token != _stop_generation:
            return
        if now >= _retry_at:
            _retries += 1
            _retry_at = now + min(8, 2 ** min(_retries, 3))
            try:
                _launch_encoder()
            except OSError:
                _state = "failed"
                _set_error("encoder", "Cannot restart encoder")


def _supervise_pusher(now):
    """Keep every destination connected; each reconnects with its own backoff without
    touching the encoder, the recording or the other destinations."""
    for name in list(_targets):
        _supervise_target(name, now)


def _supervise_target(name, now):
    with _lock:
        target = _targets.get(name)
        if not (_want_stream and target):
            return
        pusher = target["proc"]
        if pusher and pusher.healthy():
            if target["state"] != "live":
                target.update(state="live", error="", failures=0)
                target["started_at"] = target["started_at"] or datetime.now(timezone.utc).isoformat()
                _summarize_targets()
            return
        if pusher and pusher.poll() is None and now - pusher.launched < 20:
            return
        if pusher:
            target.update(state="reconnecting", error=pusher.error or "Connection lost")
            _summarize_targets()
        target["proc"], token = None, _stop_generation
    _kill_process(pusher)
    with _lock:
        if _targets.get(name) is not target or token != _stop_generation or target["proc"] or not _want_stream:
            return
        if now >= target["retry_at"]:
            if pusher:
                target["failures"] += 1
                target["retry_at"] = now + min(8, 2 ** min(target["failures"], 3))  # 2, 4, 8, 8 … s
            try:
                _launch_pusher(target)
            except OSError:
                target.update(state="failed", error="Cannot start the connection")
                _summarize_targets()


def _supervise_recorder(now):
    """Keep the recorder writing; a restart continues in a new part of the same recording."""
    global _recorder_proc, _rec_failures, _rec_retry_at, _record_warning, _disk_check_at, _disk_warning
    with _lock:
        if not (_want_record and _recording):
            return
        session, recorder = _recording, _recorder_proc
        alive = recorder and recorder.poll() is None and (recorder.healthy() or now - recorder.launched < 20)
        if not alive:
            _recorder_proc = None
    if alive:
        if recorder.first_progress and not getattr(recorder, "start_noted", False):
            wall, seconds = recorder.first_progress
            recordings.part_started(session, wall - seconds)
            recorder.start_noted = True
        if recorder.healthy():
            with _lock:
                if _rec_failures and _recording == session:
                    _rec_failures, _record_warning = 0, ""
        if now >= _disk_check_at:
            _disk_check_at = now + 10
            # Stop while the parts can still be joined: that needs their size once more.
            room = recordings.room_left(session)
            minutes = recordings.minutes_left(room, _profile, _iso_bytes_per_hour())
            if not room:
                logger.error("Disk almost full; stopping the recording")
                stop_recording()
                _record_warning = "Disk almost full — recording stopped; the file is being saved."
            elif minutes < LOW_ROOM_MINUTES:
                if not _disk_warning:
                    logger.warning("Recording room is running out: about %d min left", minutes)
                _disk_warning = f"Disk space for only about {max(1, minutes)} more min of recording — free up space or it stops"
            else:
                _disk_warning = ""
        return
    if recorder:
        _kill_process(recorder)
        recordings.close_part(session)
    with _lock:
        if not (_want_record and _recording == session) or _recorder_proc:
            return
        if now >= _rec_retry_at:
            if recorder:
                _rec_failures += 1
                _rec_retry_at = now + min(10, 2 ** min(_rec_failures, 3))
                _record_warning = "Recording was interrupted and resumed"
                logger.warning("Recorder for %s restarted: %s", session, recorder.error)
            try:
                _launch_recorder()
            except (OSError, ValueError) as exc:
                _record_warning = f"Cannot record: {exc}"


def _watch_encoder():
    while not _stop.wait(.5):
        try:
            _supervise_encoder()
            _supervise_live_capture(time.monotonic())
            _supervise_iso(time.monotonic())
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
    for worker in (_thread, _encoder_thread, _monitor_thread):
        if worker and worker is not threading.current_thread():
            worker.join(timeout=20)
    with _lock:
        session = _recording
        _stop_all(finalize=False)
    if session:
        # Logout or shutdown: save the MP4 now if it fits the time left; otherwise the
        # parts stay safe and the next start joins them.
        recordings.finalize(session, timeout=SHUTDOWN_SAVE_SECONDS)
    with _lock:
        _await_retired()
        for key in list(_meters):
            _kill_process(_meters.pop(key))
        _kill_process(_monitor_proc, "monitor")
        _kill_process(_mediamtx_proc)
        _mediamtx_proc = None
