# CivicMesh Inkplate 6 — Library + Display API Research

**Library inspected:** `Inkplate-Arduino-library` v11.0.0 (HEAD `57da3104` "Merge branch 'dev'", tagged `11.0.0`).
Source: `Inkplate-Arduino-library/library.properties:3`.

**Docs inspected:** `soldered-documentation/soldered-documentation/docs/inkplate/` (the source for `docs.soldered.com/inkplate`).

**Conventions in this document.** Citations of the form `path:line` refer to files in `Inkplate-Arduino-library/` unless they start with `docs/`, in which case they refer to `soldered-documentation/soldered-documentation/docs/`. **Documented** means the behavior is asserted in code comments or in the docs site. **Inferred** means I read the implementation and concluded behavior, but did not find a comment that made the contract explicit.

---

## 0. Board revision question (V1 vs V2)

**The Inkplate 6 has two PCB revisions selected by the Arduino board macro, both served by the same driver (`src/boards/Inkplate6/`):**

| Define | Marketing name | Touchpads | I/O expander |
|---|---|---|---|
| `ARDUINO_INKPLATE6` | e‑Radionica Inkplate 6 (older, blue PCB) | **3 capacitive pads** | MCP23017 |
| `ARDUINO_INKPLATE6V2` | Soldered Inkplate 6 (newer, purple PCB) | **none** | PCAL6416A |

Sources:
- Driver header guard accepting both: `src/boards/Inkplate6/Inkplate6Driver.h:5`.
- `Touchpad` member only declared under `ARDUINO_INKPLATE6` (V1): `src/boards/Inkplate6/Inkplate6Driver.h:79–81`.
- `Touchpad::begin` / `Touchpad::read` only compiled under V1: `src/features/touchpad/touchpad.cpp:1` (and same guard in `touchpad.h:18`).
- `featureSelect.h` includes `touchpad/touchpad.h` only for the V1 macro: `src/features/featureSelect.h:8`.
- Soldered docs page on touchpads explicitly states the V2 (purple) Soldered Inkplate has **no touchpads**: `docs/inkplate/touchpads/overview.md:9–18`.
- Driver also splits **waveform repetitions** between revisions: V1 uses `rep=4` for clean, V2 uses `rep=5` (`Inkplate6Driver.cpp:338–342`); partial-update uses `rep=5` vs `rep=6` (`Inkplate6Driver.cpp:499–503`). V2 also configures unused SPI pins as inputs and disables SD supply on init (`Inkplate6Driver.cpp:846–854`) — slightly lower deep-sleep current.

> **Recommendation for v0:** target **V2 (`ARDUINO_INKPLATE6V2`, Soldered/purple)** unless the user owns a V1 unit. The current Soldered store ships V2; documentation defaults to V2; touchpad wake is unsuitable for v0 anyway (see §3). If the unit happens to be V1, the firmware works unchanged — just don't write any touchpad code.

---

## A. API reference

### 1. Display and refresh mechanics

**Class and constructor.** `class Inkplate : public Graphics, public InkplateBoardClass, public NetworkController` (`src/Inkplate.h:31`). Inkplate 6 has multiple display modes, so the active constructor is:

```cpp
Inkplate display(INKPLATE_1BIT);   // or INKPLATE_3BIT
```

Defined at `src/Inkplate.cpp:29`, gated by `MULTIPLE_DISPLAY_MODES`. Mode constants: `INKPLATE_1BIT = 0`, `INKPLATE_3BIT = 1` (`src/system/defines.h:41–42`). The `BLACK = 1`, `WHITE = 0` palette holds for Inkplate 6 (`src/system/defines.h:33–35`).

**Init sequence.**
```cpp
display.begin();          // src/Inkplate.cpp:55
display.clearDisplay();   // clears framebuffer only — NOT the panel
display.display();        // first full refresh
```

`begin()` is idempotent (early return when `_beginDone == 1`, `Inkplate.cpp:61`), starts `Wire`, runs the EPD driver init (`initDriver`), and forwards the constructor mode to `selectDisplayMode`. `initDriver` allocates four PSRAM framebuffers via `ps_malloc` (`Inkplate6Driver.cpp:870–873`) plus two grayscale LUTs in regular heap, totalling ~720 KB of state — Inkplate 6 has PSRAM, so this is fine, but it constrains your free PSRAM budget.

`clearDisplay()` only zeroes the framebuffer (`Inkplate6Driver.cpp:227–236`); a subsequent `display()` is required to wipe the panel.

**Drawing primitives.** Inherits Adafruit GFX and adds Inkplate-specific helpers:

| API | Source | Notes |
|---|---|---|
| `display.drawPixel(x, y, color)` | `Inkplate.cpp:91` | color: 0/1 in 1‑bit, 0–7 in 3‑bit (`Inkplate6Driver.cpp:46–61`) |
| `display.drawLine`, `drawFastHLine`, `drawFastVLine`, `drawThickLine` | Adafruit GFX | `drawThickLine` is Inkplate-added (`docs/inkplate/6/basics/drawing_graphics.md`) |
| `display.drawRect`, `fillRect`, `drawRoundRect`, `fillRoundRect` | Adafruit GFX | |
| `display.drawCircle`, `fillCircle`, `drawElipse`, `fillElipse` | Adafruit GFX + Inkplate | Note Soldered's spelling: "Elipse" |
| `display.drawTriangle`, `fillTriangle`, `drawPolygon`, `fillPolygon` | Adafruit GFX + Inkplate | Polygons require CCW-sorted vertices |
| `display.print(...)`, `display.println(...)`, `printf(...)` | Arduino Print | |
| `display.setCursor(x, y)`, `setTextSize(s)`, `setTextWrap(bool)` | Adafruit GFX | |
| `display.setTextColor(fg)` / `setTextColor(fg, bg)` | Adafruit GFX | Two-arg form fills the glyph background — the canonical "white-on-black" hook |
| `display.setFont(&FreeSansBold18pt7b)` | Adafruit GFX | Pass a `const GFXfont *`; pass `NULL` to revert to the built-in 5×7 font |
| `display.drawTextBox(x0, y0, x1, y1, text, size=1, font=NULL, vspace=0, border=false, fontSize=8)` | `src/graphics/Graphics.h:40` | Word-wraps to box; truncates with `...` if it overruns |
| `display.image.draw(buf, x, y, w, h, color=BLACK, bg=0xFF)` | `src/graphics/Image/Image.h:85` | Renders a PROGMEM monochrome bitmap |
| `display.image.draw(path, x, y, dither=true, invert=false)` | `src/graphics/Image/Image.h:83` | SD/Web image dispatcher (auto-detect format) |
| `display.image.drawJpegFromWeb(url, x, y, dither, invert)` etc. | `src/graphics/Image/Image.h:110–119` | We don't need these for v0 |
| `display.getTextBounds(text, x, y, &x1, &y1, &w, &h)` | Adafruit GFX | Used in the Quotables example to right-align the author signature (`examples/Inkplate6/Projects/Inkplate6_Quotables/Inkplate6_Quotables.ino:155–158`) |

**Inverted (white-on-black) text** — pattern from `Inkplate6_Black_And_White.ino:310–317`:
```cpp
display.fillRect(0, 0, display.width(), 60, BLACK);  // black header bar
display.setTextColor(WHITE, BLACK);                  // white text on black bg
display.setCursor(20, 18);
display.setFont(&FreeSansBold18pt7b);
display.print("CIVICMESH");
```

