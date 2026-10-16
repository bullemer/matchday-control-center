"""
media_engine.py — Media Engine for Matchday Live Control Center

Manages:
  • MediaMTX (auto-download, config generation, process lifecycle, camera status polling)
  • Two-stage seamless camera switcher (Stage A: YouTube encoder, Stage B: camera relay)
  • Atomic scoreboard overlay file for FFmpeg drawtext reload=1
"""

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import tarfile
import requests

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MEDIAMTX_VERSION = "1.12.2"
MEDIAMTX_DOWNLOAD_URL = (
    f"https://github.com/bluenviron/mediamtx/releases/download/"
    f"v{MEDIAMTX_VERSION}/mediamtx_{MEDIAMTX_VERSION}_linux_amd64.tar.gz"
)
MEDIAMTX_BIN = "./mediamtx"
MEDIAMTX_CONFIG = "./mediamtx.yml"
MEDIAMTX_API = "http://127.0.0.1:9997"

RTMP_BASE = "rtmp://127.0.0.1:1935"
YOUTUBE_RTMP_BASE = "rtmp://a.rtmp.youtube.com/live2"

SCOREBOARD_PATH = os.path.join("static", "scoreboard.txt")

CAMERA_IDS = ["cam1", "cam2", "cam3"]

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_mediamtx_proc: subprocess.Popen | None = None
_youtube_proc: subprocess.Popen | None = None    # Stage A — persistent YouTube encoder
_relay_proc: subprocess.Popen | None = None       # Stage B — camera → /program relay
_relay_lock = threading.Lock()

_camera_status: dict[str, bool] = {c: False for c in CAMERA_IDS}
_camera_poll_thread: threading.Thread | None = None
_camera_poll_stop = threading.Event()

_active_camera: str | None = None
_audio_source_mode: str = "cam1"   # "cam1" (locked) or "active" (follows video)
_youtube_stream_key: str | None = None


# ═══════════════════════════════════════════════════════════════════════════
# 1.  MediaMTX Management
# ═══════════════════════════════════════════════════════════════════════════

def ensure_mediamtx() -> bool:
    """Download and extract the MediaMTX Linux amd64 binary if it is missing."""
    if os.path.isfile(MEDIAMTX_BIN) and os.access(MEDIAMTX_BIN, os.X_OK):
        logger.info("MediaMTX binary already present at %s", MEDIAMTX_BIN)
        return True

    tarball = f"mediamtx_{MEDIAMTX_VERSION}_linux_amd64.tar.gz"
    logger.info("MediaMTX binary not found — downloading v%s …", MEDIAMTX_VERSION)

    try:
        resp = requests.get(MEDIAMTX_DOWNLOAD_URL, stream=True, timeout=120)
        resp.raise_for_status()

        with open(tarball, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)

        logger.info("Extracting %s …", tarball)
        with tarfile.open(tarball, "r:gz") as tar:
            tar.extractall(path=".")

        os.chmod(MEDIAMTX_BIN, 0o755)
        logger.info("MediaMTX binary ready at %s", MEDIAMTX_BIN)
        return True

    except Exception:
        logger.exception("Failed to download/extract MediaMTX")
        return False
    finally:
        # Clean up tarball
        if os.path.isfile(tarball):
            try:
                os.remove(tarball)
            except OSError:
                pass


def generate_mediamtx_config() -> str:
    """Generate mediamtx.yml and return the path."""
    config = """\
###############################################
# Matchday Live Control Center — MediaMTX Config
# Auto-generated — do not edit manually
###############################################

# ── Global ──────────────────────────────────
logLevel: info

# ── API ─────────────────────────────────────
api: yes
apiAddress: 127.0.0.1:9997

# ── RTMP ────────────────────────────────────
rtmp: yes
rtmpAddress: :1935

# ── HLS ─────────────────────────────────────
hls: yes
hlsAddress: :8888

# ── WebRTC ──────────────────────────────────
webrtc: yes
webrtcAddress: :8889

# ── Paths ───────────────────────────────────
paths:
  cam1:
  cam2:
  cam3:
  program:
"""
    with open(MEDIAMTX_CONFIG, "w") as f:
        f.write(config)

    logger.info("Generated MediaMTX config at %s", MEDIAMTX_CONFIG)
    return MEDIAMTX_CONFIG


