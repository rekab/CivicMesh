# Inkplate 6 firmware build/flash workflow.
#
# All targets that open the serial port pre-flight through check-port.sh,
# which refuses anything that isn't a CH340 (VID 1a86). See README.md.
#
# Three sketches live here:
#   bulletin/  — Phase 3A external-display firmware (default). Polls
#                /api/external-display/state, renders via ../render/.
#                REQUIRES SSID=<wifi-ssid>; ENDPOINT_URL and FW_VERSION
#                have defaults. Example:
#                    make flash SSID=civicmesh-dev
#                    make flash SSID=civicmesh-dev ENDPOINT_URL=http://civicmesh:8080/api/external-display/state
#   hello/     — Phase 0 hardware-validation Hello World.
#   fortunes/  — Random-fortune demo: pick from a ~650-entry BSD corpus,
#                render full-panel, deep-sleep 1-5 minutes, repeat.
# Switch sketches per-invocation:
#     make flash SKETCH_DIR=hello
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
# rather than editing this default. `bulletin` is the Phase 3A production
# target; hello/fortunes remain reachable via SKETCH_DIR=hello.
SKETCH_DIR ?= bulletin

# Bulletin-only build inputs. SSID is required (the require-bulletin-config
# guard below hard-errors at make-time if missing). ENDPOINT_URL defaults
# to the lab AP's gateway IP per docs/external-display-api.md; override
# for dev (e.g. http://civicmesh:8080/api/external-display/state).
SSID ?=
ENDPOINT_URL ?= http://10.0.0.1/api/external-display/state
FW_VERSION ?= 0.1.0

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

# arduino-cli takes only the LAST --build-property of the same key, so
# the bulletin's -D flags must ride on the same compiler.cpp.extra_flags
# as the bundled-GFX -I. Quoting: arduino-cli passes the property value
# straight to the compiler (no intermediate shell), so we only need to
# satisfy make + bash; the backslash-escaped \" survives bash's outer
# double-quote and arrives at the compiler as a literal ", which is
# what's needed to form a valid C string literal in the macro value.
# (Single-quoting the \" would survive bash but become a LITERAL ' in
# the compiler arg — Plan A in the prior session — which then breaks
# C parsing. That's why we don't single-quote.)
# If a future SSID contains spaces, this breaks (split on space by
# arduino-cli's property tokenizer) — fall back to a generated header
# (Plan C: write bulletin/build_config.h from a make rule, drop the
# -D flags, sketch #includes it).
COMPILE_EXTRA_FLAGS := -I$(INKPLATE_GFX_DIR) \
    -DCIVICMESH_SSID=\"$(SSID)\" \
    -DCIVICMESH_ENDPOINT=\"$(ENDPOINT_URL)\" \
    -DCIVICMESH_FW_VERSION=\"$(FW_VERSION)\"
BUNDLED_GFX_BUILD_PROP := --build-property "compiler.cpp.extra_flags=$(COMPILE_EXTRA_FLAGS)"

.PHONY: help check-port require-bulletin-config compile flash monitor flash-monitor fortunes-regenerate

# Hard-fail at make-time if a bulletin build was requested without SSID.
# (Compile-time fail per Phase 3A PR 1 spec — no silent default; flashing
# with empty SSID would associate with nothing and render failure_shell
# forever.) Other sketches don't need SSID, so the guard is a no-op there.
require-bulletin-config:
ifeq ($(SKETCH_DIR),bulletin)
ifeq ($(strip $(SSID)),)
	$(error SSID is required for bulletin builds. e.g. make flash SSID=civicmesh-dev)
endif
endif

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
	@echo "  SKETCH_DIR=$(SKETCH_DIR)    (e.g. SKETCH_DIR=hello, SKETCH_DIR=fortunes)"
	@echo ""
	@echo "Bulletin-only (required when SKETCH_DIR=bulletin, the default):"
	@echo "  SSID=<wifi-ssid>            REQUIRED. Open network, no password."
	@echo "  ENDPOINT_URL=$(ENDPOINT_URL)"
	@echo "  FW_VERSION=$(FW_VERSION)"

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

compile: require-bulletin-config
	arduino-cli compile --fqbn $(FQBN) --libraries .. \
		$(BUNDLED_GFX_BUILD_PROP) $(SKETCH_DIR)

flash: require-bulletin-config check-port
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
