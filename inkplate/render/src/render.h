#pragma once

#include <Adafruit_GFX.h>

namespace civicmesh {
namespace render {

// Render a frame from combined envelope+payload JSON onto a GFX surface.
//
// The same function backs both targets:
//   * ESP32: gfx is an Inkplate(INKPLATE_1BIT). Caller must invoke
//     display.display() afterward to push the framebuffer to the panel.
//   * Host: gfx is a GFXcanvas1(800, 600). Caller serializes the canvas
//     buffer to a 1-bit PNG.
//
// The renderer trusts server-side NFKD-to-ASCII normalization (applied at
// external_display.py:_normalize_text). Bytes outside 0x20-0x7E render as
// the font's empty glyph (intentional — visible bug-report behavior).
//
// Returns true on success. Returns false on JSON parse error or any
// envelope field that fails type validation; in that case the canvas
// state is unspecified.
bool render_frame(Adafruit_GFX& gfx, const char* combined_json);

}  // namespace render
}  // namespace civicmesh
