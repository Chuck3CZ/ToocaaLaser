# ToocaaLaser – local control for the TOOCAA NOVA

*🇬🇧 English · [🇨🇿 Čeština](README.cs.md)*

An open alternative to TOOCAA Studio for the **TOOCAA NOVA** laser engraver
(10 W / 20 W / 2 W IR, 297 × 210 mm). It talks to the machine's controller
directly over **USB** or your **local Wi-Fi**. No TOOCAA account and no cloud
servers.

**▶ Web app:** <https://chuck3cz.github.io/ToocaaLaser/> (Chrome/Edge). Open it
and pick *Simulator* to try it without the machine.

> ⚠️ Unofficial project, not affiliated with TOOCAA. Lasers burn things and
> eyes. Use it only with the lid closed, extraction running and someone
> watching the machine. Use at your own risk.

## What the protocol is based on

You don't need TOOCAA's firmware image. The controller identifies itself on the
USB console (LightBurn, firmware 1.0.0):

```
[VER:1.3a.202506201830:]
[OPT:PHBW]
[MSG:Using machine:ESP32_V4]
```

This is the build-info format of **[Grbl_ESP32](https://github.com/bdring/Grbl_Esp32)**
version 1.3a (OPT: P = parking, H = single-axis homing, B = Bluetooth,
W = Wi-Fi). It means:

| Channel | Protocol | Status |
|---|---|---|
| USB (COM port) | Grbl 1.1 line protocol, 115200 Bd | ✅ confirmed (LightBurn uses it) |
| Wi-Fi Telnet, TCP 23 | same stream as USB | ⚠ stock Grbl_ESP32 – verify with `probe` |
| Wi-Fi HTTP 80 / WebSocket 81 | ESP3D-style web UI | ⚠ stock Grbl_ESP32 – verify with `probe` |
| Autofocus, crosshair, air assist | TOOCAA macros (G-code) | ❓ will be in `toocaa_nova.lbdev` |
| Camera | TOOCAA Studio only | ❓ unknown, investigate later |

So the core is the standard, public, documented Grbl protocol, not something
proprietary. Only TOOCAA's extras (autofocus, camera, air pump) still need
confirming. The `.lbdev` audit and `probe` are there to answer exactly that.

## What's in here

| File | What it is |
|---|---|
| [`index.html`](index.html) | **The web app** (single file, no build step). USB over Web Serial, Wi-Fi via the bridge, built-in simulator. SVG → G-code, toolpath preview, check mode, frame, job streaming, jog, console, `.lbdev` audit. CZ/EN. |
| [`nova.py`](nova.py) | Python library + CLI: `probe`, `info`, `status`, `jog`, `home`, `unlock`, `check`, `run`, `hold`, `resume`, `stop` |
| [`bridge.py`](bridge.py) | Localhost bridge (WebSocket ↔ NOVA over Wi-Fi/USB) that also serves the web app offline |
| [`lbdev_audit.py`](lbdev_audit.py) | **Read-only** audit of `toocaa_nova.lbdev` (JSON/XML; finds macros including base64-encoded ones) |
| [`grbl_sim.py`](grbl_sim.py) | NOVA simulator (Grbl_ESP32 1.3a, A4 bed, soft limits, check mode, alarms) |
| [`test_toocaa_laser.py`](test_toocaa_laser.py) | End-to-end tests against the simulator (`python3 -m unittest -v`) |

Python 3.9+ with the standard library only. USB from Python needs `pip install pyserial`.

## Recommended order (start read-only)

1. **Audit the LightBurn profile.** No connection needed:
   `python3 lbdev_audit.py toocaa_nova.lbdev`, or in the web app under
   *LightBurn profile audit*. You get the device type, baud rate, bed size and
   the **autofocus/air assist/crosshair macros**.
2. **Probe the Wi-Fi services.** This sends no machine command:
   `python3 nova.py probe <NOVA IP>`
3. **Read the identity and settings.** Read-only:
   `python3 nova.py -p COM5 info` (USB) or `-p <NOVA IP>` (telnet)
4. **Check mode.** Grbl `$C` parses the whole job without moving:
   `python3 nova.py -p COM5 check job.gcode`
5. **Jog with the laser off:** `python3 nova.py -p COM5 --allow-motion jog X10 Y10`
6. Only then a **job with the laser**: `--allow-motion --allow-laser run job.gcode`

## Web app – connection options

- **USB:** Chrome/Edge on a desktop computer, *Connect*, then pick the NOVA's port.
  Everything runs inside the browser.
- **Wi-Fi:** run `python3 bridge.py --port <NOVA IP>` on your computer and pick
  *Wi-Fi via local bridge* in the app. An https page (GitHub Pages) can't open
  `ws://192.168.x.x` directly, but it can open `ws://localhost`. The bridge also
  serves the app at <http://localhost:8765/>, so it works completely offline.
- **Simulator:** no machine needed.

## Safety model

These rules hold in both the web app and `nova.py`. The machine's own
protections are never bypassed.

- **Read-only by default.** Only query commands are sent (`?`, `$I`, `$$`, `$G`…).
- **Motion** (`$J=` jog, `$H`, `$X`, frame, job) needs *Allow motion*. A jog
  never fires the laser.
- A **job that turns the beam on** (M3/M4 with S>0) also needs *Allow laser*.
  It is refused when `$32` (laser mode) ≠ 1 or when the design leaves the
  297 × 210 mm area.
- **Settings are never sent** (`$n=`, `$RST`, `$NVX`, `[ESPxxx]`, `$Wifi/...=`).
- Streaming sends one line at a time and stops on any `error`, `ALARM`, `Door`
  or unexpected `Hold`. **STOP / Esc / Ctrl-C** sends feed hold + soft reset,
  and Grbl switches the laser off immediately.
- Connecting sends no reset, so a running job isn't interrupted.
- The bridge listens on 127.0.0.1 only and accepts WebSocket connections only
  from allowed Origins, so another website can't control the laser through it.
- Software STOP **does not replace** the lid interlock or the power switch.

## Keeping the NOVA off the cloud

TOOCAA Studio is a web app on TOOCAA's servers. This one only needs your
computer and the machine.

1. Give the NOVA a fixed IP on your router (DHCP reservation).
2. Add a firewall rule: **block WAN for that IP/MAC** and allow the LAN. Local
   USB and Wi-Fi control keep working.
3. TOOCAA delivers firmware updates **only over Wi-Fi through Studio**. If you
   ever want one, lift the block for that one update.

## Roadmap

- [x] Grbl_ESP32 client (USB/telnet), simulator, tests
- [x] Web app: SVG → G-code, preview, check, frame, run, jog, console
- [x] `.lbdev` audit (CLI + browser)
- [ ] Verify Wi-Fi services on a real NOVA (`probe`)
- [ ] TOOCAA macros (autofocus, crosshair, air pump) from the `.lbdev`
- [ ] Raster engraving of bitmaps (dithering, scanline)
- [ ] Material library, layers, projects (SQLite/IndexedDB)
- [ ] Camera: stream, calibration, placing designs on a photo of the bed

## Sources

- Console output from a NOVA after the 1.0.0 update: [hagensieker.com – Toocaa NOVA review](https://hagensieker.com/2026/08/14/toocaa-nova-smart-home-laser-engraver-review/)
- NOVA specs and connectivity: [The Gadgeteer – TOOCAA NOVA](https://the-gadgeteer.com/2025/12/13/toocaa-nova-laser-engraver-just-right/)
- Grbl 1.1 protocol: [gnea/grbl wiki – Interface](https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface)
- Grbl_ESP32: [bdring/Grbl_Esp32](https://github.com/bdring/Grbl_Esp32)

## License

MIT. TOOCAA is a trademark of its owner.
