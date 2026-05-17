#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace civicmesh {
namespace host {

// Write a 1-bit grayscale PNG to a file.
//
// `buffer` is GFXcanvas1's raw framebuffer: MSB-first packed pixels,
// row-major, with rows padded to a byte boundary. Bits set to 1
// represent "drawn" (renderer's COLOR_BLACK); bits cleared to 0
// represent "background" (renderer's COLOR_WHITE). The PNG is
// emitted with the conventional 1-bit-grayscale polarity (0 = black,
// 1 = white), so the function inverts each byte during encode.
//
// Returns true on success; writes a short message to stderr on
// failure and returns false.
bool write_png_1bit_to_file(const std::string& path,
                            const uint8_t* buffer,
                            int width,
                            int height);

// Same, but writes the encoded PNG bytes to stdout (binary).
bool write_png_1bit_to_stdout(const uint8_t* buffer,
                              int width,
                              int height);

}  // namespace host
}  // namespace civicmesh
