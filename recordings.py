"""Local match recordings: crash-safe parts, highlight markers, clips, reel and chapters.

A recording session lives in RECORDINGS_DIR/<id>/ with a session.json. The recorder
writes the final program (with scoreboard) as MPEG-TS parts, one per recorder process,
so a crash or encoder restart loses seconds, not the match. Stopping joins the parts
into one MP4. Markers carry their offset on the recording timeline (seconds from the
start of the joined file), so clips can be cut from the parts or from the MP4 alike.

While a replay is on air the program shows the replay, so the recording holds the replay,
not the live game. The live camera is then also recorded (copy only) into live/: each
"live window" knows where it sits on the recording timeline, and a highlight marked
inside one is cut from it.

Reporting cameras (interviews, surroundings) are recorded on their own for the whole
session, on air or not, into iso/ (copy only; a new part after each camera reconnect).
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)
BASE = Path(__file__).resolve().parent
DEFAULT_DIR = BASE / "recordings"
RECORDINGS_DIR = Path(os.environ.get("MCC_RECORDINGS_DIR", DEFAULT_DIR))
GB = 1024 ** 3
MIN_FREE_START = 3 * GB   # refuse to start recording below this
MIN_FREE_KEEP = 1 * GB    # stop recording automatically below this


def configure_storage(folder, min_free_gb, persist=None):
    """Validate, persist and apply storage under the same lock as session creation."""
    global RECORDINGS_DIR, MIN_FREE_START
    with _lock:
        if storage_busy():
            raise ValueError("Wait for recording, saving and exports to finish before changing storage")
        target = Path(os.environ.get("MCC_RECORDINGS_DIR") or folder or DEFAULT_DIR)
        try:
            target.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=target):
                pass
        except OSError as exc:
            raise ValueError(f"Cannot record into {target}: {exc.strerror or exc}")
        if persist:
            persist()
        RECORDINGS_DIR = target
        MIN_FREE_START = int(min_free_gb * GB)
        return target


def storage_busy():
    with _lock:
        return (any(path.parent == RECORDINGS_DIR for path in _open_sessions)
                or bool(_finalizing) or any(job["state"] == "running" for job in _exports.values()))


JOIN_MARGIN = 200 * 1024 ** 2   # headroom when joining parts into the final file
BYTES_PER_HOUR = {"720p": 2.1e9, "1080p": 2.8e9}   # what an hour of recording takes
LABELS = ("Highlight", "Goal", "Chance", "Save", "Card", "Foul", "Other")
CLIP_PRE, CLIP_POST = 20, 10
_lock = threading.RLock()
_exports_changed = threading.Condition(_lock)
_finalizing = set()
_open_sessions = set()  # from creation through finalization, including the stop-to-save handoff
_exports = {}  # session folder -> latest export status


def _now():
    return time.time()


def _dir(sid):
    if not re.fullmatch(r"[0-9]{8}-[0-9]{6}(-[0-9]+)?", sid or ""):
        raise ValueError("Unknown recording")
    return RECORDINGS_DIR / sid


def _load(sid):
    path = _dir(sid) / "session.json"
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise ValueError("Unknown recording") from None


def _save(session):
    folder = _dir(session["id"])
    fd, tmp = tempfile.mkstemp(dir=folder, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(session, f, indent=1)
    os.replace(tmp, folder / "session.json")


def free_bytes():
    folder = RECORDINGS_DIR  # measure without creating it (a background check must not)
    while not folder.exists() and folder != folder.parent:
        folder = folder.parent
    return shutil.disk_usage(folder).free


def parts_bytes(sid):
    """Size of the parts recorded so far: joining them needs this much room again."""
    try:
        with _lock:
            session = _load(sid)
        folder = _dir(sid)
        files = [p["file"] for p in session["parts"]] + [p["file"] for p in session.get("iso", []) if not p.get("mp4")]
        return sum((folder / f).stat().st_size for f in files if (folder / f).exists())
    except (ValueError, OSError, KeyError, json.JSONDecodeError):
        return 0


def room_left(sid=None):
    """Bytes that can still be recorded so that the finished recording can also be saved:
    every recorded byte is needed twice until the parts are joined into the final file."""
    written = parts_bytes(sid) if sid else 0
    return max(0, int((free_bytes() - written - JOIN_MARGIN - MIN_FREE_KEEP) // 2))


def minutes_left(room, profile, extra_bytes_per_hour=0):
    """Minutes of recording that fit; extra: cameras recorded on their own as well."""
    per_hour = BYTES_PER_HOUR.get(profile, max(BYTES_PER_HOUR.values())) + extra_bytes_per_hour
    return int(room / per_hour * 60)


def _slug(text):
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:60] or "match"


def create_session(profile, title):
    """Create a new, empty recording session and return its id."""
    with _lock:
        RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        sid, n = stamp, 1
        while (RECORDINGS_DIR / sid).exists():
            n += 1
            sid = f"{stamp}-{n}"
        (RECORDINGS_DIR / sid / "clips").mkdir(parents=True)
        session = {"id": sid, "title": title or "Match", "profile": profile,
                   "created": _now(), "state": "recording", "parts": [], "markers": [],
                   "file": None, "duration": None, "error": ""}
        _save(session)
        _open_sessions.add(_dir(sid))
        return sid


def new_part(sid):
    """Register the next MPEG-TS part and return its path for the recorder."""
    with _lock:
        session = _load(sid)
        name = f"part-{len(session['parts']) + 1:03d}.ts"
        session["parts"].append({"file": name, "start": None, "duration": None})
        _save(session)
        return str(_dir(sid) / name)


def part_started(sid, wall_start):
    """The newest part's first frame was written at wall_start (epoch seconds)."""
    with _lock:
        session = _load(sid)
        if session["parts"] and session["parts"][-1]["start"] is None:
            session["parts"][-1]["start"] = wall_start
            _save(session)


