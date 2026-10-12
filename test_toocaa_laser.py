#!/usr/bin/env python3
"""End-to-end tests against the offline simulator. Run: python3 -m unittest -v"""

import base64
import json
import time
import unittest

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
        self.assertIn("$130=297.000", info["settings"])
        self.assertTrue(info["status"].startswith("<Idle"))

    def test_status(self):
        st = self.connect().status()
        self.assertEqual(st.state, "Idle")
        self.assertEqual(st.mpos, (0.0, 0.0, 0.0))

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
        self.assertEqual(st.mpos[:2], (10.0, 5.0))
        self.assertEqual(self.m.burn, [])

    def test_jog_outside_bed_is_rejected_by_controller(self):
        n = self.connect(motion=True)
        with self.assertRaises(nova.GrblError) as cm:
            n.jog(-5, 0)
        self.assertEqual(cm.exception.code, 15)

    def test_jog_step_limit(self):
        with self.assertRaises(nova.NotAllowed):
            self.connect(motion=True).jog(250, 0)


class TestStreaming(SimCase):
    JOB = ["G21 G90", "M4 S0", "G0 X10 Y10", "G1 X50 Y10 S800 F3000",
           "G1 X50 Y40", "G0 X0 Y0", "M5"]

    def test_check_mode_moves_nothing(self):
        n = self.connect()  # no permissions needed for $C check
        self.assertEqual(n.stream(self.JOB, check_only=True), len(self.JOB))
        self.assertEqual(self.m.burn, [])
        self.assertEqual(self.m.pos[:2], [0.0, 0.0])
        self.assertEqual(n.status().state, "Idle")

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

    def test_xml_profile(self):
        xml = b'<Device Type="GRBL" Baud="115200"><Macro Name="Cross">M3 S10</Macro></Device>'
        rep = lbdev_audit.audit(xml)
        self.assertEqual(rep["format"], "xml")
        self.assertTrue(any("M3 S10" in s["lines"] for s in rep["gcode"]))


if __name__ == "__main__":
    unittest.main()
