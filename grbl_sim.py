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
"""

from __future__ import annotations

import argparse
import re
import socket
import threading
import time

VERSION = "[VER:1.3a.202506201830:]"
OPTIONS = "[OPT:PHBW]"
MACHINE = "[MSG:Using machine:ESP32_V4]"
BANNER = "Grbl 1.3a ['$' for help]"

SETTINGS = {
    0: "10", 1: "250", 2: "0", 3: "0", 4: "0", 5: "0", 6: "0", 10: "1",
    11: "0.010", 12: "0.002", 13: "0", 20: "1", 21: "0", 22: "1", 23: "3",
    24: "500.000", 25: "3000.000", 26: "250", 27: "1.000", 30: "1000",
    31: "0", 32: "1", 100: "80.000", 101: "80.000", 102: "800.000",
    110: "36000.000", 111: "36000.000", 112: "600.000",
    120: "3000.000", 121: "3000.000", 122: "100.000",
    130: "297.000", 131: "210.000", 132: "40.000",
}
WORD_RE = re.compile(r"([A-Z])\s*([-+]?\d*\.?\d+)")


class Machine:
    def __init__(self, width=297.0, height=210.0, move_time=0.0):
        self.w, self.h = width, height
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
        line = "<%s|MPos:%.3f,%.3f,%.3f|FS:%.0f,%.0f" % (
            st, *self.pos, self.f if on else 0, self.s if self.spindle else 0)
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
            if u.startswith("$"):
                return self.system(ln, u)
            if self.state == "Alarm":
                return ["error:9"]
            if self.state == "Door":
                return ["error:9"]
            return self.gcode(u)

    def system(self, ln, u):
        if u == "$I":
            return [VERSION, OPTIONS, MACHINE, "ok"]
        if u == "$$":
            return ["$%d=%s" % kv for kv in sorted(SETTINGS.items())] + ["ok"]
        if u == "$G":
            return ["[GC:G%d G54 G17 G21 G%d G94 M%d M9 T0 F%.0f S%.0f]" % (
                self.motion, 90 if self.absolute else 91,
                self.spindle or 5, self.f, self.s), "ok"]
        if u == "$#":
            return ["[G54:0.000,0.000,0.000]", "[G92:0.000,0.000,0.000]",
                    "[TLO:0.000]", "[PRB:0.000,0.000,0.000:0]", "ok"]
        if u == "$":
            return ["[HLP:$$ $+ $# $S $L $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H $F $E=err ~ ! ? ctrl-x]", "ok"]
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
        if moved and not (0 <= target[0] <= self.w and 0 <= target[1] <= self.h):
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
                        conn.sendall((r + "\r\n").encode())
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


def main():
    ap = argparse.ArgumentParser(description="TOOCAA NOVA (Grbl_ESP32) simulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=2323)
    ap.add_argument("--move-time", type=float, default=0.05,
                    help="seconds each motion line reports Run")
    a = ap.parse_args()
    srv = SimServer(a.host, a.port, Machine(move_time=a.move_time)).start()
    print("NOVA simulator on %s:%d  (Ctrl-C to quit)" % (a.host, srv.port))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.close()


if __name__ == "__main__":
    main()