def _probe_duration(path):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "default=nw=1:nk=1", str(path)],
                                capture_output=True, text=True, timeout=30)
        return max(0.0, float(result.stdout.strip()))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def close_part(sid):
    """The recorder for the newest part exited: measure what it wrote."""
    with _lock:
        session = _load(sid)
        if not session["parts"] or session["parts"][-1]["duration"] is not None:
            return
        part = session["parts"][-1]
    path = _dir(sid) / part["file"]
    duration = _probe_duration(path) if path.exists() and path.stat().st_size else 0.0
    with _lock:
        session = _load(sid)
        last = session["parts"][-1]
        if last["file"] == part["file"]:
            if duration is None and last["start"]:
                duration = max(0.0, _now() - last["start"])
            last["duration"] = round(duration or 0.0, 3)
            if not last["duration"]:
                # Nothing usable was written (e.g. the recorder could not connect).
                session["parts"].pop()
                path.unlink(missing_ok=True)
            _save(session)


def _position(session, wall):
    """Offset on the joined recording timeline for a wall-clock moment."""
    offset = 0.0
    for part in session["parts"]:
        if part["duration"] is not None:
            if part["start"] and wall < part["start"] + part["duration"]:
                return offset + max(0.0, wall - part["start"])
            offset += part["duration"]
        elif part["start"]:
            return offset + max(0.0, wall - part["start"])
    return offset


_written_cache = {}


def written_duration(sid):
    """Seconds actually in the recording files: closed parts plus what the open part holds
    now. Marks and replays use this, not the wall clock: the stream can fall behind real
    time (e.g. while no camera is publishing), and the files are what gets replayed."""
    with _lock:
        session = _load(sid)
    total = 0.0
    for part in session["parts"]:
        if part["duration"] is not None:
            total += part["duration"]
            continue
        path = _dir(sid) / part["file"]
        try:
            size = path.stat().st_size
        except OSError:  # not created yet: the clock is the best estimate
            total += max(0.0, _now() - part["start"]) if part["start"] else 0.0
            break
        key = (sid, part["file"])
        cached = _written_cache.get(key)
        if cached and (cached[0] == size or _now() - cached[2] < 0.5):
            total += cached[1]
            break
        length = _probe_duration(path) if size else 0.0
        if length is None:  # unreadable for a moment: fall back to the clock
            length = max(0.0, _now() - part["start"]) if part["start"] else 0.0
        _written_cache[key] = (size, length, _now())
        total += length
        break
    return total


