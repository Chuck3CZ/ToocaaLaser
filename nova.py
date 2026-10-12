#!/usr/bin/env python3
"""
Local, cloud-free communication with the TOOCAA NOVA laser engraver.

What the protocol is based on
-----------------------------
The NOVA's controller identifies itself (LightBurn console, firmware 1.0.0+):

    [VER:1.3a.202506201830:]
    [OPT:PHBW]
    [MSG:Using machine:ESP32_V4]

That is the build-info format of **Grbl_ESP32** (bdring, version "1.3a").
OPT letters in Grbl_ESP32: P = parking, H = single-axis homing commands,
B = Bluetooth, W = Wi-Fi. So the NOVA speaks standard Grbl 1.1 line protocol
(`ok` / `error:N` / `ALARM:N`, `<...>` status reports, real-time bytes
`?` `!` `~` Ctrl-X) over USB serial, and Grbl_ESP32 exposes the same stream
over Wi-Fi via Telnet (TCP 23 by default). None of it needs the TOOCAA cloud.

TOOCAA's build is customised (autofocus, camera, flame sensor), so anything
beyond stock Grbl_ESP32 must be confirmed on the machine: run `probe` and
`info` first – both are read-only.

Safety model (the firmware's own interlocks are never bypassed):
  * read-only by default – only whitelisted query commands are sent;
  * `--allow-motion` unlocks jogging/homing (`$J=`, `$H`, `$X`) – the laser
    stays off, Grbl never fires the beam on a jog;
  * `--allow-laser` is additionally required to stream G-code that can turn
    the beam on (M3/M4 with S>0);
  * settings writes (`$n=`, `$RST`, `$NVX`, `[ESPxxx]...`) are never sent;
  * streaming stops on any error/alarm/door/hold; Ctrl-C sends feed hold +
    soft reset (Grbl switches the laser off immediately on reset);
  * nothing is sent on connect (no reset) so a running job is not killed.

Usage:
    python3 nova.py probe 192.168.1.50              # which local services exist
    python3 nova.py -p COM5 info                    # USB: banner, $I, $$, $G
    python3 nova.py -p 192.168.1.50 status --watch  # Wi-Fi (telnet 23)
    python3 nova.py -p COM5 --allow-motion jog X10 Y-5 --feed 1500
    python3 nova.py -p COM5 check job.gcode         # validate in $C check mode
    python3 nova.py -p COM5 --allow-motion --allow-laser run job.gcode
    python3 nova.py -p COM5 stop                    # feed hold + soft reset

Port syntax: COM5, /dev/ttyUSB0, serial:/dev/ttyACM0@115200,
             192.168.1.50 (telnet 23), tcp:192.168.1.50:23.
USB needs pyserial (`pip install pyserial`); Wi-Fi uses only the stdlib.
"""

from __future__ import annotations

import argparse
import queue
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass, field

WORK_AREA_MM = (297.0, 210.0)  # NOVA A4 bed
DEFAULT_BAUD = 115200
DEFAULT_TELNET_PORT = 23
MAX_JOG_STEP_MM = 100.0

# Real-time bytes (Grbl 1.1)
RT_STATUS = b"?"
RT_HOLD = b"!"
RT_RESUME = b"~"
RT_RESET = b"\x18"
RT_JOG_CANCEL = b"\x85"

# Commands that only report state. Grbl_ESP32 `$` commands are matched
# case-insensitively, as the firmware does.
READONLY_COMMANDS = {
    "$", "$$", "$I", "$G", "$#", "$N", "$S", "$SC", "$CMD", "$A", "$E",
    "$SS", "$V", "$L", "$GS", "$HEAP",
    "[ESP800]", "[ESP420]",
}
# Commands that move the machine but never fire the laser.
MOTION_COMMANDS = {"$H", "$HX", "$HY", "$HZ", "$X"}

