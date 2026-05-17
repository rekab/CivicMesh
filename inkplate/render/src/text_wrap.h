#pragma once

#include <Adafruit_GFX.h>
#include <cstdint>
#include <string>
#include <vector>

namespace civicmesh {
namespace render {

// Greedy word-wrap. Uses gfx.getTextBounds at the current font and
// text size to measure each candidate line. Truncates the final line
// with "..." if it would overflow max_lines.
//
// Returns up to max_lines wrapped strings, each fitting within
// max_width pixels at the current font/size.
//
// gfx is treated as measurement-only (no drawing).
std::vector<std::string> wrap_text(Adafruit_GFX& gfx,
                                   const std::string& text,
                                   int16_t max_width,
                                   int max_lines);

}  // namespace render
}  // namespace civicmesh
