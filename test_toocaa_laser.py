#!/usr/bin/env python3
"""End-to-end tests against the offline simulator. Run: python3 -m unittest -v"""

import base64
import hashlib
import json
import socket
import threading
import time
import unittest

import bridge
import grbl_sim
import lbdev_audit
import nova


class SimCase(unittest.TestCase):
    def setUp(self):
        self.srv = grbl_sim.SimServer(port=0, machine=grbl_sim.Machine(move_time=0.0)).start()
        self.m = self.srv.machine

    def tearDown(self):
        for n in getattr(self, "_novas", []):
            n.close()
        self.srv.close()

    def connect(self, motion=False, laser=False):
        n = nova.Nova(nova.open_transport("tcp:127.0.0.1:%d" % self.srv.port),
                      nova.Permissions(motion, laser))
        self._novas = getattr(self, "_novas", []) + [n]
        return n


class TestReadOnly(SimCase):
    def test_info_identifies_grbl_esp32(self):
        info = self.connect().info()
        self.assertIn("[VER:1.3a.202506201830:]", info["build"])
        self.assertIn("[MSG:Using machine:ESP32_V4]", info["build"])
        self.assertIn("$130=300.000", info["settings"])  # real NOVA $$
        self.assertTrue(info["status"].startswith("<Idle"))

    def test_status(self):
        st = self.connect().status()
        self.assertEqual(st.state, "Idle")
        self.assertEqual(st.mpos, (0.0, 210.0, 0.0, 0.0))  # X,Y,Z,A – NOVA reports Y mirrored

    def test_motion_refused_without_flag(self):
        n = self.connect()
        for cmd in ("G0 X10", "$J=G91 X1 F100", "$H", "$X"):
            with self.assertRaises(nova.NotAllowed):
                n.command(cmd)
        self.assertEqual(self.m.history, [])

    def test_laser_refused_without_flag(self):
        n = self.connect(motion=True)
        with self.assertRaises(nova.NotAllowed):
            n.command("M3 S500")
        with self.assertRaises(nova.NotAllowed):
            n.stream(["G1 X10 F1000", "M4 S300", "G1 X20"])
        self.assertEqual(self.m.history, [])

    def test_settings_writes_never_sent(self):
        n = self.connect(motion=True, laser=True)
        for cmd in ("$32=0", "$RST=*", "$NVX", "[ESP401]P=1", "$Sta/SSID=x", "$SLP"):
            with self.assertRaises(nova.NotAllowed):
                n.command(cmd)
        with self.assertRaises(nova.NotAllowed):
            n.stream(["G0 X1", "$130=500"])
        self.assertEqual(self.m.history, [])


class TestMotion(SimCase):
    def test_jog(self):
        n = self.connect(motion=True)
        n.jog(10, 5, 1500)
        st = n.wait_idle(5)
        self.assertEqual(n.work_pos(st)[:2], (10.0, 5.0))
        self.assertEqual(st.mpos[:2], (10.0, 205.0))  # as the NOVA reports it
        self.assertEqual(self.m.burn, [])

    def test_jog_outside_bed_is_rejected_by_controller(self):
        self.m.nova = False  # stock Grbl: $J= with soft limits
        n = self.connect(motion=True)
        with self.assertRaises(nova.GrblError) as cm:
            n.jog(-5, 0)
        self.assertEqual(cm.exception.code, 15)

    def test_nova_jog_falls_back_to_gcode_move(self):
        n = self.connect(motion=True)       # NOVA: $J= -> error:3
        n.jog(10, 5, 1500)
        self.assertEqual(n.work_pos(n.wait_idle(5))[:2], (10.0, 5.0))
        self.assertTrue(n.no_jog)
        self.assertIn("G91 G21 G1 X10.000 Y5.000 F1500 S0", self.m.history)
        self.assertEqual(self.m.burn, [])
        n.jog(-3, 0)                         # back in G90 afterwards
        self.assertEqual(n.work_pos(n.wait_idle(5))[:2], (7.0, 5.0))
        self.assertTrue(self.m.absolute)

    def test_nova_jog_outside_bed_refused_by_client(self):
        n = self.connect(motion=True)
        with self.assertRaises(nova.NotAllowed):
            n.jog(-5, 0)
        with self.assertRaises(nova.NotAllowed):  # from home, Y- would go past the rear edge
            n.jog(0, -5)
        self.assertEqual(self.m.state, "Idle")

    def test_jog_step_limit(self):
        with self.assertRaises(nova.NotAllowed):
            self.connect(motion=True).jog(250, 0)