**Rotation.** `display.setRotation(r)` with `r ∈ {0,1,2,3}` (0°, 90°, 180°, 270°) — `Inkplate.cpp:117`. The library swaps `_width`/`_height` so all subsequent draw calls are in the rotated frame. Default is landscape 800×600.

**Grayscale support and tradeoffs.**
- 1‑bit (BW) is the default and only mode that supports partial update (`partialUpdate` early-exits if `getDisplayMode()==1`, see `Inkplate6Driver.cpp:435–436`).
- 3‑bit grayscale: 8 levels (0–7), `display.drawPixel(x, y, 0..7)` (`docs/inkplate/6/basics/display_modes.md:46–66`).
- 3‑bit framebuffer is 4× larger (240 KB instead of 60 KB) — already allocated by `begin()` regardless of mode, so memory is not a runtime cost (`initializeFramebuffers`, `Inkplate6Driver.cpp:870–873`).
- 3‑bit full refresh runs 9 phases of cleaning + 9 frames of waveform (`display3b`, `Inkplate6Driver.cpp:266–305`); BW runs `rep=5` frames + a black flash (V2: `display1b`, `Inkplate6Driver.cpp:316–414`).
- **Documented refresh times** (`docs/inkplate/inkplate_features.md:138–141`): full = 1.26 s, fast (partial, BW only) = 0.26 s.
- **Recommendation for CivicMesh:** use **1‑bit BW**. Bulletins are typographic; grayscale buys nothing visual and disables partial update.

**Full vs partial refresh — the canonical confusion.**

- **Full refresh:** `display.display(bool _leaveOn = false)` (`Inkplate6Driver.cpp:246`). Cycles all rails, performs the 5 BW dark-frame phases + clean + 1 white frame + 1 black flash, leaves the panel powered down by default. Always safe.
- **Partial refresh:** `display.partialUpdate(bool _forced = false, bool _leaveOn = false)` (`Inkplate6Driver.cpp:433`). Returns the count of pixels turning black-to-white (`uint32_t changeCount`) — useful as a "needs full refresh because too much changed" heuristic.
- **Critical preconditions for partial update:**
  1. Mode must be 1‑bit. In 3‑bit mode, `partialUpdate()` returns 0 immediately (`Inkplate6Driver.cpp:435–436`).
  2. A full `display()` must have run first since power-up. The driver initializes `_blockPartial = 1` (`Inkplate6Driver.h:101`); `display1b()` clears it to 0 (`Inkplate6Driver.cpp:413`). If you call `partialUpdate()` while `_blockPartial == 1` and `_forced == false`, the driver silently falls through to a **full refresh** (`Inkplate6Driver.cpp:438–442`).
  3. Because deep-sleep wake re-runs `setup()` and re-instantiates everything, `_blockPartial` is **1 again on every wake** — meaning naive `partialUpdate()` after a wake silently full-refreshes.
- **Auto full-refresh threshold:** `display.setFullUpdateThreshold(N)` forces a full refresh every N partials (`Inkplate6Driver.cpp:551`). Default `_partialUpdateLimiter = 10` is *not* armed unless you call this — calling it with a non-zero N also sets `_blockPartial = 1` (lines 551–559), forcing the next call to be a full refresh. Docs recommend ~50 partials max between fulls (`docs/inkplate/6/basics/partial_update.md:19`); the partial-update example forces every 9 (`examples/Inkplate6/Basic/Inkplate6_Partial_Update/Inkplate6_Partial_Update.ino:65,81`).
- **Safe partial-after-deep-sleep pattern (documented):** `examples/Inkplate6/Advanced/DeepSleep/Inkplate6_Partial_Update_With_Deep_Sleep/Inkplate6_Partial_Update_With_Deep_Sleep.ino:73–99`. The pattern is: on wake, redraw what *was* on screen → call `display.preloadScreen()` to copy the framebuffer into the previous-frame buffer (`DMemoryNew`, see `Inkplate.cpp:37–40`) → clear → redraw new content → call `display.partialUpdate(true)` (the `true` is `_forced`). The example explicitly warns: *"Use this only in this scenario, otherwise YOU CAN DAMAGE YOUR SCRREN"* (sic, line 86).
- **Crash/watchdog risks during refresh:** I did not find a documented crash mode tied to either refresh path itself; both call `vscan_start`/`vscan_end` in tight loops with `delayMicroseconds(230)` between rows (`Inkplate6Driver.cpp:298, 364, 387, 406`). The blocking time of full refresh is ~1.3 s and partial ~0.3 s — well below any default ESP32 watchdog (the Arduino IDF task watchdog is disabled by default in Arduino-ESP32). No `yield()` or `delay()` is needed by the application during refresh.

> **v0 recommendation: full refresh only.** Reasons:
> 1. After every wake from deep sleep, you'd have to re-render the previous frame *before* drawing the new one, just so `partialUpdate(true)` has the right diff baseline. That requires persisting the entire previous bulletin state in RTC RAM or NVS — substantial complexity for ~1 s of saved refresh time once every 5 minutes.
> 2. Partial update of arbitrary text content can leave ghost glyphs visible to the public. A bulletin board that occasionally shows shadows of last hour's notice fails the "honest about failure" test.
> 3. The Soldered example for partial-update-with-deep-sleep includes the warning "you can damage your screen". Reliability budget is better spent elsewhere.
> 4. Power: full refresh draws the EPD rails for ~1.3 s vs ~0.3 s for partial. The dominant cost in our wake cycle is WiFi association (multiple seconds), not the refresh.

### 2. Power and deep sleep

**Inkplate wraps nothing — you call `esp_sleep_*` directly.** Pattern from the doc page (`docs/inkplate/6/deepsleep/sleep_simple.md:20–28`) and `examples/Inkplate6/Advanced/DeepSleep/Inkplate6_Simple_Deep_Sleep/Inkplate6_Simple_Deep_Sleep.ino:81–82`:

```cpp
#define uS_TO_S_FACTOR 1000000ULL
esp_sleep_enable_timer_wakeup((uint64_t)next_poll_seconds * uS_TO_S_FACTOR);
esp_deep_sleep_start();   // never returns; setup() runs from scratch on wake
```

To allow the on-board `WAKE` button to also force an early refresh (GPIO 36, active LOW): `esp_sleep_enable_ext0_wakeup(GPIO_NUM_36, LOW);` — both this and the timer can be armed before sleeping (`examples/Inkplate6/Advanced/DeepSleep/Inkplate6_Wake_Up_Button/Inkplate6_Wake_Up_Button.ino:76–82`). On wake, `esp_sleep_get_wakeup_cause()` returns `ESP_SLEEP_WAKEUP_TIMER` or `ESP_SLEEP_WAKEUP_EXT0` (same example, lines 108–121).

**Optional power tweak from the simple-sleep example:** `rtc_gpio_isolate(GPIO_NUM_12)` — only relevant on older boards with ESP32-WROVER-E (`Inkplate6_Partial_Update_With_Deep_Sleep.ino:97`, comment line 96). Safe to omit on V2; safe to include on V1.

**Does the e‑paper image persist through deep sleep? Yes, documented.**
- `docs/inkplate/6/deepsleep/sleep_simple.md:11`: *"Since e‑Paper does not require any power to retain the displayed image, Inkplate 6 can use little to no current while in deep sleep mode."*
- The Simple_Deep_Sleep example's whole premise is that the previous image is still on the panel when the ESP32 wakes; the example overwrites it with a new full refresh.

