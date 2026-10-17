"""Network controls scoped to the application's own NetworkManager profile."""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time

logger = logging.getLogger(__name__)
REQUIRED_TCP_PORTS = [5000, 1935, 8888, 8889]
REQUIRED_UDP_PORTS = [8189]
DEFAULT_HOTSPOT_IP = "10.42.0.1"
PROFILE = "Matchday-Control-Hotspot"
PROFILE_FILE = Path(__file__).resolve().parent / "data/hotspot-uuid"
_network_lock = threading.Lock()
# Left blank, NetworkManager may advertise mixed WPA1/TKIP, which current phones reject or
# hide. Pin plain WPA2-AES on 2.4 GHz (5 GHz AP channels are often not allowed on laptops).
HOTSPOT_RADIO = ["802-11-wireless.band", "bg", "wifi-sec.key-mgmt", "wpa-psk",
                 "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
                 "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable"]


def run_cmd(cmd, timeout=10):
    if not cmd or not shutil.which(cmd[0]):
        return False, "Required tool is unavailable"
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                env={**os.environ, "LC_ALL": "C"})
        output = (result.stdout if result.returncode == 0 else result.stderr).strip()
        # Never echo command arguments (which can include a Wi-Fi passphrase).
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"{cmd[0]} timed out after {timeout}s"
    except OSError:
        return False, f"Could not run {cmd[0]}"


def _field(uuid, field):
    ok, out = run_cmd(["nmcli", "-g", field, "connection", "show", "uuid", uuid])
    return out if ok else ""


def _owned_uuid():
    if not PROFILE_FILE.exists():
        return None
    uuid = PROFILE_FILE.read_text().strip()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        return None
    if _field(uuid, "connection.id") != PROFILE or _field(uuid, "802-11-wireless.mode") != "ap":
        return None
    return uuid


def _nmcli_fields(line):
    """Split one `nmcli -t` line; values escape ':' and '\\' with a backslash."""
    return [part.replace("\\:", ":").replace("\\\\", "\\")
            for part in re.split(r"(?<!\\):", line)]


def _is_usb_device(dev):
    return "/usb" in os.path.realpath(f"/sys/class/net/{dev}/device")


def _device_ip(dev):
    ok, ip = run_cmd(["nmcli", "-g", "IP4.ADDRESS", "device", "show", dev])
    return ip.splitlines()[0].split("/")[0] if ok and ip else None


