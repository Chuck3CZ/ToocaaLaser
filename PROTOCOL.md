# TOOCAA NOVA – protocol notes

What's known about how software talks to the NOVA, and where each fact
comes from. Everything here was read from public, official TOOCAA material
(the LightBurn profile, the support wiki, the TOOCAA Studio web app and its
TOOCAA Connect browser extension). Nothing was sent to a machine.

Status: **📄 documented / found in official software**, not yet tested on a
real NOVA unless marked ✅.

## 1. Controller: Grbl_ESP32 1.3a

Console banner (LightBurn, firmware 1.0.0, [review](https://hagensieker.com/2026/08/14/toocaa-nova-smart-home-laser-engraver-review/)):

```
[VER:1.3a.202506201830:]
[OPT:PHBW]
[MSG:Using machine:ESP32_V4]
```

Standard Grbl 1.1 line protocol: `ok`, `error:N`, `ALARM:N`, `<State|MPos:…>`,
and the real-time bytes `?` `!` `~` `Ctrl-X`.

## 2. Official LightBurn profile – `toocaa_nova.lbdev`

Download: <https://wiki.toocaa.com/toocaa_nova.lbdev>, linked from the wiki
page [ConnectLightBurn](https://wiki.toocaa.com/en/toocaa-nova/ConnectLightBurn).
JSON, 1081 B, sha256 `e87b1028c7fbe8b180a9e9881eb2f177464cf2234958c84adda4b1f434110474`.

| Key | Value | Meaning |
|---|---|---|
| `Name` / `ProfilePath` | `GRBL` | LightBurn's GRBL driver (Grbl ≥ 1.1f, M4 dynamic power) |
| `Type` | `Serial` | USB COM port only, LightBurn has no Wi-Fi path |
| `Width` × `Height` | 297 × 210 | work area, mm |
| `MirrorX` / `MirrorY` | false / true | origin **rear-left** (LightBurn "top-left") |
| `HomeOnStartup` | true | LightBurn sends `$H` on connect |
| `Macro0_Label` / `Macro0_Content` | `FOCUS` / `G38.2` | **autofocus = bare `G38.2`** (TOOCAA's firmware extension: stock Grbl needs an axis word) |

The profile contains no baud rate. LightBurn's GRBL default is 115200, the
same value TOOCAA Studio uses. Air pump and crosshair macros aren't in the
profile, even though the wiki mentions them.

## 3. USB (TOOCAA Studio)

Studio is a Chrome web app (<https://www.toocaastudio.com>). It uses
**Web Serial** with `port.open({baudRate: 115200})`, the same approach as
ToocaaLaser.

## 4. Wi-Fi (TOOCAA Studio + TOOCAA Connect)

- An https page can't reach the LAN, so Studio needs the
  [TOOCAA Connect](https://wiki.toocaa.com/toocaaconnect-v0.5.crx) extension
  (MV3, ID `pmfbjhppbodfpmffhmjoajbhekeoelbm`). It's a generic proxy: it
  relays `fetch` (http-fetch), WebSocket (ws-open/send/close) and chunked
  uploads for pages on `*.toocaastudio.com`. ToocaaLaser's `bridge.py` does
  the same job.
- **WebSocket:** `ws://<NOVA-IP>:8002/ws?clientId=<random>`. Each command is
  one text message that ends in `\n`.
- **HTTP on port 8002:** `GET /v1/deviceInfo` (accessories, details),
  `GET /v1/ipComfirm` (shows a **Confirm** prompt on the touchscreen),
  `GET /v1/downloadLog`, `POST /v1/uploadFile` (jobs and OTA firmware).
- On a new connection the NOVA asks for confirmation on its touchscreen.
- Status messages carry TOOCAA fields such as `alarm`, `inetractMode`
  (sic, 5 = child lock), `workState`, `grblState`, `coverPlateState`
  (lid), `rotationPartState`, `airPumpState` and `usbConnectState`.

## 5. TOOCAA `FF` commands (Studio, `graphics-engine` bundle)

Plain-text lines, sent like G-code.

| Purpose | Command |
|---|---|
| home / stop / pause / resume | `$H` / `Ctrl-X` / `!` / `~` |
| autofocus at a point | `FF FC X<x> Y<y>`, reply `FF FC 200 …` |
| focus (Grbl path) | `G38.2` |
| Z home / focus home | `FF FB` / `FF FD` |
| framing | `FF BD`, `FF BF` (matrix) |
| device info / status | `FF F1` / `FF F2` |
| Wi-Fi connect | `FF F0` |
| camera photo / type | `FF PT` / `FF PT -3` |
| camera calibration | `FF R0` reset, `FF R1` border focus, `FF R2` process, `FF FQ` batch focus, `FF R7`/`FF R9` write config, `FF R8` read config |
| unlock alarm | `$X` |

Replies: `FF S3 200` home finished, `FF S4 200` paused, `FF S5 200` resumed,
`FF S6 200` stopped, `FF S9 200` job finished, `FF AL` alarm, `FF LD` lid,
`FF ER` exception, `FF RP` rotary, `FF CT …` camera type.

**Job framing:** `FF S0 <name> <time> [IsRotate]`, `FF S1 <chunk>` …
`FF S2 <last chunk>`, `FF S3 <…>`, `FF S9`. Over USB Studio sends
`FF S2 GCode`, then the G-code lines, then `FF S9`. Over Wi-Fi the job goes
through `POST /v1/uploadFile`.

**Air assist:** `M8` / `M9`. Studio's own generated G-code uses them
(`M8` … `M9`, `M5`, `M2`).

## 6. Camera

In LightBurn the camera appears as **"TOOCAA NOVA Smart Camera / 2K AF
WEBCAM"** ([wiki](https://wiki.toocaa.com/en/toocaa-nova/ConnectLightBurnCamera)),
so it's a standard USB (UVC) webcam. A browser can reach it with
`getUserMedia`, no TOOCAA protocol needed. Over Wi-Fi, Studio gets photos
through `FF PT` (`wirelessPhoto`).

## Still to verify on the machine

1. `python3 nova.py probe <IP>`: port 8002 open, what `/v1/deviceInfo` returns.
2. `python3 nova.py -p <IP> info`: Grbl `?`, `$I` and `$$` over the WebSocket.
3. `G38.2` autofocus over USB and Wi-Fi.
4. Origin direction: jog `Y+` and watch which way the head moves.
5. Crosshair / red-dot pointer command (not found yet).