**Required teardown before sleep.**
- Turn off WiFi: the simplest pattern from `Inkplate6_HTTP_Request.ino` and the Quotables example doesn't bother — you can simply call `esp_deep_sleep_start()` and let WiFi go down when the chip does. For cleanliness and a guaranteed minimum draw, `WiFi.disconnect(true); WiFi.mode(WIFI_OFF);` before sleep (see also `NetworkController::disconnect()` at `src/system/NetworkController/NetworkController.cpp:175–178`, which does only the latter).
- Turn off the panel: `display()` already calls `einkOff()` at the end (`Inkplate6Driver.cpp:411`) unless you passed `_leaveOn=true`. Don't call `display(true)` followed by deep sleep — explicit warning in the Fastest_Display_Refreshes example: *"Always call einkOff() before entering deep sleep."* (`examples/Inkplate6/Advanced/Other/Inkplate6_Fastest_Display_Refreshes/Inkplate6_Fastest_Display_Refreshes.ino:42`).
- (V2 only, automatic): `gpioInit()` already configures unused SPI pins as inputs and disables SD supply (`Inkplate6Driver.cpp:846–854`).
- Optional V1: `rtc_gpio_isolate(GPIO_NUM_12)` (see above).

**Battery telemetry — what's actually exposed.**

| API | Returns | Cost | Source |
|---|---|---|---|
| `display.readBattery()` | `double` battery voltage in volts (e.g. `3.92`) — header types it as `double`; the doc page shows `float`, in practice they're equivalent on ESP32 because `double` is 64-bit there but the cast back to a result value via the printed example treats it as float-precision data. | One ADC read on GPIO35, ~5 ms blocking | `src/boards/Inkplate6/Inkplate6Driver.cpp:967–1005`; `src/boards/Inkplate6/Inkplate6Driver.h:65`; `docs/inkplate/6/readbattery/read_bat_voltage.md:46–51` |
| `display.readTemperature()` | `int8_t` panel temperature, range ‑10…85 °C, ±1 in 0…50 °C | I2C read from TPS65186 PMIC | `Inkplate6Driver.cpp:1189` |
| `display.isPowerGood()` | `bool` — all PMIC rails are within tolerance | I2C read | `Inkplate6Driver.h:68` |

**No percent-of-charge is exposed.** Map voltage to a coarse status with a Li‑ion lookup (e.g. `>3.9 V = good, 3.6–3.9 V = low, <3.6 V = critical`). Voltage is loaded — accuracy depends on whether you're actively transmitting WiFi, so read it before WiFi.begin() or at the very end of the wake cycle.

**Documented current-draw figures.**
- Deep sleep with all peripherals asleep: **~25 µA** (`docs/inkplate/inkplate_features.md:142`, `docs/inkplate/6/deepsleep/sleep_simple.md:13` says "20-30µA").
- **Active draw is not documented in this repo.** Empirically, ESP32 + EPD refresh + WiFi association sits in the 100–200 mA range during the ~3–8 s wake. I will not invent a precise figure; you'll need to measure on the bench.

**Watchdog and blocking-call traps.**
- `connectWiFi()` (library helper) does `delay(1000)` *before* its first status check (`NetworkController.cpp:54`), then loops with 1 s `delay`s up to `timeout` seconds (default 23 s in `NetworkController.h:29`). Worst-case wall-clock to detect a non-existent AP: ~24 s of busy-blocking `delay`. Acceptable on this single-task firmware; just don't assume the call returns quickly.
- Raw `WiFi.begin()` + `while (status != WL_CONNECTED)` patterns in the docs use `delay(500)` in a loop with **no timeout** (`docs/inkplate/6/wifi/wifi_basic.md:30–34`, `docs/inkplate/6/wifi/wifi_get_post.md:65–70`). **Don't copy this pattern as-is** — it hangs forever on AP-down, which we cannot tolerate.
- The Quotables example loops `while (!network.getData(...)) { delay(1000); }` with **no retry limit** (`Inkplate6_Quotables.ino:144–148`). Same problem — replace with bounded retry.
- Refresh paths (`display1b`, `display3b`, `partialUpdate`) are blocking for their full duration (~0.3–1.3 s); ESP32 task WDT is disabled by default in Arduino-ESP32, so this won't trip a reset, but other tasks (e.g. WiFi RX) won't run during refresh — refresh after the HTTP response is in hand, not concurrently.

### 3. Hardware interactions (touchpads, buttons)

**Touchpads (V1 only — see §0).**
- 3 capacitive pads exposed as `display.touchpad.read(pad)` with `pad ∈ {1, 2, 3}` (`src/features/touchpad/touchpad.cpp:11–28`). Returns `0` or `1`. The pads are wired to MCP23017 expander1 pins 10/11/12 — i.e. **on the I²C side**, not on ESP32 GPIO.
- **Touchpad cannot wake from ESP32 deep sleep through any first-class library API.** The library exposes only polled reads. To do touch-wake you'd need to (a) configure the I²C expander to assert its INT line on a pin change, (b) wire that INT to an RTC-capable ESP32 GPIO, (c) configure `ext0` or `ext1` wake — none of which the library helps with, and there are no examples.
- Calibration / thresholds: none — the pads are a digital input from the expander; sensitivity is in the expander's hardware threshold. Docs note the pads are "sensitive to environment and grounding and may behave differently depending on humidity, grounding, or enclosures" (`Inkplate6_Read_Touchpads.ino:54–55`).

**Hardware buttons.**
- **WAKE button on GPIO 36, active-LOW.** Used with `esp_sleep_enable_ext0_wakeup(GPIO_NUM_36, LOW)` (`Inkplate6_Wake_Up_Button.ino:79`).
- **RESET button** is a hardware reset, not visible to firmware.

> **v0 realism call: defer touchpad wake; ship timer-wake plus the WAKE button as the only interactive surface.** Pressing WAKE forces an off-cycle refresh — useful as a "did the relay come back yet?" smoke test for hub volunteers. Don't plan any other interactivity for v0. If the unit is V1, don't write any touchpad code; the firmware will still build with `#ifdef ARDUINO_INKPLATE6` guards or just by leaving the pads alone.

### 4. Offline networking

**Open-WiFi connect with a hard timeout.** The library's helper handles open networks if you pass an empty password — `WiFi.begin(ssid, "")` is valid in ESP32-Arduino. Use the helper's timeout knob:

```cpp
// timeout is in seconds; default WIFI_TIMEOUT = 23 (NetworkController.h:29)
if (!display.connectWiFi(ssid, "", 20, /*printToSerial=*/false)) {
    renderApUnreachableScreen();
    sleepFor(retrySeconds);
}
```
Source: `NetworkController.cpp:40–88`. Behavior: sets `WIFI_MODE_STA`, calls `WiFi.begin(ssid, pass)`, hard-waits 1 s, then 1 s polls until `WL_CONNECTED` or timeout. Returns `bool`.

**mDNS / `civicmesh.internal` resolution.** ESP32-Arduino does not resolve `*.local` via DNS by default; it requires the `ESP_mDNS`/`ESPmDNS` library and an active mDNS responder on the Pi. **`civicmesh.internal` is not an mDNS name** (mDNS uses `.local`); it's a regular DNS suffix that requires the Pi's dnsmasq (or equivalent) to resolve it for clients on the captive AP. That **does** work over standard DNS as long as the Pi is the DNS server handed out by DHCP. Verify on the Pi side: `dig @192.168.x.1 civicmesh.internal` should return an A record.

**Recommendation:** put both `endpoint_url` and a static-IP fallback (`http://192.168.4.1/api/display/state`) in firmware config. Try the hostname first; on `WiFi.hostByName` or HTTP failure, retry the IP. This protects against a malformed dnsmasq config without requiring mDNS.

