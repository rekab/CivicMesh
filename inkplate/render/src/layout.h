#pragma once

#include <Adafruit_GFX.h>
#include <cstdint>

namespace civicmesh {
namespace render {

constexpr int16_t PANEL_W = 800;
constexpr int16_t PANEL_H = 600;

constexpr int16_t HEADER_H = 50;
constexpr int16_t FOOTER_H = 30;
constexpr int16_t BODY_Y = HEADER_H;
constexpr int16_t BODY_H = PANEL_H - HEADER_H - FOOTER_H;

constexpr int16_t CHROME_PAD = 8;
constexpr int16_t BODY_PAD = 12;
constexpr int16_t MSG_GAP = 8;

constexpr float BATT_LOW_V = 3.9f;
constexpr float BATT_CRIT_V = 3.6f;

// 1-bit color constants. GFXcanvas1 sets a bit on color=1 and clears
// on color=0; the host PNG writer renders set bits as black ink and
// cleared bits as white paper. The Inkplate library may use the
// opposite polarity for its panel framebuffer — if so, PR 2's
// bulletin.ino is the right place to flip before display.display().
constexpr uint16_t COLOR_BLACK = 1;
constexpr uint16_t COLOR_WHITE = 0;

// Custom GFX fonts position glyphs from the BASELINE, not the top-left
// corner like the built-in 5x7 font. set_cursor_top_left wraps the math
// so screens express "I want text starting at this top-left corner"
// uniformly across both font modes.
//
// glyph_h_estimate: the visible glyph height in pixels at the active
// scale. For LessPerfectDOSVGA at setTextSize(2) this is ~16 base * 2 = 32.
// For the built-in font at setTextSize(N) this is 8 * N - 1.
inline void set_cursor_top_left(Adafruit_GFX& gfx,
                                int16_t left,
                                int16_t top,
                                int16_t glyph_h_estimate) {
  gfx.setCursor(left, top + glyph_h_estimate);
}

}  // namespace render
}  // namespace civicmesh