def new_live_window(sid, camera):
    """Register a recording of the live camera while a replay covers it; returns its path."""
    with _lock:
        session = _load(sid)
        windows = session.setdefault("live", [])
        number = len(windows) + 1
        name = f"live/live-{number:02d}-{camera}.ts"
        (_dir(sid) / "live").mkdir(exist_ok=True)
        windows.append({"id": f"l{number}", "file": name, "camera": camera, "offset": None,
                        "duration": None, "mp4": None})
        _save(session)
        return str(_dir(sid) / name)


def note_camera(sid, camera, info):
    """Remember what was in a camera slot during this recording (label, mount, the phone
    app that reported, what arrived): later users of the files need to know the model."""
    with _lock:
        session = _load(sid)
        cameras = session.setdefault("cameras", {})
        if cameras.get(camera) != info:
            cameras[camera] = info
            _save(session)


def new_iso_part(sid, camera, source=None):
    """Register a new part of a reporting camera's own recording; returns its path.
    source: what was in the slot when this part began (see note_camera)."""
    with _lock:
        session = _load(sid)
        if source:
            session.setdefault("cameras", {})[camera] = source
        parts = session.setdefault("iso", [])
        number = sum(1 for p in parts if p["camera"] == camera) + 1
        name = f"iso/{camera}-{number:02d}.ts"
        (_dir(sid) / "iso").mkdir(exist_ok=True)
        parts.append({"id": f"i{len(parts) + 1}", "file": name, "camera": camera, "start": _now(),
                      "offset": None, "duration": None, "mp4": None, "source": source})
        _save(session)
        return str(_dir(sid) / name)


def iso_started(sid, path, wall_start):
    """The camera's first frame arrived: that, not the request, is where the part begins."""
    with _lock:
        session = _load(sid)
        for part in session.get("iso", []):
            if str(_dir(sid) / part["file"]) == path:
                part["start"] = wall_start
                _save(session)


def _finish_iso_parts(sid):
    """Measure the reporting camera parts, place them on the recording timeline, save
    them as MP4 and drop empty ones (a camera that disconnected at once)."""
    folder = _dir(sid)
    with _lock:
        session = _load(sid)
    parts = session.get("iso", [])
    if not parts:
        return
    kept = []
    for part in parts:
        file = folder / part["file"]
        if part.get("mp4") and (folder / part["mp4"]).exists():
            kept.append(part)
            continue
        if not file.exists():
            continue
        if part["duration"] is None:
            part["duration"] = round(_probe_duration(file) or 0.0, 3)
        if not part["duration"]:
            file.unlink(missing_ok=True)
            continue
        part["offset"] = round(_position(session, part["start"]), 2)
        part["mp4"] = _remux_live(sid, part)
        kept.append(part)
    with _lock:
        fresh = _load(sid)
        fresh["iso"] = kept
        _save(fresh)


def _remux_live(sid, window):
    """The live window as MP4 (copy only); keeps the .ts when that fails."""
    folder = _dir(sid)
    source = folder / window["file"]
    name = window["file"][:-3] + ".mp4"
    tmp = folder / (window["file"][:-3] + ".part.mp4")
    try:
        _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(source),
              "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-movflags", "+faststart", str(tmp)])
        os.replace(tmp, folder / name)
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        logger.warning("Live window %s of %s stays MPEG-TS: %s", window["file"], sid, exc)
        return None
    source.unlink(missing_ok=True)
    return name


def close_live_window(sid, path, aired=True, wall_end=None):
    """The live camera recording ended: place it on the recording timeline, or drop it
    when no replay was actually on air (a replay that failed to start)."""
    folder = _dir(sid)
    with _lock:
        session = _load(sid)
        window = next((w for w in session.get("live", []) if str(folder / w["file"]) == path), None)
    if window is None or window["duration"] is not None:
        return None
    file = folder / window["file"]
    duration = _probe_duration(file) if aired and file.exists() and file.stat().st_size else 0.0
    if not duration:
        file.unlink(missing_ok=True)
        with _lock:
            session = _load(sid)
            session["live"] = [w for w in session.get("live", []) if w["id"] != window["id"]]
            _save(session)
        return None
    # It ends now: where it began on the recording timeline follows from its length.
    begin = (wall_end or _now()) - duration
    window.update(duration=round(duration, 3), offset=round(_position(session, begin), 2))
    window["mp4"] = _remux_live(sid, window)
    with _lock:
        session = _load(sid)
        for i, w in enumerate(session.get("live", [])):
            if w["id"] == window["id"]:
                session["live"][i] = window
        _save(session)
    return window


