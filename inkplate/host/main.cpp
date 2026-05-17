// host_render — reads combined envelope+payload JSON from stdin,
// renders to a 1-bit 800x600 PNG, writes the PNG to stdout.
//
// Exits 0 on success, 1 on JSON parse error, 2 on render error.
// Errors go to stderr.

#include <Adafruit_GFX.h>

#include <cstdio>
#include <iostream>
#include <sstream>
#include <string>

#include "render.h"
#include "png_writer.h"

namespace {

constexpr int kW = 800;
constexpr int kH = 600;

std::string slurp_stdin() {
  std::ostringstream oss;
  oss << std::cin.rdbuf();
  return oss.str();
}

}  // namespace

int main(int argc, char** argv) {
  (void)argc;
  (void)argv;

  std::string combined = slurp_stdin();
  if (combined.empty()) {
    std::fprintf(stderr, "host_render: empty stdin\n");
    return 1;
  }

  GFXcanvas1 canvas(kW, kH);
  canvas.fillScreen(0);  // clear to "off" / white

  if (!civicmesh::render::render_frame(canvas, combined.c_str())) {
    std::fprintf(stderr, "host_render: render_frame returned false\n");
    return 2;
  }

  if (!civicmesh::host::write_png_1bit_to_stdout(canvas.getBuffer(), kW, kH)) {
    return 2;
  }
  return 0;
}
