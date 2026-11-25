"""Phone camera status, remote commands and zoom (Chameleon Stream app).

A streaming phone reports zoom, health and tally through the SRT camera slot's HTTP
endpoint. A Gimbal-mode phone can also report from its idle preview. Its control
session receives short-lived operator commands and reports the hardware state back.
"""
import json
import math
import os
import re
import threading
import time
import uuid

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


# Each phone link has a fresh session. Commands expire even if the phone loses Wi-Fi.
CONTROL_POLL_S = 4
CONTROL_TTL = 12
_commands = {}
ACTIONS = ("start_stream", "stop_stream", "connect_gimbal", "disconnect_gimbal", "start_tracking", "stop_tracking")


def _phone_control(data):
    session = data.get("control_session")
    if not isinstance(session, str) or not 1 <= len(session) <= 64 or data.get("mode") != "gimbal":
        return None
    stream, gimbal, tracking = data.get("stream"), data.get("gimbal"), data.get("tracking")
    if stream not in ("stopped", "connecting", "live") or gimbal not in ("disconnected", "connecting", "ready") or type(tracking) is not bool:
        raise ValueError("Invalid phone control state")
    error = data.get("gimbal_error")
    return {"session": session, "stream": stream, "gimbal": gimbal, "tracking": tracking,
            "gimbal_error": error[:120] if gimbal == "disconnected" and isinstance(error, str) else None}


def _actions(control):
    actions = ["start_stream" if control["stream"] == "stopped" else "stop_stream",
               "connect_gimbal" if control["gimbal"] == "disconnected" else "disconnect_gimbal"]
    if control["gimbal"] == "ready":
        actions.append("stop_tracking" if control["tracking"] else "start_tracking")
    return actions


def request_action(cam, action):
    with _changed:
        report = _reports.get(cam, {})
        control = report.get("control")
        if not control or time.time() - report["at"] > CONTROL_TTL:
            raise ValueError("Open Chameleon Stream in Gimbal mode and enable dashboard control")
        if action not in ACTIONS or action not in _actions(control):
            raise ValueError("This action is unavailable in the phone’s current state")
        pending = _commands.get(cam)
        if pending and pending["expires"] > time.monotonic() and "error" not in pending:
            raise ValueError("The phone is still handling the previous command")
        _commands[cam] = {"id": uuid.uuid4().hex, "action": action, "session": control["session"],
                          "expires": time.monotonic() + CONTROL_TTL}
        _changed.notify_all()


# Touchline (iPhone on a DockKit gimbal) takes named tuning numbers from here while it
# runs: the operator or an automatic tuner sets them, the phone's next report gets them
# at once and reports back what it applied and how well its picture follows play.
TUNING_FILE = None        # set by the app: tuning survives a restart of this app
TUNING_MAX = 24
_TUNING_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
_tuning = {}              # cam -> {name: number}


def _named_numbers(values, what, clearing=False):
    if not isinstance(values, dict) or len(values) > TUNING_MAX:
        raise ValueError(f"{what} must be an object with at most {TUNING_MAX} numbers")
    for name, value in values.items():
        if not isinstance(name, str) or not _TUNING_NAME.fullmatch(name):
            raise ValueError(f"{what}: '{name}' is not a valid name")
        if clearing and value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 1e9:
            raise ValueError(f"{what}: {name} must be a number")
    return dict(values)


def load_tuning(path):
    """Remember where tuning is kept and read what an earlier run saved."""
    global TUNING_FILE
    with _changed:
        TUNING_FILE = path
        _tuning.clear()
        try:
            saved = json.loads(path.read_text())
            for cam, values in saved.items():
                _tuning[cam] = _named_numbers(values, "tuning")
        except (OSError, ValueError, AttributeError):
            _tuning.clear()


def set_tuning(cam, changes):
    """Merge tuning numbers for one camera; null removes a name (the phone then uses its
    own default). The waiting phone is answered at once. Returns the full set."""
    changes = _named_numbers(changes, "tuning", clearing=True)
    with _changed:
        values = dict(_tuning.get(cam, {}))
        for name, value in changes.items():
            if value is None:
                values.pop(name, None)
            else:
                values[name] = value
        if len(values) > TUNING_MAX:
            raise ValueError(f"At most {TUNING_MAX} tuning numbers per camera")
        _tuning[cam] = values
        if TUNING_FILE is not None:
            temporary = TUNING_FILE.with_suffix(".tmp")
            temporary.write_text(json.dumps(_tuning, sort_keys=True))
            os.replace(temporary, TUNING_FILE)
        _changed.notify_all()
        return dict(values)


