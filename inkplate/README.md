# Inkplate firmware

Phase 0 hardware-validation Hello World for the Inkplate 6 e-paper display.
No WiFi, HTTP, CivicMesh data, or deep sleep yet — just proving the
arduino-cli toolchain compiles, the USB cable + auto-reset chain works, and
the panel does one full refresh. Pairs eventually with the server-side
contract at `docs/external-display-api.md`.

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

| Revision | PCB | Board macro | Notable |
|---|---|---|---|
| V1 | e-Radionica (older, blue) | `ARDUINO_INKPLATE6` | 3 capacitive touchpads, MCP23017 I/O expander |
| V2 | Soldered (newer, purple) | `ARDUINO_INKPLATE6V2` | No touchpads, PCAL6416A I/O expander |

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

**If `make flash` fails with `Failed to connect to ESP32: No serial
data received`** (after a long row of `Connecting......` dots) the
Inkplate's ESP32 is asleep and esptool's auto-reset isn't waking it.
Press the **WAKE button** on the back of the panel and rerun `make
flash`.

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
- `make flash` compiles, uploads, and the panel does one full refresh
  showing three text lines: `CivicMesh`, `Hello, Inkplate`,
  `hardware validation step 1`.