def start_mediamtx() -> bool:
    """Start the MediaMTX process.  Returns True on success."""
    global _mediamtx_proc

    if _mediamtx_proc and _mediamtx_proc.poll() is None:
        logger.warning("MediaMTX is already running (PID %d)", _mediamtx_proc.pid)
        return True

    if not os.path.isfile(MEDIAMTX_BIN):
        logger.error("MediaMTX binary not found — call ensure_mediamtx() first")
        return False

    generate_mediamtx_config()

    try:
        _mediamtx_proc = subprocess.Popen(
            [MEDIAMTX_BIN, MEDIAMTX_CONFIG],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        logger.info("MediaMTX started (PID %d)", _mediamtx_proc.pid)

        # Start camera status polling
        _start_camera_polling()
        return True

    except FileNotFoundError:
        logger.error("MediaMTX binary not executable or not found at %s", MEDIAMTX_BIN)
        return False
    except Exception:
        logger.exception("Failed to start MediaMTX")
        return False


def stop_mediamtx() -> None:
    """Stop the MediaMTX process and camera polling."""
    global _mediamtx_proc

    _stop_camera_polling()

    if _mediamtx_proc is None:
        logger.info("MediaMTX is not running")
        return

    try:
        os.killpg(os.getpgid(_mediamtx_proc.pid), signal.SIGTERM)
        _mediamtx_proc.wait(timeout=5)
        logger.info("MediaMTX stopped")
    except ProcessLookupError:
        logger.info("MediaMTX process already exited")
    except subprocess.TimeoutExpired:
        logger.warning("MediaMTX did not stop gracefully — sending SIGKILL")
        try:
            os.killpg(os.getpgid(_mediamtx_proc.pid), signal.SIGKILL)
            _mediamtx_proc.wait(timeout=3)
        except Exception:
            logger.exception("Failed to kill MediaMTX")
    except Exception:
        logger.exception("Error stopping MediaMTX")
    finally:
        _mediamtx_proc = None


def get_mediamtx_running() -> bool:
    """Return True if the MediaMTX process is currently alive."""
    return _mediamtx_proc is not None and _mediamtx_proc.poll() is None


# ---------------------------------------------------------------------------
#  Camera status polling
# ---------------------------------------------------------------------------

def get_camera_status() -> dict[str, bool]:
    """Return a dict like {"cam1": True, "cam2": False, "cam3": False}."""
    return dict(_camera_status)


def _poll_camera_status() -> None:
    """Background thread: queries MediaMTX API every 2 s to update _camera_status."""
    while not _camera_poll_stop.is_set():
        try:
            resp = requests.get(
                f"{MEDIAMTX_API}/v3/paths/list",
                timeout=2,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items") or []

            live_paths: set[str] = set()
            for item in items:
                name = item.get("name", "")
                # A camera is considered "publishing" when it has at least
                # one reader/source and the source type is not empty/"").
                ready = item.get("ready", False)
                if ready:
                    live_paths.add(name)

            for cam in CAMERA_IDS:
                _camera_status[cam] = cam in live_paths

        except requests.ConnectionError:
            # MediaMTX not yet ready — mark all cameras offline
            for cam in CAMERA_IDS:
                _camera_status[cam] = False
        except Exception:
            logger.debug("Camera status poll failed", exc_info=True)

        _camera_poll_stop.wait(2)


def _start_camera_polling() -> None:
    """Start the background camera-polling thread (idempotent)."""
    global _camera_poll_thread

    if _camera_poll_thread and _camera_poll_thread.is_alive():
        return

    _camera_poll_stop.clear()
    _camera_poll_thread = threading.Thread(
        target=_poll_camera_status,
        daemon=True,
        name="camera-poll",
    )
    _camera_poll_thread.start()
    logger.info("Camera status polling started")


def _stop_camera_polling() -> None:
    """Signal the camera-polling thread to stop and wait for it."""
    global _camera_poll_thread

    _camera_poll_stop.set()
    if _camera_poll_thread:
        _camera_poll_thread.join(timeout=5)
        _camera_poll_thread = None
    logger.info("Camera status polling stopped")


# ═══════════════════════════════════════════════════════════════════════════
# 2.  Two-Stage Seamless Switcher
# ═══════════════════════════════════════════════════════════════════════════

# ── helpers ─────────────────────────────────────────────────────────────────

def _kill_process(proc: subprocess.Popen | None, label: str) -> None:
    """Gracefully terminate then kill a subprocess."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)
        logger.info("%s stopped (PID %d)", label, proc.pid)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        logger.warning("%s did not stop gracefully — SIGKILL", label)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait(timeout=3)
        except Exception:
            logger.exception("Failed to kill %s", label)
    except Exception:
        logger.exception("Error stopping %s", label)


def _ensure_scoreboard_file() -> None:
    """Create the scoreboard file with sane defaults if it doesn't exist."""
    os.makedirs("static", exist_ok=True)
    if not os.path.isfile(SCOREBOARD_PATH):
        update_scoreboard_overlay("HOME", 0, "AWAY", 0, "00:00")


# ── Stage A — Persistent YouTube Encoder ────────────────────────────────

def start_youtube_stream(stream_key: str) -> bool:
    """
    Launch the persistent Stage-A FFmpeg process:
      • Pulls from rtmp://127.0.0.1:1935/program
      • Burns in the drawtext scoreboard overlay (reload=1)
      • Encodes 1080p/720p @ 30 fps → YouTube RTMP
    """
    global _youtube_proc, _youtube_stream_key

    if _youtube_proc and _youtube_proc.poll() is None:
        logger.warning("YouTube encoder already running (PID %d)", _youtube_proc.pid)
        return True

    _ensure_scoreboard_file()
    _youtube_stream_key = stream_key
    destination = f"{YOUTUBE_RTMP_BASE}/{stream_key}"

    scoreboard_abs = os.path.abspath(SCOREBOARD_PATH)

    # drawtext filter — white text on semi-transparent black bar, bottom-left
    drawtext_filter = (
        f"drawtext="
        f"textfile='{scoreboard_abs}':"
        f"reload=1:"
        f"fontcolor=white:"
        f"fontsize=28:"
        f"box=1:"
        f"boxcolor=black@0.6:"
        f"boxborderw=8:"
        f"x=20:"
        f"y=h-th-20:"
        f"font=monospace"
    )

    cmd = [
        "ffmpeg",
        "-re",
        "-live_start_index", "-1",
        # Input from the /program path on MediaMTX
        "-i", f"{RTMP_BASE}/program",
        # Video filter — scoreboard overlay
        "-vf", drawtext_filter,
        # Video encoding
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", "4500k",
        "-maxrate", "4500k",
        "-bufsize", "9000k",
        "-g", "60",
        "-r", "30",
        "-pix_fmt", "yuv420p",
        # Scale to 720p for reliable upload bandwidth (change to 1920:1080 for 1080p)
        "-s", "1280x720",
        # Audio encoding
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        # Output to YouTube RTMP
        "-f", "flv",
        destination,
    ]

    logger.info("Starting YouTube encoder → %s", destination)
    logger.debug("Stage A cmd: %s", " ".join(cmd))

    try:
        _youtube_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        )
        logger.info("YouTube encoder started (PID %d)", _youtube_proc.pid)
        return True

    except FileNotFoundError:
        logger.error("ffmpeg not found in PATH — install ffmpeg first")
        return False
    except Exception:
        logger.exception("Failed to start YouTube encoder")
        return False


def stop_youtube_stream() -> None:
    """Stop Stage A (YouTube encoder) and Stage B (camera relay)."""
    global _youtube_proc, _relay_proc, _active_camera, _youtube_stream_key

    with _relay_lock:
        _kill_process(_relay_proc, "Camera relay (Stage B)")
        _relay_proc = None
        _active_camera = None

    _kill_process(_youtube_proc, "YouTube encoder (Stage A)")
    _youtube_proc = None
    _youtube_stream_key = None
    logger.info("YouTube streaming stopped")


def get_youtube_running() -> bool:
    """Return True if the YouTube encoder is alive."""
    return _youtube_proc is not None and _youtube_proc.poll() is None


def get_active_camera() -> str | None:
    """Return the currently active camera id (e.g. 'cam1') or None."""
    return _active_camera


def get_youtube_stream_key() -> str | None:
    """Return the currently configured YouTube stream key."""
    return _youtube_stream_key


# ── Stage B — Camera Relay (seamless switch) ────────────────────────────

def switch_camera(cam_id: str) -> bool:
    """
    Seamless camera switch.

    Relays the requested camera feed to rtmp://127.0.0.1:1935/program using
    ``ffmpeg -c copy`` (Stage B).  The persistent Stage-A YouTube encoder
    keeps pulling from /program and never drops the YouTube connection.

    Audio behaviour:
      • mode='cam1'  → always take audio from cam1 (main commentary mic)
      • mode='active' → take audio from whichever camera is switched on-air
    """
    global _relay_proc, _active_camera

    cam_id = cam_id.lower().strip()
    if cam_id not in CAMERA_IDS:
        logger.error("Invalid camera id '%s' — must be one of %s", cam_id, CAMERA_IDS)
        return False

    with _relay_lock:
        # Kill existing relay
        if _relay_proc and _relay_proc.poll() is None:
            logger.info(
                "Stopping current relay (%s) to switch to %s",
                _active_camera, cam_id,
            )
            _kill_process(_relay_proc, "Camera relay (Stage B)")
            _relay_proc = None

        # Build the relay command
        video_source = f"{RTMP_BASE}/{cam_id}"
        destination = f"{RTMP_BASE}/program"

        if _audio_source_mode == "cam1" and cam_id != "cam1":
            # Take video from selected cam, audio from cam1
            audio_source = f"{RTMP_BASE}/cam1"
            cmd = [
                "ffmpeg",
                "-re",
                # Input 0 — video source (selected camera)
                "-i", video_source,
                # Input 1 — audio source (cam1 / main mic)
                "-i", audio_source,
                # Map video from input 0, audio from input 1
                "-map", "0:v:0",
                "-map", "1:a:0",
                # Copy codecs (no re-encode) for zero-latency relay
                "-c", "copy",
                "-f", "flv",
                destination,
            ]
        else:
            # Take both video and audio from the same camera
            cmd = [
                "ffmpeg",
                "-re",
                "-i", video_source,
                "-c", "copy",
                "-f", "flv",
                destination,
            ]

        logger.info("Switching to %s (audio: %s)", cam_id, _audio_source_mode)
        logger.debug("Stage B cmd: %s", " ".join(cmd))

        try:
            _relay_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            )
            _active_camera = cam_id
            logger.info(
                "Camera relay started: %s → /program (PID %d)",
                cam_id, _relay_proc.pid,
            )
            return True

        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH — install ffmpeg first")
            return False
        except Exception:
            logger.exception("Failed to start camera relay for %s", cam_id)
            return False


