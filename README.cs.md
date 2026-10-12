# ToocaaLaser – lokální ovládání TOOCAA NOVA

*[🇬🇧 English](README.md) · 🇨🇿 Čeština*

Otevřená alternativa k TOOCAA Studiu pro laser **TOOCAA NOVA** (10 W / 20 W /
2 W IR, plocha 297 × 210 mm). S řadičem stroje komunikuje přímo přes **USB**
nebo **lokální Wi-Fi**. Nepotřebuje účet TOOCAA ani jejich cloud.

**▶ Webová aplikace:** <https://chuck3cz.github.io/ToocaaLaser/> (Chrome/Edge).
Bez stroje stačí vybrat *Simulátor*.

> ⚠️ Neoficiální projekt bez vztahu k TOOCAA. Laser pálí věci i oči. Používej
> ho jen se zavřeným víkem, se zapnutým odsáváním a pod dohledem. Na vlastní
> riziko.

## Na čem je protokol postavený

Firmware TOOCAA k tomu potřeba není. Řadič se sám ohlásí na USB konzoli
(LightBurn, firmware 1.0.0):

```
[VER:1.3a.202506201830:]
[OPT:PHBW]
[MSG:Using machine:ESP32_V4]
```

To je formát build-info firmwaru **[Grbl_ESP32](https://github.com/bdring/Grbl_Esp32)**
verze 1.3a (OPT: P = parking, H = homing jednotlivých os, B = Bluetooth,
W = Wi-Fi). Z toho plyne:

| Kanál | Protokol | Stav |
|---|---|---|
| USB (COM port) | Grbl 1.1, 115200 Bd | ✅ používá ho LightBurn i TOOCAA Studio |
| Wi-Fi | WebSocket `ws://IP:8002/ws` + HTTP `/v1/…` | 📄 nalezeno v TOOCAA Studiu – ověř přes `probe` |
| Autofokus | `G38.2` (oficiální makro LightBurn „FOCUS“), Studio: `FF FC X Y` | 📄 oficiální profil |
| Vzduch | `M8` / `M9` | 📄 G-code z TOOCAA Studia |
| Počátek | vzadu vlevo (`MirrorY: true` v profilu) | 📄 oficiální profil – ověř jedním posunem |
| Kamera | běžná USB webkamera („2K AF WEBCAM“) | 📄 wiki – zatím nezapojeno |
| Zaměřovací kříž | ? | ❓ zatím nenalezeno |

Jádrem je tedy standardní, veřejně zdokumentovaný protokol Grbl. Nadstavby
TOOCAA pocházejí z oficiálního
[`toocaa_nova.lbdev`](https://wiki.toocaa.com/toocaa_nova.lbdev) a z veřejného
kódu TOOCAA Studia. Podrobnosti a zdroje: **[PROTOCOL.md](PROTOCOL.md)**.

## Co tu je

| Soubor | Co to je |
|---|---|
| [`index.html`](index.html) | **Webová aplikace** (jediný soubor, bez buildu). Editor motivů (více objektů, posun/zvětšení/otáčení myší, kopírovat/vložit/duplikovat, kopie do mřížky, zpět/znovu, text, tvary, SVG, PNG/JPG s rozptylem, gravírování obrysu i výplně, předvolby materiálů, projekty) a ovládání stroje: USB přes Web Serial, Wi-Fi přes most, simulátor, ověření, rámeček, odeslání, posun, autofokus, vzduch, konzole, audit `.lbdev`. CZ/EN. |
| [`nova.py`](nova.py) | Knihovna + CLI v Pythonu: `probe`, `info`, `status`, `jog`, `home`, `unlock`, `check`, `run`, `hold`, `resume`, `stop` |
| [`bridge.py`](bridge.py) | Lokální most (WebSocket ↔ NOVA přes Wi-Fi/USB), který zároveň servíruje aplikaci offline |
| [`PROTOCOL.md`](PROTOCOL.md) | Vše, co víme o protokolu NOVA (USB, Wi-Fi 8002, příkazy `FF`, profil), se zdroji |
| [`lbdev_audit.py`](lbdev_audit.py) | Audit souboru `toocaa_nova.lbdev` **jen pro čtení** (JSON/XML, najde i makra kódovaná v base64) |
| [`grbl_sim.py`](grbl_sim.py) | Simulátor NOVA (Grbl_ESP32 1.3a, plocha A4, soft limity, check mód, alarmy) |
| [`test_toocaa_laser.py`](test_toocaa_laser.py) | End-to-end testy proti simulátoru (`python3 -m unittest -v`) |

Stačí Python 3.9+ se standardní knihovnou. Pro USB z Pythonu je potřeba `pip install pyserial`.

## Doporučený postup (začni jen čtením)

1. **Audit profilu LightBurn.** Nic se nepřipojuje:
   `python3 lbdev_audit.py toocaa_nova.lbdev`, nebo v aplikaci v sekci
   *Audit profilu LightBurn*. Ukáže typ zařízení, baud, rozměry a **makra pro
   autofokus, vzduch a kříž**.
2. **Průzkum služeb na Wi-Fi.** Stroji se neposílá žádný příkaz:
   `python3 nova.py probe <IP NOVA>`
3. **Identita a nastavení.** Jen čtení:
   `python3 nova.py -p COM5 info` (USB) nebo `-p <IP NOVA>` (Wi-Fi, pak potvrď na displeji)
4. **Ověření úlohy.** Grbl `$C` projde celou úlohu bez pohybu:
   `python3 nova.py -p COM5 check uloha.gcode`
5. **Posun s vypnutým laserem:** `python3 nova.py -p COM5 --allow-motion jog X10 Y10`
6. Teprve potom **úloha s laserem**: `--allow-motion --allow-laser run uloha.gcode`

## Webová aplikace – připojení

- **USB:** Chrome/Edge na počítači, *Připojit* a vybrat port NOVA. Vše běží
  přímo v prohlížeči.
- **Wi-Fi:** na počítači spusť `python3 bridge.py --port <IP NOVA>` (připojí se
  na WebSocket NOVA na portu 8002, takže potvrď na displeji) a v aplikaci
  zvol *Wi-Fi přes lokální most*. Stránka z https (GitHub Pages) nesmí otevřít
  `ws://192.168.x.x` napřímo, ale `ws://localhost` ano. Most navíc servíruje
  aplikaci na <http://localhost:8765/>, takže funguje úplně offline.
- **Simulátor:** bez stroje.

## Bezpečnost

Platí pro webovou aplikaci i `nova.py`. Ochrany stroje se nikdy neobcházejí.

- **Výchozí stav je jen čtení.** Posílají se jen dotazy (`?`, `$I`, `$$`, `$G`…).
- **Pohyb** (posun `$J=`, `$H`, `$X`, rámeček, úloha) vyžaduje *Povolit pohyb*.
  Posun nikdy nezapne laser.
- **Úloha, která zapíná paprsek** (M3/M4 s S>0), navíc vyžaduje *Povolit laser*.
  Odmítne se, když `$32` (laserový režim) ≠ 1 nebo když motiv přesahuje
  plochu 297 × 210 mm.
- **Nastavení se nikdy neposílá** (`$n=`, `$RST`, `$NVX`, `[ESPxxx]`, `$Wifi/...=`).
- Úloha se posílá po jednom řádku a zastaví se při `error`, `ALARM`, `Door`
  nebo nečekaném `Hold`. **STOP / Esc / Ctrl-C** pošle pozastavení + soft reset
  a Grbl laser okamžitě vypne.
- Při připojení se neposílá reset, takže běžící úloha se nepřeruší.
- Most poslouchá jen na 127.0.0.1 a WebSocket přijme jen od povolených Originů,
  takže jiná webová stránka laser přes most ovládat nemůže.
- Softwarový STOP **nenahrazuje** blokování víka ani vypínač.

## Jak NOVA odstřihnout od cloudu

TOOCAA Studio je webová aplikace na serverech TOOCAA. Tahle potřebuje jen tvůj
počítač a stroj.

1. Na routeru dej NOVA pevnou IP (DHCP rezervace).
2. Ve firewallu **zablokuj WAN pro tuhle IP/MAC** a LAN nech povolenou. Lokální
   ovládání přes USB i Wi-Fi funguje dál.
3. Aktualizace firmwaru dává TOOCAA **jen přes Wi-Fi a Studio**. Kdybys ji
   někdy chtěl, blokaci na tu jednu aktualizaci zruš.

## Plán

- [x] Klient Grbl_ESP32 (USB/telnet), simulátor, testy
- [x] Webová aplikace: SVG → G-code, náhled, ověření, rámeček, úloha, posun, konzole
- [x] Editor: více objektů, posun/zvětšení/otáčení myší, kopírovat/vložit/duplikovat, kopie do mřížky, zpět/znovu, text (libovolné písmo v systému), obdélník/kruh, PNG/JPG s prahem/rozptylem, gravírování výplně, předvolby materiálů, ukládání projektů + automatické ukládání
- [x] Rychlé odesílání (Grbl character counting)
- [x] Audit `.lbdev` (CLI i v prohlížeči)
- [x] Nalezen oficiální `.lbdev`: autofokus `G38.2`, počátek vzadu vlevo; vzduch M8/M9
- [x] Wi-Fi přes WebSocket (`ws://IP:8002/ws`, stejně jako TOOCAA Studio)
- [ ] Ověřit na skutečné NOVA (`probe`, `info`, autofokus, počátek)
- [ ] Zaměřovací kříž / červené ukazovátko
- [ ] Gravírování ve stupních šedi (proměnný výkon), výběr více objektů / seskupení, zarovnání
- [ ] Kamera: stream, kalibrace, umístění motivu na fotku plochy

## Zdroje

- Výpis konzole NOVA po aktualizaci na 1.0.0: [hagensieker.com – recenze Toocaa NOVA](https://hagensieker.com/2026/08/14/toocaa-nova-smart-home-laser-engraver-review/)
- Parametry a připojení NOVA: [The Gadgeteer – TOOCAA NOVA](https://the-gadgeteer.com/2025/12/13/toocaa-nova-laser-engraver-just-right/)
- Protokol Grbl 1.1: [gnea/grbl wiki – Interface](https://github.com/gnea/grbl/wiki/Grbl-v1.1-Interface)
- Grbl_ESP32: [bdring/Grbl_Esp32](https://github.com/bdring/Grbl_Esp32)

## Licence

MIT. TOOCAA je ochranná známka svého vlastníka.