def get_network_status():
    """Report the internet uplink and the network camera phones should join.

    Cameras use the app's hotspot when it is active; otherwise they join the same
    Wi-Fi/LAN the laptop is connected to (a USB tether is never a camera network)."""
    info = dict(wifi_ip="N/A", usb_iface=None, usb_status="Disconnected", usb_state=None,
                hotspot_active=False, hotspot_ssid=None, hotspot_iface=None, internet_ok=False,
                lan_iface=None, lan_type=None, lan_name=None, lan_ip=None,
                camera_mode="none", camera_ip=None, uplink=None)
    ok, raw = run_cmd(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"])
    candidates, lans = [], []
    if ok:
        for line in raw.splitlines():
            parts = _nmcli_fields(line)
            if len(parts) != 4:
                continue
            dev, kind, state, connection = parts
            # Identify USB-backed ethernet by sysfs, not by an enx name guess.
            usb = kind == "ethernet" and _is_usb_device(dev)
            if usb:
                candidates.append((state == "connected", dev, state))
            elif kind in ("wifi", "ethernet") and state == "connected":
                lans.append((kind != "wifi", dev, kind, connection))  # prefer Wi-Fi
        if candidates:
            _, dev, state = sorted(candidates, reverse=True)[0]
            info.update(usb_iface=dev, usb_status=f"{dev} ({state})", usb_state=state)
    uuid = _owned_uuid()
    hotspot_dev = None
    if uuid:
        ok, active = run_cmd(["nmcli", "-t", "-f", "UUID,DEVICE", "connection", "show", "--active"])
        if ok:
            for line in active.splitlines():
                key, _, dev = line.partition(":")
                if key == uuid:
                    hotspot_dev = dev
                    info.update(hotspot_active=True, hotspot_iface=dev)
                    info["hotspot_ssid"] = _field(uuid, "802-11-wireless.ssid") or None
                    info["wifi_ip"] = _device_ip(dev) or "N/A"
    for _, dev, kind, connection in sorted(lans):
        if dev == hotspot_dev:
            continue
        ip = _device_ip(dev)
        if ip:
            info.update(lan_iface=dev, lan_type=kind, lan_name=connection, lan_ip=ip)
            break
    if info["hotspot_active"] and info["wifi_ip"] != "N/A":
        info.update(camera_mode="hotspot", camera_ip=info["wifi_ip"])
    elif info["lan_ip"]:
        info.update(camera_mode="lan", camera_ip=info["lan_ip"])
    ok, route = run_cmd(["ip", "-4", "-o", "route", "get", "8.8.8.8"], timeout=2)
    if ok:
        match = re.search(r"\bdev (\S+)", route)
        dev = match.group(1) if match else None
        if dev and dev == info["usb_iface"]:
            info["uplink"] = "USB tether"
        elif dev and dev == info["lan_iface"]:
            info["uplink"] = info["lan_name"] or dev
        else:
            info["uplink"] = dev
    # A route-independent ping is only general connectivity, not proof of a tether uplink.
    ok, _ = run_cmd(["ping", "-c", "1", "-W", "1", "8.8.8.8"], timeout=2)
    info["internet_ok"] = ok
    return info


def connect_usb(iface):
    if not isinstance(iface, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", iface):
        return False, "Invalid interface"
    if not _is_usb_device(iface):
        return False, "Interface is not a USB network device"
    with _network_lock:
        ok, out = run_cmd(["nmcli", "--wait", "25", "device", "connect", iface], timeout=30)
        return ok, f"Connected {iface}" if ok else out


def _hotspot_active(uuid):
    ok, active = run_cmd(["nmcli", "-t", "-f", "UUID", "connection", "show", "--active"])
    return ok and uuid in active.splitlines()


def get_hotspot_config():
    """SSID and password of the app's hotspot. NetworkManager already stores them, so the
    dashboard can prefill the form without the app keeping its own copy of the secret."""
    uuid = _owned_uuid()
    if not uuid:
        return {"ssid": "MatchdayLive", "password": "", "saved": False}
    ok, psk = run_cmd(["nmcli", "-s", "-g", "802-11-wireless-security.psk", "connection", "show", "uuid", uuid])
    return {"ssid": _field(uuid, "802-11-wireless.ssid") or "MatchdayLive",
            "password": psk if ok else "", "saved": True}


def start_hotspot(ssid, password=""):
    """Start the hotspot. An empty password keeps the saved one. If it is already running
    with these settings nothing is restarted, so connected phones are not dropped."""
    if not isinstance(ssid, str) or not 1 <= len(ssid.encode()) <= 32:
        return False, "SSID must be 1–32 bytes"
    if not isinstance(password, str) or (password and not 8 <= len(password) <= 63):
        return False, "Passphrase must be 8–63 characters"
    with _network_lock:
        uuid = _owned_uuid()
        if uuid:
            saved = get_hotspot_config()
            password = password or saved["password"]
            if not password:
                return False, "Enter a hotspot password (8–63 characters)"
            unchanged = saved["ssid"] == ssid and saved["password"] == password
            if unchanged and _hotspot_active(uuid):
                return True, "Hotspot is already running"
            ok, out = run_cmd(["nmcli", "connection", "modify", "uuid", uuid,
                               "802-11-wireless.ssid", ssid, "wifi-sec.psk", password, *HOTSPOT_RADIO])
            if ok:
                ok, out = run_cmd(["nmcli", "connection", "up", "uuid", uuid], timeout=30)
            return ok, "Hotspot started" if ok else "Could not activate hotspot; check NetworkManager permissions"
        if not password:
            return False, "Enter a hotspot password (8–63 characters)"
        ok, devices = run_cmd(["nmcli", "-t", "-f", "DEVICE,TYPE", "device"])
        if not ok:
            return False, devices
        device = None
        for line in devices.splitlines():
            dev, _, kind = line.partition(":")
            if kind == "wifi":
                capable, ap = run_cmd(["nmcli", "-g", "WIFI-PROPERTIES.AP", "device", "show", dev])
                if capable and ap.strip() == "yes":
                    device = dev
                    break
        if not device:
            return False, "No Wi-Fi adapter with access-point support found"
        # Create an identifiable profile before activation; persist its UUID immediately.
        import uuid as uuid_module
        uuid = str(uuid_module.uuid4())
        ok, out = run_cmd(["nmcli", "connection", "add", "type", "wifi", "ifname", device,
            "con-name", PROFILE, "connection.uuid", uuid, "ssid", ssid,
            "802-11-wireless.mode", "ap", "ipv4.method", "shared", "ipv6.method", "disabled",
            *HOTSPOT_RADIO, "wifi-sec.psk", password, "connection.autoconnect", "no"])
        if not ok:
            return False, "Cannot create hotspot; check NetworkManager permissions"
        PROFILE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        PROFILE_FILE.write_text(uuid)
        ok, out = run_cmd(["nmcli", "connection", "up", "uuid", uuid], timeout=30)
        return ok, "Hotspot started" if ok else "Hotspot activation failed; inspect NetworkManager logs"


def stop_hotspot():
    with _network_lock:
        uuid = _owned_uuid()
        if not uuid:
            return False, "No application-owned hotspot found"
        ok, out = run_cmd(["nmcli", "connection", "down", "uuid", uuid], timeout=30)
        return ok, "Hotspot stopped" if ok else out


UFW_CONF = Path("/etc/ufw/ufw.conf")
UFW_DEFAULTS = Path("/etc/default/ufw")
_reached = {}  # peer IP -> last time it had a connection to the dashboard, RTMP or WebRTC


def _conf_value(path, key):
    try:
        for line in path.read_text().splitlines():
            name, _, value = line.partition("=")
            if name.strip() == key:
                return value.strip().strip('"').lower()
    except OSError:
        return None
    return None


def _peer_ip(address):
    host = address.rsplit(":", 1)[0].strip("[]")
    return host[7:] if host.startswith("::ffff:") else host


def _same_subnet(a, b):
    return a.count(".") == 3 and a.split(".")[:3] == b.split(".")[:3]


def _firewall_commands(network, tool):
    hotspot = network.get("camera_mode") == "hotspot"
    iface = network.get("hotspot_iface") if hotspot else network.get("lan_iface")
    if tool == "firewalld":
        return ["sudo firewall-cmd --permanent --add-port={5000,1935,8888,8889}/tcp --add-port=8189/udp",
                "sudo firewall-cmd --reload"]
    if not iface:
        return []
    if hotspot:
        # Hotspot phones also need DHCP/DNS from the laptop, and forwarding for internet.
        return [f"sudo ufw allow in on {iface}", f"sudo ufw route allow in on {iface}"]
    return [f"sudo ufw allow in on {iface} to any port 5000,1935,8888,8889 proto tcp",
            f"sudo ufw allow in on {iface} to any port 8189 proto udp"]


def get_firewall_status(network=None):
    """Whether camera phones can reach the app. Rules need root to read, so the strongest
    evidence is a phone on the camera network actually connecting; configuration that is
    readable without root (ufw enabled + default policy, firewalld) is the fallback."""
    network = network or {}
    ok, out = run_cmd(["ss", "-H", "-lntu"])
    listening = set()
    if ok:
        for line in out.splitlines():
            fields = line.split()
            if len(fields) >= 5:
                port = fields[4].rsplit(":", 1)[-1]
                if port.isdigit():
                    listening.add(f"{port}/{fields[0]}")
    ports = {}
    for proto, numbers in (("tcp", REQUIRED_TCP_PORTS), ("udp", REQUIRED_UDP_PORTS)):
        for port in numbers:
            key = f"{port}/{proto}"
            ports[key] = ("listening; reachability unverified" if key in listening else
                          "not listening" if ok else "unknown")
    missing = [key for key, value in ports.items() if value == "not listening"]

    now = time.monotonic()
    ok_est, established = run_cmd(["ss", "-H", "-tn", "state", "established",
                                   "( sport = :5000 or sport = :1935 or sport = :8889 )"])
    if ok_est:
        for line in established.splitlines():
            fields = line.split()
            if len(fields) >= 4:
                ip = _peer_ip(fields[-1])
                if not ip.startswith("127.") and ip != "::1":
                    _reached[ip] = now
    camera_ip = network.get("camera_ip")
    reached = sorted(ip for ip, seen in _reached.items()
                     if now - seen < 3600 and camera_ip and _same_subnet(ip, camera_ip))

    ufw_enabled = _conf_value(UFW_CONF, "ENABLED") == "yes"
    ufw_policy = _conf_value(UFW_DEFAULTS, "DEFAULT_INPUT_POLICY") if ufw_enabled else None
    fwd_ok, fwd = run_cmd(["systemctl", "is-active", "firewalld"], timeout=3)
    firewalld = fwd_ok and fwd.strip() == "active"

    commands = []
    if missing:
        level, label = "error", "Not listening"
        summary = f"The server is not listening on {', '.join(missing)}; check that MediaMTX is running."
    elif reached:
        level, label = "ok", "Phones reach server"
        summary = f"Verified: {len(reached)} device(s) on the camera network connected ({', '.join(reached[:4])})."
    elif ufw_enabled and ufw_policy in ("drop", "reject"):
        level, label = "warning", "ufw firewall on"
        summary = ("The ufw firewall blocks incoming connections by default. If phones cannot join or "
                   "connect, run these once (skip if you already allowed them):")
        commands = _firewall_commands(network, "ufw")
    elif firewalld:
        level, label = "warning", "firewalld on"
        summary = "firewalld is running. If phones cannot connect, open the app's ports once:"
        commands = _firewall_commands(network, "firewalld")
    elif not ufw_enabled and not firewalld and _conf_value(UFW_CONF, "ENABLED") is not None:
        level, label = "unknown", "Reachability unverified"
        summary = "UFW and firewalld are not enabled. Other firewall rules or Wi-Fi isolation may still block phones; test from a camera phone."
    else:
        level, label = "unknown", "Not checked"
        summary = "Could not determine the firewall state. Open the dashboard on a phone to test."
    return {"ufw_active": ufw_enabled, "firewalld_active": firewalld, "ports": ports,
            "all_open": None, "status": level, "label": label, "summary": summary,
            "commands": commands, "reached": reached,
            "details": "; ".join(f"{key}: {value}" for key, value in ports.items())}


def _is_usb_audio_card(card_num: int, card_name: str) -> bool:
    """
    Determine whether ALSA card *card_num* is a USB audio device.

    Checks for the presence of ``/proc/asound/card<N>/usbbus`` first
    (authoritative), then falls back to looking for 'USB' in the card name.
    """
    usbbus_path = f"/proc/asound/card{card_num}/usbbus"
    if os.path.exists(usbbus_path):
        logger.debug("Card %d identified as USB via %s.", card_num, usbbus_path)
        return True

    if "usb" in card_name.lower():
        logger.debug("Card %d identified as USB via name: %s", card_num, card_name)
        return True

    return False


def _parse_arecord_devices() -> list[dict]:
    """
    Parse ``arecord -l`` output and return a list of capture device dicts::

        {
            "card_num":   int,
            "device_num": int,
            "card_name":  str,
            "device_name": str,
            "alsa_device": str,   # e.g. "hw:1,0"
            "is_usb":     bool,
        }

    Returns an empty list if ``arecord`` is unavailable or produces no output.
    """
    ok, output = run_cmd(["arecord", "-l"], timeout=5)
    if not ok:
        logger.warning("arecord unavailable or failed — ALSA device list skipped.")
        return []

    devices: list[dict] = []
    # Example line:
    #   card 1: Device [USB Condenser Mic], device 0: USB Audio [USB Audio]
    pattern = re.compile(
        r"^card\s+(\d+):\s+(\S+)\s+\[(.+?)\],\s+device\s+(\d+):\s+(.+?)\s+\[(.+?)\]",
    )
    for line in output.splitlines():
        m = pattern.match(line)
        if not m:
            continue
        card_num = int(m.group(1))
        card_name = m.group(3)
        device_num = int(m.group(4))
        device_name = m.group(6)
        alsa_hw = f"hw:{card_num},{device_num}"
        is_usb = _is_usb_audio_card(card_num, card_name)

        devices.append({
            "card_num": card_num,
            "device_num": device_num,
            "card_name": card_name,
            "device_name": device_name,
            "alsa_device": alsa_hw,
            "is_usb": is_usb,
        })
        logger.debug(
            "ALSA capture device: %s — %s (USB=%s)", alsa_hw, card_name, is_usb,
        )

    return devices


def _parse_pulse_sources():
    ok, output = run_cmd(["pactl", "--format=json", "list", "sources"], timeout=5)
    if not ok:
        return []
    try:
        return json.loads(output)
    except ValueError:
        return []


def get_usb_audio_devices() -> list[dict]:
    """
    Detect USB audio capture devices and return a list of dicts::

        [
            {
                "id":           str,   # unique id (ALSA hw string or pulse name)
                "name":         str,   # human-readable label
                "type":         str,   # "usb" or "builtin"
                "alsa_device":  str,   # e.g. "hw:1,0"
                "pulse_source": str | None,
            },
            ...
        ]

    USB devices are listed first, followed by a single built-in mic fallback
    (if one exists).  The function degrades gracefully when ``arecord`` or
    ``pactl`` are not installed.
    """
    alsa_devices = _parse_arecord_devices()
    pulse_sources = _parse_pulse_sources()

    # Build a quick lookup: ALSA card+device → PulseAudio source name.
    # PipeWire / PulseAudio source names often contain the ALSA card number
    # (e.g. "alsa_input.usb-…" or "alsa_input.pci-…").  We match by
    # checking whether "card<N>" or "hw_<card>_<dev>" appears in the name.
    def _find_pulse_source(card_num: int, device_num: int) -> str | None:
        for src in pulse_sources:
            properties = src.get("properties", {})
            if (str(properties.get("alsa.card")) == str(card_num)
                    and str(properties.get("alsa.device")) == str(device_num)
                    and not src.get("name", "").endswith(".monitor")):
                return src["name"]
        return None

    usb_devices: list[dict] = []
    builtin_devices: list[dict] = []

    for dev in alsa_devices:
        pulse_name = _find_pulse_source(dev["card_num"], dev["device_num"])
        entry = {
            "id": pulse_name if pulse_name else dev["alsa_device"],
            "name": dev["card_name"],
            "type": "usb" if dev["is_usb"] else "builtin",
            "alsa_device": dev["alsa_device"],
            "pulse_source": pulse_name,
        }
        if dev["is_usb"]:
            usb_devices.append(entry)
        else:
            builtin_devices.append(entry)

    # If no ALSA devices were found at all, try to offer a PulseAudio-only
    # fallback for any source whose name hints at a USB device.
    if not alsa_devices and pulse_sources:
        logger.info("No ALSA devices parsed — falling back to PulseAudio sources.")
        for src in pulse_sources:
            name_lower = src["name"].lower()
            # Skip monitors (speaker loopbacks)
            if ".monitor" in name_lower:
                continue
            is_usb = "usb" in name_lower
            entry = {
                "id": src["name"],
                "name": src["name"],
                "type": "usb" if is_usb else "builtin",
                "alsa_device": "",
                "pulse_source": src["name"],
            }
            if is_usb:
                usb_devices.append(entry)
            else:
                builtin_devices.append(entry)

    # Return USB devices first; append at most one built-in fallback.
    result = list(usb_devices)
    if builtin_devices:
        result.append(builtin_devices[0])
        logger.debug(
            "Including built-in mic fallback: %s", builtin_devices[0]["name"],
        )

    logger.info(
        "USB audio detection complete: %d USB device(s), %d built-in fallback(s).",
        len(usb_devices),
        min(len(builtin_devices), 1),
    )
    return result
