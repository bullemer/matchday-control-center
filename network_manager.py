#!/usr/bin/env python3
"""
network_manager.py — Network & USB Tethering Module
=====================================================
Linux nmcli wrapper for USB tether detection, Wi-Fi Hotspot control,
internet connectivity checks, and firewall port inspection.

All subprocess calls degrade gracefully with clear log messages
if a binary or interface is missing.
"""

import logging
import re
import shutil
import subprocess

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Ports required for the broadcast stack
REQUIRED_TCP_PORTS = [5000, 1935, 8888, 8889]
REQUIRED_UDP_PORTS = [8189]

# Default hotspot gateway IP assigned by NetworkManager
DEFAULT_HOTSPOT_IP = "10.42.0.1"

# Binaries we depend on
_REQUIRED_BINS = {
    "nmcli": "NetworkManager CLI — install with: sudo apt install network-manager",
    "ip": "iproute2 — install with: sudo apt install iproute2",
    "ping": "iputils-ping — install with: sudo apt install iputils-ping",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _check_binary(name: str) -> bool:
    """Return True if *name* is found on PATH, log a warning otherwise."""
    if shutil.which(name):
        return True
    hint = _REQUIRED_BINS.get(name, "")
    logger.warning("Binary '%s' not found on PATH. %s", name, hint)
    return False


def run_cmd(cmd: list[str], timeout: int = 10) -> tuple[bool, str]:
    """
    Run *cmd* and return ``(success, output)``.

    * On success the full stripped stdout is returned.
    * On failure (non-zero exit, timeout, missing binary) the error output
      or exception message is returned and a warning is logged.
    """
    binary = cmd[0] if cmd else None
    if binary and not _check_binary(binary):
        msg = f"Cannot execute command — '{binary}' is not installed."
        return False, msg

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        output = result.stdout.strip()
        if result.returncode == 0:
            return True, output
        logger.warning("Command %s exited with code %d: %s", cmd, result.returncode, output)
        return False, output

    except subprocess.TimeoutExpired:
        msg = f"Command {cmd} timed out after {timeout}s."
        logger.warning(msg)
        return False, msg

    except FileNotFoundError:
        msg = f"Binary '{binary}' not found."
        logger.warning(msg)
        return False, msg

    except OSError as exc:
        msg = f"OS error running {cmd}: {exc}"
        logger.error(msg)
        return False, msg


# ---------------------------------------------------------------------------
# Network Status
# ---------------------------------------------------------------------------

def get_network_status() -> dict:
    """
    Return a dict describing the current network state::

        {
            "wifi_ip":        str,   # e.g. "10.42.0.1" or "N/A"
            "usb_iface":      str | None,
            "usb_status":     str,   # e.g. "usb0 (connected)" or "Disconnected"
            "hotspot_active":  bool,
            "internet_ok":     bool,
        }

    Detection is best-effort — missing tools produce warnings, not crashes.
    """
    info: dict = {
        "wifi_ip": "N/A",
        "usb_iface": None,
        "usb_status": "Disconnected",
        "hotspot_active": False,
        "internet_ok": False,
    }

    # --- nmcli device list --------------------------------------------------
    ok, dev_out = run_cmd(
        ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]
    )
    if ok:
        for line in dev_out.splitlines():
            # nmcli -t uses ':' as the field separator.
            # Connection names may contain colons so we limit the split.
            parts = line.split(":", 3)
            if len(parts) < 4:
                continue
            dev, dtype, state, conn = parts

            # USB tether detection: usb0, enx* (usbnet CDC), or
            # any ethernet device whose connection name contains "iphone".
            is_usb_tether = dev.startswith(("usb", "enx"))
            is_iphone = dtype == "ethernet" and "iphone" in conn.lower()
            if is_usb_tether or is_iphone:
                info["usb_iface"] = dev
                info["usb_status"] = f"{dev} ({state})"
                logger.info("Detected USB tether interface: %s [%s]", dev, state)

            # Hotspot detection via connection name
            if dtype == "wifi" and "hotspot" in conn.lower() and state == "connected":
                info["hotspot_active"] = True
                logger.info("Wi-Fi hotspot is active (connection: %s).", conn)
    else:
        logger.warning("Could not query nmcli devices: %s", dev_out)

    # --- IP address detection -----------------------------------------------
    ok_ip, ip_out = run_cmd(["ip", "-4", "-o", "addr", "show"])
    if ok_ip:
        for line in ip_out.splitlines():
            tokens = line.split()
            # tokens layout: <index> <device> inet <ip/prefix> ...
            if len(tokens) >= 4 and tokens[1].startswith("wl"):
                addr = tokens[3].split("/")[0]
                info["wifi_ip"] = addr
                # If the hotspot IP is present, the hotspot is active even
                # if the connection name didn't contain "hotspot".
                if addr == DEFAULT_HOTSPOT_IP:
                    info["hotspot_active"] = True
                logger.debug("Wi-Fi interface %s has IP %s.", tokens[1], addr)
    else:
        logger.warning("Could not query IP addresses: %s", ip_out)

    # --- Internet connectivity (ping 8.8.8.8, 1 s timeout) -----------------
    ok_ping, _ = run_cmd(["ping", "-c", "1", "-W", "1", "8.8.8.8"], timeout=5)
    info["internet_ok"] = ok_ping
    if ok_ping:
        logger.info("Internet connectivity check passed (8.8.8.8 reachable).")
    else:
        logger.warning("Internet connectivity check failed (8.8.8.8 unreachable).")

    return info


