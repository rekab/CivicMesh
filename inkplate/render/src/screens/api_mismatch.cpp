#include "screens.h"

#include <cstdio>

#include "../fonts/LessPerfectDOSVGA.h"
#include "../layout.h"

namespace civicmesh {
namespace render {
namespace screens {

namespace {
constexpr int kCustomFontBaseHeight = 16;
constexpr int kBuiltinFontBaseHeight = 8;
}  // namespace

void draw_api_mismatch(Adafruit_GFX& gfx,
                       const Envelope& env,
                       const Payload& payload,
                       const std::string& raw_payload_json) {
  gfx.fillScreen(COLOR_WHITE);

  gfx.setFont(&LessPerfectDOSVGA);
  gfx.setTextSize(3);
  gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);

  const char* line1 = "API VERSION MISMATCH";
  int16_t x1, y1;
  uint16_t w, h;
  gfx.getTextBounds(line1, 0, 0, &x1, &y1, &w, &h);
  set_cursor_top_left(gfx, (PANEL_W - w) / 2, 60,
                      kCustomFontBaseHeight * 3);
  gfx.print(line1);

  gfx.setTextSize(2);
  char line2[96];
  std::snprintf(line2, sizeof(line2), "Firmware: v%d   Server: v%d",
                env.expected_api_version, payload.api_version);
  gfx.getTextBounds(line2, 0, 0, &x1, &y1, &w, &h);
  set_cursor_top_left(gfx, (PANEL_W - w) / 2, 140,
                      kCustomFontBaseHeight * 2);
  gfx.print(line2);

  const char* line3 = "This hub needs reflashing.";
  gfx.getTextBounds(line3, 0, 0, &x1, &y1, &w, &h);
  set_cursor_top_left(gfx, (PANEL_W - w) / 2, 200,
                      kCustomFontBaseHeight * 2);
  gfx.print(line3);

  // Raw JSON dump for diagnostic value.
  gfx.setFont();
  gfx.setTextSize(1);
  gfx.setCursor(BODY_PAD, 280);
  size_t n = raw_payload_json.size();
  if (n > 200) n = 200;
  for (size_t i = 0; i < n; ++i) {
    char c = raw_payload_json[i];
    if (c == '\n' || c == '\r') {
      gfx.print(' ');
    } else {
      gfx.print(c);
    }
  }
  if (raw_payload_json.size() > 200) gfx.print("...");
}

}  // namespace screens
}  // namespace render
}  // namespace civicmesh