def _live_window_at(session, offset):
    """The finished live window covering a timeline offset, if any."""
    for window in session.get("live", []):
        if window["duration"] and window["offset"] is not None \
                and window["offset"] <= offset <= window["offset"] + window["duration"]:
            return window
    return None


def add_cut(sid, camera, source, mode="single", secondary=None, wall=None):
    """Log what went on air: camera (or "replay"), who chose it (start, manual, auto, replay)
    and the layout, at its place on the recording timeline and in wall-clock time."""
    wall = _now() if wall is None else wall
    offset = written_duration(sid)
    with _lock:
        session = _load(sid)
        cuts = session.setdefault("cuts", [])
        cuts.append({"t": round(wall, 3), "offset": round(offset, 2), "camera": camera, "source": source,
                     "mode": mode, "secondary": secondary})
        _save(session)
        return cuts[-1]


def cuts(sid):
    with _lock:
        return list(_load(sid).get("cuts", []))


def cuts_csv(sid):
    rows = ["time,offset_s,wall_clock,camera,source,layout,second_camera"]
    for c in cuts(sid):
        clock = datetime.fromtimestamp(c["t"]).isoformat(timespec="milliseconds")
        rows.append(f"{_timestamp(c['offset'])},{c['offset']},{clock},{c['camera']},{c['source']},"
                    f"{c['mode']},{c['secondary'] or ''}")
    return "\n".join(rows) + "\n"


def add_marker(sid, label="Highlight", clock="", score="", wall=None, auto=None):
    """auto names the team for a goal marked by the scoreboard, so a quick correction
    (the -1 button) can withdraw it again."""
    if label not in LABELS:
        raise ValueError("Unknown highlight type")
    with _lock:
        session = _load(sid)
        if session["state"] != "recording":
            raise ValueError("Recording has ended")
        live = wall is None
        wall = _now() if live else wall
    # "Now" is the end of what has been written; an explicit wall time uses the timeline.
    offset = written_duration(sid) if live else _position(session, wall)
    with _lock:
        session = _load(sid)
        marker = {"id": f"m{len(session['markers']) + 1}-{int(wall * 1000) % 100000}",
                  "label": label, "offset": round(offset, 2),
                  "clock": clock, "score": score, "clip": None, "auto": auto, "wall": wall}
        session["markers"].append(marker)
        _save(session)
        return marker


def withdraw_auto_goal(sid, team, within=120):
    """The score went back down: drop that team's recent automatic goal marker."""
    with _lock:
        session = _load(sid)
        recent = [m for m in session["markers"] if m.get("auto") == team and _now() - m.get("wall", 0) < within]
        if not recent:
            return False
    delete_marker(sid, recent[-1]["id"])
    return True


def update_marker(sid, mid, label):
    if label not in LABELS:
        raise ValueError("Unknown highlight type")
    with _lock:
        session = _load(sid)
        marker = _marker(session, mid)
        marker["label"] = label
        _save(session)
        return marker


def delete_marker(sid, mid):
    with _lock:
        session = _load(sid)
        marker = _marker(session, mid)
        session["markers"].remove(marker)
        _save(session)
    if marker.get("clip"):
        (_dir(sid) / marker["clip"]).unlink(missing_ok=True)


def _marker(session, mid):
    for marker in session["markers"]:
        if marker["id"] == mid:
            return marker
    raise ValueError("Unknown highlight")


def _source(session, offset):
    """(file, offset within it) holding a timeline offset: the joined MP4 when it exists."""
    if session.get("file"):
        return _dir(session["id"]) / session["file"], offset
    start = 0.0
    for part in session["parts"]:
        length = part["duration"]
        if length is None or offset < start + length:
            return _dir(session["id"]) / part["file"], max(0.0, offset - start)
        start += length
    if session["parts"]:
        last = session["parts"][-1]
        return _dir(session["id"]) / last["file"], max(0.0, last["duration"] or 0.0)
    raise ValueError("Nothing has been recorded yet")


