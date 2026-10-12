#!/usr/bin/env python3
"""
Read-only audit of a LightBurn device profile (`toocaa_nova.lbdev`).

The .lbdev is not an SDK – it is LightBurn's export of one device: controller
type, baud rate, bed size, origin, power scaling, start/end G-code and the
custom macro buttons (autofocus, air assist, crosshair...). This script never
connects to anything; it only reads the file and reports:

  * the format (JSON / XML / unknown) and every key path with its value,
  * highlighted machine facts (device type, baud, width/height, max power...),
  * every embedded G-code/command snippet – including base64-encoded ones –
    which is where TOOCAA's proprietary macros will show up.

    python3 lbdev_audit.py toocaa_nova.lbdev            # human report
    python3 lbdev_audit.py toocaa_nova.lbdev --json     # machine-readable
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
import xml.etree.ElementTree as ET

INTERESTING = re.compile(
    r"(type|driver|baud|width|height|size|origin|home|power|speed|rate|"
    r"macro|button|gcode|start|end|cmd|command|focus|air|cross|laser|"
    r"pointer|frame|port|ip|address|host|url|version|name|mirror|label|content)", re.I)
GCODE_LINE = re.compile(
    r"^\s*(\$[A-Za-z0-9#/=]|\[ESP\d+\]|[GM]\d+(\.\d+)?\b|[SFXYZ][-+]?\d|"
    r"T\d+\b|%|\?|!|~)", re.M)
SECRET = re.compile(r"(pass|psk|token|secret|key)", re.I)


def load(data: bytes):
    text = data.decode("utf-8-sig", "replace")
    try:
        return "json", json.loads(text)
    except ValueError:
        pass
    try:
        return "xml", xml_to_tree(ET.fromstring(text))
    except ET.ParseError:
        pass
    return "unknown", {"strings": re.findall(r"[\x20-\x7e]{4,}", data.decode("latin-1"))}


def xml_to_tree(el):
    node = dict(el.attrib)
    for child in el:
        node.setdefault(child.tag, []).append(xml_to_tree(child))
    if el.text and el.text.strip():
        node["#text"] = el.text.strip()
    return {el.tag: node}


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, "%s.%s" % (path, k) if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, "%s[%d]" % (path, i))
    else:
        yield path, obj


def maybe_b64(s: str) -> str | None:
    if len(s) < 8 or len(s) % 4 or not re.fullmatch(r"[A-Za-z0-9+/=\s]+", s):
        return None
    try:
        raw = base64.b64decode(s, validate=False)
    except (binascii.Error, ValueError):
        return None
    try:
        dec = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if sum(c.isprintable() or c in "\r\n\t" for c in dec) < 0.95 * len(dec):
        return None
    return dec


def audit(data: bytes) -> dict:
    fmt, tree = load(data)
    entries, facts, snippets = [], [], []
    for path, val in walk(tree):
        sval = "" if val is None else str(val)
        if SECRET.search(path.rsplit(".", 1)[-1]) and sval:
            sval = "******"
        entries.append({"path": path, "value": sval})
        leaf = re.sub(r"\[\d+\]", "", path.rsplit(".", 1)[-1])
        if INTERESTING.search(leaf) and len(sval) < 200 and "\n" not in sval:
            facts.append({"path": path, "value": sval})
        for source, text in (("plain", sval), ("base64", maybe_b64(sval) or "")):
            if text and GCODE_LINE.search(text):
                lines = [l.strip() for l in re.split(r"[\r\n]+|\\n", text) if l.strip()]
                snippets.append({"path": path, "encoding": source, "lines": lines})
    return {"format": fmt, "entries": entries, "facts": facts, "gcode": snippets}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="read-only audit of a LightBurn .lbdev")
    ap.add_argument("file")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="list every key, not just facts")
    a = ap.parse_args(argv)
    with open(a.file, "rb") as f:
        rep = audit(f.read())
    if a.json:
        json.dump(rep, sys.stdout, indent=2, ensure_ascii=False)
        print()
        return 0
    print("format: %s, %d values" % (rep["format"], len(rep["entries"])))
    print("\n== machine facts ==")
    for e in rep["facts"]:
        print("  %-50s %s" % (e["path"], e["value"]))
    print("\n== embedded G-code / macros ==")
    if not rep["gcode"]:
        print("  (none found)")
    for s in rep["gcode"]:
        print("  %s%s" % (s["path"], " (base64)" if s["encoding"] == "base64" else ""))
        for l in s["lines"]:
            print("      " + l)
    if a.all:
        print("\n== all values ==")
        for e in rep["entries"]:
            print("  %-50s %s" % (e["path"], e["value"][:120]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
