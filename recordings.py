"""Local match recordings: crash-safe parts, highlight markers, clips, reel and chapters.

A recording session lives in RECORDINGS_DIR/<id>/ with a session.json. The recorder
writes the final program (with scoreboard) as MPEG-TS parts, one per recorder process,
so a crash or encoder restart loses seconds, not the match. Stopping joins the parts
into one MP4. Markers carry their offset on the recording timeline (seconds from the
start of the joined file), so clips can be cut from the parts or from the MP4 alike.
"""
from __future__ import annotations

import json
import logging
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
RECORDINGS_DIR = Path(os.environ.get("MCC_RECORDINGS_DIR", BASE / "recordings"))
GB = 1024 ** 3
MIN_FREE_START = 3 * GB   # refuse to start recording below this
MIN_FREE_KEEP = 1 * GB    # stop recording automatically below this
LABELS = ("Highlight", "Goal", "Chance", "Save", "Card", "Foul", "Other")
CLIP_PRE, CLIP_POST = 20, 10
_lock = threading.RLock()
_finalizing = set()


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
    """Cut a highlight around its marker without re-encoding (starts on a keyframe)."""
    with _lock:
        session = _load(sid)
        marker = dict(_marker(session, mid))
        source, offset = _source(session, marker["offset"])
    if not source.exists():
        raise ValueError("Recording file is missing")
    start = max(0.0, offset - pre)
    name = f"clips/{mid}.mp4"
    target = _dir(sid) / name
    tmp = target.with_suffix(".part.mp4")
    _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
          "-ss", f"{start:.2f}", "-i", str(source), "-t", f"{offset - start + post:.2f}",
          "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc",
          "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(tmp)])
    os.replace(tmp, target)
    with _lock:
        session = _load(sid)
        _marker(session, mid)["clip"] = name
        _save(session)
    return name


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
    folder = _dir(sid)
    listing = folder / "reel.txt"
    listing.write_text("".join(f"file '{p.relative_to(folder)}'\n" for p in clips))
    tmp = folder / "reel.part.mp4"
    try:
        _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
              "-safe", "0", "-i", str(listing), "-c", "copy", "-movflags", "+faststart", str(tmp)], timeout=300)
        os.replace(tmp, folder / "highlights.mp4")
    finally:
        listing.unlink(missing_ok=True)
    return "highlights.mp4"


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


def finalize(sid):
    """Join the parts into one MP4 (copy only) and remove the parts when that worked."""
    with _lock:
        if sid in _finalizing:
            return False, "Already finalizing"
        session = _load(sid)
        parts = [p for p in session["parts"] if (_dir(sid) / p["file"]).exists()]
        if not parts:
            session["state"] = "empty" if not session.get("file") else "done"
            _save(session)
            return False, "Nothing was recorded"
        _finalizing.add(sid)
        session["state"], session["error"] = "finalizing", ""
        _save(session)
    folder = _dir(sid)
    name = f"{sid[:8]}_{_slug(session['title'])}.mp4"
    listing = folder / "parts.txt"
    tmp = folder / "joined.part.mp4"
    try:
        listing.write_text("".join(f"file '{p['file']}'\n" for p in parts))
        _run(["ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-f", "concat",
              "-safe", "0", "-i", str(listing), "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
              "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", str(tmp)], timeout=3600)
        if not tmp.stat().st_size:
            raise RuntimeError("Joined file is empty")
        os.replace(tmp, folder / name)
        duration = _probe_duration(folder / name)
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
        with _lock:
            _finalizing.discard(sid)


def finalize_async(sid):
    threading.Thread(target=finalize, args=(sid,), daemon=True, name=f"finalize-{sid}").start()


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
        shutil.rmtree(_dir(sid))


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
            "bytes": _size(folder), "markers": markers,
            "reel": "highlights.mp4" if (folder / "highlights.mp4").exists() else None,
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