def source_at(sid, offset):
    """(file path, offset within it) for a timeline offset of a session."""
    with _lock:
        return _source(_load(sid), offset)


def frame_at(sid, offset, width=640):
    """A JPEG still of the recording at a timeline offset (CAM REPLAY preview while scrubbing)."""
    source, inner = source_at(sid, max(0.0, offset))
    result = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{inner:.2f}",
                             "-i", str(source), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5",
                             "-f", "image2", "-c:v", "mjpeg", "pipe:1"], capture_output=True, timeout=15)
    if result.returncode or not result.stdout:
        raise ValueError("No picture at that moment yet")
    return result.stdout


def _run(cmd, timeout=120):
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr.strip().splitlines() or ["ffmpeg failed"])[-1][-300:])


def export_clip(sid, mid, pre=CLIP_PRE, post=CLIP_POST):
    """Cut a highlight around its marker without re-encoding (starts on a keyframe).
    A highlight marked while a replay was on air is cut from the live camera instead."""
    with _lock:
        session = _load(sid)
        marker = dict(_marker(session, mid))
    window = _live_window_at(session, marker["offset"])
    if window:
        return _export_live_clip(sid, session, marker, window, pre, post)
    start = max(0.0, marker["offset"] - pre)
    length = marker["offset"] - start + post
    folder = _dir(sid)
    name = f"clips/{mid}.mp4"
    target = folder / name
    listing = None
    fd, temporary = tempfile.mkstemp(dir=target.parent, suffix=".part.mp4")
    os.close(fd)
    tmp = Path(temporary)
    try:
        if session.get("file"):
            source = folder / session["file"]
            if not source.exists():
                raise ValueError("Recording file is missing")
            inputs = ["-ss", f"{start:.2f}", "-i", str(source)]
        else:
            # A restart splits the timeline into TS parts. Read every part that
            # overlaps the clip, rather than only the part containing the marker.
            parts, elapsed, seek = [], 0.0, start
            for part in session["parts"]:
                duration = part["duration"]
                if duration is None or elapsed + duration > start:
                    if not parts:
                        seek = max(0.0, start - elapsed)
                    if not (folder / part["file"]).is_file():
                        raise ValueError("Recording file is missing")
                    parts.append(part)
                if duration is None:
                    break
                elapsed += duration
                if elapsed >= start + length:
                    break
            if not parts:
                raise ValueError("Nothing has been recorded at this moment")
            fd, path = tempfile.mkstemp(dir=folder, suffix=".concat")
            listing = Path(path)
            seekable = True
            with os.fdopen(fd, "w") as output:
                for part in parts:
                    output.write(f"file '{part['file']}'\n")
                    duration = part["duration"]
                    if duration is None:
                        duration = _probe_duration(folder / part["file"])
                    if duration and math.isfinite(duration):
                        output.write(f"duration {duration:.6f}\n")
                    else:
                        seekable = False
            # Durations let concat seek to the preceding keyframe without scanning
            # hours of footage. Fall back to output seeking for an unprobeable part.
            inputs = ["-f", "concat", "-safe", "0"]
            inputs += (["-ss", f"{seek:.2f}", "-i", str(listing)] if seekable else
                       ["-i", str(listing), "-ss", f"{seek:.2f}"])
        _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *inputs,
              "-t", f"{length:.2f}", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
              "-bsf:a", "aac_adtstoasc", "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(tmp)])
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
        if listing is not None:
            listing.unlink(missing_ok=True)
    with _lock:
        session = _load(sid)
        found = _marker(session, mid)
        found["clip"], found["clip_live"] = name, False
        _save(session)
    return name


CLIP_SIZES = {"720p": (1280, 720), "1080p": (1920, 1080)}


