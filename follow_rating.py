"""How well a self-following gimbal camera (Touchline) keeps play in its picture.

The AI Director already rates every match camera's picture about once a second. This
module keeps the last minute of those ratings for each Touchline camera and reduces them
to a few numbers: the operator sees them in the gimbal panel, the phone gets them back
with its next report, and a row per minute goes to a file together with the phone's
active tuning, so two settings can be compared after a match.
"""
import csv
import json
import threading
import time
from collections import deque

WINDOW_S = 60.0           # the rating covers this much of the recent past
MIN_SAMPLES = 10          # fewer ratings than this say nothing yet
LOG_EVERY_S = 60.0        # one file row per camera this often
LOG_FILE = None           # set by the app; None = keep nothing on disk
LOG_FIELDS = ("time", "camera", "score", "ball_pct", "play_pct", "following_pct", "frozen_pct", "seconds", "tuning")

_lock = threading.Lock()
_samples = {}             # cam -> deque of (time, score, ball in picture, play part, following, frozen)
_logged = {}              # cam -> time of its last file row


def add(cam, rated, following, now=None):
    """One AI Director rating of this camera's picture. following: the phone steers itself."""
    score = rated.get("score")
    if not rated.get("online") or type(score) not in (int, float):
        return
    now = time.time() if now is None else now
    ball = (rated.get("ball") or {}).get("part") or 0
    play = (rated.get("action") or {}).get("part") or 0
    frozen = bool((rated.get("quality") or {}).get("frozen"))
    with _lock:
        samples = _samples.setdefault(cam, deque())
        if samples and now - samples[-1][0] < 0.5:
            return                      # the worker's state file has not changed yet
        samples.append((now, float(score), ball > 0, float(play), bool(following), frozen))
        while samples and now - samples[0][0] > WINDOW_S:
            samples.popleft()


def summary(cam, now=None):
    """Numbers for the last minute, or None while there are too few fresh ratings."""
    now = time.time() if now is None else now
    with _lock:
        samples = [s for s in _samples.get(cam, ()) if now - s[0] <= WINDOW_S]
    if len(samples) < MIN_SAMPLES or now - samples[-1][0] > 10:
        return None
    count = len(samples)
    share = lambda index: round(100 * sum(1 for s in samples if s[index]) / count)
    return {"score": round(sum(s[1] for s in samples) / count, 1),
            "ball_pct": share(2),
            "play_pct": round(100 * sum(s[3] for s in samples) / count),
            "following_pct": share(4),
            "frozen_pct": share(5),
            "seconds": round(samples[-1][0] - samples[0][0])}


def summaries(now=None):
    with _lock:
        cams = list(_samples)
    result = {cam: summary(cam, now) for cam in cams}
    return {cam: value for cam, value in result.items() if value}


def log_row(cam, tuning, now=None):
    """Append this camera's rating to the file, at most once per LOG_EVERY_S."""
    now = time.time() if now is None else now
    if LOG_FILE is None or now - _logged.get(cam, 0) < LOG_EVERY_S:
        return False
    numbers = summary(cam, now)
    if not numbers:
        return False
    _logged[cam] = now
    new = not LOG_FILE.exists()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=LOG_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "camera": cam,
                         **numbers, "tuning": json.dumps(tuning or {}, sort_keys=True)})
    return True


def reset():
    with _lock:
        _samples.clear()
        _logged.clear()
