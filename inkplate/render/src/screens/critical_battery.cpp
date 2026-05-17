#include "screens.h"

#include "../fonts/LessPerfectDOSVGA.h"
#include "../layout.h"

namespace civicmesh {
namespace render {
namespace screens {

namespace {
constexpr int kCustomFontBaseHeight = 16;
}

void draw_critical_battery(Adafruit_GFX& gfx, const Envelope& env) {
  (void)env;
  gfx.fillScreen(COLOR_WHITE);

  gfx.setFont(&LessPerfectDOSVGA);
  gfx.setTextSize(4);  // ~64px
  gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);

  const char* big = "BATTERY CRITICAL";
  int16_t x1, y1;
  uint16_t w, h;
  gfx.getTextBounds(big, 0, 0, &x1, &y1, &w, &h);
  int16_t big_y = (PANEL_H - kCustomFontBaseHeight * 4 - 60) / 2;
  set_cursor_top_left(gfx, (PANEL_W - w) / 2, big_y,
                      kCustomFontBaseHeight * 4);
  gfx.print(big);

  gfx.setTextSize(2);
  const char* small = "Please replace.";
  gfx.getTextBounds(small, 0, 0, &x1, &y1, &w, &h);
  set_cursor_top_left(gfx, (PANEL_W - w) / 2,
                      big_y + kCustomFontBaseHeight * 4 + 30,
                      kCustomFontBaseHeight * 2);
  gfx.print(small);
}

}  // namespace screens
}  // namespace render
}  // namespace civicmesh