**HTTP client and JSON parser.** The official examples use:
- `<HTTPClient.h>` — comes with ESP32-Arduino. Headers: `src/system/NetworkController/NetworkController.h:22`. Used in `Inkplate6_HTTP_Request.ino:111–124` and the News example.
- **ArduinoJson** — third-party, install via Library Manager. The News project uses `StaticJsonDocument<35000>` (`examples/Inkplate6/Projects/Inkplate6_News/src/Network.cpp:23`). For our small payload, prefer ArduinoJson 7's `JsonDocument doc;` (heap-allocated, grows as needed) or `StaticJsonDocument<2048>` if you stay on v6.

**Canonical fetch pattern (v0 firmware will parametrize this):**
```cpp
HTTPClient http;
http.setTimeout(5000);             // 5 s socket timeout
http.setConnectTimeout(5000);
if (!http.begin("http://civicmesh.internal/api/display/state")) {
    renderDnsFailureScreen(); return;
}
int code = http.GET();
if (code != HTTP_CODE_OK) {
    renderHttpErrorScreen(code); http.end(); return;
}
JsonDocument doc;
DeserializationError err = deserializeJson(doc, http.getStream());
http.end();
if (err) { renderBadJsonScreen(err.c_str()); return; }
```

**Heap/memory gotchas (≤2 KB payload).**
- Inkplate 6 has PSRAM (the framebuffers are `ps_malloc`'d), so internal heap is mostly free for ArduinoJson. A 2 KB payload + ArduinoJson overhead is comfortably <8 KB internal heap.
- Avoid `http.getString()` for binary or large bodies — it builds a single Arduino `String` in heap and is hard on fragmentation. For our small JSON, it's fine; for streaming, use `http.getStream()` directly into ArduinoJson (the News example does this, line 102).

**Failure-mode handling.**
- AP unreachable → `connectWiFi()` returns `false`. Render "AP unreachable" screen.
- DNS failure → `http.begin()` returns `false` *or* the `GET` returns a negative error code. Render "DNS failure" screen (or "Pi unreachable" — distinguishing is fiddly; consider grouping them).
- TCP failure → negative `httpCode`. Render "Pi unreachable".
- Non-200 → render "Pi error: HTTP {code}".
- Malformed JSON → `deserializeJson` returns non-empty error. Render "Bad JSON" with `err.c_str()`.
- Stale payload (server claims OK but data is old) → server's `status` field says `"stale"`; render "Last update: …" footer in inverted styling.

**HTTPS.** Confirm: avoid HTTPS entirely. The captive AP has no certs, and `WiFiClientSecure::setInsecure()` (used by the library helper at `NetworkController.cpp:405`) defeats the purpose anyway. Plain HTTP on the LAN is correct.

### 5. Storage and persistence

| Mechanism | When to use | Source |
|---|---|---|
| `RTC_DATA_ATTR` (RTC RAM, ~8 KB) | Counters, last `state_hash`, last `next_poll_seconds`, "is this first boot since power-on" flag — **lost on power loss / battery swap, kept across deep-sleep cycles** | `Inkplate6_Wake_Up_Button.ino:64`, `Inkplate6_Partial_Update_With_Deep_Sleep.ino:69–70` |
| `Preferences` (NVS key/value) | Device config (Hub name, SSID, endpoint URL, fallback IP, firmware/API version, last-good payload), survives power loss, wear-levelled | Recommended; not used in any Inkplate 6 example, but a standard Arduino-ESP32 component (`#include <Preferences.h>`) |
| `EEPROM` (NVS-backed emulation) | Avoid for application data — **addresses 0–75 are reserved for waveform data per Soldered**: *"DO NOT read from or write to addresses below 76"* (`examples/Inkplate6/Advanced/Other/Inkplate6_EEPROM_Usage/Inkplate6_EEPROM_Usage.ino:36`); the library itself stores VCOM at offset 0 (`Inkplate6Driver.cpp:1063`). Don't share this namespace with config | |
| SPIFFS / LittleFS | Not used by any official Inkplate 6 example. Possible but unnecessary for v0 | |
| SD card | Avoid — `sdCardInit()` reconfigures SPI pins and is on a shared bus; failure modes are extra surface area for v0; the V2 driver explicitly disables SD power in `gpioInit` to save standby current | `Inkplate6Driver.cpp:894–917` |

> **v0 recommendation:** `Preferences` for everything that survives power loss; `RTC_DATA_ATTR` for between-wake state. Skip EEPROM and SD entirely.

**Flash-wear concerns.** NVS is wear-levelled; a 5-minute poll cadence equals 288 polls/day. Cache the last-good payload only when `state_hash` changes (i.e. write rarely). With this discipline, NVS lifetime is far beyond any plausible deployment.

### 6. Fonts, graphics, QR codes

**Built-in font.** Adafruit GFX 5×7 bitmap, scalable by integer factor via `setTextSize(s)`. Coarse — fine for diagnostic text, ugly for headlines.

**Custom fonts.** Bundled `Fonts/` directory ships dozens of Adafruit GFX fonts (`Inkplate-Arduino-library/Fonts/Free*`). Include the header and call `setFont(&FreeSansBold18pt7b)` (`docs/inkplate/6/basics/printing_text.md:90–104`). Pass `setFont(NULL)` to revert to the built-in font.

**Bitmap rendering.** `display.image.draw(buf, x, y, w, h, color, bg)` for 1‑bit bitmaps in PROGMEM (`src/graphics/Image/Image.h:85`). Generate with the Soldered Image Converter web tool at `tools.soldered.com/tools/image-converter/` (`docs/inkplate/6/basics/image_converter.md`). Output is a header file with `image[]` + `image_w` + `image_h`.

**Built-in QR support: NO.** I grepped the entire `src/` tree for `qr|qrcode|qr_code` (case-insensitive) and the only match is the false-positive `SQR(a)` macro in `defines.h`. No QR examples either. If v0 needs to render a QR (for a Wi‑Fi join card or similar), use a small Arduino-compatible library — `ricmoo/QRCode` is the most common and known to work on ESP32, generating a `qrcode_t` then drawing it with nested `display.fillRect(x*S, y*S, S, S, BLACK)` over a module size `S`.

**Practical legibility judgment for a 6" e‑paper QR.**
- 6" diagonal ⇒ ~122 mm × 91 mm visible area at 800×600.
- ECC level M, version 6 (41×41 modules) at S=8 px ⇒ ~50×50 mm — comfortably scannable from 30–60 cm with any phone camera, even via the slight surface texture of the e-ink film.
- Lower-density payloads (a 30-character URL) at version 3 (29×29 modules) at S=12 px ⇒ ~58 × 58 mm — even more forgiving.
- The constraint isn't pixel density (e-paper is monochrome and high-contrast); it's *what URL/Wi‑Fi config you encode*. Keep payloads short.

### 7. Failure-mode UX support

For each screen, the supporting Inkplate APIs:

| Screen | Render approach | APIs used |
|---|---|---|
| Booting / joining WiFi | Centered title + animated "spinner" via `partialUpdate` if you've done a full refresh first; otherwise just a static "Connecting…" message | `setFont`, `drawTextBox`, `display`, optional `partialUpdate` |
| AP unreachable | Inverted header bar ("CivicMesh — AP unreachable"), body explaining "Hub WiFi not in range" | `fillRect(BLACK)` + `setTextColor(WHITE, BLACK)` for header; `drawTextBox` for body |
| DNS failure | Same shell, body "Cannot find civicmesh.internal" | as above |
| Pi unreachable | Same shell, body "Hub server not responding" | as above |
| Bad JSON / API version mismatch | Same shell, body shows `firmware version`, `expected API version`, server's reported `api_version` | as above; render `Inkplate6/Fonts/FreeMono*.h` for the version triple to make mismatches scannable |
| Stale data (server says so) | Normal bulletin layout but a footer line: "Last update: 12 minutes ago — server reports stale" in inverted styling | normal layout + `fillRect`/`setTextColor` for footer |
| Radio down, portal still available | Bulletin shows the human-relayed messages from server, with a full-width inverted footer "Mesh radio offline — portal still up at hub.local/portal" | as above |
| Low battery | Battery icon (PROGMEM bitmap from `Inkplate6_Read_Battery_Voltage` example) + voltage in inverted footer when `readBattery() < threshold` | `image.draw` + `print` |
| Diagnostic footer (always-on) | One-line at y = `display.height() - 18`: `Hub: A1 · FW v0 · API v1 · 12:34 · 3.92 V` | small font, `setCursor`, `print` |

The pattern across all of these is the same shell — full-width inverted header, body with `drawTextBox`, full-width inverted footer — which means a `renderShell(headerText, bodyLines, footerLine)` helper covers every state. Implement once.

---

## B. Example inventory (area 8)

Only Inkplate 6–specific examples; ranked by relevance to the CivicMesh bulletin client.

| Path (relative to `Inkplate-Arduino-library/`) | Demonstrates | APIs used | Why it matters | Reusable pattern | Gotchas |
|---|---|---|---|---|---|
| `examples/Inkplate6/Projects/Inkplate6_Quotables/Inkplate6_Quotables.ino` | WiFi connect → fetch JSON → render text box → 5-min deep sleep | `connectWiFi`, `HTTPClient`, `ArduinoJson`, `drawTextBox`, `display`, `esp_sleep_enable_timer_wakeup`, `esp_deep_sleep_start` | **Closest fit to CivicMesh wake cycle** — same 5-minute cadence, same "render and sleep" shape | Outer flow + WiFi-failure deep-sleep retry (lines 124–141) | Infinite retry loop on `network.getData` (lines 144–148) — **don't copy that** |
| `examples/Inkplate6/Projects/Inkplate6_News/Inkplate6_News.ino` (+ `src/Network.cpp`) | WiFi + HTTPS GET + ArduinoJson parse + multi-section layout + deep sleep | Same as above plus `setFont`, `drawTextBox` with text layout, NTP via `getNTPEpoch` | Real-world JSON-to-display layout; demonstrates `getNTPEpoch` if you ever need wall-clock time | Layout helper `drawNews` shows date/time row + repeating article boxes — directly transferable to bulletin layout | `StaticJsonDocument<35000>` is wildly oversized for our payload; hard `ESP.restart()` after 7 s (`Network.cpp:69–73`) is an option for total WiFi failure |
| `examples/Inkplate6/Advanced/DeepSleep/Inkplate6_Simple_Deep_Sleep/Inkplate6_Simple_Deep_Sleep.ino` | Minimal timer-wake deep sleep + RTC RAM counter + 3-bit slideshow | `esp_sleep_enable_timer_wakeup`, `esp_deep_sleep_start`, `RTC_DATA_ATTR` | The boring/reliable deep-sleep skeleton | Whole `setup()` is the template for v0 firmware | Uses 3-bit mode (no partial); we'll use 1-bit |
| `examples/Inkplate6/Advanced/DeepSleep/Inkplate6_Wake_Up_Button/Inkplate6_Wake_Up_Button.ino` | Timer + EXT0 (WAKE button GPIO 36) wake; uses `esp_sleep_get_wakeup_cause` to branch | `esp_sleep_enable_ext0_wakeup(GPIO_NUM_36, LOW)`, `esp_sleep_get_wakeup_cause` | If we expose the WAKE button as "force refresh", this is the template | Wake-cause switch (lines 110–121) for telling the user why the screen just changed | None notable |
| `examples/Inkplate6/Advanced/DeepSleep/Inkplate6_Partial_Update_With_Deep_Sleep/Inkplate6_Partial_Update_With_Deep_Sleep.ino` | Safe `partialUpdate(true)` after wake by re-rendering the previous frame and calling `preloadScreen()` | `preloadScreen`, `partialUpdate(true)`, `RTC_DATA_ATTR`, `rtc_get_reset_reason` | The *only* documented safe partial-after-deep-sleep pattern | Ignore for v0 (deferred) — keep as reference if we add partial later | Comment line 86: *"otherwise YOU CAN DAMAGE YOUR SCRREN"* |
| `examples/Inkplate6/Basic/Inkplate6_Partial_Update/Inkplate6_Partial_Update.ino` | Loop-time partial updates with periodic full refresh via `setFullUpdateThreshold` | `partialUpdate(false, true)`, `setFullUpdateThreshold(9)` | Reference for `setFullUpdateThreshold` semantics | Threshold pattern; `leaveOn=true` for back-to-back partials | Don't combine `leaveOn=true` with deep sleep |
| `examples/Inkplate6/Advanced/Other/Inkplate6_Read_Battery_Voltage/Inkplate6_Read_Battery_Voltage.ino` | Reads `display.readBattery()` and renders a battery icon + voltage | `readBattery`, `image.draw` for the symbol | Direct fit for the diagnostic footer / low-battery screen | Battery icon in `battSymbol.h` is reusable PROGMEM | `delay(10000)` loop is just demo; we read once per wake |
| `examples/Inkplate6/Advanced/Other/Inkplate6_Read_Temperature/Inkplate6_Read_Temperature.ino` | `readTemperature()` from PMIC | `readTemperature` | Optional: surface in diagnostic footer or to gate refresh in extreme heat/cold | | E-ink performance degrades outside 0–50 °C; not a v0 concern indoors |
| `examples/Inkplate6/Advanced/WEB_WiFi/Inkplate6_HTTP_Request/Inkplate6_HTTP_Request.ino` | Plain HTTP GET, prints body to display | `WiFi.begin`, `HTTPClient`, `getString`, `setFont` | Minimal HTTP-on-display demo; useful as a smoke test sketch before integrating | Don't use the unbounded `while (WiFi.status() != WL_CONNECTED)` loop in production | |
| `examples/Inkplate6/Basic/Inkplate6_Hello_World/Inkplate6_Hello_World.ino` | Smallest possible sketch | `begin`, `clearDisplay`, `setCursor`, `setTextSize`, `print`, `display` | First-boot sanity check | | |
| `examples/Inkplate6/Basic/Inkplate6_Black_And_White/Inkplate6_Black_And_White.ino` | Tour of every shape primitive + bitmap drawing + inverted text + rotation | All GFX shape calls, `image.draw`, `setTextColor(WHITE, BLACK)`, `setRotation` | One-stop reference for "how do I draw X" | Inverted-text pattern (lines 310–317) is the white-on-black header recipe | Long `delay(5000)` between demos — strip those if reusing |
| `examples/Inkplate6/Basic/Inkplate6_TextBox/Inkplate6_TextBox.ino` | `drawTextBox` with and without optional parameters | `drawTextBox` | Reference for body-text layout | Custom-font case requires manual y-offset for ascender baseline (line 160) | |
| `examples/Inkplate6/Advanced/IO/Inkplate6_Read_Touchpads/Inkplate6_Read_Touchpads.ino` | Polled touchpad read in `loop()` | `display.touchpad.read(1..3)` | Confirms touchpad is **polled-only** — no wake-from-sleep API | | V1 only (`#if defined(ARDUINO_INKPLATE6)` at line 65) |
| `examples/Inkplate6/Advanced/Other/Inkplate6_Fastest_Display_Refreshes/Inkplate6_Fastest_Display_Refreshes.ino` | Keep panel powered between partial updates | `einkOn`, `einkOff`, `partialUpdate(0, 1)` | Not relevant for v0 (we sleep after each refresh) | | Always `einkOff()` before deep sleep |
| `examples/Inkplate6/Advanced/Other/Inkplate6_EEPROM_Usage/Inkplate6_EEPROM_Usage.ino` | EEPROM read/write | `EEPROM.begin/put/get/commit` | Documents the EEPROM 0–75 reserved-for-waveform constraint | | Use `Preferences` instead |
| `examples/Inkplate6/Basic/Inkplate6_Image_Converter/Inkplate6_Image_Converter.ino` | Render a PROGMEM bitmap | `image.draw` | Workflow for the CivicMesh logo / status icons | | |
| `examples/Inkplate6/Advanced/RTC/Inkplate6_RTC_Simple/Inkplate6_RTC_Simple.ino` | `display.rtc.*` for the on-board PCF85063A | `display.rtc.setEpoch`, `display.rtc.getEpoch`, etc. | Optional: keep wall-clock time across power loss using the coin-cell-backed RTC | | Not needed for v0 — server provides the time we display |
| `examples/Inkplate6/Diagnostics/Inkplate6_Burn_In_Clean/Inkplate6_Burn_In_Clean.ino` | `burnInClean(cycles, delay_ms)` to scrub long-static images | `burnInClean` | One-shot to run from the bench if you ever see persistent shadowing | | |

---

## C. Gotchas and risks list

1. **`partialUpdate()` silently turns into a full refresh after deep-sleep wake** because `_blockPartial` is re-initialized to 1 (`Inkplate6Driver.h:101`). If you measure refresh duration and see ~1.3 s when expecting ~0.3 s, this is why. Avoid by either accepting full refresh (v0 plan) or following the `preloadScreen` + `partialUpdate(true)` pattern with re-rendered prior content.
2. **`partialUpdate()` always returns 0 in 3‑bit mode** (`Inkplate6Driver.cpp:435–436`). If you switch modes, partial silently no-ops.
3. **Ghosting from runaway partial updates.** Library default `_partialUpdateLimiter = 10` is **not active** unless you call `setFullUpdateThreshold()`. If partials run unbounded, ghosts accumulate. Soldered docs recommend ~50 max between fulls (`docs/inkplate/6/basics/partial_update.md:19`).
4. **Library `connectWiFi` does a leading `delay(1000)` before the first status check** (`NetworkController.cpp:54`). Connection minimum is ~1 s + N × 1 s polls. Plan timeouts accordingly.
5. **Quotables example's `while (!network.getData(...)) { delay(1000); }` (`Inkplate6_Quotables.ino:144–148`) hangs forever on a permanently-down API.** Don't ship that pattern.
6. **`http.getString()` builds a single Arduino `String` in heap** — fragmentation hazard if the response is unexpectedly large. Use `http.getStream()` directly into ArduinoJson.
7. **EEPROM addresses 0–75 are reserved for waveform data** (`Inkplate6_EEPROM_Usage.ino:36`). The library writes VCOM at offset 0 (`Inkplate6Driver.cpp:1063`). If you must use EEPROM for application data, start at 76+; better, use `Preferences`.
8. **Touchpads cannot wake ESP32 from deep sleep** with library APIs — they sit on an I²C expander, not on RTC GPIOs (`src/features/touchpad/touchpad.cpp:11–28`).
9. **`mDNS / *.local` resolution requires `<ESPmDNS.h>` and an mDNS responder** on the AP. `civicmesh.internal` is regular DNS — works only if dnsmasq on the Pi resolves it. Ship a static-IP fallback in config.
10. **Don't combine `display(true)` (leaveOn) with `esp_deep_sleep_start()`.** The panel rails stay live and you bleed milliamps. The `Fastest_Display_Refreshes` example warns explicitly: *"Always call einkOff() before entering deep sleep."*
11. **`begin()` allocates ~720 KB across PSRAM** (1‑bit, 1‑bit prev, partial diff, 3‑bit, 2× LUT). Inkplate 6 has plenty, but third-party PSRAM-hungry libraries (ArduinoJson with very large StaticJsonDocument, large image buffers) can collide. Keep a memory budget.
12. **WiFi-active battery telemetry is noisy.** Read `display.readBattery()` *before* `WiFi.begin()` or after `WiFi.disconnect(true); WiFi.mode(WIFI_OFF);` for a stable number.
13. **The library's `disconnect()` only does `WiFi.mode(WIFI_OFF)`** (`NetworkController.cpp:175–178`), it doesn't `WiFi.disconnect(true)`. Stored credentials persist in flash — fine for us, just don't expect a clean wipe.
14. **Two PCB revisions, V1/V2, share one driver but differ in waveform repetitions** (`Inkplate6Driver.cpp:338–342, 499–503`). If you ever see slight contrast difference between two units, it's probably this. Always select the right Arduino board macro.
15. **Refresh blocks for 0.3–1.3 s — task watchdog is fine (disabled by default in Arduino-ESP32) but other tasks (WiFi RX) won't run.** Order: do the HTTP request → close it → then call `display.display()`. Don't refresh while a TCP connection is half-open.
16. **`drawTextBox` truncates with `...` when content overruns**, but does not return whether truncation occurred. If you need to detect overflow, measure with `getTextBounds` first.
17. **No built-in QR.** A QR library is a third-party dependency you must vet.
18. **PCF85063A RTC is on a coin cell; don't assume it's set.** Only relevant if you use `display.rtc.*` — for v0, server provides time strings already formatted, so we don't need it.
19. **`image.draw` for PROGMEM bitmaps takes width/height in pixels even though the buffer is byte-packed** — count pixels, not bytes, in your width.
20. **Custom GFX fonts use baseline-anchored glyphs** (`drawTextBox` example notes a manual offset of 32 px is needed at line 160). Plan layouts with this in mind, or stick with the built-in font for diagnostics.

---

## D. Recommended minimal v0 firmware architecture

Single Arduino sketch, modular by file. Everything lives in `setup()` because deep-sleep restarts the program — `loop()` stays empty.

```
src/
  CivicmeshDisplay.ino     // setup() orchestrator; loop() is empty
  config.h                 // SSID, endpoint URL, fallback IP, fonts list, hub name, firmware version
  net.h / net.cpp          // wifiConnectOrFail(), httpGetState(), parses payload into struct
  payload.h                // struct DisplayState — typed view of the JSON
  render.h / render.cpp    // renderShell(header, body, footer); render*Screen() helpers
  cache.h / cache.cpp      // Preferences load/save for last-good payload + state_hash
  power.h / power.cpp      // batteryVoltageStable(), sleepFor(seconds), wakeReason()
```

**`setup()` orchestrator (pseudocode, sources cited above):**

1. `display.begin()`; `display.clearDisplay()`.
2. `wakeReason = esp_sleep_get_wakeup_cause()` — log or include in diagnostic footer.
3. `voltage = display.readBattery()` — read before WiFi for accuracy.
4. `cache.load(&lastGood)` — `Preferences::getBytes` for last successful payload + its `state_hash` + its render time.
5. `if (!net.wifiConnectOrFail(20))` → `render.failure("AP unreachable")` → `display.display()` → `power.sleepFor(retrySeconds)`.
6. `state = net.httpGetState(primaryUrl)` (try hostname); on transport failure, retry `fallbackIp`.
7. On any net/parse failure → `render.failure(reasonScreen)` with `lastGood.timestamp` so the user knows the bulletin shown isn't current → use `lastGood` data when reasonable → sleep with backoff.
8. On success: `if (state.state_hash != lastGood.state_hash)` → `cache.save(state)` and `render.bulletin(state)`; else `render.bulletin(lastGood)` (saves NVS write, but still refresh — full refresh covers any panel anomalies). Always render so the diagnostic footer is current.
9. `display.display()` — full refresh, panel powers down at end of call (`Inkplate6Driver.cpp:411`).
10. `WiFi.disconnect(true); WiFi.mode(WIFI_OFF);`
11. `esp_sleep_enable_timer_wakeup(state.next_poll_seconds * 1000000ULL)` (server-driven cadence; clamp 60..3600).
12. `esp_sleep_enable_ext0_wakeup(GPIO_NUM_36, LOW)` — let WAKE button force-refresh.
13. `esp_deep_sleep_start()`.

**What belongs in `config.h` (compile-time defaults, runtime-overridable from server):**
- `SSID` (open, password "")
- `PRIMARY_URL` ("http://civicmesh.internal/api/display/state")
- `FALLBACK_URL` ("http://192.168.4.1/api/display/state")
- `HUB_ID` (e.g. "A1") — shown in footer
- `FIRMWARE_VERSION` (semver string)
- `EXPECTED_API_VERSION` (e.g. "1") — compared against server's `api_version` field
- `WIFI_TIMEOUT_S` (20)
- `HTTP_TIMEOUT_MS` (5000)
- `MAX_BACKOFF_S` (1800) — cap on retry sleep
- `LOW_BATTERY_VOLTS` (3.55)
- `CRIT_BATTERY_VOLTS` (3.40)

**What to hardcode initially (defer to v1):**
- Single SSID (no roaming via `WiFiMulti`).
- Single primary endpoint (no DNS-record discovery).
- 1‑bit BW only.
- Full refresh only.
- Time-only deep-sleep (no RTC alarm, no touch wake).
- One layout (header + bulletin body + footer).

**What to defer:**
- OTA updates.
- MQTT, WebSocket, server-push.
- Animations.
- SD card / Preferences-backed mutable config (just recompile for v0; revisit when you need field re-config).
- `setFont` with non-default fonts — use the built-in 5×7 scaled for v0; fonts cost flash and add a layout-tuning loop. Add custom fonts in v0.1.
- Server-rendered bitmap fallback (only if Inkplate rendering ever blocks us — it won't).
- QR code (add when content stabilizes; needs a library + payload discipline).
- Touchpad anything.

---

## E. Recommended v0 prototype scope

- **1‑bit BW mode only.**
- **Full refresh only.** Partial update is deferred until v1 at the earliest.
- **Timer wake only,** plus the WAKE button on GPIO 36 as an opt-in "force refresh" — costs nothing to enable and gives volunteers a smoke test.
- **No touchpad code,** even on V1 hardware. Don't accidentally make the v0 firmware revision-specific.
- **One JSON endpoint** (`GET /api/display/state`), HTTP only.
- **Simple bulletin layout:** inverted header (CivicMesh + Hub ID), 3–6 message body via `drawTextBox`, inverted footer (firmware version, API version, last-update time, battery voltage).
- **Clear failure screens:** AP unreachable, DNS failure, Pi unreachable, HTTP error, bad JSON, API version mismatch, stale, low battery — same shell, different header text.
- **Cached last-good** in `Preferences` (NVS), used only when a fetch fails; render with a "stale" footer so the public sees that the data isn't live.
- **Explicitly out of scope for v0:** OTA, MQTT/WebSockets, animations, SD dependence, custom fonts, QR codes, partial refresh, touchpad input, BLE, multi-AP, IPv6, NTP (server provides formatted time strings).

---

## F. Server API recommendation

### Endpoint set: single `GET /api/display/state` for v0; defer everything else

**Recommended:**
- `GET /api/display/state` — single render-ready payload, plain HTTP, ETag-cacheable.

**Deferred (v0.1 if needed):**
- `GET /api/display/health` — tiny `state_hash` + cadence response. Only worth adding if profiling shows full `state` GETs are too costly for the Pi battery budget. With ETag/`If-None-Match` on the single endpoint (see below), the marginal benefit shrinks further.

**Rejected for v0:**
- Split `status` / `messages` / `config` — three round-trips per wake triples WiFi-on time, which is the dominant power cost. Bundle into one.
- Server-rendered bitmap — unnecessary; Inkplate text/shape APIs are sufficient and a bitmap endpoint moves layout drift onto the Pi (more code surface to keep in sync with field hardware).

### `GET /api/display/state` — full spec

**Purpose.** A render-ready, generic ambient-display payload. The server picks what's safe to surface, computes freshness, summarizes radio/portal/DB status, and tells the client when to come back.

**v0 vs deferred:** v0.

**Pi/SQLite cost per request:** target ≤ 30 ms wall-time. One read of `display_state` materialised view (or one query that aggregates: latest mesh activity, radio status, portal status). Cache the rendered payload + `state_hash` on the Pi for ≥30 seconds; many wakes will hit the cache.

**Cacheability.** Strong ETag derived from `state_hash` (e.g. `ETag: "ds-v1-7f3a..."`). Client sends `If-None-Match` with the previously seen ETag; server returns `304 Not Modified` with empty body when unchanged. **But:** for v0, *the Inkplate still refreshes on every wake even on 304* — the diagnostic footer (last-update time, battery) needs updating, and a full refresh once every 5 minutes refreshes the panel against burn-in. So 304 saves the JSON-body bytes only, not the refresh; that's fine.

**Required vs optional fields.** Required: `schema_version`, `api_version`, `state_hash`, `next_poll_seconds`, `mode`, `radio_state`, `portal_state`, `status`, `header`, `body`, `footer`, `server_time`. Optional: `messages`, `meta`, `low_battery_threshold_volts`.

**Example response (well within 2 KB):**

```json
{
  "schema_version": 1,
  "api_version": "1",
  "state_hash": "7f3a2c91",
  "server_time": "2026-05-10T14:32:18-07:00",
  "next_poll_seconds": 300,
  "mode": "public",
  "status": "ok",
  "radio_state": "ok",
  "portal_state": "ok",
  "header": {
    "title": "CivicMesh",
    "subtitle": "Hub A1 — Greenwood Library"
  },
  "body": {
    "layout": "bulletin",
    "lines": [
      "Water available at the Hub. Bring containers.",
      "Next radio sync 14:45.",
      "Power outage expected through 18:00.",
      "Volunteers needed for evening shift — see staff."
    ]
  },
  "footer": {
    "left": "Last update 14:32 PT",
    "right": "Tap WAKE to refresh"
  },
  "meta": {
    "messages_total": 14,
    "last_mesh_seen": "2026-05-10T14:30:51-07:00",
    "low_battery_threshold_volts": 3.55
  }
}
```

**Failure-state encoding (server-side).** The client always renders something; the server tells it which screen to render via two orthogonal axes:

- `status` ∈ `"ok" | "stale" | "starting" | "error"` — the freshness/health of the **payload itself**.
- `radio_state` ∈ `"ok" | "degraded" | "down" | "needs_human" | "unknown"` and `portal_state` ∈ `"ok" | "down" | "unknown"` — the freshness of the **mesh/portal** behind the data.

So "radio down, portal up" is `status: "ok", radio_state: "down", portal_state: "ok"`, and the client renders the radio-down screen with the bulletin still showing the human-relayed messages.

**`mode` field.** `"public" | "disaster" | "drill" | "diagnostic" | "demo"` — gives the server a knob to swap layout class (e.g. `disaster` = use red-equivalent inverted styling for warnings; `diagnostic` = dump version/health table for volunteers; `demo` = canned content). Client doesn't need to understand every mode; unknown modes fall back to `"public"`.

**`next_poll_seconds` clamping.** Client clamps to `[60, 3600]`. Server can drive cadence up (every 60 s during a disaster mode) or down (every hour overnight).

**`api_version` mismatch.** If client's `EXPECTED_API_VERSION != server's api_version`, render the API-mismatch screen with both versions shown — this is the canonical "field unit needs reflashing" signal.

**Why one endpoint instead of split.**

1. **Pi power budget.** WiFi association + the first 200 ms of the GET dominates the wake-cycle energy budget. One round-trip vs three is roughly 3× the WiFi-on time. The Pi runs on a battery pack and is the system bottleneck — every saved second of WiFi-RX on the Inkplate is also a saved second of the Pi's WiFi-AP keeping the radio hot.
2. **One source of truth for `state_hash`.** With split endpoints, the server has to carefully coordinate hashes across responses. With a single payload, the hash is just `sha256` of the payload's content fields.
3. **Generic for future clients.** A second e-paper, an LCD kiosk, or a volunteer's diagnostic phone all consume the same JSON and pick what to render.
4. **Easier to evolve.** New fields are additive; clients ignore unknown fields. Adding a new endpoint forces a coordinated client + server release.

### When to add `GET /api/display/health` (v0.1+)

If profiling shows the Pi can't sustain N clients × 1 full GET / 5 min, add a tiny endpoint:

```json
{ "state_hash": "7f3a2c91", "next_poll_seconds": 300, "server_time": "..." }
```

Client first GETs `/health`; on `state_hash` change, follows up with `/state`. This adds round-trip latency on changes but lets unchanged states be ~100-byte responses. Don't ship this in v0.

---

## G. Verification checklist (the 13 explicit questions)

1. **Exact Inkplate class and init sequence; V1 vs V2.**
   ```cpp
   #include <Inkplate.h>
   Inkplate display(INKPLATE_1BIT);   // src/Inkplate.cpp:29
   void setup() {
     display.begin();                 // src/Inkplate.cpp:55
     display.clearDisplay();          // src/boards/Inkplate6/Inkplate6Driver.cpp:227
     display.display();               // first full refresh
   }
   ```
   **Targeting V2** (`ARDUINO_INKPLATE6V2`, Soldered/purple PCB) — current shipping revision; V2 also has slightly cleaner deep-sleep current per `gpioInit` (`Inkplate6Driver.cpp:846–854`). Firmware works unchanged on V1.

2. **Exact full-refresh method.** `display.display(bool _leaveOn = false)` — defined `src/boards/Inkplate6/Inkplate6Driver.cpp:246`. Always pass nothing (defaults to `false` → panel powers off after refresh).

3. **Exact partial-refresh method.** `display.partialUpdate(bool _forced = false, bool _leaveOn = false)` — `Inkplate6Driver.cpp:433`. Returns `uint32_t changeCount` (count of pixels going black-to-white). Caveats in §1 and §C apply.

4. **Does the screen persist through ESP32 deep sleep?** **Yes — documented.** `docs/inkplate/6/deepsleep/sleep_simple.md:11`: *"e‑Paper does not require any power to retain the displayed image."* Plus the Simple_Deep_Sleep example overwrites the previous still-visible image on each wake (`Inkplate6_Simple_Deep_Sleep.ino:71–74`).

5. **Simplest reliable timer-based sleep/wake pattern.**
   ```cpp
   #define uS_TO_S 1000000ULL
   esp_sleep_enable_timer_wakeup((uint64_t)next_poll_seconds * uS_TO_S);
   esp_deep_sleep_start();
   ```
   `docs/inkplate/6/deepsleep/sleep_simple.md:20–28`; `examples/Inkplate6/Advanced/DeepSleep/Inkplate6_Simple_Deep_Sleep/Inkplate6_Simple_Deep_Sleep.ino:81–82`.

6. **Can the touchpads wake from deep sleep?** **No — not as a first-class library feature.** Pads are read via `display.touchpad.read(N)` over I²C against expander1 (`src/features/touchpad/touchpad.cpp:11–28`). No wake API; no example. To do it, you'd have to configure the I²C expander INT line and wire it to an RTC-capable ESP32 GPIO yourself. **Defer.** And on V2 (`ARDUINO_INKPLATE6V2`) there are no pads at all (`docs/inkplate/touchpads/overview.md:13`).

7. **Battery telemetry exposed.** `display.readBattery()` → `double` voltage in volts (`Inkplate6Driver.cpp:967–1005`; `Inkplate6Driver.h:65`). `display.readTemperature()` → `int8_t` °C (`Inkplate6Driver.cpp:1189`). `display.isPowerGood()` → bool. **No percent or charging state exposed by the library** — the MCP73831 charger LED is hardware-only.

8. **How to draw an inverted (white-on-black) header.**
   ```cpp
   display.fillRect(0, 0, display.width(), 60, BLACK);
   display.setTextColor(WHITE, BLACK);   // src/Inkplate.h, via Adafruit GFX setTextColor(fg, bg)
   display.setCursor(20, 18);
   display.setTextSize(3);
   display.print("CIVICMESH  HUB A1");
   ```
   Pattern from `examples/Inkplate6/Basic/Inkplate6_Black_And_White/Inkplate6_Black_And_White.ino:310–317`.

9. **Built-in QR support — yes/no.** **No.** Confirmed by exhaustive grep of `src/`. For v0 if needed, use `ricmoo/QRCode` and render module-by-module via `display.fillRect`.

10. **Closest official example to "WiFi fetch → render → sleep".** `examples/Inkplate6/Projects/Inkplate6_Quotables/Inkplate6_Quotables.ino` — same 5-minute cadence, 1‑bit BW, single deep-sleep at the end of `setup()`. Borrow the structure; replace the unbounded retry (`while (!network.getData(...))` at lines 144–148) with a bounded retry that falls through to a failure screen and a backoff sleep.

11. **Should v0 use partial refresh at all?** **No.** Reasoned in §1 and §E.

12. **Top 5 implementation traps to avoid.**
    1. Naive `partialUpdate()` after a deep-sleep wake silently full-refreshes (because `_blockPartial=1` on every fresh boot). Either accept the full or use the documented `preloadScreen` + `partialUpdate(true)` dance — no third option.
    2. Don't combine `display(true)` (`leaveOn`) with `esp_deep_sleep_start()` — you'll bleed milliamps with the EPD rails left live.
    3. `connectWiFi` does a 1-second leading `delay` before the first status check (`NetworkController.cpp:54`). Plan for ~24 s worst-case on a missing AP.
    4. `while (!network.getData(...))` patterns from official examples have no retry limit. Replace with bounded retry + failure screen + sleep.
    5. `ESPmDNS` / `*.local` is a separate library; `civicmesh.internal` requires the Pi to actually serve that DNS name. Always ship a static-IP fallback in config.

13. **Server endpoint shape — single all-in-one, or split?** **Single `GET /api/display/state`** for v0. Reasoning in §F: WiFi association dominates the wake-cycle energy budget, so one round-trip beats three; ETag + `If-None-Match` covers the bandwidth case for unchanged content. Add `/api/display/health` only if profiling later shows the Pi can't sustain the load — don't ship it in v0.
