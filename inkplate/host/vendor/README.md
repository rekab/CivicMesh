# Vendored third-party dependencies

These libraries are committed in tree so the host build at
`inkplate/host/` works offline and without a `make deps` step. Updates
are a manual copy from upstream; record any change here and bump SHAs.

The ESP32 build (PR 2) uses Adafruit_GFX and ArduinoJson from the
arduino-cli library cache, not these copies; lodepng is host-only.

## Adafruit_GFX

- **Upstream:** https://github.com/adafruit/Adafruit-GFX-Library
- **Pinned commit:** `ac6d7c3869a693d406f77b9bfcd486b0673169f0` (HEAD of `master`, 2026-05-17)
- **License:** BSD-2-Clause (see `Adafruit_GFX/LICENSE`)
- **Files copied:** `Adafruit_GFX.h`, `Adafruit_GFX.cpp`, `gfxfont.h`,
  `glcdfont.c`, plus `license.txt` renamed to `LICENSE`.
- **Host-compat stubs:** `Adafruit_GFX/host_compat/{Arduino.h,Print.h,
  WProgram.h,Adafruit_I2CDevice.h,Adafruit_SPIDevice.h}` are
  CivicMesh-authored, not from upstream. They provide the minimum
  surface Adafruit_GFX needs on a host (`String`, `Print`, PROGMEM
  macros, and two empty headers that Adafruit_GFX.h includes but never
  uses). The host Makefile puts `host_compat/` on the include path
  before `Adafruit_GFX/`, so `#include "Arduino.h"` from
  Adafruit_GFX.h resolves to the stub.

## lodepng

- **Upstream:** https://github.com/lvandeve/lodepng
- **Pinned commit:** `22561883dd63fd1850f18e1f6adac321e4f609b0` (HEAD of `master`, 2026-05-17)
- **License:** zlib (see `lodepng/LICENSE`)
- **Files copied:** `lodepng.h`, `lodepng.cpp`, `LICENSE`.

## ArduinoJson

- **Upstream:** https://github.com/bblanchon/ArduinoJson
- **Pinned tag:** `v7.4.2` (commit `733bc4ee82630c88c0a619a883cd3a206efae977`)
- **License:** MIT (see `ArduinoJson/LICENSE`)
- **Files copied:** full `src/` tree, plus `LICENSE.txt` renamed to
  `LICENSE`. The library is header-only; `src/ArduinoJson.h` and
  `src/ArduinoJson.hpp` are the entry points.

## Updating

To update any of these, clone the upstream repo, copy the same set of
files over the existing vendored directory, update the pinned commit
SHA (or tag) above, and run `make check` from `inkplate/host/` to
verify fixtures still pass.