# ---------------------------------------------------------------------------
# USB Tether Connection
# ---------------------------------------------------------------------------

def connect_usb(iface: str) -> tuple[bool, str]:
    """
    Bring up USB tether interface *iface* via ``nmcli device connect``.

    Returns ``(success, message)``.
    """
    if not iface:
        msg = "No USB interface name provided."
        logger.error(msg)
        return False, msg

    # Basic sanitisation — interface names are alphanumeric / dashes
    if not re.match(r"^[a-zA-Z0-9_\-]+$", iface):
        msg = f"Invalid interface name: {iface!r}"
        logger.error(msg)
        return False, msg

    logger.info("Connecting USB tether interface: %s", iface)
    ok, out = run_cmd(["nmcli", "device", "connect", iface])
    if ok:
        logger.info("USB tether %s connected successfully.", iface)
        return True, f"Interface {iface} connected."
    else:
        logger.error("Failed to connect USB tether %s: %s", iface, out)
        return False, f"Failed to connect {iface}: {out}"


# ---------------------------------------------------------------------------
# Wi-Fi Hotspot
# ---------------------------------------------------------------------------

def start_hotspot(ssid: str, password: str) -> tuple[bool, str]:
    """
    Start a Wi-Fi hotspot via ``nmcli device wifi hotspot``.

    * *ssid* — network name (1-32 chars).
    * *password* — WPA2 passphrase (8-63 chars).

    Returns ``(success, message)``.
    """
    if not ssid or len(ssid) > 32:
        msg = "SSID must be between 1 and 32 characters."
        logger.error(msg)
        return False, msg

    if not password or len(password) < 8 or len(password) > 63:
        msg = "Password must be between 8 and 63 characters."
        logger.error(msg)
        return False, msg

    logger.info("Starting Wi-Fi hotspot SSID=%s", ssid)
    ok, out = run_cmd(
        ["nmcli", "device", "wifi", "hotspot", "ssid", ssid, "password", password],
        timeout=30,
    )
    if ok:
        logger.info("Hotspot '%s' started successfully.", ssid)
        return True, f"Hotspot '{ssid}' started."
    else:
        logger.error("Failed to start hotspot '%s': %s", ssid, out)
        return False, f"Failed to start hotspot: {out}"


def stop_hotspot() -> tuple[bool, str]:
    """
    Stop the active Wi-Fi hotspot by bringing down the Hotspot connection.

    Returns ``(success, message)``.
    """
    logger.info("Stopping Wi-Fi hotspot.")

    # Identify the hotspot connection name first
    ok, conn_out = run_cmd(
        ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"]
    )
    hotspot_name = None
    if ok:
        for line in conn_out.splitlines():
            parts = line.split(":", 2)
            if len(parts) >= 2 and "hotspot" in parts[0].lower():
                hotspot_name = parts[0]
                break

    if hotspot_name:
        ok_down, out = run_cmd(["nmcli", "connection", "down", hotspot_name])
        if ok_down:
            logger.info("Hotspot '%s' stopped.", hotspot_name)
            return True, f"Hotspot '{hotspot_name}' stopped."
        else:
            logger.error("Failed to stop hotspot '%s': %s", hotspot_name, out)
            return False, f"Failed to stop hotspot: {out}"

    # Fallback: try to disconnect the wifi device directly
    ok_dev, dev_out = run_cmd(
        ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE", "device"]
    )
    if ok_dev:
        for line in dev_out.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[1] == "wifi" and parts[2] == "connected":
                wifi_dev = parts[0]
                ok_disc, disc_out = run_cmd(["nmcli", "device", "disconnect", wifi_dev])
                if ok_disc:
                    logger.info("Disconnected Wi-Fi device %s (hotspot stopped).", wifi_dev)
                    return True, f"Disconnected {wifi_dev}."
                else:
                    logger.error("Failed to disconnect %s: %s", wifi_dev, disc_out)
                    return False, f"Failed to disconnect {wifi_dev}: {disc_out}"

    msg = "No active hotspot connection found to stop."
    logger.warning(msg)
    return False, msg