def _export_live_clip(sid, session, marker, window, pre, post):
    """Clip from the live camera recording (the program showed a replay then). Encoded to
    the recording's size and frame rate, so it plays and joins like the other clips."""
    folder = _dir(sid)
    source = folder / (window["mp4"] or window["file"])
    if not source.exists():
        raise ValueError("Live recording file is missing")
    inner = marker["offset"] - window["offset"]
    start = max(0.0, inner - pre)
    length = min(window["duration"], inner + post) - start
    width, height = CLIP_SIZES.get(session.get("profile"), CLIP_SIZES["720p"])
    name = f"clips/{marker['id']}.mp4"
    target = folder / name
    target.parent.mkdir(exist_ok=True)
    tmp = target.with_suffix(".part.mp4")
    try:
        _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-ss", f"{start:.2f}",
              "-i", str(source), "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
              "-t", f"{length:.2f}", "-map", "0:v:0", "-map", "0:a:0?" if _has_audio(source) else "1:a:0",
              "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                     f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30",
              "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
              "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-shortest",
              "-movflags", "+faststart", str(tmp)], timeout=300)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    with _lock:
        session = _load(sid)
        found = _marker(session, marker["id"])
        found["clip"], found["clip_live"] = name, True
        _save(session)
    return name


def _has_audio(path):
    try:
        result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                                 "stream=index", "-of", "csv=p=0", str(path)],
                                capture_output=True, text=True, timeout=30)
        return bool(result.stdout.strip())
    except (OSError, subprocess.TimeoutExpired):
        return False


def build_reel(sid):
    """Join every highlight clip, in match order, into one reel."""
    with _lock:
        markers = sorted(_load(sid)["markers"], key=lambda m: m["offset"])
    if not markers:
        raise ValueError("Mark some highlights first")
    clips = []
    for marker in markers:
        name = marker["clip"] if marker.get("clip") and (_dir(sid) / marker["clip"]).exists() else export_clip(sid, marker["id"])
        clips.append(_dir(sid) / name)
    with _lock:
        mixed = len({bool(m.get("clip_live")) for m in _load(sid)["markers"]}) > 1
    folder = _dir(sid)
    listing = folder / "reel.txt"
    listing.write_text("".join(f"file '{p.relative_to(folder)}'\n" for p in clips))
    tmp = folder / "reel.part.mp4"
    try:
        _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
              "-safe", "0", "-i", str(listing),
              # Clips from the live camera come from another encoder: join by re-encoding.
              *(["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "160k"] if mixed else ["-c", "copy"]),
              "-movflags", "+faststart", str(tmp)], timeout=900 if mixed else 300)
        os.replace(tmp, folder / "highlights.mp4")
    finally:
        listing.unlink(missing_ok=True)
    return "highlights.mp4"


def start_export(sid, mid=None, background=True):
    """One export per session; repeat dashboard requests can follow the same job."""
    with _lock:
        folder = _dir(sid)
        session = _load(sid)
        if mid is not None:
            _marker(session, mid)
        elif not session["markers"]:
            raise ValueError("Mark some highlights first")
        if sid in _finalizing or session["state"] == "finalizing":
            raise ValueError("Wait for the recording to finish saving")
        job = _exports.get(folder, {})
        if job.get("state") == "running":
            if background and job["marker"] == mid:
                return None
            raise ValueError("An export is already running for this recording")
        _exports[folder] = {"marker": mid, "state": "running", "message": ""}

    def run():
        try:
            name = export_clip(sid, mid) if mid is not None else build_reel(sid)
        except Exception as exc:
            with _lock:
                _exports[folder].update(state="failed", message=str(exc))
            if not background:
                raise
            logger.exception("Export failed for %s", sid)
        else:
            with _lock:
                _exports[folder].update(state="done", message="")
            return name
        finally:
            with _exports_changed:
                _exports_changed.notify_all()

    if not background:
        return run()
    try:
        threading.Thread(target=run, daemon=True, name=f"export-{sid}").start()
    except RuntimeError:
        with _lock:
            _exports.pop(folder, None)
            _exports_changed.notify_all()
        raise


def _timestamp(seconds):
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapters(session):
    """YouTube chapter lines for the uploaded recording (first chapter must be 0:00)."""
    lines = ["0:00 Kick-off"]
    for marker in sorted(session["markers"], key=lambda m: m["offset"]):
        if marker["offset"] < 10:
            continue  # YouTube chapters must be at least 10 s long
        detail = " ".join(x for x in (marker.get("score"), f"({marker['clock']})" if marker.get("clock") else "") if x)
        lines.append(f"{_timestamp(marker['offset'])} {marker['label']}" + (f" – {detail}" if detail else ""))
    return "\n".join(lines)


