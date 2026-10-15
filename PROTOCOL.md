# TOOCAA NOVA – protocol notes

What's known about how software talks to the NOVA, and where each fact
comes from. Everything here was read from public, official TOOCAA material
(the LightBurn profile, the support wiki, the TOOCAA Studio web app and its
TOOCAA Connect browser extension). Nothing was sent to a machine.

Status: **📄 documented / found in official software**, not yet tested on a
real NOVA unless marked ✅. Section 7 was recorded on a real NOVA
(firmware V1.0.2) on 2026-10-13. The raw capture is in
[`tools/captures/`](tools/captures/), with serial numbers masked.

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
ToocaaLaser. ✅ The USB bridge is a **CH340** (VID `0x1A86`, PID `0x7523`).

Studio streams G-code with Grbl character counting and a **512 B** RX
buffer (`RX_BUFFER_SIZE = 512`). Every `ok` frees the oldest line. Over USB
Studio **doesn't poll `?`**: it asks `FF F2` before a job and otherwise
reacts to messages the NOVA sends on its own.

## 4. Wi-Fi (TOOCAA Studio + TOOCAA Connect)

- An https page can't reach the LAN, so Studio needs the
  [TOOCAA Connect](https://wiki.toocaa.com/toocaaconnect-v0.5.crx) extension
  (MV3, ID `pmfbjhppbodfpmffhmjoajbhekeoelbm`). It's a generic proxy: it
  relays `fetch` (http-fetch), WebSocket (ws-open/send/close) and chunked
  uploads for pages on `*.toocaastudio.com`. ToocaaLaser's `bridge.py` does
  the same job.
- **Connection sequence (Studio):** `GET http://IP:8002/info` (is it there?),
  `GET /v1/ipComfirm` (the NOVA shows **Confirm** on its touchscreen and
  answers `{"code":200}` once you tap it), `GET /v1/deviceInfo`, then the
  WebSocket. ToocaaLaser (extension link, `nova.py`, `bridge.py`) does the same.
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
| Z home (Grbl) | `$HZ` |
| Z jog / material thickness | `G91 G0 Z<d>` / `G90 G0 Z<35-t>` (\|d\|, t ≤ 35 mm) |
| autofocus on rotary | `FF FC X<x> A<a> Y<y>` |
| Wi-Fi scan | `FF F3`, reply base64 JSON `[{ssid, passwordRequired}]` |
| device name | `FF CN <name>`, reply `FF CN 200` / `FF CN 500` |
| consumables | `FF CW <code> <value>`: `C3/C4` lens, `Z3/Z4` filter, `G3/G4` purifier, `FM3/FM4` focus module, `CF3/CF4` fan, `GR3/GR4` rails, `CA3/CA4` camera (`…3 0` notify, `…4 0` ignore, `…4 1` reset counter) |

Replies: `FF S3 200` home finished, `FF S4 200` paused, `FF S5 200` resumed,
`FF S6 200` stopped, `FF S9 200` job finished, `FF AL` alarm, `FF LD` lid,
`FF ER` exception, `FF RP` rotary, `FF CT …` camera type.
`FF FC 200 <mm>` / `FF FD 200 <mm>` = autofocus finished, measured material
thickness.

`FF ER n` (Studio's handler): 0 command not recognised (old firmware),
1 device still initialising, 2 busy, 3 child lock, 4 job file can't be
parsed, 5 job file missing, 6 camera in use, 98 Z motor reset failed.

Before a job Studio sends `FF F2` and only starts when `grblState == 0` and
`inetractMode` isn't 4 (local file mode on the touchscreen) or 5 (child
lock).

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

## 7. ✅ Recorded on a real NOVA (USB, firmware V1.0.2, 2026-10-13)

How it was recorded: TOOCAA Studio in Chrome, with a read-only hook on Web
Serial (the same as `tools/studio-sniffer.js`). After Studio connected, a
few read-only queries were sent by hand. Nothing moved and the laser was off.

**What Studio sends on connect**

```
TX FF F1            device info            → FF F1 <base64 JSON>
TX FF R8            camera calibration     → FF R8 NOVA_CALIB_V1 method=none scheme=hg_gcode …
TX FF F2            status                 → FF F2 <base64 JSON>
TX $X               unlock                 → ok
TX \xc2\xa3         TOOCAA L2 "firmware"   (`£` = 0xA3, but sent UTF-8 encoded – a Studio leftover)
TX \xa5             TOOCAA L2 "laser power" (hex command)  → ok
```

The last two bytes are Studio code for the older TOOCAA **L2**. The NOVA
answers them, but they're not needed.

**Differences from stock Grbl. These matter for every client:**

| | NOVA | Stock Grbl 1.1 |
|---|---|---|
| `?` | `<Idle\|…>` **followed by `ok`** | report only |
| `FF xx` | one `FF xx …` reply line, **no `ok`** | – |
| `$G` | `error:3` | `[GC:…]` |
| `$#` | `error:1` | offsets |
| `$J=` jogging | `error:3` (Studio has no X/Y jog either, only `G91 G0 Z…`) | jog |
| status report | 4 axes `MPos:x,y,z,a` (A = rotary), `Pn:XY` when idle | 3 axes |
| lid | `FF LD 1` / `FF LD 0` sent unprompted when the lid opens or closes | – |

Without `$J=`, ToocaaLaser moves the head with `G91 G21 G1 X… Y… F… S0` and then
`G90`. The beam stays off: laser mode `$32=1` doesn't fire at S0. With `$20=0`
there are no soft limits, so the client checks the target against the bed
from a fresh `?` report and only sends the move when the machine is `Idle`.

The extra `ok` after `?` is the dangerous one. A client that polls `?` while
streaming with character counting takes that `ok` as an acknowledgement of a
job line and overflows the RX buffer. `nova.py` and `index.html` detect it
on the first status request and drop that `ok`.

**`$I`**: `[VER:1.3a.202506201830:]`, `[OPT:PHBW]`,
`[MSG:Using machine:ESP32_V4]`.

**`$$`** (selection): `$22=0` homing in the Grbl sense is off, but `$H`
still works through TOOCAA's own homing; `$20=0` no soft limits; `$30=1000`
gives S 0–1000; `$32=1` laser mode (the line is printed twice); steps/mm
`$100–$102=100`, `$103=45` (A); max rate X/Y `36000` mm/min; acceleration
X/Y `5000` mm/s²; max travel `$130=300`, `$131=220` (the work area is still
297 × 210). The full dump is in the capture and in `grbl_sim.py`.

**`FF F1` – device info** (base64 JSON):

```json
{"data": {
  "toocaaNova": {"SN": "TCNVBA…", "version": "V1.0.2", "hardware_version": "v0.0.0", "manufactureDate": "2025-12-15"},
  "laser":      {"SN": "TCNVFA…", "power": 20, "version": "0.9.2", "workTime": 0.57, "isOnline": true, …},
  "airAssist":  {"SN": "TCNVHA…", "version": "0.9.4", "isOnline": true, …},
  "rotary":     {"isOnline": true}}}
```

**`FF F2` – status** (base64 JSON):

```json
{"airPumpState": 1, "alarm": "NONE", "coverPlateState": 0, "grblState": 0,
 "inetractMode": 0, "laserTemperature": 28.0, "rotationPartState": 1,
 "usbConnectState": 1, "workState": 0,
 "consumables": {"laserLens": {"wearTime": 1265}, "machineFilter": {"wearTime": 1265},
                 "smokePurifier": {"added": false, "wearTime": 0}}}
```

`wearTime` is in seconds. Studio compares it with `cycleHours`: lens and
filter 24 h, purifier 2160 h, focus module 480 h, fan and camera 30 h,
rails 60 h. `inetractMode` was 0 right after Studio connected and 2 after
commands were typed by hand. What 2 means isn't known yet; Studio only
checks 1, 4 and 5.

## 8. 📄 Rotary and camera (TOOCAA Studio code, 2026-10-15)

**Accessories.** `FF F1` → `data.rotary.isOnline` and `laser.power` /
`airAssist.isOnline`. When the rotary is online Studio switches to
`isRotaryMode: true, rotaryMode: "chuck"`. Plugging the rotary in or out is
reported unprompted as `FF RP 1` / `FF RP 0`. Studio offers two modes:
"Flat surface" and "Chuck rotary".

**Rotary geometry** (graphics-engine `$d`, `cO`, `pO`):

| | |
|---|---|
| design area | 190 mm × π·Ø (Ø is entered by the user, default 70 mm) |
| Y → A (chuck) | `A = y / (π·Ø) · 360` degrees (sign +1). Roller: `A = −y / (16π/32)` |
| head Y during the job | `pO(Ø) = Ø ≥ 75 ? 210 : 210 + 5 − (75 − Ø)` |
| job header | `G00 G17 G40 G21 G54`, `G90`, `M4`, `G0 X<minX>`, `G0 Y<pO>` |
| vectors | `G0 X.. A.. F..`, `G1 X.. A.. S<power·10> F..` (absolute A) |
| fill | `G90 G0 X.. A..`, then `G91` relative `G1 X.. A.. S..` |
| job end | `M200 X1000 Y1000`, `M9`, `G1 S0`, `M5`, `G90`, `G0 X0Y210`, `M2` |
| framing | `FF BD X<x0> A<a0> X<x1> A<a1> D<Ø>` (flat: `FF BD X Y X Y`) |
| autofocus | `FF FC X<x> A<a> Y<pO>` |

Other things Studio puts in every NOVA job: `; air 1|2|3` before each layer
(3 for cutting or speed < 3000, 1 for power < 50 %, otherwise 2),
`M200 X<a> Y<a>` acceleration (1250 for line and cut, 2000 for fill), and
`M8` before every vector path.

**USB job envelope** (`execute` → `sO`): `FF S0 <base64 name> <time s> [IsRotate]`,
`FF S1 <thumbnail chunk>` … `FF S2 <last chunk>`, then **every G-code line
as `FF S3 <line>`** (character-counted like normal G-code), then `FF S9`.
Over Wi-Fi the same job is a file in `POST /v1/uploadFile` (`isRotate`).
Not recorded yet whether the NOVA also accepts plain G-code for rotary.
ToocaaLaser streams plain G-code with A words for now.

**Camera.** A USB UVC webcam in the lid, `0c45:6368` or `1bcf:28c4`, label
containing "TOOCAA". Studio opens it with `getUserMedia` at 2592 × 1944 and
fixes barrel distortion with a WebGL shader, using one coefficient
`k = −0.12`:

```
rsq = (u−½)² + ((v−½)/ratio)²,  lim = (¼ + (½/ratio)²) / (2·ratio)
scale = k ≥ 0 ? 1 + k·lim : 1 / (1 − k·lim)
src = ½ + (uv − ½) · (1 + k·rsq) / scale
```

It then corrects perspective from 4 user-clicked points (glfx `perspective`),
crops to the work area and places the result under the design.
`FF R0`–`FF R9` are the automatic calibration, used over Wi-Fi with
`FF PT` photos. ToocaaLaser does the same in `index.html`: lens correction
on the CPU and a homography from 4 clicked points, stored in `localStorage`.

## Still to verify on the machine

1. `python3 nova.py probe <IP>`: port 8002 open, what `/v1/deviceInfo` returns.
2. `python3 nova.py -p <IP> info`: Grbl `?`, `$I` and `$$` over the WebSocket.
   ✅ Done over USB, see section 7. Wi-Fi still open: does `?` get the extra `ok` there too?
3. `G38.2` vs. `FF FC X.. Y..` autofocus over USB and Wi-Fi.
4. Origin direction: jog `Y+` and watch which way the head moves.
5. Crosshair / red-dot pointer command (not found yet).
6. A real job over USB: framing (`FF BD`), `FF S2 GCode` … `FF S9`, and what
   the NOVA replies while it runs.
7. What `inetractMode` 1 and 2 mean.
8. Rotary job over USB: does plain G-code with `A` work, or does the NOVA need
   the `FF S0 … IsRotate` / `FF S3` envelope? Is the axis line really at `pO(Ø)`?