# ---------------------------------------------------------------------------
# Firewall / Port Check
# ---------------------------------------------------------------------------

def get_firewall_status() -> dict:
    """
    Check whether the required broadcast ports are open.

    Returns::

        {
            "ufw_active": bool | None,   # None if ufw is not installed
            "ports": {
                "5000/tcp":  "open" | "closed" | "unknown",
                "1935/tcp":  ...,
                "8888/tcp":  ...,
                "8889/tcp":  ...,
                "8189/udp":  ...,
            },
            "all_open": bool,
        }

    Uses ``ufw status`` when available, falls back to ``ss -tulnp``.
    """
    result: dict = {
        "ufw_active": None,
        "ports": {},
        "all_open": False,
    }

    # --- Try ufw first ------------------------------------------------------
    ufw_available = _check_binary("ufw")
    ufw_rules: str = ""
    if ufw_available:
        ok_ufw, ufw_out = run_cmd(["sudo", "ufw", "status"], timeout=5)
        if ok_ufw:
            if "inactive" in ufw_out.lower():
                result["ufw_active"] = False
                logger.info("UFW firewall is inactive — all ports presumed open.")
            else:
                result["ufw_active"] = True
                ufw_rules = ufw_out
                logger.info("UFW firewall is active.")
        else:
            logger.warning(
                "Could not query ufw status (may need sudo): %s", ufw_out
            )

    # --- Build port status using ss -----------------------------------------
    listening_ports: set[str] = set()  # e.g. {"tcp:5000", "udp:8189"}
    ok_ss, ss_out = run_cmd(["ss", "-tulnp"])
    if ok_ss:
        for line in ss_out.splitlines():
            # Match lines like  "tcp  LISTEN ... *:5000 ..."
            match = re.search(r"(tcp|udp)\s.*[\s:*](\d+)\s", line.lower())
            if match:
                proto, port = match.group(1), match.group(2)
                listening_ports.add(f"{proto}:{port}")
    else:
        # Fallback to netstat if ss is unavailable
        ok_ns, ns_out = run_cmd(["netstat", "-tulnp"])
        if ok_ns:
            for line in ns_out.splitlines():
                match = re.search(r"(tcp|udp)\s.*[\s:](\d+)\s", line.lower())
                if match:
                    proto, port = match.group(1), match.group(2)
                    listening_ports.add(f"{proto}:{port}")
        else:
            logger.warning(
                "Neither 'ss' nor 'netstat' available — cannot inspect listening ports."
            )

    # Evaluate each required port
    all_open = True
    for port in REQUIRED_TCP_PORTS:
        key = f"{port}/tcp"
        if result["ufw_active"] is False:
            # Firewall inactive → not blocked
            status = "open" if f"tcp:{port}" in listening_ports else "not listening"
        elif f"tcp:{port}" in listening_ports:
            # Port is listening; check ufw if active
            if result["ufw_active"] and ufw_rules:
                if str(port) in ufw_rules:
                    status = "open"
                else:
                    status = "listening (may be blocked by ufw)"
                    all_open = False
            else:
                status = "open"
        else:
            status = "closed"
            all_open = False
        result["ports"][key] = status

    for port in REQUIRED_UDP_PORTS:
        key = f"{port}/udp"
        if result["ufw_active"] is False:
            status = "open" if f"udp:{port}" in listening_ports else "not listening"
        elif f"udp:{port}" in listening_ports:
            if result["ufw_active"] and ufw_rules:
                if str(port) in ufw_rules:
                    status = "open"
                else:
                    status = "listening (may be blocked by ufw)"
                    all_open = False
            else:
                status = "open"
        else:
            status = "closed"
            all_open = False
        result["ports"][key] = status

    result["all_open"] = all_open
    logger.info("Firewall port check complete: all_open=%s", all_open)
    return result
