# Inkplate firmware

Arduino sketches for the Inkplate 6 e-paper display. The CivicMesh
production renderer (which will consume `/api/external-display/state`)
is not yet here; what *is* here are two scaffolding sketches that
exercise the toolchain end-to-end on real hardware.

## Sketches

| Directory | What it does | When to use |
|---|---|---|
| `firmware/hello/` | Phase 0 Hello World. Renders three static text lines, no networking, no sleep. Adafruit-GFX-only API surface so the future renderer can compile against `GFXcanvas1` on the host. | First-boot smoke test after wiring up a new board, or whenever you want a known-good reference sketch. |
| `firmware/fortunes/` | Picks a random fortune from a ~650-entry corpus, renders it full-panel via `drawTextBox`, deep-sleeps 1-5 random minutes, repeats. Exercises `esp_random`, `drawTextBox` word-wrap, and the deep-sleep wake cycle. Corpus extracted from `fortunes-min` by `tools/build_fortunes.py` (see NOTICE for the BSD attribution). | Demoing the panel without the full CivicMesh stack, or validating the wake/render/sleep loop before building the production renderer on top of it. |

The Makefile defaults to `hello`; switch with `SKETCH_DIR=fortunes` (see
the Workflow section below).

Pairs eventually with the server-side contract at
`docs/external-display-api.md`.

## Prerequisites

You're in the `dialout` group (otherwise `/dev/ttyUSB*` access is denied):

```bash
sudo usermod -aG dialout "$USER"   # then log out and back in
```

`arduino-cli` + the Soldered (Dasduino) board manager URL + the
`Inkplate_Boards:esp32` core + the `InkplateLibrary` Arduino library.
One-shot install (intentionally not automated — audit each line before
running):

```bash
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | sh
arduino-cli config init
arduino-cli config add board_manager.additional_urls \
  https://github.com/SolderedElectronics/Dasduino-Board-Definitions-for-Arduino-IDE/raw/master/package_Dasduino_Boards_index.json
arduino-cli core update-index
arduino-cli core install Inkplate_Boards:esp32
arduino-cli lib install InkplateLibrary
```

Verify the install with `arduino-cli board listall | grep -i inkplate` —
that lists the exact FQBN strings the installed core exposes (one per
board variant; pick the one matching your hardware revision).

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

The default is `SKETCH_DIR=hello`, which preserves the original Phase 0
smoke-test workflow.

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
