# Inkplate 6 firmware build/flash workflow.
#
# All targets that open the serial port pre-flight through check-port.sh,
# which refuses anything that isn't a CH340 (VID 1a86). See README.md.
#
# Two sketches live here:
#   hello/     — Phase 0 hardware-validation Hello World (default).
#   fortunes/  — Random-fortune demo: pick from a ~650-entry BSD corpus,
#                render full-panel, deep-sleep 1-5 minutes, repeat.
# Switch sketches per-invocation:
#     make flash SKETCH_DIR=fortunes

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
	@echo "  SKETCH_DIR=$(SKETCH_DIR)    (e.g. SKETCH_DIR=fortunes)"

check-port:
	@$(CHECK_PORT) $(INKPLATE_PORT)

compile:
	arduino-cli compile --fqbn $(FQBN) $(SKETCH_DIR)

flash: check-port
	arduino-cli compile --fqbn $(FQBN) --upload -p $(INKPLATE_PORT) $(SKETCH_DIR)

monitor: check-port
	arduino-cli monitor -p $(INKPLATE_PORT) -c baudrate=115200

flash-monitor: flash monitor

# Regenerate the fortunes header from /usr/share/games/fortunes/. Run
# after editing the script or after the source corpus changes; commit
# the resulting fortunes/fortunes_data.h.
fortunes-regenerate:
	python3 $(FORTUNES_BUILDER)
