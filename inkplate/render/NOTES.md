# Render library notes

Library shape: `inkplate/render/` is an Arduino library (it has a
`library.properties`). The host build at `inkplate/host/` includes its
sources directly via `-I../render/src`. The ESP32 build at
`inkplate/firmware/bulletin/` uses arduino-cli's `--libraries ..`
discovery from `inkplate/firmware/` (see `firmware/Makefile`).

## Adafruit_GFX-only dependency rule

`render/src/` must include only `Adafruit_GFX.h` and the ArduinoJson
headers (`ArduinoJson.h`). No `Inkplate.h`, no `Arduino.h`, no
`WiFi.h`, no `esp_*`. The ESP32 sketch in `firmware/bulletin/` owns
everything from those headers and hands a configured `Inkplate`
instance to `render_frame()` (since `Inkplate` inherits from
`Graphics` which inherits from `Adafruit_GFX`, the polymorphism is
free).

Enforce with:

```sh
grep -rE 'Inkplate\.h|esp_|Arduino\.h|WiFi\.h' inkplate/render/src/
```

This must return nothing. The grep allows ArduinoJson includes.

Why the rule: the renderer is the seam between server-shaped data and
panel-shaped pixels. Keeping it Adafruit_GFX-only means it builds on
the host (where there is no ESP32 runtime) so layout iteration costs
seconds, not flashes.

## Convention signpost — `set_cursor_top_left`

Custom GFX fonts position glyphs from the BASELINE, not the top-left
corner like the built-in 5×7 font. Every screen file uses
`set_cursor_top_left(gfx, x, y, glyph_h)` from `layout.h` so the
top-left convention is uniform regardless of font. If you add a screen
that calls `gfx.setCursor()` directly with a custom font active, the
text will land in the wrong place.

## Phase 2 library footguns

Cross-reference `docs/inkplate-research.md` §C ("Gotchas and risks
list") for the full taxonomy.

1. **`clearDisplay()` is framebuffer-only.** A subsequent `display()`
   call is required to push to the panel. The host PNG writer doesn't
   care; `firmware/bulletin/bulletin.ino` calls `display.display()`
   after `render_frame()` returns — any future renderer consumer
   must do the same.
2. **`setRotation` swaps W/H for subsequent draws.** Don't call. If
   you do, call exactly once before any draws — never mid-frame.
3. **`drawTextBox` is an Inkplate library extension** (not in
   upstream Adafruit_GFX), and its `...` truncation behavior with a
   custom GFX font is unverified. The renderer ships `text_wrap.cpp`
   — a greedy word-wrap that calls `getTextBounds` to measure —
   instead. Don't swap back to `drawTextBox`; the wrap helper keeps
   host PNG and ESP32 panel output byte-identical.

## Phase 3 footguns (status after Phase 3A PR 2)

- **PSRAM allocation.** Inkplate's 1-bit framebuffer (~60KB) and the
  library's working buffers live in external PSRAM. If PSRAM init
  failed, subsequent `display.display()` / `render_frame` calls touch
  unallocated memory. `Inkplate::begin()` is `void` in v11
  (`InkplateLibrary/src/Inkplate.h:40`), so the failure signal is the
  esp32-core's `psramFound()` returning false. **Addressed** in
  `firmware/bulletin/bulletin.ino`'s `setup()` — `psramFound()` is
  checked after `display.begin()` and a false result logs to serial
  and enters deep sleep instead of barreling ahead into UB.
- **`getString` heap fragmentation.** Multiple long-string ops on
  ESP32 fragment the heap quickly. **Addressed** —
  `firmware/bulletin/bulletin_net.cpp` uses `http.getStream()` into
  ArduinoJson, never `http.getString()`.
- **WiFi-active `readBattery` noise.** Battery ADC reads are wildly
  inaccurate while WiFi radio is on. **Addressed** — the bulletin
  sketch's `sample_battery_with_wifi_off()` does
  `WiFi.disconnect(true); WiFi.mode(WIFI_OFF); delay(100);` before
  every `display.readBattery()`.
- **`display(true)` + deep-sleep interaction.** Calling
  `display(true)` (partial refresh) then immediately deep-sleeping
  leaves the panel in an undefined state. **Not triggered today** —
  the bulletin sketch only calls `display.display()` (full refresh).
  Kept here as a warning for any future code that wants to add
  partial-refresh: don't deep-sleep right after one without a settle
  delay or a follow-up full refresh.
- **WAKE-button EMI on GPIO 36.** Not in the original Phase 3
  footgun list, discovered during PR 2 hardware bring-up: the
  e-paper's TPS65186 PMIC switching (±15V rails) induces µs-scale
  transients on GPIO 36's WAKE-button trace, which fire spurious
  FALLING-edge interrupts and cause back-to-back refresh cycles.
  **Addressed** in `firmware/bulletin/bulletin.ino` with two
  filters: ISR-side `gpio_get_level` (kills µs transients shorter
  than ISR-dispatch latency) and a loop-side `digitalRead` confirm
  after the debounce window.

## Color polarity

`layout.h` defines `COLOR_BLACK = 1` and `COLOR_WHITE = 0`. This
matches GFXcanvas1's "set bit = drawn" convention, which the host PNG
writer renders as black ink on white paper.

Verified on hardware during PR 2: the Inkplate library's
`INKPLATE_1BIT` mode uses the same `BLACK=1, WHITE=0` convention
(upstream `src/system/defines.h:33-35`), so the renderer's
framebuffer pushes to the panel without any per-pixel flip. The
on-panel image matches `fixtures/normal_bulletin__pinned_first/
expected.png` directly. If a future Inkplate library version changes
that, the right place to flip is in
`firmware/bulletin/bulletin.ino` between `render_frame()` and
`display.display()` — *not* this library. The renderer stays
panel-agnostic.

## Memory posture

Host build uses `std::string` / `std::vector` freely; ArduinoJson v7's
`JsonDocument` grows on demand. ESP32 has 8MB PSRAM, so the same
posture works there too — Phase 3A PR 1 confirmed this on hardware
(the bulletin sketch's twin `JsonDocument`s, one for the server
response and one for the combined envelope+payload, fit comfortably
in PSRAM). Phase 3B+ may want to switch to fixed-size buffers if
NVS-cache + multi-channel state grows the working set into heap
fragmentation territory; the `getString` footgun above still applies
to any future code that does long-string ops.
