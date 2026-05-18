# Inkplate 6 firmware build/flash workflow.
#
# All targets that open the serial port pre-flight through check-port.sh,
# which refuses anything that isn't a CH340 (VID 1a86). See README.md.
#
# Three sketches live here:
#   hello/     — Phase 0 hardware-validation Hello World (default).
#   fortunes/  — Random-fortune demo: pick from a ~650-entry BSD corpus,
#                render full-panel, deep-sleep 1-5 minutes, repeat.
#   bulletin/  — Phase 2 smoke target. Renders one hard-coded fixture
#                JSON through ../render/, halts. Requires arduino-cli
#                discovery of the renderer library via --libraries ..
#                (see "Library discovery" comment below).
# Switch sketches per-invocation:
#     make flash SKETCH_DIR=fortunes
#     make flash SKETCH_DIR=bulletin

# The Inkplate's stable by-id path. This default is the real path on the
# CivicMesh dev Pi. On other hardware, find your unit's path with:
#     ls /dev/serial/by-id/
# and override per-invocation:
#     make flash INKPLATE_PORT=/dev/serial/by-id/...
# NEVER fall back to /dev/ttyUSB0 — that path is ambiguous when both
# the Inkplate and Heltec are plugged in. check-port.sh enforces this.
INKPLATE_PORT ?= /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0

# Fully-Qualified Board Name for arduino-cli.
# This default targets V2 (Soldered, purple PCB) — the current shipping
# revision. V1 (e-Radionica, older blue PCB) uses Inkplate_Boards:esp32:Inkplate6.
# Verify your board's exact FQBN with:
#     arduino-cli board listall | grep -i inkplate
# See README.md "V1 vs V2" for the hardware-revision details.
FQBN ?= Inkplate_Boards:esp32:Inkplate6V2

# Which sketch directory to build/flash. Override on the command line
# rather than editing this default; the hello/ default keeps the
# Phase 0 smoke-test workflow stable.
SKETCH_DIR ?= hello

CHECK_PORT := scripts/check-port.sh
FORTUNES_BUILDER := tools/build_fortunes.py

# InkplateLibrary v11 bundles its own Adafruit_GFX at
# src/graphics/Adafruit_GFX/. Our renderer (../render/) includes
# <Adafruit_GFX.h> directly. Installing the standalone "Adafruit GFX
# Library" via arduino-cli causes a link-time duplicate-symbol conflict
# (both copies define GFXcanvas16, etc.) because arduino-cli compiles
# both. Workaround: don't install the standalone, and add InkplateLibrary's
# nested Adafruit_GFX dir to the include path so <Adafruit_GFX.h>
# resolves to the bundled copy. See inkplate/README.md Prerequisites.
ARDUINO_USER_DIR ?= $(shell arduino-cli config get directories.user 2>/dev/null)
INKPLATE_GFX_DIR := $(ARDUINO_USER_DIR)/libraries/InkplateLibrary/src/graphics/Adafruit_GFX
BUNDLED_GFX_BUILD_PROP := --build-property compiler.cpp.extra_flags=-I$(INKPLATE_GFX_DIR)

.PHONY: help check-port compile flash monitor flash-monitor fortunes-regenerate

help:
	@echo "Targets:"
	@echo "  check-port            verify INKPLATE_PORT is a CH340 (Inkplate), not a CP2102 (Heltec)"
	@echo "  compile               arduino-cli compile (no port access)"
	@echo "  flash                 check-port + arduino-cli compile --upload"
	@echo "  monitor               check-port + arduino-cli monitor at 115200"
	@echo "  flash-monitor         flash, then monitor"
	@echo "  fortunes-regenerate   rebuild fortunes/fortunes_data.h from /usr/share/games/fortunes"
	@echo ""
	@echo "Overrides:"
	@echo "  INKPLATE_PORT=$(INKPLATE_PORT)"
	@echo "  FQBN=$(FQBN)"
	@echo "  SKETCH_DIR=$(SKETCH_DIR)    (e.g. SKETCH_DIR=fortunes, SKETCH_DIR=bulletin)"

check-port:
	@$(CHECK_PORT) $(INKPLATE_PORT)

# Library discovery for the renderer at ../render/ (used by the bulletin
# sketch). Plan A is the default here. If `make compile SKETCH_DIR=bulletin`
# fails with "cannot find render.h" or "no such library 'CivicMeshRender'",
# fall back in order:
#   Plan A (current):  --libraries .. scans the parent dir (inkplate/) for
#                      any subdir with a library.properties. Only render/
#                      qualifies; host/vendor/ is too deep to be picked up.
#   Plan B:            --library ../render points directly at the library
#                      directory (singular flag, exact path). Swap the two
#                      occurrences of `--libraries ..` for `--library ../render`.
#   Plan C:            Symlink the library into the user's Arduino sketchbook
#                      and drop the flag entirely (most portable; also makes
#                      the library visible to the Arduino IDE):
#                          mkdir -p ~/Arduino/libraries
#                          ln -s "$$(realpath ../render)" ~/Arduino/libraries/CivicMeshRender
#                      Then drop `--libraries ..` from both targets below.
#                      Document the symlink step in inkplate/README.md.

compile:
	arduino-cli compile --fqbn $(FQBN) --libraries .. \
		$(BUNDLED_GFX_BUILD_PROP) $(SKETCH_DIR)

flash: check-port
	arduino-cli compile --fqbn $(FQBN) --libraries .. \
		$(BUNDLED_GFX_BUILD_PROP) \
		--upload -p $(INKPLATE_PORT) $(SKETCH_DIR)

monitor: check-port
	arduino-cli monitor -p $(INKPLATE_PORT) -c baudrate=115200

flash-monitor: flash monitor

# Regenerate the fortunes header from /usr/share/games/fortunes/. Run
# after editing the script or after the source corpus changes; commit
# the resulting fortunes/fortunes_data.h.
fortunes-regenerate:
	python3 $(FORTUNES_BUILDER)
