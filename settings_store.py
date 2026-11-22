"""Operator settings from the Settings tab, saved in data/settings.json (0600).

Login (user + admin password) keeps its own files (data/operator-user,
data/operator-password) as before. Environment variables still win where they exist
(MCC_CAMERA_PASSWORD, MCC_RECORDINGS_DIR, MCC_OPERATOR_USER/PASSWORD): the tab then shows
the value as fixed.
"""
import json
import os
import re
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent
PATH = BASE / "data" / "settings.json"
PROFILES = ("1080p", "720p", "480p")

DEFAULTS = {
    "camera_password": "",          # empty: cameras publish without a password
    "home_name": "Home",
    "away_name": "Guest",
    "half_length": 45,              # minutes
    "profile": "720p",              # preselected stream / recording quality
    "recordings_dir": "",           # empty: <app>/recordings
    "min_free_gb": 3,               # refuse to start recording below this much free space
}

CAMERA_PASSWORD_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")   # goes into SRT stream ids and RTMP URLs

_lock = threading.Lock()
_values = dict(DEFAULTS)


def load():
    try:
        data = json.loads(PATH.read_text())
    except (OSError, ValueError):
        data = {}
    clean = dict(DEFAULTS)
    if isinstance(data, dict):
        for key, value in data.items():
            if key in DEFAULTS:
                try:
                    clean[key] = _check(key, value)
                except ValueError:
                    pass  # a broken value falls back to the default
    with _lock:
        _values.clear()
        _values.update(clean)
    return dict(clean)


def get(key=None):
    with _lock:
        return _values[key] if key else dict(_values)


def _check(key, value):
    if key == "camera_password":
        if not isinstance(value, str) or (value and not CAMERA_PASSWORD_RE.fullmatch(value)):
            raise ValueError("Camera password: 8-64 letters, digits, - or _ (or empty for none)")
        return value
    if key in ("home_name", "away_name"):
        if not isinstance(value, str) or not value.strip() or len(value) > 40 or any(ord(c) < 32 for c in value):
            raise ValueError("Team names: 1-40 characters")
        return value.strip()
    if key == "half_length":
        if type(value) is not int or not 10 <= value <= 60:
            raise ValueError("Half length: 10-60 minutes")
        return value
    if key == "profile":
        if value not in PROFILES:
            raise ValueError("Unknown quality profile")
        return value
    if key == "recordings_dir":
        if not isinstance(value, str) or (value and not os.path.isabs(value)) or "\0" in value:
            raise ValueError("Recording folder: an absolute path (or empty for the default)")
        return value.rstrip("/") or ("/" if value else "")
    if key == "min_free_gb":
        if type(value) not in (int, float) or not 1 <= value <= 500:
            raise ValueError("Minimum free space: 1-500 GB")
        return value
    raise ValueError(f"Unknown setting {key}")


def update(changes):
    """Validate and save several settings at once; returns all settings."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Nothing to change")
    checked = {k: _check(k, v) for k, v in changes.items()}
    with _lock:
        _values.update(checked)
        PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = PATH.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # holds the camera password
        with os.fdopen(fd, "w") as f:
            json.dump(_values, f, indent=1)
        os.replace(tmp, PATH)
        return dict(_values)