GRBL_ERRORS = {
    1: "Expected command letter", 2: "Bad number format",
    3: "Invalid statement", 4: "Negative value", 5: "Homing not enabled",
    8: "Not idle", 9: "G-code locked out during alarm or jog state",
    10: "Soft limits require homing", 11: "Line too long",
    15: "Jog travel exceeds machine limits", 16: "Invalid jog command",
    17: "Laser mode requires PWM output", 20: "Unsupported G-code",
    22: "Feed rate undefined", 33: "Invalid target",
}
GRBL_ALARMS = {
    1: "Hard limit triggered", 2: "Soft limit – target outside machine travel",
    3: "Reset while in motion – position lost, re-home",
    4: "Probe fail (initial state)", 5: "Probe fail (no contact)",
    6: "Homing fail – reset during cycle", 7: "Homing fail – door opened",
    8: "Homing fail – pull-off did not clear switch",
    9: "Homing fail – switch not found",
}
SECRET_RE = re.compile(r"(pass(word)?|psk|token|key)", re.I)


class NovaError(Exception):
    pass


class GrblError(NovaError):
    def __init__(self, code: int, line: str = ""):
        self.code = code
        self.line = line
        super().__init__("error:%d %s%s" % (
            code, GRBL_ERRORS.get(code, ""), (" (in: %s)" % line) if line else ""))


class GrblAlarm(NovaError):
    def __init__(self, code: int):
        self.code = code
        super().__init__("ALARM:%d %s" % (code, GRBL_ALARMS.get(code, "")))


class NotAllowed(NovaError):
    pass


# --------------------------------------------------------------------------
# Transports
# --------------------------------------------------------------------------