class TestNovaFeatures(SimCase):
    def test_focus_is_motion_gated(self):
        with self.assertRaises(nova.NotAllowed):
            self.connect().command("G38.2")
        out = self.connect(motion=True).command("G38.2")
        self.assertTrue(out[0].startswith("[PRB:"))

    def test_air_assist(self):
        n = self.connect(motion=True)
        n.command("M8")
        self.assertTrue(n.status().air_assist)
        n.command("M9")
        self.assertFalse(n.status().air_assist)


class TestWsTransport(unittest.TestCase):
    """WsTransport against a tiny WebSocket server that answers like the
    NOVA's ws://IP:8002/ws endpoint (one text message per line)."""

    def setUp(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.got = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        conn, _ = self.srv.accept()
        _, h = bridge.read_request(conn)
        acc = base64.b64encode(hashlib.sha1(h["sec-websocket-key"].encode() + bridge.WS_GUID).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                     b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        try:
            while True:
                op, data = bridge.ws_recv(conn)
                if op == 8:
                    break
                self.got.append(data)
                if data.startswith(b"$I"):
                    for l in (b"[VER:1.3a.202506201830:]", b"[MSG:Using machine:ESP32_V4]", b"ok"):
                        bridge.ws_send(conn, l + b"\n", 1)
                elif data.startswith(b"?"):
                    bridge.ws_send(conn, b"<Idle|MPos:0.000,0.000,0.000|FS:0,0>", 1)
        except ConnectionError:
            pass
        conn.close()

    def tearDown(self):
        self.srv.close()

    def test_ws_round_trip(self):
        port = self.srv.getsockname()[1]
        n = nova.Nova(nova.open_transport("ws://127.0.0.1:%d/ws?clientId=test" % port))
        try:
            self.assertIn("[VER:1.3a.202506201830:]", n.command("$I"))
            self.assertEqual(n.status().state, "Idle")
        finally:
            n.close()
        self.assertEqual(self.got[0], b"$I\n")

    def test_bare_ip_means_nova_wifi(self):
        url = nova.nova_ws_url("192.168.1.50")
        self.assertTrue(url.startswith("ws://192.168.1.50:8002/ws?clientId="))


class TestStreaming(SimCase):
    JOB = ["G21 G90", "M4 S0", "G0 X10 Y10", "G1 X50 Y10 S800 F3000",
           "G1 X50 Y40", "G0 X0 Y0", "M5"]

    def test_check_mode_moves_nothing(self):
        n = self.connect()  # no permissions needed for $C check
        self.assertEqual(n.stream(self.JOB, check_only=True), len(self.JOB))
        self.assertEqual(self.m.burn, [])
        self.assertEqual(self.m.pos[:2], [0.0, 0.0])
        self.assertEqual(n.status().state, "Idle")

    def test_check_mode_stock_grbl_uses_dollar_c(self):
        self.m.nova = False  # stock Grbl: real $C check mode on the controller
        n = self.connect()
        self.assertEqual(n.stream(self.JOB, check_only=True), len(self.JOB))
        self.assertEqual(n.status().state, "Idle")

    def test_nova_has_no_dollar_c(self):
        n = self.connect()
        with self.assertRaises(nova.GrblError) as cm:
            n._toggle_check_mode()
        self.assertEqual(cm.exception.code, 3)

    def test_check_mode_reports_bad_line(self):
        n = self.connect()
        with self.assertRaises(nova.GrblError) as cm:
            n.stream(["G1 X10 F100", "G1 X400"], check_only=True)
        self.assertEqual(cm.exception.code, 15)
        self.assertEqual(n.status().state, "Idle")

    def test_run_job(self):
        n = self.connect(motion=True, laser=True)
        n.stream(self.JOB)
        self.assertEqual(len(self.m.burn), 2)
        self.assertEqual(self.m.spindle, 0)

    def test_run_stops_on_alarm(self):
        n = self.connect(motion=True, laser=True)
        with self.assertRaises(nova.GrblAlarm):
            n.stream(["M4 S500", "G1 X10 F1000", "G1 X999", "G1 X20"])
        self.assertNotIn("G1 X20", self.m.history)

    def test_run_refuses_when_not_idle(self):
        self.m.state = "Door"
        n = self.connect(motion=True, laser=True)
        with self.assertRaises(nova.NovaError):
            n.stream(self.JOB)
        self.assertEqual(self.m.history, [])

    def test_emergency_stop_resets(self):
        self.m.move_time = 5.0
        n = self.connect(motion=True)
        n.command("G1 X100 F100")
        self.assertEqual(n.status().state, "Run")
        n.emergency_stop()
        time.sleep(0.2)
        self.assertEqual(n.status().state, "Alarm")  # ALARM:3, position lost
        self.assertEqual(self.m.spindle, 0)


class TestNovaQuirks(SimCase):
    """Behaviour recorded on a real NOVA over USB (see PROTOCOL.md)."""

    def test_extra_ok_after_status_is_dropped(self):
        n = self.connect()
        n.status()
        self.assertTrue(n.ok_after_status)
        # many status polls must not leave `ok`s that answer later commands
        for _ in range(5):
            n.realtime(nova.RT_STATUS)
        time.sleep(0.2)
        self.assertIn("[VER:1.3a.202506201830:]", n.command("$I"))
        self.assertTrue(n._lines.empty())

    def test_stock_grbl_has_no_extra_ok(self):
        self.srv.machine.nova = False
        n = self.connect()
        n.status()
        self.assertFalse(n.ok_after_status)

    def test_ff_device_info_and_status(self):
        d = self.connect().device()
        self.assertEqual(d["info"]["data"]["laser"]["power"], 20)
        self.assertTrue(d["info"]["data"]["laser"]["SN"].endswith("…"))
        self.assertEqual(d["status"]["alarm"], "NONE")
        self.assertIn("inetractMode", d["status"])

    def test_ff_gating(self):
        n = self.connect()
        with self.assertRaises(nova.NotAllowed):
            n.ff("FF FC X10 Y10")      # autofocus moves the head
        with self.assertRaises(nova.NotAllowed):
            n.ff("FF F0 ssid pass")    # never sent
        self.assertEqual(self.m.history, [])

    def test_unsupported_parser_state(self):
        with self.assertRaises(nova.GrblError) as e:
            self.connect().command("$G")
        self.assertEqual(e.exception.code, 3)


class TestHelpers(unittest.TestCase):
    def test_parse_status(self):
        st = nova.parse_status("<Run|MPos:1.000,2.500,0.000|FS:3000,800|A:SF>")
        self.assertEqual((st.state, st.mpos, st.feed, st.spindle), ("Run", (1, 2.5, 0), 3000, 800))
        self.assertTrue(st.laser_on and st.air_assist)
        self.assertEqual(nova.parse_status("<Hold:0|MPos:0,0,0|FS:0,0>").substate, "0")

    def test_classify(self):
        c = nova.classify
        self.assertEqual(c("$I"), "readonly")
        self.assertEqual(c("$$"), "readonly")
        self.assertEqual(c("G0 X1"), "motion")
        self.assertEqual(c("G1 X1 S100"), "laser")
        self.assertEqual(c("M3 S1"), "laser")
        self.assertEqual(c("$100=80"), "forbidden")
        self.assertEqual(c("; just a comment"), "readonly")
        self.assertEqual(c("FF F2"), "readonly")
        self.assertEqual(c("FF FC X10 Y20"), "motion")
        self.assertEqual(c("FF F0 net pass"), "forbidden")   # Wi-Fi credentials
        self.assertEqual(c("FF CW C4 1"), "forbidden")       # consumable counter reset

    def test_mask_serials(self):
        m = nova.mask_serials({"data": {"laser": {"SN": "TCNVFA12345678", "power": 20}}})
        self.assertEqual(m["data"]["laser"], {"SN": "TCNVFA…", "power": 20})

    def test_fires_laser(self):
        self.assertFalse(nova.file_fires_laser(["G0 X1", "M5", "S0"]))
        self.assertTrue(nova.file_fires_laser(["M4", "G1 X1 S10"]))

    def test_redact(self):
        self.assertEqual(nova.redact("$Sta/Password=hunter2"), "$Sta/Password=******")
        self.assertEqual(nova.redact("$130=297"), "$130=297")


class TestLbdev(unittest.TestCase):
    def test_json_profile(self):
        macro = "M8\nG4 P0.5"
        doc = {"DeviceList": [{"DisplayName": "TOOCAA NOVA", "DeviceType": "GRBL",
                               "BaudRate": 115200, "Width": 297, "Height": 210,
                               "WifiPassword": "secret",
                               "Macros": [{"Name": "Air on", "Commands": macro},
                                          {"Name": "Focus", "Commands":
                                           base64.b64encode(b"$HZ\nG0 Z5").decode()}]}]}
        rep = lbdev_audit.audit(json.dumps(doc).encode())
        self.assertEqual(rep["format"], "json")
        facts = {e["path"]: e["value"] for e in rep["facts"]}
        self.assertEqual(facts["DeviceList[0].BaudRate"], "115200")
        self.assertEqual(facts["DeviceList[0].Width"], "297")
        vals = {e["path"]: e["value"] for e in rep["entries"]}
        self.assertEqual(vals["DeviceList[0].WifiPassword"], "******")
        g = {s["path"]: s for s in rep["gcode"]}
        self.assertEqual(g["DeviceList[0].Macros[0].Commands"]["lines"], ["M8", "G4 P0.5"])
        self.assertEqual(g["DeviceList[0].Macros[1].Commands"]["encoding"], "base64")
        self.assertIn("$HZ", g["DeviceList[0].Macros[1].Commands"]["lines"])

    def test_official_nova_profile_shape(self):
        # structure of https://wiki.toocaa.com/toocaa_nova.lbdev (2026-10)
        doc = {"DeviceList": [{"DisplayName": "TOOCAA NOVA", "Name": "GRBL", "Type": "Serial",
                               "Width": 297, "Height": 210, "HomeOnStartup": True,
                               "MirrorX": False, "MirrorY": True,
                               "Settings": {"CommPort": "(Choose)", "Macro0_Label": "FOCUS",
                                            "Macro0_Content": "G38.2\n"}}]}
        rep = lbdev_audit.audit(json.dumps(doc).encode())
        facts = {e["path"]: e["value"] for e in rep["facts"]}
        self.assertEqual(facts["DeviceList[0].MirrorY"], "True")
        self.assertEqual(facts["DeviceList[0].Settings.Macro0_Label"], "FOCUS")
        self.assertEqual(rep["gcode"][0]["lines"], ["G38.2"])

    def test_xml_profile(self):
        xml = b'<Device Type="GRBL" Baud="115200"><Macro Name="Cross">M3 S10</Macro></Device>'
        rep = lbdev_audit.audit(xml)
        self.assertEqual(rep["format"], "xml")
        self.assertTrue(any("M3 S10" in s["lines"] for s in rep["gcode"]))


if __name__ == "__main__":
    unittest.main()