def reopen(sid):
    """Continue an interrupted, unsaved session after a restart: the recorder adds a part."""
    with _lock:
        try:
            session = _load(sid)
        except ValueError:
            return False
        if session["state"] not in ("unfinished", "recording") or session.get("file") or sid in _finalizing:
            return False
        for part in session["parts"]:
            if part["duration"] is None:
                path = _dir(sid) / part["file"]
                part["duration"] = round(_probe_duration(path) or 0.0, 3) if path.exists() else 0.0
        session.update(state="recording", error="")
        _save(session)
        _open_sessions.add(_dir(sid))
        logger.info("Recording %s continues after a restart", sid)
        return True


def finalize(sid, timeout=3600, *, background=False):
    """Reserve the session before queuing work so a second request cannot start another join."""
    with _lock:
        folder = _dir(sid)
        if sid in _finalizing:
            return False, "Already finalizing"
        _load(sid)
        _finalizing.add(sid)

    def finish():
        try:
            with _exports_changed:
                if not _exports_changed.wait_for(
                        lambda: _exports.get(folder, {}).get("state") != "running", timeout=timeout):
                    session = _load(sid)
                    session.update(state="unfinished", error="Export still running. Finish saving after it completes.")
                    _save(session)
                    return False, session["error"]
            return _join_parts(sid, timeout)
        finally:
            with _lock:
                _open_sessions.discard(folder)
                _finalizing.discard(sid)

    if not background:
        return finish()
    try:
        threading.Thread(target=finish, daemon=True, name=f"finalize-{sid}").start()
    except RuntimeError:
        with _lock:
            _finalizing.discard(sid)
            _open_sessions.discard(folder)
        raise
    return True, "Saving the recording…"


def _join_parts(sid, timeout):
    with _lock:
        session = _load(sid)
        parts = [p for p in session["parts"] if (_dir(sid) / p["file"]).exists()]
        if parts:
            session["state"], session["error"] = "finalizing", ""
        else:
            session["state"] = "empty" if not session.get("file") else "done"
        _save(session)
    if not parts:
        _finish_iso_parts(sid)  # the reporting cameras may have recorded on their own
        return False, "Nothing was recorded"
    folder = _dir(sid)
    name = f"{sid[:8]}_{_slug(session['title'])}.mp4"
    listing = folder / "parts.txt"
    tmp = folder / "joined.part.mp4"
    try:
        # Joining copies every part before any is removed, so it needs their size again.
        needed = sum((folder / p["file"]).stat().st_size for p in parts) + JOIN_MARGIN
        free = free_bytes()
        if free < needed:
            raise RuntimeError(f"not enough free space: needs {needed / GB:.1f} GB, "
                               f"{free / GB:.1f} GB free. The recording parts are safe; "
                               "free up space, then press Finish saving")
        listing.write_text("".join(f"file '{p['file']}'\n" for p in parts))
        _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
              "-safe", "0", "-i", str(listing), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
              "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(tmp)], timeout=timeout)
        if not tmp.stat().st_size:
            raise RuntimeError("Joined file is empty")
        os.replace(tmp, folder / name)
        duration = _probe_duration(folder / name)
        # The side files first: "done" means everything of the session is saved.
        _finish_live_windows(sid)
        _finish_iso_parts(sid)
        with _lock:
            session = _load(sid)
            session.update(file=name, state="done", duration=duration, error="")
            _save(session)
        for part in parts:
            (folder / part["file"]).unlink(missing_ok=True)
        logger.info("Recording %s saved as %s", sid, name)
        return True, name
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        with _lock:
            session = _load(sid)
            session.update(state="unfinished", error=f"Could not join the recording: {exc}")
            _save(session)
        logger.error("Finalizing %s failed: %s", sid, exc)
        return False, str(exc)
    finally:
        listing.unlink(missing_ok=True)


def _finish_live_windows(sid):
    """Live windows the app could not finish (it stopped during a replay): measure them,
    place them by their file times and save them as MP4."""
    folder = _dir(sid)
    with _lock:
        session = _load(sid)
    changed = False
    for window in session.get("live", []):
        file = folder / window["file"]
        if window["duration"] is None and file.exists():
            window["duration"] = round(_probe_duration(file) or 0.0, 3)
            window["offset"] = round(_position(session, file.stat().st_mtime - window["duration"]), 2)
            changed = True
        if not window.get("mp4") and file.exists() and window["duration"]:
            window["mp4"] = _remux_live(sid, window)
            changed = True
    if changed:
        with _lock:
            fresh = _load(sid)
            fresh["live"] = [w for w in session["live"] if w["duration"]]
            _save(fresh)