class TcpTransport:
    """Grbl_ESP32 Telnet server: raw TCP, no option negotiation in practice;
    IAC sequences are stripped anyway."""

    def __init__(self, host: str, port: int = DEFAULT_TELNET_PORT, timeout: float = 5.0):
        self.addr = (host, port)
        self.sock = socket.create_connection(self.addr, timeout=timeout)
        self.sock.settimeout(0.2)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def write(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read(self) -> bytes:
        try:
            data = self.sock.recv(4096)
        except socket.timeout:
            return b""
        if not data:
            raise ConnectionError("connection closed by %s:%d" % self.addr)
        return strip_telnet_iac(data)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def __str__(self) -> str:
        return "tcp:%s:%d" % self.addr


class SerialTransport:
    def __init__(self, device: str, baud: int = DEFAULT_BAUD):
        try:
            import serial  # pyserial
        except ImportError:
            raise NovaError("USB needs pyserial: pip install pyserial")
        s = serial.Serial()
        s.port = device
        s.baudrate = baud
        s.timeout = 0.2
        # ESP32 boards reset when DTR/RTS toggle on open; keep them low so
        # connecting never restarts the controller mid-job.
        s.dtr = False
        s.rts = False
        s.open()
        self.ser = s
        self.device = device
        self.baud = baud

    def write(self, data: bytes) -> None:
        self.ser.write(data)
        self.ser.flush()

    def read(self) -> bytes:
        return self.ser.read(self.ser.in_waiting or 1)

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass

    def __str__(self) -> str:
        return "serial:%s@%d" % (self.device, self.baud)


def strip_telnet_iac(data: bytes) -> bytes:
    if b"\xff" not in data:
        return data
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0xFF and i + 1 < len(data):
            cmd = data[i + 1]
            if cmd == 0xFF:
                out.append(0xFF)
                i += 2
            elif 0xFB <= cmd <= 0xFE:
                i += 3
            else:
                i += 2
            continue
        out.append(b)
        i += 1
    return bytes(out)


def open_transport(port: str, baud: int = DEFAULT_BAUD):
    """COM5 | /dev/ttyUSB0 | serial:DEV[@BAUD] | tcp:HOST[:PORT] | HOST[:PORT]"""
    if port.startswith("serial:"):
        dev, _, b = port[7:].partition("@")
        return SerialTransport(dev, int(b) if b else baud)
    if port.startswith("tcp:"):
        port = port[4:]
    elif re.match(r"(?i)^(com\d+|/dev/)", port):
        return SerialTransport(port, baud)
    host, _, p = port.rpartition(":") if port.count(":") == 1 else (port, "", "")
    return TcpTransport(host or port, int(p) if p else DEFAULT_TELNET_PORT)


# --------------------------------------------------------------------------
# Status / G-code helpers
# --------------------------------------------------------------------------

@dataclass
class Status:
    state: str
    substate: str = ""
    mpos: tuple | None = None
    wpos: tuple | None = None
    wco: tuple | None = None
    feed: float | None = None
    spindle: float | None = None
    pins: str = ""
    accessories: str = ""
    overrides: tuple | None = None
    raw: str = ""

    @property
    def laser_on(self) -> bool:
        return "S" in self.accessories or "C" in self.accessories

    @property
    def air_assist(self) -> bool:
        # Grbl reports coolant in A:; diode lasers usually wire air assist
        # to flood (M8). Unconfirmed for NOVA until the .lbdev is audited.
        return "F" in self.accessories or "M" in self.accessories


def _floats(s: str) -> tuple:
    return tuple(float(x) for x in s.split(","))


def parse_status(line: str) -> Status:
    """`<Idle|MPos:0.000,0.000,0.000|FS:0,0|WCO:...|Pn:XYZ|A:SF>`"""
    body = line.strip().lstrip("<").rstrip(">")
    parts = body.split("|")
    state, _, sub = parts[0].partition(":")
    st = Status(state=state, substate=sub, raw=line.strip())
    for p in parts[1:]:
        k, _, v = p.partition(":")
        try:
            if k == "MPos":
                st.mpos = _floats(v)
            elif k == "WPos":
                st.wpos = _floats(v)
            elif k == "WCO":
                st.wco = _floats(v)
            elif k == "FS":
                f = _floats(v)
                st.feed, st.spindle = f[0], f[1] if len(f) > 1 else None
            elif k == "F":
                st.feed = float(v)
            elif k == "Pn":
                st.pins = v
            elif k == "A":
                st.accessories = v
            elif k == "Ov":
                st.overrides = _floats(v)
        except ValueError:
            pass
    return st


_COMMENT_RE = re.compile(r"\(.*?\)|;.*$")
_WORD_RE = re.compile(r"([A-Z])\s*([-+]?\d*\.?\d+)")


def clean_line(line: str) -> str:
    return _COMMENT_RE.sub("", line).strip()


def gcode_words(line: str) -> list[tuple[str, float]]:
    return [(l, float(v)) for l, v in _WORD_RE.findall(clean_line(line).upper())]


def file_fires_laser(lines: list[str]) -> bool:
    """Conservative: any M3/M4 together with any positive S anywhere."""
    spindle_on = False
    s_pos = False
    for ln in lines:
        for letter, val in gcode_words(ln):
            if letter == "M" and val in (3, 4):
                spindle_on = True
            elif letter == "S" and val > 0:
                s_pos = True
    return spindle_on and s_pos


def classify(cmd: str) -> str:
    """-> 'readonly' | 'motion' | 'laser' | 'forbidden'"""
    c = cmd.strip()
    u = c.upper()
    if not c:
        return "readonly"
    if u in READONLY_COMMANDS:
        return "readonly"
    if u in MOTION_COMMANDS:
        return "motion"
    if u.startswith("$J="):
        return "motion"
    if u.startswith("$") or u.startswith("["):
        return "forbidden"  # settings writes, file ops, sleep, restore...
    if not gcode_words(c):
        return "readonly"  # comment-only line
    return "laser" if file_fires_laser([c]) or any(
        l == "S" and v > 0 for l, v in gcode_words(c)) else "motion"


def redact(line: str) -> str:
    if SECRET_RE.search(line) and "=" in line:
        k, _, _ = line.partition("=")
        return k + "=******"
    return line


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

@dataclass
class Permissions:
    motion: bool = False
    laser: bool = False


@dataclass
class Nova:
    transport: object
    perms: Permissions = field(default_factory=Permissions)
    log: callable = None

    def __post_init__(self):
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._status: "queue.Queue[Status]" = queue.Queue()
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self.last_status: Status | None = None
        self.messages: list[str] = []  # async [MSG:..], banners, ALARM
        self.closed_error: Exception | None = None
        self._rx = threading.Thread(target=self._reader, daemon=True)
        self._rx.start()

    # -- low level ---------------------------------------------------------

    def _reader(self):
        buf = b""
        while not self._stop.is_set():
            try:
                data = self.transport.read()
            except Exception as e:  # connection lost
                self.closed_error = e
                self._lines.put("\x00closed")
                return
            if not data:
                continue
            buf += data
            while True:
                i = buf.find(b"\n")
                if i < 0:
                    break
                raw, buf = buf[:i], buf[i + 1:]
                line = raw.decode("utf-8", "replace").strip("\r\n\x00 ")
                if not line:
                    continue
                if self.log:
                    self.log("< " + redact(line))
                if line.startswith("<") and line.endswith(">"):
                    st = parse_status(line)
                    self.last_status = st
                    self._status.put(st)
                    continue
                if line.startswith(("[MSG", "Grbl ", "ALARM:")) or line.startswith("[ESP"):
                    self.messages.append(line)
                self._lines.put(line)

    def _write(self, data: bytes):
        if self.closed_error:
            raise NovaError("connection lost: %s" % self.closed_error)
        with self._wlock:
            self.transport.write(data)

    def realtime(self, b: bytes):
        """Real-time bytes bypass the permission gate: they only stop/report."""
        if b not in (RT_STATUS, RT_HOLD, RT_RESUME, RT_RESET, RT_JOG_CANCEL):
            raise NotAllowed("not a real-time command")
        if self.log and b != RT_STATUS:
            self.log("> RT %r" % b)
        self._write(b)

    def close(self):
        self._stop.set()
        self.transport.close()

    def drain(self, wait: float = 0.3) -> list[str]:
        out, end = [], time.time() + wait
        while time.time() < end:
            try:
                out.append(self._lines.get(timeout=0.05))
            except queue.Empty:
                pass
        return out

    # -- commands ----------------------------------------------------------

    def check_allowed(self, cmd: str) -> str:
        kind = classify(cmd)
        if kind == "forbidden":
            raise NotAllowed("refusing %r: settings/system commands are not sent by "
                             "this tool (use the touchscreen or LightBurn)" % cmd)
        if kind == "motion" and not self.perms.motion:
            raise NotAllowed("refusing %r: needs --allow-motion" % cmd)
        if kind == "laser" and not (self.perms.motion and self.perms.laser):
            raise NotAllowed("refusing %r: can fire the laser, needs "
                             "--allow-motion --allow-laser" % cmd)
        return kind

    def command(self, cmd: str, timeout: float = 10.0) -> list[str]:
        """Send one line, return reply lines up to `ok`. Raises on error/alarm."""
        self.check_allowed(cmd)
        if len(cmd) > 255:
            raise GrblError(11, cmd)
        # drop stale lines (async messages from before this command)
        while not self._lines.empty():
            self._lines.get_nowait()
        if self.log:
            self.log("> " + cmd)
        self._write(cmd.encode() + b"\n")
        out = []
        end = time.time() + timeout
        while True:
            left = end - time.time()
            if left <= 0:
                raise NovaError("timeout waiting for reply to %r" % cmd)
            try:
                line = self._lines.get(timeout=left)
            except queue.Empty:
                continue
            if line == "\x00closed":
                raise NovaError("connection lost: %s" % self.closed_error)
            if line == "ok":
                return out
            m = re.match(r"error:(\d+)", line)
            if m:
                raise GrblError(int(m.group(1)), cmd)
            m = re.match(r"ALARM:(\d+)", line)
            if m:
                raise GrblAlarm(int(m.group(1)))
            out.append(line)

    def status(self, timeout: float = 2.0) -> Status:
        while not self._status.empty():
            self._status.get_nowait()
        self.realtime(RT_STATUS)
        try:
            return self._status.get(timeout=timeout)
        except queue.Empty:
            raise NovaError("no status report (is this a Grbl controller?)")

    def wait_idle(self, timeout: float = 600.0, poll: float = 0.25) -> Status:
        end = time.time() + timeout
        while time.time() < end:
            st = self.status()
            if st.state == "Idle":
                return st
            if st.state in ("Alarm", "Door", "Hold"):
                raise NovaError("machine is in %s state: %s" % (st.state, st.raw))
            time.sleep(poll)
        raise NovaError("timeout waiting for Idle")

    def info(self) -> dict:
        res = {"status": self.status().raw}
        for key, cmd in (("build", "$I"), ("parser", "$G"), ("settings", "$$")):
            try:
                res[key] = [redact(l) for l in self.command(cmd)]
            except NovaError as e:
                res[key] = ["<%s>" % e]
        return res

    def settings(self) -> dict[str, str]:
        out = {}
        for ln in self.command("$$"):
            k, sep, v = ln.partition("=")
            if sep:
                out[k.strip()] = v.strip()
        return out

    def jog(self, x: float = 0.0, y: float = 0.0, feed: float = 1000.0,
            relative: bool = True) -> None:
        if relative and max(abs(x), abs(y)) > MAX_JOG_STEP_MM:
            raise NotAllowed("jog step > %g mm refused" % MAX_JOG_STEP_MM)
        if not relative and not (0 <= x <= WORK_AREA_MM[0] and 0 <= y <= WORK_AREA_MM[1]):
            raise NotAllowed("target outside the %gx%g mm work area" % WORK_AREA_MM)
        st = self.status()
        if st.state not in ("Idle", "Jog"):
            raise NovaError("jog needs Idle state, machine is %s" % st.state)
        words = "".join(" %s%.3f" % (a, v) for a, v in (("X", x), ("Y", y)) if relative is False or v)
        self.command("$J=%s G21%s F%.0f" % ("G91" if relative else "G90", words, feed))

    def stream(self, lines: list[str], check_only: bool = False,
               progress=None, poll_every: float = 0.5) -> int:
        """Send-and-wait streaming (one line in flight). Stops on the first
        error, alarm, door, or unexpected hold. Returns lines sent."""
        prog = [clean_line(l) for l in lines]
        prog = [l for l in prog if l]
        for l in prog:
            if l.startswith("$") or l.startswith("["):
                raise NotAllowed("job contains a system/settings line: %r" % l)
        fires = file_fires_laser(prog)
        if not check_only:
            if not self.perms.motion:
                raise NotAllowed("running a job needs --allow-motion")
            if fires and not self.perms.laser:
                raise NotAllowed("job turns the laser on: needs --allow-laser")
        st = self.status()
        if st.state != "Idle":
            raise NovaError("job needs Idle state, machine is %s (%s)" % (st.state, st.raw))

        saved = self.perms
        if check_only:
            # In $C check mode Grbl parses everything but moves nothing.
            # Only lift the client-side gate once the controller confirms it.
            self._toggle_check_mode()
            if self.status().state != "Check":
                self._toggle_check_mode()
                raise NovaError("controller did not enter check mode – aborting")
            self.perms = Permissions(motion=True, laser=True)
        sent = 0
        last_poll = 0.0
        try:
            for l in prog:
                if time.time() - last_poll > poll_every:
                    last_poll = time.time()
                    st = self.status()
                    if st.state in ("Alarm", "Door", "Hold", "Sleep"):
                        raise NovaError("stopped: machine entered %s (%s)" % (st.state, st.raw))
                self.command(l, timeout=120.0)
                sent += 1
                if progress:
                    progress(sent, len(prog))
            if not check_only:
                self.wait_idle()
        except BaseException:
            if not check_only:
                self.emergency_stop()
            raise
        finally:
            if check_only:
                self.perms = saved
                try:
                    if self.status().state == "Check":
                        self._toggle_check_mode()  # leaving it soft-resets
                except NovaError:
                    pass
                self.drain(0.5)
            elif fires:
                try:
                    self.command("M5")
                except NovaError:
                    pass
        return sent

    def _toggle_check_mode(self) -> list[str]:
        """`$C` – Grbl G-code check mode. Bypasses classify() on purpose:
        it is a system command, but it only ever disables motion."""
        while not self._lines.empty():
            self._lines.get_nowait()
        if self.log:
            self.log("> $C")
        self._write(b"$C\n")
        out, end = [], time.time() + 5
        while time.time() < end:
            try:
                line = self._lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line == "ok":
                return out
            if line.startswith("error:"):
                raise GrblError(int(line[6:]), "$C")
            out.append(line)
        raise NovaError("timeout on $C")

    def emergency_stop(self):
        """Feed hold then soft reset. Software stop – not a substitute for
        the machine's lid interlock / power switch."""
        for b in (RT_HOLD, RT_RESET):
            try:
                self.realtime(b)
            except NovaError:
                pass
            time.sleep(0.05)


# --------------------------------------------------------------------------
# Probe: discover local services without sending any machine command
# --------------------------------------------------------------------------

PROBE_PORTS = [23, 80, 81, 443, 8080, 8888, 8899, 1883, 5000, 9000]


def probe(host: str, ports=None, timeout: float = 1.5) -> list[dict]:
    """TCP connect scan of typical Grbl_ESP32/ESP3D ports. For HTTP ports it
    does one `GET /` (reads the server banner/title). For telnet it only
    listens for a greeting. Nothing is written to the controller."""
    results = []
    for port in ports or PROBE_PORTS:
        r = {"port": port, "open": False}
        try:
            s = socket.create_connection((host, port), timeout=timeout)
        except OSError as e:
            r["error"] = type(e).__name__
            results.append(r)
            continue
        r["open"] = True
        s.settimeout(timeout)
        try:
            if port in (80, 8080, 5000, 9000):
                s.sendall(b"GET / HTTP/1.0\r\nHost: %s\r\n\r\n" % host.encode())
            data = b""
            end = time.time() + timeout
            while time.time() < end and len(data) < 8192:
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
            text = strip_telnet_iac(data).decode("utf-8", "replace")
            hdr = re.search(r"(?im)^server:\s*(.+)$", text)
            title = re.search(r"(?is)<title>(.*?)</title>", text)
            r["banner"] = (text.splitlines() or [""])[0][:120]
            if hdr:
                r["server"] = hdr.group(1).strip()
            if title:
                r["title"] = title.group(1).strip()[:80]
        except OSError as e:
            r["error"] = type(e).__name__
        finally:
            s.close()
        results.append(r)
    return results


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _print_status(st: Status):
    pos = st.mpos or st.wpos
    print("%-6s %s  pos=%s  F=%s S=%s  laser=%s air=%s%s" % (
        st.state, st.substate,
        ",".join("%.2f" % v for v in pos) if pos else "?",
        st.feed, st.spindle, "ON" if st.laser_on else "off",
        "on" if st.air_assist else "off",
        ("  pins=" + st.pins) if st.pins else ""))


def _parse_axes(words: list[str]) -> dict:
    out = {}
    for w in words:
        m = re.fullmatch(r"([XYxy])([-+]?\d*\.?\d+)", w)
        if not m:
            raise SystemExit("bad axis word %r (use e.g. X10 Y-5)" % w)
        out[m.group(1).lower()] = float(m.group(2))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="TOOCAA NOVA local control (Grbl_ESP32)")
    ap.add_argument("-p", "--port", help="COM5, /dev/ttyUSB0, IP (telnet), tcp:IP:PORT")
    ap.add_argument("-b", "--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("-v", "--verbose", action="store_true", help="print raw traffic")
    ap.add_argument("--allow-motion", action="store_true", help="allow jog/home/unlock/run")
    ap.add_argument("--allow-laser", action="store_true", help="allow jobs that fire the laser")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("probe", help="scan an IP for local services (no commands sent)")
    p.add_argument("host")
    p.add_argument("--ports", help="comma list, default: %s" % ",".join(map(str, PROBE_PORTS)))
    sub.add_parser("info", help="status + $I + $G + $$ (read-only)")
    p = sub.add_parser("status", help="one status report")
    p.add_argument("--watch", action="store_true")
    p = sub.add_parser("send", help="send one line (gated by safety tier)")
    p.add_argument("line")
    p = sub.add_parser("jog", help="relative jog, laser off, e.g. jog X10 Y-5")
    p.add_argument("axes", nargs="+")
    p.add_argument("--feed", type=float, default=1000.0)
    sub.add_parser("home", help="$H homing cycle")
    sub.add_parser("unlock", help="$X clear alarm lock (shows the alarm first)")
    p = sub.add_parser("check", help="validate a G-code file in $C check mode (no motion)")
    p.add_argument("file")
    p = sub.add_parser("run", help="stream a G-code file")
    p.add_argument("file")
    sub.add_parser("hold", help="feed hold (!)")
    sub.add_parser("resume", help="cycle resume (~)")
    sub.add_parser("stop", help="feed hold + soft reset (laser off)")
    a = ap.parse_args(argv)

    if a.cmd == "probe":
        ports = [int(x) for x in a.ports.split(",")] if a.ports else None
        for r in probe(a.host, ports):
            if r["open"]:
                extra = "  ".join("%s=%r" % (k, r[k]) for k in ("server", "title", "banner") if r.get(k))
                print("%5d open   %s" % (r["port"], extra))
            else:
                print("%5d closed" % r["port"])
        print("\nNext: python3 nova.py -p %s info   (telnet 23, read-only)" % a.host)
        return 0

    if not a.port:
        ap.error("--port is required for %s" % a.cmd)
    log = (lambda s: print(s, file=sys.stderr)) if a.verbose else None
    nova = Nova(open_transport(a.port, a.baud),
                Permissions(motion=a.allow_motion, laser=a.allow_laser), log)
    try:
        if a.cmd == "info":
            info = nova.info()
            print("transport:", nova.transport)
            print("status:   ", info["status"])
            for k in ("build", "parser", "settings"):
                print("\n[%s]" % k)
                for l in info[k]:
                    print("  " + l)
            s = dict(l.split("=", 1) for l in info["settings"] if "=" in l)
            if s.get("$32") not in (None, "1"):
                print("\n! $32 (laser mode) is %s – expected 1 on a laser" % s.get("$32"))
            if s.get("$130") and s.get("$131"):
                print("\nmax travel X=%s Y=%s mm (NOVA spec %gx%g)" % (
                    s["$130"], s["$131"], *WORK_AREA_MM))
        elif a.cmd == "status":
            while True:
                _print_status(nova.status())
                if not a.watch:
                    break
                time.sleep(0.5)
        elif a.cmd == "send":
            for l in nova.command(a.line):
                print(redact(l))
            print("ok")
        elif a.cmd == "jog":
            ax = _parse_axes(a.axes)
            nova.jog(ax.get("x", 0.0), ax.get("y", 0.0), a.feed)
            _print_status(nova.wait_idle(60))
        elif a.cmd == "home":
            nova.command("$H", timeout=120)
            _print_status(nova.status())
        elif a.cmd == "unlock":
            st = nova.status()
            _print_status(st)
            for m in nova.messages[-5:]:
                print("  last msg:", m)
            if st.state != "Alarm":
                print("not in Alarm – nothing to unlock")
                return 0
            nova.command("$X")
            print("unlocked – position may be wrong; home before running a job")
        elif a.cmd in ("check", "run"):
            with open(a.file, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
            t0 = time.time()

            def prog(i, n):
                if i == n or i % 50 == 0:
                    print("\r%d/%d lines (%.0f%%)" % (i, n, 100.0 * i / n), end="", flush=True)

            n = nova.stream(lines, check_only=(a.cmd == "check"), progress=prog)
            print("\n%s: %d lines OK in %.1f s" % (a.cmd, n, time.time() - t0))
        elif a.cmd == "hold":
            nova.realtime(RT_HOLD)
        elif a.cmd == "resume":
            nova.realtime(RT_RESUME)
        elif a.cmd == "stop":
            nova.emergency_stop()
            time.sleep(0.3)
            print("stop sent (feed hold + soft reset)")
            for l in nova.drain(0.5):
                print("  " + l)
        return 0
    except KeyboardInterrupt:
        print("\ninterrupted – sending feed hold + soft reset", file=sys.stderr)
        nova.emergency_stop()
        return 130
    except NovaError as e:
        print("\n%s: %s" % (type(e).__name__, e), file=sys.stderr)
        return 2
    finally:
        time.sleep(0.05)
        nova.close()


if __name__ == "__main__":
    sys.exit(main())
