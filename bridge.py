#!/usr/bin/env python3
"""
Local bridge for the ToocaaLaser web app: WebSocket on 127.0.0.1 <-> NOVA
(Wi-Fi WebSocket on port 8002, or USB serial). Same job as TOOCAA's
"TOOCAA Connect" browser extension, without an extension. Also serves the web app itself, so it works
fully offline at http://localhost:8765/.

Why it exists: a page served over https (GitHub Pages) may not open
ws://192.168.x.x (mixed content), but it may open ws://localhost. USB works
straight from the browser (Web Serial) and does not need this bridge.

    python3 bridge.py --port 192.168.1.50             # NOVA Wi-Fi (ws://IP:8002/ws)
    python3 bridge.py --port tcp:127.0.0.1:2323       # the simulator
    python3 bridge.py --port COM5                     # USB via Python

The bridge relays raw bytes; the safety gate lives in the client (web app /
nova.py). It only listens on loopback and only accepts WebSocket connections
from allowed Origins, so an arbitrary website open in your browser cannot
drive the laser through it. One client at a time.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import os
import socket
import struct
import sys
import threading

from nova import DEFAULT_BAUD, open_transport

HERE = os.path.dirname(os.path.abspath(__file__))
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_ORIGINS = ["https://chuck3cz.github.io"]


def read_request(conn: socket.socket) -> tuple[str, dict]:
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk or len(data) > 16384:
            raise ConnectionError("bad request")
        data += chunk
    head = data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    lines = head.split("\r\n")
    headers = {}
    for l in lines[1:]:
        k, _, v = l.partition(":")
        headers[k.strip().lower()] = v.strip()
    return lines[0], headers


def send_http(conn, code: str, body: bytes, ctype="text/plain; charset=utf-8"):
    conn.sendall(("HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\n"
                  "Cache-Control: no-store\r\nConnection: close\r\n\r\n"
                  % (code, ctype, len(body))).encode() + body)


def ws_recv(conn: socket.socket) -> tuple[int, bytes]:
    def exact(n):
        buf = b""
        while len(buf) < n:
            c = conn.recv(n - len(buf))
            if not c:
                raise ConnectionError("ws closed")
            buf += c
        return buf
    b0, b1 = exact(2)
    op, masked, ln = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", exact(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", exact(8))[0]
    if ln > 1 << 20:
        raise ConnectionError("frame too large")
    mask = exact(4) if masked else b"\0\0\0\0"
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(exact(ln)))
    return op, payload


def ws_send(conn: socket.socket, payload: bytes, op: int = 2, lock=None):
    n = len(payload)
    if n < 126:
        hdr = struct.pack(">BB", 0x80 | op, n)
    elif n < 65536:
        hdr = struct.pack(">BBH", 0x80 | op, 126, n)
    else:
        hdr = struct.pack(">BBQ", 0x80 | op, 127, n)
    if lock:
        with lock:
            conn.sendall(hdr + payload)
    else:
        conn.sendall(hdr + payload)


class Bridge:
    def __init__(self, port: str, baud: int, origins: list[str], verbose=False):
        self.port, self.baud, self.origins = port, baud, origins
        self.busy = threading.Lock()
        self.verbose = verbose

    def handle(self, conn: socket.socket, addr):
        try:
            reqline, h = read_request(conn)
            method, path, _ = (reqline.split(" ") + ["", ""])[:3]
            if h.get("upgrade", "").lower() == "websocket":
                return self.websocket(conn, h)
            if method == "GET" and path.split("?")[0] in ("/", "/index.html"):
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    return send_http(conn, "200 OK", f.read(), "text/html; charset=utf-8")
            send_http(conn, "404 Not Found", b"not found\n")
        except (OSError, ConnectionError) as e:
            if self.verbose:
                print("client %s: %s" % (addr, e), file=sys.stderr)
        finally:
            conn.close()

    def websocket(self, conn, h):
        origin = h.get("origin", "")
        if origin not in self.origins:
            print("rejected WebSocket from origin %r (allow with --origin)" % origin, file=sys.stderr)
            return send_http(conn, "403 Forbidden", b"origin not allowed\n")
        if not self.busy.acquire(blocking=False):
            return send_http(conn, "409 Conflict", b"another client is connected\n")
        try:
            try:
                tr = open_transport(self.port, self.baud)
            except Exception as e:
                return send_http(conn, "502 Bad Gateway", ("cannot open %s: %s\n" % (self.port, e)).encode())
            accept = base64.b64encode(hashlib.sha1(
                h.get("sec-websocket-key", "").encode() + WS_GUID).digest()).decode()
            conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          "Connection: Upgrade\r\nSec-WebSocket-Accept: %s\r\n\r\n" % accept).encode())
            print("client %s connected -> %s" % (origin, tr))
            wlock = threading.Lock()
            done = threading.Event()

            def pump():
                try:
                    while not done.is_set():
                        data = tr.read()
                        if data:
                            ws_send(conn, data, 2, wlock)
                except Exception as e:
                    if not done.is_set():
                        try:
                            ws_send(conn, ("\r\n[bridge] machine connection lost: %s\r\n" % e).encode(), 2, wlock)
                            ws_send(conn, b"", 8, wlock)
                        except OSError:
                            pass
                done.set()

            threading.Thread(target=pump, daemon=True).start()
            try:
                while not done.is_set():
                    op, payload = ws_recv(conn)
                    if op == 8:
                        break
                    if op == 9:
                        ws_send(conn, payload, 10, wlock)
                    elif op in (1, 2) and payload:
                        tr.write(payload)
            except (OSError, ConnectionError):
                pass
            finally:
                done.set()
                tr.close()
                print("client disconnected")
        finally:
            self.busy.release()


def main(argv=None):
    ap = argparse.ArgumentParser(description="ToocaaLaser localhost bridge")
    ap.add_argument("--port", required=True, help="NOVA IP / tcp:HOST:PORT / COM5 / /dev/ttyUSB0")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--listen", type=int, default=8765, help="local port (default 8765)")
    ap.add_argument("--origin", action="append", default=[],
                    help="extra allowed page origin (repeatable), e.g. null for file://")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    origins = DEFAULT_ORIGINS + ["http://localhost:%d" % a.listen,
                                 "http://127.0.0.1:%d" % a.listen] + a.origin
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", a.listen))
    srv.listen(8)
    b = Bridge(a.port, a.baud, origins, a.verbose)
    print("ToocaaLaser bridge: http://localhost:%d/  ->  %s" % (a.listen, a.port))
    print("allowed origins: %s" % ", ".join(origins))
    try:
        while True:
            conn, addr = srv.accept()
            threading.Thread(target=b.handle, args=(conn, addr), daemon=True).start()
    except KeyboardInterrupt:
        pass
    finally:
        srv.close()


if __name__ == "__main__":
    main()
