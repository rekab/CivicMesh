# Render library notes

Library shape: `inkplate/render/` is an Arduino library (it has a
`library.properties`). The host build at `inkplate/host/` includes its
sources directly via `-I../render/src`; the ESP32 build (PR 2) will
let arduino-cli discover it via `--libraries ..` from
`inkplate/firmware/`.

## Adafruit_GFX-only dependency rule

`render/src/` must include only `Adafruit_GFX.h` and the ArduinoJson
headers (`ArduinoJson.h`). No `Inkplate.h`, no `Arduino.h`, no
`WiFi.h`, no `esp_*`. The ESP32 sketch in `firmware/bulletin/` (PR 2)
owns everything from those headers and hands a configured `Inkplate`
instance to `render_frame()`.

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

## Phase 2 library footguns (the three that bite during PR 1)

Cross-reference `docs/inkplate-research.md` §C ("Gotchas and risks
list") for the full taxonomy.

1. **`clearDisplay()` is framebuffer-only.** A subsequent `display()`
   call is required to push to the panel. The host PNG writer doesn't
   care; PR 2's `bulletin.ino` must call `display.display()` after
   `render_frame()` returns.
2. **`setRotation` swaps W/H for subsequent draws.** Don't call. If
   you do, call exactly once before any draws — never mid-frame.
3. **`drawTextBox` is an Inkplate library extension** (not in
   upstream Adafruit_GFX), and its `...` truncation behavior with a
   custom GFX font is unverified. PR 1 sidesteps this entirely by
   shipping `text_wrap.cpp` — a greedy word-wrap that calls
   `getTextBounds` to measure. PR 2 should NOT swap back to
   `drawTextBox`; the wrap helper keeps host and ESP32 byte-identical.

## Phase 3 footguns (out of scope here; flagged for the next person)

- **PSRAM allocation.** Inkplate's 1-bit framebuffer requires 8MB
  PSRAM; if `display.begin()` returns false at runtime, PSRAM init
  failed. Phase 3 sketch needs the failure handling.
- **`display(true)` + deep-sleep interaction.** Calling
  `display(true)` (partial refresh) then immediately deep-sleeping
  leaves the panel in an undefined state. Phase 3 control loop must
  separate refresh from sleep.
- **`getString` heap fragmentation.** Multiple long-string ops on
  ESP32 fragment the heap quickly. Phase 3 should pool buffers.
- **WiFi-active `readBattery` noise.** Battery ADC reads are wildly
  inaccurate while WiFi radio is on. Phase 3 must `WiFi.disconnect()`
  before sampling, or sample only in deep-sleep pre-wake.

## Color polarity

`layout.h` defines `COLOR_BLACK = 1` and `COLOR_WHITE = 0`. This
matches GFXcanvas1's "set bit = drawn" convention, which the host PNG
writer renders as black ink on white paper. If the Inkplate library's
1-bit mode uses the opposite polarity for its panel framebuffer, PR
2's `bulletin.ino` is the right place to flip — *not* this library.
The renderer should remain panel-agnostic.

## Memory posture

Host build uses `std::string` / `std::vector` freely; ArduinoJson v7's
`JsonDocument` grows on demand. ESP32 has 8MB PSRAM, so the same
posture works there too, but Phase 3 may want to switch to
`StaticJsonDocument`-equivalent or fixed-size buffers if heap
fragmentation becomes a problem (see `getString` footgun above).
