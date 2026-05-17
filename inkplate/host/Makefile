# Host PNG renderer for the CivicMesh Inkplate render library.
#
# No port logic, no arduino-cli, no check-port.sh dependency — this
# binary never opens a serial device. Vendor deps are committed under
# vendor/; see vendor/README.md for upstream sources and pinned SHAs.
#
# host_compat/ shims Arduino.h, Print.h, WProgram.h, Adafruit_I2CDevice.h,
# Adafruit_SPIDevice.h with the minimum surface Adafruit_GFX needs on a
# host. The -Ivendor/Adafruit_GFX/host_compat path comes BEFORE
# -Ivendor/Adafruit_GFX so the stubs resolve those includes.

CXX ?= c++

CXXFLAGS = -std=c++17 -O2 -Wall -Wextra -DHOST_BUILD -DARDUINO=100 \
           -DARDUINOJSON_ENABLE_ARDUINO_STREAM=0 \
           -DARDUINOJSON_ENABLE_ARDUINO_PRINT=0 \
           -DARDUINOJSON_ENABLE_ARDUINO_STRING=0 \
           -DARDUINOJSON_ENABLE_PROGMEM=0 \
           -I../render/src \
           -Ivendor/Adafruit_GFX/host_compat \
           -Ivendor/Adafruit_GFX \
           -Ivendor/lodepng \
           -Ivendor/ArduinoJson/src

RENDER_SRC := $(wildcard ../render/src/*.cpp) \
              $(wildcard ../render/src/screens/*.cpp)

VENDOR_SRC := vendor/Adafruit_GFX/Adafruit_GFX.cpp \
              vendor/lodepng/lodepng.cpp

HOST_SRC := main.cpp png_writer.cpp

SRC := $(RENDER_SRC) $(VENDOR_SRC) $(HOST_SRC)

host_render: $(SRC)
	$(CXX) $(CXXFLAGS) -o $@ $(SRC)

.PHONY: clean check
clean:
	rm -f host_render *.o
	rm -rf main.dSYM

check: host_render
	../tools/regen_fixtures.sh --check
