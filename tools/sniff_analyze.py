#!/usr/bin/env python3
"""
Summarise a TOOCAA Studio capture made with studio-sniffer.js.

    python3 sniff_analyze.py studio-capture-*.txt            # report
    python3 sniff_analyze.py capture.txt --gcode out.gcode   # extract streamed G-code

Report: duration and counts, every distinct command type Studio sent (FF xx,
$-commands, M/G codes, real-time bytes) with the replies that followed it,
status-report fields, the markers you set, and lines nobody recognises.
TOOCAA `FF F1` / `FF F2` replies are decoded from base64 JSON (serial numbers
masked), and the NOVA's extra `ok` after a `?` status report is flagged.
"""
from __future__ import annotations

import argparse
import base64
import collections
import json
import re
import sys

LINE = re.compile(r"^\s*([\d.]+)s (TX|RX|WS|HTTP|NOTE|MARK)\s+(.*)$")


def parse(path):
    out = []
    for raw in open(path, encoding="utf-8", errors="replace"):
        m = LINE.match(raw.rstrip("\n"))
        if m:
            out.append((float(m.group(1)), m.group(2), m.group(3)))
    return out


def decode_ff(line: str):
    """`FF F1 <b64>` / `FF F2 <b64>` -> dict with serial numbers masked, else None."""
    parts = line.split(None, 2)
    if len(parts) < 3 or parts[0] != "FF" or parts[1] not in ("F1", "F2"):
        return None
    try:
        obj = json.loads(base64.b64decode(parts[2]).decode("utf-8"))
    except ValueError:
        return None

    def mask(o):
        if isinstance(o, dict):
            return {k: (str(v)[:6] + "…" if k == "SN" and v else mask(v)) for k, v in o.items()}
        return [mask(v) for v in o] if isinstance(o, list) else o
    return mask(obj)


def kind(cmd: str) -> str:
    c = cmd.strip()
    if re.match(r"^\\x[0-9a-f]{2}", c):
        return "BYTE " + re.sub(r"\s.*", "", c)
    if "⟨real-time⟩" in c:
        return "RT " + c.split()[0]
    if c.startswith("FF "):
        return " ".join(c.split()[:2])
    if c.startswith("$"):
        return re.split(r"[=\s]", c, maxsplit=1)[0] + ("=" if "=" in c else "")
    if c.startswith("["):
        return c.split("]")[0] + "]"
    words = re.findall(r"([GM])(\d+(?:\.\d+)?)", c.upper())
    if words:
        return " ".join(sorted({a + b for a, b in words}))
    if re.match(r"^[XYZSF][-\d.]", c.upper()):
        return "(modal move)"
    return "?? " + c[:30]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture")
    ap.add_argument("--gcode", help="write every TX line that looks like G-code to this file")
    ap.add_argument("--window", type=float, default=0.6, help="seconds of RX shown after each command")
    a = ap.parse_args(argv)
    ev = parse(a.capture)
    if not ev:
        print("no events found – is this a studio-sniffer capture?")
        return 1
    tx = [(t, s) for t, d, s in ev if d == "TX" or (d == "WS" and s.startswith("WS> "))]
    tx = [(t, s[4:] if s.startswith("WS> ") else s) for t, s in tx]
    rx = [(t, s[4:] if s.startswith("WS< ") else s) for t, d, s in ev if d == "RX" or (d == "WS" and s.startswith("WS< "))]
    print(f"# {a.capture}: {ev[-1][0] - ev[0][0]:.1f} s, {len(tx)} sent, {len(rx)} received\n")

    print("## markers")
    for t, d, s in ev:
        if d in ("MARK", "NOTE"):
            print(f"  {t:9.3f}s {d} {s}")

    groups = collections.OrderedDict()
    real = [(t, s) for t, s in tx if kind(s) not in ("RT ?", "FF F2")]   # commands, not polling
    nxt = {id(item): (real[i + 1][0] if i + 1 < len(real) else float("inf")) for i, item in enumerate(real)}
    for item in real:
        groups.setdefault(kind(item[1]), []).append(item)
    polls = collections.Counter(kind(s) for _, s in tx if kind(s) in ("RT ?", "FF F2"))
    print("\n## command types sent (count, first example, replies within the window)")
    for k, n in polls.items():
        print(f"\n{k:14} ×{n}  (status polling)")
    for k, items in sorted(groups.items(), key=lambda kv: kv[1][0][0]):
        t0, ex = items[0]
        t1 = min(nxt[id(items[0])], t0 + a.window)
        replies = [s for t, s in rx if t0 <= t < t1 and not s.startswith("<")][:6]
        print(f"\n{k:14} ×{len(items)}  first at {t0:.3f}s: {ex[:110]}")
        for r in replies:
            print(f"{'':16}→ {r[:110]}")

    print("\n## status report formats")
    fields = collections.Counter()
    states = collections.Counter()
    for _, s in rx:
        if s.startswith("<") and s.endswith(">"):
            parts = s[1:-1].split("|")
            states[parts[0].split(":")[0]] += 1
            for p in parts[1:]:
                fields[p.split(":")[0]] += 1
    print("  states:", dict(states))
    print("  fields:", dict(fields))
    axes = {len(s.split("MPos:")[1].split("|")[0].split(",")) for _, s in rx if "MPos:" in s}
    if axes:
        print("  MPos axes:", sorted(axes))
    rxl = [s for _, s in rx]
    after = [rxl[i + 1] for i, s in enumerate(rxl[:-1]) if s.startswith("<") and s.endswith(">")]
    if after:
        n_ok = sum(1 for s in after if s == "ok")
        print(f"  line after a status report is `ok` in {n_ok}/{len(after)} cases"
              + ("  ← NOVA quirk: `?` gets an extra ok" if n_ok and n_ok == len(after) else ""))

    decoded = [(t, s) for t, s in rx if decode_ff(s) is not None]
    if decoded:
        print("\n## TOOCAA FF F1 / FF F2 payloads (base64 JSON, decoded)")
        last = {}
        for t, s in decoded:
            last[s.split()[1]] = (t, decode_ff(s))
        for k, (t, obj) in sorted(last.items()):
            print(f"  FF {k} (last, {t:.3f}s):")
            for ln in json.dumps(obj, indent=2, ensure_ascii=False).splitlines():
                print("    " + ln)

    print("\n## replies Studio got that are not ok / status (distinct)")
    seen = collections.Counter((s[:6] + "<base64 JSON, decoded above>") if decode_ff(s) is not None else s
                               for _, s in rx if s != "ok" and not s.startswith("<"))
    for s, n in seen.most_common(60):
        print(f"  ×{n:<4} {s[:120]}")

    unknown = [(t, s) for t, s in tx if kind(s).startswith("??")]
    if unknown:
        print("\n## unrecognised TX lines")
        for t, s in unknown[:40]:
            print(f"  {t:9.3f}s {s[:120]}")

    if a.gcode:
        g = [s for _, s in tx if re.match(r"^\s*([GMSXYZF]\d|[GM]\d)", s.upper()) or s.startswith("FF S")]
        open(a.gcode, "w").write("\n".join(g) + "\n")
        print(f"\nwrote {len(g)} G-code lines to {a.gcode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
