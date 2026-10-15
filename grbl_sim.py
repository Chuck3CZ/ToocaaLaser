#!/usr/bin/env python3
"""
Offline TOOCAA NOVA simulator: a TCP server that answers like the NOVA's
Grbl_ESP32 controller (build 1.3a, A4 bed). Lets you develop and test
`nova.py`, the bridge and the web app without the machine or the cloud.

    python3 grbl_sim.py                 # listens on 127.0.0.1:2323
    python3 nova.py -p tcp:127.0.0.1:2323 info

Simplifications: moves complete instantly but the state reads `Run` for
`--move-time` seconds after each motion line; no acceleration; soft limits
on the 297 x 210 mm bed are always on (target outside -> ALARM:2).

NOVA quirks recorded from a real machine over USB (2026-10, firmware V1.0.2,
see PROTOCOL.md) are on by default, `--stock-grbl` turns them off:
  * `?` is answered with the status report **and an extra `ok`**;
  * status reports carry 4 axes (X,Y,Z,A) and `Pn:XY`;
  * `$G` -> error:3, `$#` -> error:1, `$J=` (jogging) and `$C` (check) -> error:3;
  * `FF F1` / `FF F2` / `FF R8` reply with one `FF xx ...` line and no `ok`
    (F1/F2 payload = base64 JSON).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import socket
import threading
import time

VERSION = "[VER:1.3a.202506201830:]"
OPTIONS = "[OPT:PHBW]"
MACHINE = "[MSG:Using machine:ESP32_V4]"
BANNER = "Grbl 1.3a ['$' for help]"

# `$$` of a real NOVA (firmware V1.0.2, Grbl_ESP32 1.3a), USB capture 2026-10
SETTINGS = {
    0: "3", 1: "250", 2: "0", 3: "0", 4: "0", 5: "0", 6: "0", 10: "1",
    11: "0.010", 12: "0.002", 13: "0", 20: "0", 21: "0", 22: "0", 23: "3",
    24: "200.000", 25: "2000.000", 26: "500.000", 27: "3.000", 30: "1000.000",
    31: "0.000", 32: "1",
    100: "100.000", 101: "100.000", 102: "100.000", 103: "45.000",
    104: "100.000", 105: "100.000",
    110: "36000.000", 111: "36000.000", 112: "36000.000", 113: "6000.000",
    114: "36000.000", 115: "36000.000",
    120: "5000.000", 121: "5000.000", 122: "1000.000", 123: "500.000",
    124: "3000.000", 125: "3000.000",
    130: "300.000", 131: "220.000", 132: "300.000", 133: "300.000",
    134: "300.000", 135: "300.000",
}
# Shapes of the TOOCAA `FF F1` / `FF F2` replies (serial numbers are fake)
NOVA_DEVICE_INFO = {"data": {
    "airAssist": {"SN": "TCNVHA0000000000", "hardware_version": "0.0.0", "isOnline": True,
                  "manufactureDate": "2025-12-15", "version": "0.9.4"},
    "laser": {"SN": "TCNVFA0000000000", "hardware_version": "0.0.0", "isOnline": True,
              "manufactureDate": "2025-11-28", "power": 20, "version": "0.9.2", "workTime": 0.5},
    "rotary": {"isOnline": False},
    "toocaaNova": {"SN": "TCNVBA0000000000", "hardware_version": "v0.0.0",
                   "manufactureDate": "2025-12-15", "version": "V1.0.2"}}}
NOVA_CALIB = ("NOVA_CALIB_V1 method=none scheme=hg_gcode avg=0.0000000000,0.0000000000 "
              "struct=0.0000000000 ref=0.1000000000 center=148.5000000000,109.5000000000 cmValid=0")
WORD_RE = re.compile(r"([A-Z])\s*([-+]?\d*\.?\d+)")


class Machine:
    def __init__(self, width=297.0, height=210.0, move_time=0.0, nova_quirks=True):
        self.w, self.h = width, height
        self.nova = nova_quirks
        self.move_time = move_time
        self.lock = threading.RLock()
        self.clients: list[socket.socket] = []
        self.history: list[str] = []  # every accepted line (for tests)
        self.burn: list[tuple] = []   # (x0,y0,x1,y1,S) segments with laser on
        self.reset_state(alarm=None)

    def reset_state(self, alarm):
        self.state = "Alarm" if alarm else "Idle"
        self.pos = [0.0, 0.0, 0.0] if not hasattr(self, "pos") else self.pos
        self.absolute = True
        self.spindle = 0  # 0 off, 3 M3, 4 M4
        self.s = 0.0
        self.f = 0.0
        self.motion = 0
        self.coolant = ""
        self.check = False
        self.busy_until = 0.0
        self.held = False

    # -- output -----------------------------------------------------------
    def broadcast(self, line):
        for c in list(self.clients):
            try:
                c.sendall((line + "\r\n").encode())
            except OSError:
                pass

    def status_line(self):
        st = self.state
        if st in ("Idle", "Run", "Jog") and time.time() < self.busy_until:
            st = "Jog" if st == "Jog" else "Run"
        elif st in ("Run", "Jog"):
            st = self.state = "Idle"
        if self.held:
            st = "Hold:0"
        acc = ""
        if self.spindle == 3:
            acc += "S"
        elif self.spindle == 4:
            acc += "C"
        acc += self.coolant
        on = self.spindle and st.startswith("Run")
        line = "<%s|MPos:%.3f,%.3f,%.3f%s|FS:%.0f,%.0f" % (
            st, *self.pos, ",0.000" if self.nova else "",
            self.f if on else 0, self.s if self.spindle else 0)
        if acc:
            line += "|A:" + acc
        return line + ">"

    # -- real-time --------------------------------------------------------
    def realtime(self, b):
        with self.lock:
            if b == "?":
                return self.status_line()
            if b == "!":
                if self.state in ("Run", "Jog") or time.time() < self.busy_until:
                    self.held = True
            elif b == "~":
                self.held = False
            elif b == "\x18":
                moving = time.time() < self.busy_until and not self.check
                self.reset_state(alarm=3 if moving else None)
                self.busy_until = 0.0
                if moving:
                    self.broadcast("ALARM:3")
                self.broadcast("")
                self.broadcast(BANNER)
                if moving:
                    self.broadcast("[MSG:'$H'|'$X' to unlock]")
            elif b == "\x85":
                if self.state == "Jog":
                    self.busy_until = 0.0
                    self.state = "Idle"
        return None

    # -- lines ------------------------------------------------------------
    def line(self, raw):
        """Returns list of reply lines (ending with ok/error)."""
        with self.lock:
            ln = re.sub(r"\(.*?\)|;.*$", "", raw).strip()
            if not ln:
                return ["ok"]
            u = ln.upper()
            if self.nova and u.startswith("FF "):
                return self.toocaa(u)
            if u.startswith("$"):
                return self.system(ln, u)
            if self.state == "Alarm":
                return ["error:9"]
            if self.state == "Door":
                return ["error:9"]
            return self.gcode(u)

    def device_status(self):
        busy = self.state != "Idle" or time.time() < self.busy_until
        return {"airPumpState": 1 if self.coolant else 0, "alarm": "NONE",
                "consumables": {"laserLens": {"wearTime": 0}, "machineFilter": {"wearTime": 0},
                                "smokePurifier": {"added": False, "wearTime": 0}},
                "coverPlateState": 0, "grblState": 1 if busy else 0, "inetractMode": 0,
                "laserTemperature": 28.0, "rotationPartState": 0, "usbConnectState": 1,
                "workState": 1 if busy else 0}

    def toocaa(self, u):
        """TOOCAA `FF xx` lines: one reply line with the same prefix, no `ok`."""
        b64 = lambda d: base64.b64encode(json.dumps(d).encode()).decode()
        if u == "FF F1":
            return ["FF F1 " + b64(NOVA_DEVICE_INFO)]
        if u == "FF F2":
            return ["FF F2 " + b64(self.device_status())]
        if u == "FF R8":
            return ["FF R8 " + NOVA_CALIB]
        return ["FF ER 0"]  # Studio: "unrecognised command"

    def system(self, ln, u):
        if u == "$I":
            return [VERSION, OPTIONS, MACHINE, "ok"]
        if u == "$$":
            return ["$%d=%s" % kv for kv in sorted(SETTINGS.items())] + ["ok"]
        if u == "$G" and self.nova:
            return ["error:3"]
        if u == "$#" and self.nova:
            return ["error:1"]
        if u == "$G":
            return ["[GC:G%d G54 G17 G21 G%d G94 M%d M9 T0 F%.0f S%.0f]" % (
                self.motion, 90 if self.absolute else 91,
                self.spindle or 5, self.f, self.s), "ok"]
        if u == "$#":
            return ["[G54:0.000,0.000,0.000]", "[G92:0.000,0.000,0.000]",
                    "[TLO:0.000]", "[PRB:0.000,0.000,0.000:0]", "ok"]
        if u == "$":
            return ["[HLP:$$ $+ $# $S $L $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H $F $E=err ~ ! ? ctrl-x]", "ok"]
        if u == "$C" and self.nova:
            return ["error:3"]  # the NOVA has no $C check mode
        if u == "$C":
            if self.state == "Check" or self.check:
                self.realtime("\x18")
                return ["[MSG:Disabled]", "ok"]
            if self.state != "Idle":
                return ["error:8"]
            self.check = True
            self.state = "Check"
            return ["[MSG:Enabled]", "ok"]
        if u == "$X":
            if self.state == "Alarm":
                self.state = "Idle"
                return ["[MSG:Caution: Unlocked]", "ok"]
            return ["ok"]
        if u == "$H":
            if self.state not in ("Idle", "Alarm"):
                return ["error:8"]
            self.pos = [0.0, 0.0, 0.0]
            self.state = "Idle"
            self.history.append("$H")
            return ["ok"]
        if u.startswith("$J="):
            if self.nova:
                return ["error:3"]  # the NOVA has no $J= jogging
            return self.jog(u[3:])
        if re.match(r"\$\d+=", u):
            return ["error:8"] if self.state != "Idle" else ["ok"]
        return ["error:3"]

    def jog(self, body):
        if self.state not in ("Idle", "Jog"):
            return ["error:8"]
        words = WORD_RE.findall(body)
        if not words:
            return ["error:16"]
        rel = "G91" in body.replace(" ", "")
        target = list(self.pos)
        for l, v in words:
            v = float(v)
            if l in "XYZ":
                i = "XYZ".index(l)
                target[i] = target[i] + v if rel else v
        if not (0 <= target[0] <= self.w and 0 <= target[1] <= self.h):
            return ["error:15"]
        self.pos = target
        self.state = "Jog"
        self.busy_until = time.time() + self.move_time
        self.history.append("$J=" + body)
        return ["ok"]

    def gcode(self, u):
        words = [(l, float(v)) for l, v in WORD_RE.findall(u)]
        target = list(self.pos)
        moved = False
        absolute = self.absolute
        motion = self.motion
        for l, v in words:
            if l == "G":
                if v == 38.2:  # NOVA autofocus (LightBurn FOCUS macro)
                    if self.check:
                        return ["ok"]
                    self.pos[2] = 12.5
                    self.history.append(u)
                    return ["[PRB:%.3f,%.3f,%.3f:1]" % tuple(self.pos), "ok"]
                if v in (0, 1, 2, 3):
                    motion = int(v)
                elif v == 90:
                    absolute = True
                elif v == 91:
                    absolute = False
                elif v in (17, 20, 21, 54, 94, 4, 92):
                    pass
                else:
                    return ["error:20"]
            elif l == "M":
                if v == 200:  # NOVA acceleration `M200 X.. Y..` (TOOCAA Studio)
                    return ["ok"]
                if v in (3, 4, 5, 8, 9, 2, 30, 7):
                    pass
                else:
                    return ["error:20"]
            elif l in "XYZ":
                i = "XYZ".index(l)
                target[i] = (v if absolute else target[i] + v)
                moved = True
            elif l in "SFIJPTN":
                pass
            else:
                return ["error:20"]
        if moved and motion == 1 and not any(l == "F" for l, _ in words) and self.f <= 0:
            return ["error:22"]
        # the NOVA travels 300 x 220 and $H leaves it slightly outside (pull-off ~ -7 mm)
        lo, wx, hy = (-10.0, float(SETTINGS[130]), float(SETTINGS[131])) if self.nova else (0.0, self.w, self.h)
        if moved and not (lo <= target[0] <= wx and lo <= target[1] <= hy):
            if self.check:
                return ["error:15"]
            self.state = "Alarm"
            self.busy_until = 0
            return ["ALARM:2", "[MSG:Reset to continue]", "ok"]
        # apply modal state
        self.absolute, self.motion = absolute, motion
        for l, v in words:
            if l == "M" and v in (3, 4):
                self.spindle = int(v)
            elif l == "M" and v in (5, 2, 30):
                self.spindle = 0
            elif l == "M" and v == 8:
                self.coolant = "F"
            elif l == "M" and v == 7:
                self.coolant = "M"
            elif l == "M" and v == 9:
                self.coolant = ""
            elif l == "S":
                self.s = v
            elif l == "F":
                self.f = v
        self.history.append(u)
        if moved and not self.check:
            if self.spindle and self.s > 0 and motion != 0:
                self.burn.append((self.pos[0], self.pos[1], target[0], target[1], self.s))
            self.pos = target
            self.state = "Run"
            self.busy_until = time.time() + self.move_time
        return ["ok"]


def serve_client(m: Machine, conn: socket.socket):
    with m.lock:
        m.clients.append(conn)
    buf = ""
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            for ch in data.decode("latin-1"):
                if ch in "?!~\x18\x85":
                    r = m.realtime(ch)
                    if r:
                        conn.sendall((r + "\r\n" + ("ok\r\n" if m.nova else "")).encode())
                elif ch == "\n":
                    line, buf = buf.strip("\r"), ""
                    for out in m.line(line):
                        conn.sendall((out + "\r\n").encode())
                else:
                    buf += ch
    except OSError:
        pass
    finally:
        with m.lock:
            if conn in m.clients:
                m.clients.remove(conn)
        conn.close()


class SimServer:
    def __init__(self, host="127.0.0.1", port=2323, machine: Machine | None = None):
        self.machine = machine or Machine()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=serve_client, args=(self.machine, conn), daemon=True).start()

    def close(self):
        self.sock.close()


# --------------------------------------------------------------------------
# Wi-Fi flavour: what TOOCAA Studio talks to on port 8002 (see PROTOCOL.md)
# --------------------------------------------------------------------------

def serve_wifi_client(m: Machine, conn: socket.socket, confirm_delay: float = 0.0):
    import json
    from bridge import read_request, send_http, ws_recv, ws_send, WS_GUID
    import base64, hashlib
    try:
        reqline, h = read_request(conn)
        method, path = (reqline.split(" ") + ["", ""])[:2]
        if h.get("upgrade", "").lower() == "websocket" and path.startswith("/ws"):
            acc = base64.b64encode(hashlib.sha1(h.get("sec-websocket-key", "").encode() + WS_GUID).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          "Sec-WebSocket-Accept: %s\r\n\r\n" % acc).encode())
            lock = threading.Lock()

            class WsSock:   # lets Machine.broadcast reach this client
                def sendall(self, data):
                    for line in data.decode("latin-1").splitlines():
                        ws_send(conn, line.encode("latin-1"), 1, lock)
            ws = WsSock()
            with m.lock:
                m.clients.append(ws)
            try:
                while True:
                    op, payload = ws_recv(conn)
                    if op == 8:
                        break
                    if op == 9:
                        ws_send(conn, payload, 10, lock)
                        continue
                    buf = ""
                    for ch in payload.decode("latin-1"):
                        if ch in "?!~\x18\x85":
                            r = m.realtime(ch)
                            if r:
                                ws_send(conn, r.encode(), 1, lock)
                                if m.nova:
                                    ws_send(conn, b"ok", 1, lock)
                        elif ch == "\n":
                            for out in m.line(buf.strip("\r")):
                                ws_send(conn, out.encode("latin-1"), 1, lock)
                            buf = ""
                        else:
                            buf += ch
                    if buf.strip():
                        for out in m.line(buf.strip()):
                            ws_send(conn, out.encode("latin-1"), 1, lock)
            finally:
                with m.lock:
                    if ws in m.clients:
                        m.clients.remove(ws)
            return
        route = path.split("?")[0]
        if route == "/info":
            body = {"code": 200, "msg": "success", "data": {"model": "TOOCAA NOVA (simulator)"}}
        elif route == "/v1/ipComfirm":
            time.sleep(confirm_delay)   # the real NOVA waits for a tap on its touchscreen
            body = {"code": 200, "msg": "confirmed"}
        elif route == "/v1/deviceInfo":
            body = {"code": 200, "data": {"model": "TOOCAA NOVA", "firmware": "simulator", "laser": "20W", "airPump": True}}
        else:
            return send_http(conn, "404 Not Found", b'{"code":404}', "application/json")
        send_http(conn, "200 OK", json.dumps(body).encode(), "application/json")
    except (OSError, ConnectionError):
        pass
    finally:
        conn.close()


class WifiSimServer(SimServer):
    def __init__(self, host="127.0.0.1", port=8002, machine=None, confirm_delay=0.0):
        super().__init__(host, port, machine)
        self.confirm_delay = confirm_delay

    def _loop(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=serve_wifi_client, args=(self.machine, conn, self.confirm_delay), daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="TOOCAA NOVA (Grbl_ESP32) simulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2323)
    ap.add_argument("--stock-grbl", action="store_true",
                    help="plain Grbl_ESP32 replies, without the NOVA quirks")
    ap.add_argument("--move-time", type=float, default=0.05,
                    help="seconds each motion line reports Run")
    ap.add_argument("--wifi", type=int, metavar="PORT", nargs="?", const=8002,
                    help="also serve the NOVA Wi-Fi API (HTTP + WebSocket /ws), default port 8002")
    a = ap.parse_args()
    machine = Machine(move_time=a.move_time, nova_quirks=not a.stock_grbl)
    srv = SimServer(a.host, a.port, machine).start()
    print("NOVA simulator on %s:%d  (Ctrl-C to quit)" % (a.host, srv.port))
    if a.wifi:
        WifiSimServer(a.host, a.wifi, machine).start()
        print("NOVA Wi-Fi API on http://%s:%d  (ws://%s:%d/ws)" % (a.host, a.wifi, a.host, a.wifi))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.close()


if __name__ == "__main__":
    main()
