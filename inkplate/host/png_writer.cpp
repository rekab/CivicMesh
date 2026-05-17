#include "png_writer.h"

#include <cstdio>
#include <vector>

#include "lodepng.h"

namespace civicmesh {
namespace host {

namespace {

std::vector<uint8_t> invert_buffer(const uint8_t* buf, int width, int height) {
  const size_t row_bytes = static_cast<size_t>((width + 7) / 8);
  const size_t total = row_bytes * static_cast<size_t>(height);
  std::vector<uint8_t> out(total);
  for (size_t i = 0; i < total; ++i) {
    out[i] = static_cast<uint8_t>(~buf[i]);
  }
  return out;
}

}  // namespace

bool write_png_1bit_to_file(const std::string& path,
                            const uint8_t* buffer,
                            int width,
                            int height) {
  auto inverted = invert_buffer(buffer, width, height);
  std::vector<uint8_t> encoded;
  unsigned err = lodepng::encode(encoded, inverted.data(),
                                 static_cast<unsigned>(width),
                                 static_cast<unsigned>(height),
                                 LCT_GREY, 1);
  if (err) {
    std::fprintf(stderr, "lodepng encode error %u: %s\n", err,
                 lodepng_error_text(err));
    return false;
  }
  if (lodepng::save_file(encoded, path)) {
    std::fprintf(stderr, "lodepng save_file error for %s\n", path.c_str());
    return false;
  }
  return true;
}

bool write_png_1bit_to_stdout(const uint8_t* buffer,
                              int width,
                              int height) {
  auto inverted = invert_buffer(buffer, width, height);
  std::vector<uint8_t> encoded;
  unsigned err = lodepng::encode(encoded, inverted.data(),
                                 static_cast<unsigned>(width),
                                 static_cast<unsigned>(height),
                                 LCT_GREY, 1);
  if (err) {
    std::fprintf(stderr, "lodepng encode error %u: %s\n", err,
                 lodepng_error_text(err));
    return false;
  }
  size_t n = std::fwrite(encoded.data(), 1, encoded.size(), stdout);
  if (n != encoded.size()) {
    std::fprintf(stderr, "stdout write short: %zu of %zu\n", n, encoded.size());
    return false;
  }
  return true;
}

}  // namespace host
}  // namespace civicmesh
