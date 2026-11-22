"""Router mode: the operator's own field router (laptop by cable, cameras on its Wi-Fi)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import network_manager as n
import settings_store

# Match-day laptop: venue Wi-Fi (internet), a cable to the field router, a phone on USB.
DEVICES = ("wlp0s20f3:wifi:connected:Clubhouse\n"
           "eno1:ethernet:connected:Wired connection 1\n"
           "enx0a1b:ethernet:connected:USB tether\n"
           "docker0:bridge:connected (externally):docker0\n")
IPS = {"wlp0s20f3": "192.168.188.129/24", "eno1": "192.168.8.10/24", "enx0a1b": "10.66.1.5/24"}


def fake_nmcli(never_default=False, route_dev="wlp0s20f3", eno_ip="192.168.8.10/24", gateway=True, calls=None):
    def run(cmd, timeout=10):
        if calls is not None:
            calls.append(cmd)
        if cmd[:5] == ["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
            return True, DEVICES
        if cmd[:3] == ["nmcli", "-g", "IP4.ADDRESS"]:
            ip = eno_ip if cmd[-1] == "eno1" else IPS.get(cmd[-1], "")
            return True, ip
        if cmd[:4] == ["nmcli", "-t", "-f", "GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,DHCP4"]:
            gw = "192.168.8.1" if gateway and not never_default else ""
            return True, (f"GENERAL.HWADDR:3C\\:52\\:82\\:AA\\:BB\\:CC\nIP4.ADDRESS[1]:{eno_ip}\nIP4.GATEWAY:{gw}\n"
                          "DHCP4.OPTION[1]:dhcp_lease_time = 43200\nDHCP4.OPTION[2]:routers = 192.168.8.1\n")
        if cmd[:6] == ["nmcli", "-t", "-f", "UUID,DEVICE", "connection", "show"]:
            return True, "1111-wifi:wlp0s20f3\n2222-wired:eno1\n3333-usb:enx0a1b"
        if cmd[:3] == ["nmcli", "-g", "ipv4.method,ipv4.never-default"]:
            return True, "auto\n" + ("yes" if never_default else "no")
        if cmd[:3] == ["ip", "-4", "-o"]:
            return True, f"8.8.8.8 via 1.2.3.4 dev {route_dev} src 1.2.3.5 uid 1000"
        if cmd[:2] == ["nmcli", "connection"] or cmd[:2] == ["nmcli", "device"] or cmd[:3] == ["nmcli", "--wait", "20"]:
            return True, ""
        return False, "unexpected " + " ".join(cmd)
    return run


class RouterDetectionTests(unittest.TestCase):
    def status(self, mode, **kw):
        with patch.object(n, "run_cmd", side_effect=fake_nmcli(**kw)), patch.object(n, "_owned_uuid", return_value=None), \
                patch.object(n, "_is_usb_device", side_effect=lambda dev: dev.startswith("enx")):
            return n.get_network_status(check_internet=False, mode=mode)

    def test_router_mode_uses_the_cable_not_the_venue_wifi(self):
        auto = self.status("auto")
        self.assertEqual((auto["camera_mode"], auto["camera_ip"]), ("lan", "192.168.188.129"))   # unchanged
        s = self.status("router")
        self.assertEqual((s["camera_mode"], s["camera_ip"]), ("router", "192.168.8.10"))
        r = s["router"]
        self.assertEqual((r["iface"], r["type"], r["gateway"], r["mac"], r["uuid"], r["method"], r["prefix"]),
                         ("eno1", "ethernet", "192.168.8.1", "3C:52:82:AA:BB:CC", "2222-wired", "auto", 24))
        self.assertEqual(r["internet"], "wlp0s20f3")           # internet still over the venue Wi-Fi
        self.assertEqual(self.status("router", route_dev="eno1")["router"]["internet"], "router")
        self.assertEqual(self.status("router", route_dev="enx0a1b")["router"]["internet"], "phone")

    def test_internet_via_phone_keeps_the_router_address_known(self):
        r = self.status("router", never_default=True, route_dev="enx0a1b")["router"]
        self.assertTrue(r["never_default"])
        self.assertEqual(r["gateway"], "192.168.8.1")          # from the DHCP lease
        self.assertEqual(r["internet"], "phone")

    def test_no_cable_falls_back_to_the_router_wifi_or_reports_nothing(self):
        s = self.status("router", eno_ip="")
        self.assertEqual((s["camera_mode"], s["router"]["type"]), ("router", "wifi"))
        with patch.object(n, "run_cmd", side_effect=lambda cmd, timeout=10: (True, "") if cmd[:3] != ["ip", "-4", "-o"] else (False, "")), \
                patch.object(n, "_owned_uuid", return_value=None):
            s = n.get_network_status(check_internet=False, mode="router")
        self.assertEqual((s["camera_mode"], s["camera_ip"], s["router"]), ("none", None, {"connected": False}))


class RouterAdviceTests(unittest.TestCase):
    def net(self, **router):
        base = {"connected": True, "iface": "eno1", "type": "ethernet", "name": "Wired", "ip": "192.168.8.10",
                "mac": "3C:52:82:AA:BB:CC", "method": "auto", "internet": "router"}
        return {"router": {**base, **router}, "internet_ok": True, "hotspot_active": False, "usb_iface": None}

    def levels(self, advice):
        return [a["level"] for a in advice]

    def test_good_setup_only_suggests_a_dhcp_reservation(self):
        advice = n.router_advice(self.net(), "192.168.8.10")
        self.assertEqual(self.levels(advice), ["info"])
        self.assertIn("3C:52:82:AA:BB:CC", advice[0]["text"])
        self.assertEqual(n.router_advice(self.net(method="manual"), "192.168.8.10"), [])

    def test_problems_are_reported_most_important_first(self):
        self.assertEqual(self.levels(n.router_advice({"router": {"connected": False}})), ["error"])
        net = self.net(type="wifi")
        net.update(hotspot_active=True, internet_ok=False, usb_iface="enx0a1b")
        advice = n.router_advice(net, "192.168.8.33")
        text = " ".join(a["text"] for a in advice)
        self.assertEqual(self.levels(advice), ["warning", "warning", "error", "error", "info"])
        self.assertIn("over Wi-Fi", text)
        self.assertIn("hotspot is still on", text)
        self.assertIn("changed from 192.168.8.33 to 192.168.8.10", text)
        self.assertIn("switch “Internet” to the phone", text)


class RouterInternetTests(unittest.TestCase):
    def test_switch_sets_never_default_and_applies_without_disconnect(self):
        net = {"router": {"connected": True, "uuid": "2222-wired", "iface": "eno1"}}
        calls = []
        with patch.object(n, "run_cmd", side_effect=fake_nmcli(calls=calls)):
            ok, message = n.set_router_internet(net, "phone")
        self.assertTrue(ok, message)
        self.assertEqual(calls[0], ["nmcli", "connection", "modify", "uuid", "2222-wired",
                                    "ipv4.never-default", "yes", "ipv6.never-default", "yes"])
        self.assertEqual(calls[1], ["nmcli", "device", "reapply", "eno1"])
        calls.clear()
        with patch.object(n, "run_cmd", side_effect=fake_nmcli(calls=calls)):
            self.assertTrue(n.set_router_internet(net, "router")[0])
        self.assertIn("no", calls[0])
        self.assertFalse(n.set_router_internet(net, "satellite")[0])
        self.assertFalse(n.set_router_internet({"router": {"connected": False}}, "phone")[0])

    def test_refused_change_is_reported(self):
        net = {"router": {"connected": True, "uuid": "2222-wired", "iface": "eno1"}}
        with patch.object(n, "run_cmd", return_value=(False, "Insufficient privileges")):
            ok, message = n.set_router_internet(net, "phone")
        self.assertFalse(ok)
        self.assertIn("Insufficient privileges", message)


class RouterApiTests(unittest.TestCase):
    def setUp(self):
        import app as a
        self.a = a
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(settings_store, "PATH", Path(self.tmp.name) / "settings.json"),
                        patch.object(a, "get_network_status", side_effect=self.fake_status),
                        patch.object(a, "get_firewall_status", return_value={})]
        for p in self.patches:
            p.start()
        settings_store.load()
        self.router_ip = "192.168.8.10"
        self.saved_login = (a.app.config.get("OPERATOR_USER"), a.app.config.get("OPERATOR_PASSWORD"))
        a.app.config.update(TESTING=True, OPERATOR_USER="operator", OPERATOR_PASSWORD="pw")
        self.client = a.app.test_client()
        self.auth = {"Authorization": "Basic " + __import__("base64").b64encode(b"operator:pw").decode()}

    def tearDown(self):
        self.a.app.config.update(OPERATOR_USER=self.saved_login[0], OPERATOR_PASSWORD=self.saved_login[1])
        for p in self.patches:
            p.stop()
        settings_store.load()
        self.tmp.cleanup()

    def fake_status(self, check_internet=True, mode="auto"):
        router = {"connected": True, "iface": "eno1", "type": "ethernet", "name": "Wired", "ip": self.router_ip,
                  "mac": "3C:52:82:AA:BB:CC", "method": "auto", "internet": "router", "uuid": "2222-wired"}
        if mode == "router":
            return {"network_mode": mode, "camera_mode": "router", "camera_ip": self.router_ip, "router": router,
                    "internet_ok": True, "hotspot_active": False, "usb_iface": None}
        return {"network_mode": mode, "camera_mode": "lan", "camera_ip": "192.168.188.129", "router": None,
                "internet_ok": True}

    def test_mode_switch_remembers_the_address_and_reports_a_change(self):
        r = self.client.post("/api/network/mode", json={"mode": "router"}, headers=self.auth).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(r["network"]["camera_ip"], "192.168.8.10")
        self.assertEqual(settings_store.get("router_ip"), "192.168.8.10")
        self.router_ip = "192.168.8.57"                         # the router handed out another address
        net = self.a._network_status(check_internet=False)
        self.assertTrue(any("changed from 192.168.8.10 to 192.168.8.57" in x["text"] for x in net["router_advice"]))
        r = self.client.post("/api/network/router-confirm-ip", json={}, headers=self.auth).get_json()
        self.assertTrue(r["ok"])
        self.assertEqual(settings_store.get("router_ip"), "192.168.8.57")
        self.assertFalse(any("changed" in x["text"] for x in self.a._network_status(False)["router_advice"]))
        self.assertEqual(self.client.post("/api/network/mode", json={"mode": "mesh"}, headers=self.auth).status_code, 400)
        self.client.post("/api/network/mode", json={"mode": "auto"}, headers=self.auth)
        self.assertNotIn("router_advice", self.a._network_status(False))


if __name__ == "__main__":
    unittest.main()
