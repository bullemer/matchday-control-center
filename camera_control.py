"""Phone camera status and remote zoom (Chameleon Stream app).

While it streams over SRT the app POSTs a status report to /api/camera-report/<cam>:
battery, charging, temperature, Android thermal status and its zoom (current, min, max).
Long poll: the request is held for up to HOLD_S seconds and answered at once when the
operator asks for a zoom, so a phone sends about one request every 30 s, yet zoom reacts
immediately. The answer carries the requested zoom with a sequence number (the phone
applies each request once; a pinch on the phone is not undone). Nothing is pushed to the
phone: it needs no server and works behind any router.
"""
import math
import threading
import time

HOLD_S = 28.0             # long poll: answer after this at the latest
REPORT_STALE = 45.0       # seconds: older reports are not shown (phone gone / app stopped)
ZOOM_STEP = 1.25          # one + / - press
THERMAL_LEVELS = ("none", "light", "moderate", "severe", "critical", "emergency", "shutdown")
MODES = ("gimbal", "fixed", "reporter")

_lock = threading.Lock()
_changed = threading.Condition(_lock)  # notified on every new zoom request
_reports = {}             # cam -> cleaned report + "at"
_zoom_requests = {}       # cam -> {"zoom": float, "seq": int, "at": float}
# Request numbers keep growing across restarts of this app: a phone that applied request n
# before a restart must not ignore the first new one (it would also be n otherwise).
_SEQ_BASE = int(time.time()) - 1_700_000_000


def _number(data, key, low, high):
    value = data.get(key)
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{key} must be a number between {low} and {high}")
    return float(value)


def clean_report(data):
    if not isinstance(data, dict):
        raise ValueError("A JSON object is required")
    report = {
        "battery": _number(data, "battery", 0, 100),
        "temp_c": _number(data, "temp_c", -40, 120),
        "zoom": _number(data, "zoom", 0.1, 100),
        "zoom_min": _number(data, "zoom_min", 0.1, 100),
        "zoom_max": _number(data, "zoom_max", 0.1, 100),
        "headroom": _number(data, "headroom", 0, 10),  # Android thermal headroom, 1.0 = throttling starts
    }
    charging = data.get("charging")
    if charging is not None and type(charging) is not bool:
        raise ValueError("charging must be true or false")
    report["charging"] = charging
    report["remote_zoom"] = data.get("remote_zoom") is True
    thermal = data.get("thermal")
    if thermal is not None and thermal not in THERMAL_LEVELS:
        raise ValueError("unknown thermal status")
    report["thermal"] = thermal
    mode = data.get("mode")
    report["mode"] = mode if mode in MODES else None
    app = data.get("app")
    report["app"] = app[:40] if isinstance(app, str) and app.isprintable() else None
    if report["zoom_min"] and report["zoom_max"] and report["zoom_min"] > report["zoom_max"]:
        raise ValueError("zoom_min must not exceed zoom_max")
    return report


def warnings(report):
    """Short operator warnings for one report."""
    notes = []
    thermal = report.get("thermal")
    if thermal and THERMAL_LEVELS.index(thermal) >= THERMAL_LEVELS.index("moderate"):
        notes.append(f"phone overheating ({thermal}): shade it, lower the resolution")
    elif (report.get("temp_c") or 0) >= 43:
        notes.append("phone is getting hot")
    battery = report.get("battery")
    if battery is not None and battery <= 20 and not report.get("charging"):
        notes.append("battery low: connect a power bank")
    return notes


def record_report(cam, data, hold=None):
    """Store a phone's report; returns the answer for the phone. If the phone says which
    zoom request it already has (zoom_seq) and wants to wait, the answer comes as soon as
    there is a newer one, or after hold seconds."""
    report = clean_report(data)
    known = data.get("zoom_seq")
    wait = HOLD_S if hold is None else hold
    if data.get("wait") is not True or type(known) is not int:
        wait = 0
    deadline = time.monotonic() + wait
    with _changed:
        report["at"] = time.time()
        _reports[cam] = report
        while True:
            request = _zoom_requests.get(cam)
            seq = request["seq"] if request else _SEQ_BASE
            left = deadline - time.monotonic()
            if seq != known or left <= 0:
                return {"zoom": request["zoom"] if request else None, "zoom_seq": seq}
            _changed.wait(left)


def get_reports():
    """Fresh reports for the dashboard, with age and warnings."""
    now = time.time()
    with _lock:
        result = {}
        for cam, report in _reports.items():
            age = now - report["at"]
            if age > REPORT_STALE:
                continue
            item = {k: v for k, v in report.items() if k != "at"}
            item["age_s"] = round(age, 1)
            item["warning"] = "; ".join(warnings(report))
            pending = _zoom_requests.get(cam)
            # A request newer than the phone's last report is still on its way: show that.
            item["zoom_target"] = pending["zoom"] if pending and pending["at"] >= report["at"] else None
            result[cam] = item
        return result


def request_zoom(cam, zoom=None, step=None, wide=False):
    """Operator zoom: an absolute ratio, step +1 / -1 (x1.25), or wide (the phone's minimum)."""
    with _lock:
        report = _reports.get(cam)
        if not report or time.time() - report["at"] > REPORT_STALE + HOLD_S:
            raise ValueError("This camera does not report (Chameleon Stream app over SRT required)")
        if not report.get("remote_zoom"):
            raise ValueError("Remote zoom is off on this phone (gimbal auto zoom is in control)")
        low = report.get("zoom_min") or 1.0
        high = report.get("zoom_max") or 10.0
        pending = _zoom_requests.get(cam)
        # While the phone still glides to the last request, build on that; otherwise on what
        # the phone reports (the operator there may have pinched since).
        recent = pending and time.time() - pending["at"] < 2.0
        current = pending["zoom"] if recent else (report.get("zoom") or 1.0)
        if wide:
            target = low
        elif step is not None:
            if step not in (-1, 1):
                raise ValueError("step must be -1 or 1")
            target = current * ZOOM_STEP if step > 0 else current / ZOOM_STEP
        elif type(zoom) in (int, float) and math.isfinite(zoom):
            target = float(zoom)
        else:
            raise ValueError("Give zoom, step or wide")
        target = round(min(max(target, low), high), 2)
        _zoom_requests[cam] = {"zoom": target, "seq": (pending["seq"] if pending else _SEQ_BASE) + 1, "at": time.time()}
        _changed.notify_all()
        return target


def reset():
    with _lock:
        _reports.clear()
        _zoom_requests.clear()