def finalize_async(sid):
    return finalize(sid, background=True)


def recover():
    """At startup: sessions left 'recording' or 'finalizing' by a crash become unfinished."""
    if not RECORDINGS_DIR.is_dir():
        return
    for folder in RECORDINGS_DIR.iterdir():
        try:
            with _lock:
                session = _load(folder.name)
                if session["state"] in ("recording", "finalizing"):
                    for part in session["parts"]:
                        if part["duration"] is None:
                            path = folder / part["file"]
                            part["duration"] = round(_probe_duration(path) or 0.0, 3) if path.exists() else 0.0
                    session["state"] = "unfinished"
                    session["error"] = "The app stopped while recording; join the saved parts to finish it"
                    _save(session)
        except (ValueError, OSError, json.JSONDecodeError):
            continue


def delete_session(sid):
    with _lock:
        session = _load(sid)
        if session["state"] in ("recording", "finalizing"):
            raise ValueError("Stop the recording first")
        folder = _dir(sid)
        if sid in _finalizing or folder in _open_sessions or _exports.get(folder, {}).get("state") == "running":
            raise ValueError("Wait for saving and exports to finish")
        shutil.rmtree(folder)
        _exports.pop(folder, None)


def _size(folder):
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def describe(session, live_wall=None):
    """Public view of a session for the dashboard."""
    folder = _dir(session["id"])
    duration = session["duration"]
    if duration is None:
        duration = sum(p["duration"] or 0.0 for p in session["parts"])
        open_part = session["parts"][-1] if session["parts"] else None
        if live_wall and open_part and open_part["duration"] is None and open_part["start"]:
            duration += max(0.0, live_wall - open_part["start"])
    markers = []
    for marker in sorted(session["markers"], key=lambda m: m["offset"]):
        clip = marker.get("clip")
        markers.append({**marker, "time": _timestamp(marker["offset"]),
                        "clip": clip if clip and (folder / clip).exists() else None})
    return {"id": session["id"], "title": session["title"], "created": session["created"],
            "state": session["state"], "profile": session["profile"], "error": session.get("error", ""),
            "file": session.get("file"), "duration": round(duration or 0.0, 1),
            "bytes": _size(folder), "markers": markers, "export": dict(_exports.get(folder, {})),
            "reel": "highlights.mp4" if (folder / "highlights.mp4").exists() else None,
            "cuts": len(session.get("cuts", [])),
            "live": [{"id": w["id"], "camera": w["camera"], "time": _timestamp(w["offset"]),
                      "offset": w["offset"], "duration": w["duration"], "file": w["mp4"] or w["file"]}
                     for w in session.get("live", []) if w["duration"] and (folder / (w["mp4"] or w["file"])).exists()],
            "iso": [{"id": p["id"], "camera": p["camera"], "offset": p["offset"],
                     "time": _timestamp(p["offset"]) if p["offset"] is not None else "",
                     "duration": p["duration"], "file": p["mp4"] or p["file"],
                     "recording": session["state"] == "recording" and not p["mp4"]}
                    for p in session.get("iso", []) if (folder / (p["mp4"] or p["file"])).exists()],
            "chapters": chapters(session)}


def get(sid, live_wall=None):
    with _lock:
        return describe(_load(sid), live_wall)


def list_sessions(live_wall=None):
    if not RECORDINGS_DIR.is_dir():
        return []
    items = []
    with _lock:
        for folder in sorted(RECORDINGS_DIR.iterdir(), reverse=True):
            try:
                items.append(describe(_load(folder.name), live_wall))
            except (ValueError, OSError, json.JSONDecodeError):
                continue
    return items


def file_path(sid, name):
    """Resolve a downloadable file inside a session; never outside it."""
    folder = _dir(sid).resolve()
    path = (folder / name).resolve()
    if folder not in path.parents or path.suffix not in (".mp4", ".ts") or not path.is_file():
        raise ValueError("Unknown file")
    return path