def get_tuning(cam):
    with _lock:
        return dict(_tuning.get(cam, {}))


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
    report["control"] = _phone_control(data)
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
    for key in ("tuning_active", "quality"):  # Touchline: applied values, picture-follows-play numbers
        report[key] = _named_numbers(data[key], key) if data.get(key) is not None else None
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


TALLY_CHECK_S = 0.5      # how often a waiting request looks at the tally


def _config_key(config):
    return json.dumps(config, sort_keys=True) if config is not None else None


def record_report(cam, data, hold=None, tally=None, config=None):
    """Store a phone's report; returns the answer for the phone. If the phone says which
    zoom request (zoom_seq) and tally it already has and wants to wait, the answer comes as
    soon as either changes, or after hold seconds. tally: callable returning the camera's
    tally word (checked every TALLY_CHECK_S; it reads in-memory state only). config: callable
    returning the settings the dashboard wants on this phone (mode, resolution, bitrate,
    latency); a phone that has an older one (config_key) is answered at once."""
    report = clean_report(data)
    known = data.get("zoom_seq")
    known_tally = data.get("tally") if isinstance(data.get("tally"), str) else None
    known_config = data.get("config_key") if isinstance(data.get("config_key"), str) else None
    # Only a phone that names its tuning (even "") understands tuning answers.
    known_tuning = data.get("tuning_key") if isinstance(data.get("tuning_key"), str) else None
    wait = HOLD_S if hold is None else hold
    if data.get("wait") is not True or type(known) is not int:
        wait = 0
    control = report["control"]
    if control:
        wait = min(wait, CONTROL_POLL_S)
    deadline = time.monotonic() + wait
    with _changed:
        report["at"] = time.time()
        _reports[cam] = report
        pending = _commands.get(cam)
        if pending and (not control or control["session"] != pending["session"]):
            _commands.pop(cam)
        elif pending and data.get("action_ack") == pending["id"]:
            error = data.get("action_error")
            if isinstance(error, str) and error:
                pending["error"] = error[:120]
            elif pending["action"] in ("start_tracking", "stop_tracking") and control["gimbal"] != "ready":
                pending["error"] = "Gimbal disconnected before confirming tracking"
            elif pending["action"] not in _actions(control):
                # For tracking, wait for the gimbal's own state packet.
                _commands.pop(cam)
        while True:
            request = _zoom_requests.get(cam)
            seq = request["seq"] if request else _SEQ_BASE
            left = deadline - time.monotonic()
            now_tally = _safe_tally(tally)
            tally_changed = known_tally is not None and now_tally is not None and now_tally != known_tally
            now_config = _safe_tally(config)
            config_changed = now_config is not None and _config_key(now_config) != known_config
            now_tuning = _tuning.get(cam, {}) if known_tuning is not None else None
            tuning_changed = now_tuning is not None and _config_key(now_tuning) != known_tuning
            pending = _commands.get(cam)
            if pending and now_config is not None and now_config.get("mode") != "gimbal":
                _commands.pop(cam)
                pending = None
            current = _reports.get(cam) is report
            send = (current and pending and control and pending["session"] == control["session"]
                    and pending["expires"] > time.monotonic() and "error" not in pending
                    and data.get("action_ack") != pending["id"]
                    and (config is None or now_config is not None))
            if send or seq != known or tally_changed or config_changed or tuning_changed or left <= 0:
                answer = {"zoom": request["zoom"] if request else None, "zoom_seq": seq}
                if control:
                    answer["supports_actions"] = True
                if send:
                    answer["action"] = {"id": pending["id"], "name": pending["action"],
                                        "valid_ms": int(1000 * (pending["expires"] - time.monotonic()))}
                if now_tally is not None:
                    answer["tally"] = now_tally
                if now_config is not None:
                    answer["config"] = now_config
                    answer["config_key"] = _config_key(now_config)
                if now_tuning is not None:
                    answer["tuning"] = dict(now_tuning)
                    answer["tuning_key"] = _config_key(now_tuning)
                return answer
            _changed.wait(min(left, TALLY_CHECK_S) if (tally or config) else left)


def _safe_tally(tally):
    if tally is None:
        return None
    try:
        return tally()
    except Exception:  # a status hiccup must not break the phone's request
        return None


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
            control = item.pop("control", None)
            if control and age <= CONTROL_TTL:
                command = _commands.get(cam)
                item["control"] = {k: control[k] for k in ("stream", "gimbal", "tracking", "gimbal_error")}
                item["control"]["actions"] = _actions(control)
                if command:
                    if "error" in command or command["expires"] <= time.monotonic():
                        item["control"]["error"] = command.get("error", "Phone did not confirm the command")
                    else:
                        item["control"]["pending"] = command["action"]
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
        _commands.clear()
        _tuning.clear()