# ── Audio source toggle ────────────────────────────────────────────────

def set_audio_source(mode: str) -> bool:
    """
    Set audio source mode.

    Args:
        mode: 'cam1' — always use cam1 audio (main commentary mic)
              'active' — use audio from whichever camera is currently on-air

    If a camera relay is running and the mode changes, the relay is
    restarted automatically to apply the new audio mapping.
    """
    global _audio_source_mode

    mode = mode.lower().strip()
    if mode not in ("cam1", "active"):
        logger.error("Invalid audio mode '%s' — must be 'cam1' or 'active'", mode)
        return False

    previous = _audio_source_mode
    _audio_source_mode = mode
    logger.info("Audio source mode set to '%s'", mode)

    # If mode changed and a camera relay is active, restart it to pick up
    # the new audio mapping.
    if previous != mode and _active_camera:
        logger.info("Re-switching camera %s to apply new audio mode", _active_camera)
        switch_camera(_active_camera)

    return True


def get_audio_source() -> str:
    """Return current audio source mode ('cam1' or 'active')."""
    return _audio_source_mode


# ═══════════════════════════════════════════════════════════════════════════
# 3.  Scoreboard Overlay
# ═══════════════════════════════════════════════════════════════════════════

def update_scoreboard_overlay(
    home_name: str,
    home_score: int,
    away_name: str,
    away_score: int,
    clock_str: str,
) -> None:
    """
    Atomically write the scoreboard text file that FFmpeg's
    ``drawtext=textfile=…:reload=1`` reads on each frame.

    Uses tempfile + os.replace for atomic replacement so FFmpeg never
    reads a partially-written file.
    """
    text = f" {home_name}  {home_score} - {away_score}  {away_name}   |   {clock_str} "
    target_path = os.path.join("static", "scoreboard.txt")
    os.makedirs("static", exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir="static", suffix=".tmp", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp_path, target_path)
        logger.debug("Scoreboard updated: %s", text.strip())
    except Exception:
        logger.exception("Failed to write scoreboard overlay")
        # Clean up orphaned temp file
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# 4.  Convenience / Shutdown
# ═══════════════════════════════════════════════════════════════════════════

def shutdown_all() -> None:
    """Tear down every managed process in the correct order."""
    logger.info("Shutting down all media engine processes …")
    stop_youtube_stream()
    stop_mediamtx()
    logger.info("All media engine processes stopped")
