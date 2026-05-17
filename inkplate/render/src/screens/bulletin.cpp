#include "screens.h"

#include <cstdio>

#include "../fonts/LessPerfectDOSVGA.h"
#include "../layout.h"
#include "../relative_time.h"
#include "../text_wrap.h"

namespace civicmesh {
namespace render {
namespace screens {

namespace {

constexpr int kCustomFontBaseHeight = 16;  // LessPerfectDOSVGA native height
constexpr int kBuiltinFontBaseHeight = 8;  // 5x7 + 1px line space

void draw_inverted_bar(Adafruit_GFX& gfx, int16_t y, int16_t h) {
  gfx.fillRect(0, y, PANEL_W, h, COLOR_BLACK);
}

void draw_header(Adafruit_GFX& gfx,
                 const Envelope& env,
                 const Payload& payload) {
  draw_inverted_bar(gfx, 0, HEADER_H);
  gfx.setFont(&LessPerfectDOSVGA);
  gfx.setTextSize(2);
  gfx.setTextColor(COLOR_WHITE, COLOR_BLACK);

  const std::string site = payload.site_name.empty()
                               ? std::string("CivicMesh")
                               : payload.site_name;
  set_cursor_top_left(gfx, CHROME_PAD,
                      (HEADER_H - kCustomFontBaseHeight * 2) / 2,
                      kCustomFontBaseHeight * 2);
  gfx.print(site.c_str());

  // Right side: channel name + (n/m) indicator.
  std::string channel_label;
  if (!payload.channels.empty()) {
    int idx = env.active_channel_index;
    if (idx < 0 || idx >= static_cast<int>(payload.channels.size())) idx = 0;
    char buf[96];
    std::snprintf(buf, sizeof(buf), "%s (%d/%zu)",
                  payload.channels[idx].name.c_str(),
                  idx + 1, payload.channels.size());
    channel_label = buf;
  }
  if (!channel_label.empty()) {
    int16_t x1, y1;
    uint16_t w, h;
    gfx.getTextBounds(channel_label.c_str(), 0, 0, &x1, &y1, &w, &h);
    set_cursor_top_left(gfx, PANEL_W - CHROME_PAD - w,
                        (HEADER_H - kCustomFontBaseHeight * 2) / 2,
                        kCustomFontBaseHeight * 2);
    gfx.print(channel_label.c_str());
  }
}

void draw_body_messages(Adafruit_GFX& gfx,
                        const Envelope& env,
                        const Payload& payload) {
  if (payload.channels.empty()) {
    gfx.setFont(&LessPerfectDOSVGA);
    gfx.setTextSize(2);
    gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);
    const char* msg = "No channels configured.";
    int16_t x1, y1;
    uint16_t w, h;
    gfx.getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
    set_cursor_top_left(gfx, (PANEL_W - w) / 2,
                        BODY_Y + (BODY_H - kCustomFontBaseHeight * 2) / 2,
                        kCustomFontBaseHeight * 2);
    gfx.print(msg);
    return;
  }

  int idx = env.active_channel_index;
  if (idx < 0 || idx >= static_cast<int>(payload.channels.size())) idx = 0;
  const auto& ch = payload.channels[idx];

  if (ch.messages.empty()) {
    gfx.setFont(&LessPerfectDOSVGA);
    gfx.setTextSize(2);
    gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);
    const char* msg = "No messages yet.";
    int16_t x1, y1;
    uint16_t w, h;
    gfx.getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
    set_cursor_top_left(gfx, (PANEL_W - w) / 2,
                        BODY_Y + (BODY_H - kCustomFontBaseHeight * 2) / 2,
                        kCustomFontBaseHeight * 2);
    gfx.print(msg);
    return;
  }

  gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);
  int16_t y = BODY_Y + BODY_PAD;
  const int16_t body_left = BODY_PAD;
  const int16_t body_right = PANEL_W - BODY_PAD;
  const int16_t body_width = body_right - body_left;
  const int16_t body_bottom = BODY_Y + BODY_H - BODY_PAD;
  const int kBodyMaxLines = 2;

  for (size_t i = 0; i < ch.messages.size() && i < 5; ++i) {
    const auto& m = ch.messages[i];
    // Sender at setTextSize(2)
    gfx.setFont(&LessPerfectDOSVGA);
    gfx.setTextSize(2);
    int16_t sender_h = kCustomFontBaseHeight * 2;
    if (y + sender_h > body_bottom) break;
    set_cursor_top_left(gfx, body_left, y, sender_h);
    gfx.print(m.sender.c_str());
    y += sender_h + 2;

    // Body at setTextSize(1) — smaller for hierarchy and to fit 5 msgs.
    gfx.setTextSize(1);
    auto lines = wrap_text(gfx, m.body, body_width, kBodyMaxLines);
    int16_t body_line_h = kCustomFontBaseHeight + 2;
    for (const auto& line : lines) {
      if (y + body_line_h > body_bottom) break;
      set_cursor_top_left(gfx, body_left, y, kCustomFontBaseHeight);
      gfx.print(line.c_str());
      y += body_line_h;
    }
    y += MSG_GAP / 2;
    // Thin rule separator.
    if (y < body_bottom - 2) {
      gfx.drawFastHLine(body_left, y, body_width, COLOR_BLACK);
      y += MSG_GAP / 2 + 1;
    }
  }
}

void draw_footer(Adafruit_GFX& gfx,
                 const Envelope& env,
                 const Payload& payload) {
  const int16_t y = PANEL_H - FOOTER_H;
  draw_inverted_bar(gfx, y, FOOTER_H);
  gfx.setFont();  // built-in 5x7
  gfx.setTextSize(1);
  gfx.setTextColor(COLOR_WHITE, COLOR_BLACK);

  const int16_t text_y =
      y + (FOOTER_H - kBuiltinFontBaseHeight) / 2;

  // Left: civicmesh-fw vX.Y.Z / api vN
  char left[64];
  std::snprintf(left, sizeof(left), "civicmesh-fw %s / api v%d",
                env.firmware_version.c_str(), payload.api_version);
  gfx.setCursor(CHROME_PAD, text_y);
  gfx.print(left);

  // Center: relative time + optional stale tag
  std::string center = format_relative_time(env.seconds_since_last_update);
  if (env.status == "stale") center += " (stale)";
  int16_t x1, y1;
  uint16_t w, h;
  gfx.getTextBounds(center.c_str(), 0, 0, &x1, &y1, &w, &h);
  gfx.setCursor((PANEL_W - w) / 2, text_y);
  gfx.print(center.c_str());

  // Right: battery + radio glyphs
  std::string right;
  if (env.battery_volts >= BATT_CRIT_V &&
      env.battery_volts < BATT_LOW_V) {
    right += "BATT LOW ";
  }
  if (env.radio_state == "down") {
    right += "RADIO v";  // 'v' as a tiny down-chevron proxy in ASCII
  }
  if (!right.empty()) {
    gfx.getTextBounds(right.c_str(), 0, 0, &x1, &y1, &w, &h);
    gfx.setCursor(PANEL_W - CHROME_PAD - w, text_y);
    gfx.print(right.c_str());
  }
}

}  // namespace

void draw_bulletin(Adafruit_GFX& gfx,
                   const Envelope& env,
                   const Payload& payload) {
  gfx.fillScreen(COLOR_WHITE);
  draw_header(gfx, env, payload);
  draw_body_messages(gfx, env, payload);
  draw_footer(gfx, env, payload);
}

}  // namespace screens
}  // namespace render
}  // namespace civicmesh
