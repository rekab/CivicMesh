# Inkplate display

Renderer + Arduino sketches for the Inkplate 6 e-paper display. Two
pieces live here:

1. **Renderer** (`render/` + `host/` + `tools/` + `fixtures/`) — an
   Adafruit_GFX-only C++ library that turns the
   `/api/external-display/state` payload (plus a local "envelope" of
   device state) into a 1-bit 800×600 frame. The same source
   compiles for ESP32 (Inkplate library) and for the host PNG
   renderer (g++).
2. **Firmware sketches** (`firmware/hello/`, `firmware/fortunes/`,
   `firmware/bulletin/`) — Arduino sketches for the panel. `hello`
   and `fortunes` are standalone scaffolding that validates wiring,
   board revisions, and the wake/render/sleep cycle. `bulletin` is
   the Phase 3A production firmware: associate WiFi, poll
   `/api/external-display/state`, render through `render/`, repeat.
   Fibonacci poll cadence with 5-min channel rotation, activity-jump
   on new server-side messages, hybrid-refresh signature (skip
   `display.display()` when the visible state hasn't changed). On
   transient failures (WiFi/DNS/TCP/HTTP/JSON), renders
   `failure_shell` and keeps polling; after 5 consecutive failures
   or critical battery (<3.6V) it deep-sleeps until WAKE. Press
   WAKE during normal operation to force-poll and jump to the
   freshest-active channel.

Phase 3B+ work (NVS last-good cache, RTC / real
`seconds_since_last_update`, adaptive layouts, OTA) is the next
chunk; the roadmap lives in `docs/inkplate-research.md`. Server-side
contract that feeds the renderer is at `docs/external-display-api.md`.

### Server endpoints consumed

The bulletin firmware reads three HTTP endpoints per refresh cycle.
None are Inkplate-owned — they're general server routes the Inkplate
happens to consume. Treat this list as a soft dep registry: if you're
about to change one of these endpoints, the Inkplate renderer is
downstream.

| Endpoint | Used for | Cadence |
|---|---|---|
| `GET /api/external-display/state` | Chat content + hub identity (channels, messages, site_name, callsign). Schema in `docs/external-display-api.md`. | Every active-state poll (Fibonacci 10→180 s); once per deep-sleep wake. |
| `GET /api/stats` | UI_SPEC §5 nerd strip — uptime, CPU load + temp, available memory, outbox depth, WiFi session counts, `messages_seen.hour.bars` for the sparkline. Already cached 20 s server-side, so polling cost is bounded. | Once per deep-sleep wake (~5 min) is enough; can poll every N active-state polls instead. |
| `GET /api/status` | Captive-portal radio/mesh_bot status (`radio_status`, `age_sec`). Mesh_bot liveness, not radio analog state. | Same cadence as `/api/stats` — they update on the same timescale. |

Renderer-side parsing for the three responses lives in `render/src/`:
`payload.h` (the chat payload), `stats.h` (telemetry subset), and
`status.h` (radio status subset). All three are passed into
`draw_bulletin` as separate structs; the host_render JSON shape is
`{envelope, payload, stats, status}` so the contact-sheet harness can
fabricate `stats` and `status` without standing up a live server.

## Renderer

### Directory layout

| Directory | What's there | Where to look first |
|---|---|---|
| `render/` | C++ render library + screens + layout constants + generated `LessPerfectDOSVGA.h` font header. Adafruit_GFX-only — no Arduino-runtime dependency. | `render/NOTES.md` for the dep rule + Inkplate-library footguns |
| `host/` | g++ build of the renderer with vendored Adafruit_GFX (+ host-compat shims), lodepng, ArduinoJson. Produces `host_render`, a stdin-to-stdout PNG driver. | `host/Makefile`, `host/vendor/README.md` for upstream pins |
| `fixtures/` | 15 golden test cases. Each directory has a combined envelope+payload `in.json` and a committed 800×600 `expected.png`. | `fixtures/normal_bulletin__pinned_first/` |
| `tools/` | Font + fixture regen scripts plus a live-server fetch helper (see table below). | `tools/regen_fixtures.sh` |

### Iteration loop

The renderer is built once, then driven from JSON fixtures or live
server output. Layout changes don't require flashing — edit a screen,
`make`, render a PNG, eyeball, repeat.

```bash
cd inkplate/host && make             # builds host_render

# Render one fixture
./host_render < ../fixtures/normal_bulletin__pinned_first/in.json > /tmp/f.png

# Re-render every fixture and diff against committed goldens
../tools/regen_fixtures.sh --check

# Regenerate goldens after an intentional layout change
../tools/regen_fixtures.sh --write
git diff inkplate/fixtures/          # inspect visual delta before committing
```

If the dev hub is on a separate machine (per the CivicMesh dev-Pi
setup), view PNGs from a desktop with
`scp civicmesh:/tmp/f.png ~/Downloads/`.

### Tools

All scripts live in `inkplate/tools/`. None of them touch a serial
port; they're host-side only.

| Script | Purpose |
|---|---|
| `regen_fixtures.sh --check` | Re-render every fixture, fail on any drift vs the committed `expected.png`. Builds `host_render` if missing. |
| `regen_fixtures.sh --write` | Re-render and overwrite goldens. Use after an intentional layout change; inspect via `git diff` before committing. |
| `render-live.sh [path]` | Fetch `/api/external-display/state` from the dev hub, wrap with a stock envelope, render to PNG. Set `HUB=http://...` to override. Useful for sanity-checking the renderer against real server output. |
| `regen_font.sh` (`regen_font.py`) | Regenerate `render/src/fonts/LessPerfectDOSVGA.h` from the vendored TTF. Uses Python+freetype rather than Adafruit's C `fontconvert` because the dev Pi has `python3-freetype` but not `libfreetype-dev`. Set `PX=N` to change native glyph height (default 16). |

Example — render the live server's current payload to a PNG:

```bash
# Default: HUB=http://civicmesh (matches the dev Pi's hostname)
inkplate/tools/render-live.sh /tmp/live.png

# Against a hub on a different host or port:
HUB=http://localhost:8080 inkplate/tools/render-live.sh /tmp/live.png
```

The vendored TTF (`tools/LessPerfectDOSVGA.ttf`) and the generated
`render/src/fonts/LessPerfectDOSVGA.h` are both committed; the regen
script is for when the font changes.

### Hardware-free verification

```bash
(cd inkplate/host && make clean && make)        # clean build, no warnings
inkplate/tools/regen_fixtures.sh --check         # 15/15 fixtures pass
grep -rE 'Inkplate\.h|esp_|Arduino\.h|WiFi\.h' inkplate/render/src/
                                                 # returns nothing
```

## Firmware sketches

| Directory | What it does | When to use |
|---|---|---|
| `firmware/hello/` | Phase 0 Hello World. Renders three static text lines, no networking, no sleep. Adafruit-GFX-only API surface so the renderer can compile against `GFXcanvas1` on the host. | First-boot smoke test after wiring up a new board, or whenever you want a known-good reference sketch. |
| `firmware/fortunes/` | Picks a random fortune from a ~650-entry corpus, renders it full-panel via `drawTextBox`, deep-sleeps 1-5 random minutes, repeats. Exercises `esp_random`, `drawTextBox` word-wrap, and the deep-sleep wake cycle. Corpus extracted from `fortunes-min` by `firmware/tools/build_fortunes.py` (see NOTICE for the BSD attribution). | Demoing the panel without the full CivicMesh stack, or validating the wake/render/sleep loop before building the production renderer on top of it. |
| `firmware/bulletin/` | Phase 3A production firmware. WiFi-associate, fetch `/api/external-display/state`, wrap in a firmware-built envelope, render, repeat. Fibonacci poll cadence (10→300s, resets on activity/failure); 5-min channel rotation between polls; activity-jump on new server-side messages; failure_shell on transient failures; deep sleep after 5-failure streak or critical battery; dedicated critical_battery / api_mismatch screens. WAKE button (GPIO 36) force-polls during normal op and wakes from deep sleep. Requires `SSID=<wifi-ssid>` at `make` time; `ENDPOINT_URL` defaults to `http://10.0.0.1/api/external-display/state`. | The production firmware. Flash whenever you change the renderer or want to test the panel against a real (or dev) hub. |

The Makefile defaults to `bulletin`; switch with `SKETCH_DIR=hello`
or `SKETCH_DIR=fortunes`. The bulletin sketch is the only one that
calls the renderer; hello and fortunes are stand-alone
hardware-validation sketches.

Flash the bulletin (after the Prerequisites and the Inkplate is
plugged in on a CH340 port):

```bash
cd inkplate/firmware
make compile SSID=civicmesh-dev                                       # build only, no port access
make flash   SSID=civicmesh-dev                                       # check-port + upload (uses default ENDPOINT_URL)
make flash   SSID=civicmesh-dev ENDPOINT_URL=http://civicmesh:8080/api/external-display/state
make monitor                                                          # watch serial: [bulletin] boot / ...
```

`SSID` is required for bulletin builds — the Makefile hard-errors at
make-time if missing, so an empty SSID can't silently produce a
sketch that associates with nothing and renders failure_shell
forever.

## Prerequisites

You're in the `dialout` group (otherwise `/dev/ttyUSB*` access is denied):

```bash
sudo usermod -aG dialout "$USER"   # then log out and back in
```

`arduino-cli` + the Soldered (Dasduino) board manager URL + the
`Inkplate_Boards:esp32` core + the `InkplateLibrary` Arduino library.
The bulletin sketch additionally needs `ArduinoJson`. One-shot
install (intentionally not automated — audit each line before
running):

```bash
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
arduino-cli config init
arduino-cli config add board_manager.additional_urls \
  https://github.com/SolderedElectronics/Dasduino-Board-Definitions-for-Arduino-IDE/raw/master/package_Dasduino_Boards_index.json
arduino-cli core update-index
arduino-cli core install Inkplate_Boards:esp32
arduino-cli lib install InkplateLibrary
arduino-cli lib install ArduinoJson
```

**Do NOT install the standalone `Adafruit GFX Library`.** InkplateLibrary
v11 vendors its own copy at `InkplateLibrary/src/graphics/Adafruit_GFX/`,
and installing the standalone alongside it produces a link-time
duplicate-symbol error (both copies define `GFXcanvas16`, etc.). The
renderer's `#include <Adafruit_GFX.h>` resolves to the bundled copy via
a `--build-property compiler.cpp.extra_flags=-I<bundled-path>` injected
by `firmware/Makefile`. If you previously installed the standalone, run
`arduino-cli lib uninstall "Adafruit GFX Library"`.

Verify the install with `arduino-cli board listall | grep -i inkplate` —
that lists the exact FQBN strings the installed core exposes (one per
board variant; pick the one matching your hardware revision).

### Library discovery for the bulletin sketch — fallback ladder

`firmware/Makefile` passes `--libraries ..` so arduino-cli scans
`inkplate/` for sibling libraries and finds `render/` (which has
`library.properties`). If `make compile SKETCH_DIR=bulletin` fails
with `cannot find render.h` or `no such library 'CivicMeshRender'`,
walk these alternatives in order — no re-research needed:

1. **Plan A (Makefile default):** `arduino-cli compile ... --libraries .. <sketch>` — container scan. Verified working on the dev Pi.
2. **Plan B:** `arduino-cli compile ... --library ../render <sketch>` — singular flag, exact library path. Edit both occurrences of `--libraries ..` in `firmware/Makefile` to `--library ../render`.
3. **Plan C (most portable; works for the Arduino IDE too):** symlink the renderer library into the user's sketchbook and drop the flag entirely:
   ```bash
   mkdir -p ~/Arduino/libraries
   ln -s "$(realpath inkplate/render)" ~/Arduino/libraries/CivicMeshRender
   ```
   Then remove `--libraries ..` from both targets in `firmware/Makefile`.

The same ladder is duplicated in the comment block above `compile:` in
`firmware/Makefile` for operators who go straight to the Makefile.

## Find your Inkplate's serial port

Plug each device in **separately** (Heltec unplugged, Inkplate plugged
in) and run:

```bash
ls /dev/serial/by-id/
```

The Inkplate uses a CH340 USB-serial chip and shows up as something like
`usb-1a86_USB_Serial-if00-port0` or
`usb-1a86_USB_Single_Serial_<serial>-if00-port0`.

Edit the `INKPLATE_PORT` default at the top of `firmware/Makefile`, or
override per-invocation:

```bash
make flash INKPLATE_PORT=/dev/serial/by-id/usb-1a86_...
```

## V1 vs V2

The Inkplate 6 ships in two PCB revisions and they use different
Arduino board macros:

| Revision | PCB | Board macro | I/O expander | Touchpads |
|---|---|---|---|---|
| V1 | e-Radionica (older, blue) | `ARDUINO_INKPLATE6` | MCP23017 | 3 capacitive |
| V2 | Soldered (newer, purple) | `ARDUINO_INKPLATE6V2` | PCAL6416A | none |

Both revisions carry an ESP32-WROVER-family RF module with 8MB PSRAM
+ 4MB flash. The big PSRAM is why the Inkplate library can keep a
full 800×600 panel framebuffer in RAM without compressing it — every
sketch using `Inkplate(INKPLATE_1BIT)` (or 3BIT) implicitly relies on
PSRAM being present and enabled.

| Revision | Module (shielded RF can on PCB) | Silicon (what esptool reports) |
|---|---|---|
| V1 | ESP32-WROVER | typically `ESP32-D0WDQ6` (pre-V3 silicon) — verify per board |
| V2 | ESP32-WROVER-E | `ESP32-D0WD-V3` (revision v3.0) — confirmed via `make flash` against a current Soldered unit |

The "module" name is silkscreened on the RF can; the "silicon" name
is the underlying die that esptool reads out of the chip itself. They
are not the same identifier — a WROVER-E module contains a D0WD-V3
die. Most flashing / IDF / Arduino-ESP32 documentation keys off the
module name (and the FQBN above), so that's the more useful one for
sketch decisions. The silicon ID matters only for revision-specific
errata fixes.

The Makefile's `FQBN` default (`Inkplate_Boards:esp32:Inkplate6V2`)
targets V2 — the current shipping revision. If you have a V1 unit,
override at the command line (or change the Makefile default):

```bash
make flash FQBN=Inkplate_Boards:esp32:Inkplate6
```

Confirm the exact FQBN strings your installed core exposes with
`arduino-cli board listall | grep -i inkplate`.

Full board-revision detail is in `docs/inkplate-research.md` §0; the
recommended Hello-World init sequence (which this sketch uses) is in §G.1.

## Workflow

All from `firmware/`:

| Command | Purpose | Needs hardware? |
|---|---|---|
| `make` (or `make help`) | List targets and current variable values | No |
| `make compile` | Verify the sketch builds | No |
| `make check-port` | Sanity-check `INKPLATE_PORT` resolves to CH340 | Yes |
| `make flash` | Compile + upload (pre-flights `check-port` automatically) | Yes |
| `make monitor` | Open serial monitor at 115200 (pre-flights `check-port`) | Yes |
| `make flash-monitor` | Flash, then open serial monitor | Yes |
| `make fortunes-regenerate` | Rebuild `fortunes/fortunes_data.h` from `/usr/share/games/fortunes/` (requires the `fortunes-min` Debian package installed) | No |

To switch which sketch a target operates on, override `SKETCH_DIR`:

```bash
make flash SKETCH_DIR=fortunes
make flash-monitor SKETCH_DIR=hello
```

The default is `SKETCH_DIR=bulletin` (the production firmware).
`SKETCH_DIR=hello` preserves the Phase 0 smoke-test workflow for a
fresh board; `SKETCH_DIR=fortunes` is the deep-sleep demo.

**If `make flash` fails with `Failed to connect to ESP32: No serial
data received`** (after a long row of `Connecting......` dots) the
Inkplate's ESP32 is asleep and esptool's auto-reset isn't waking it.
Press the **WAKE button** on the back of the panel and rerun `make
flash`. The fortunes sketch enters deep sleep right after rendering,
so this is the normal case for re-flashing it.

## The safety check, explained

`scripts/check-port.sh` rejects anything that isn't VID `1a86` (CH340).
The high-stakes case it protects against:

> The dev/deploy Pi has the Heltec V3 plugged in over USB as well as
> the Inkplate. The Heltec runs MeshCore, holding the radio's keys and
> identity. Flashing an Inkplate Arduino sketch to the Heltec port
> would brick the radio's firmware — including its keys.

USB chip and VID by device:

| Device | Chip | VID | Behavior on flash |
|---|---|---|---|
| Inkplate 6 | CH340 | `1a86` | Allow |
| Heltec V3 | CP2102 | `10c4` | Loud refusal: "REFUSING to flash to avoid bricking the radio" |
| Anything else | — | — | Hard refusal |

The check reads `ID_VENDOR_ID` via `udevadm info --query=property` and
hard-fails on every ambiguity: missing port, symlink to nothing,
`udevadm` failure, missing VID field, any VID other than `1a86`. There
is no fallback path; there is no `/dev/ttyUSB0` guess.

## Acceptance

- `make check-port` succeeds (exits 0, prints `OK: ... is CH340 (Inkplate)`
  to stderr) when `INKPLATE_PORT` points at the Inkplate's by-id path.
- `make check-port INKPLATE_PORT=/dev/serial/by-id/usb-Silicon_Labs_CP2102_*`
  fails loudly with the "REFUSING to flash" message.
- `make flash` (hello sketch) compiles, uploads, and the panel does one
  full refresh showing three text lines: `CivicMesh`, `Hello, Inkplate`,
  `hardware validation step 1`.
- `make flash SKETCH_DIR=fortunes` compiles, uploads, and the panel
  shows a single random fortune plus a footer of the form
  `#412 / 646  -  next refresh in 3 min`. The footer index and "next
  refresh in N min" value should change on every wake (every 1-5 min).
