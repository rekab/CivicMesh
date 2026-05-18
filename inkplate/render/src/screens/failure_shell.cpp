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

constexpr int kCustomFontBaseHeight = 16;
constexpr int kBuiltinFontBaseHeight = 8;

struct FailureCopy {
  const char* header;       // shown in inverted header bar
  const char* big_line;     // displayed prominently in body
  const char* detail;       // smaller explanatory text
};

FailureCopy copy_for(const std::string& reason) {
  if (reason == "ap_unreachable")
    return {"! HUB UNREACHABLE", "AP UNREACHABLE",
            "The hub WiFi is not responding."};
  if (reason == "dns_failure")
    return {"! HUB UNREACHABLE", "DNS FAILURE",
            "The hub did not resolve."};
  if (reason == "pi_unreachable")
    return {"! HUB UNREACHABLE", "HUB UNREACHABLE",
            "AP is up but the hub is not."};
  if (reason == "http_error")
    return {"! HUB ERROR", "HTTP ERROR",
            "The hub responded but with an error code."};
  if (reason == "bad_json")
    return {"! HUB ERROR", "BAD JSON",
            "The hub response did not parse."};
  return {"! HUB ERROR", "ERROR", reason.c_str()};
}

void draw_inverted_bar(Adafruit_GFX& gfx, int16_t y, int16_t h) {
  gfx.fillRect(0, y, PANEL_W, h, COLOR_BLACK);
}

void draw_header(Adafruit_GFX& gfx, const FailureCopy& copy) {
  draw_inverted_bar(gfx, 0, HEADER_H);
  gfx.setFont(&LessPerfectDOSVGA);
  gfx.setTextSize(2);
  gfx.setTextColor(COLOR_WHITE, COLOR_BLACK);
  set_cursor_top_left(gfx, CHROME_PAD,
                      (HEADER_H - kCustomFontBaseHeight * 2) / 2,
                      kCustomFontBaseHeight * 2);
  gfx.print(copy.header);
}

void draw_footer(Adafruit_GFX& gfx,
                 const Envelope& env,
                 const Payload* fallback_for_apiver) {
  const int16_t y = PANEL_H - FOOTER_H;
  draw_inverted_bar(gfx, y, FOOTER_H);
  gfx.setFont();
  gfx.setTextSize(1);
  gfx.setTextColor(COLOR_WHITE, COLOR_BLACK);
  const int16_t text_y =
      y + (FOOTER_H - kBuiltinFontBaseHeight) / 2;

  int api_v = fallback_for_apiver ? fallback_for_apiver->api_version
                                  : env.expected_api_version;
  char left[64];
  std::snprintf(left, sizeof(left), "civicmesh-fw %s / api v%d",
                env.firmware_version.c_str(), api_v);
  gfx.setCursor(CHROME_PAD, text_y);
  gfx.print(left);

  std::string center = "Last good fetch " +
                       format_relative_time(env.seconds_since_last_update);
  int16_t x1, y1;
  uint16_t w, h;
  gfx.getTextBounds(center.c_str(), 0, 0, &x1, &y1, &w, &h);
  gfx.setCursor((PANEL_W - w) / 2, text_y);
  gfx.print(center.c_str());
}

void draw_upper_body(Adafruit_GFX& gfx, const FailureCopy& copy) {
  gfx.setFont(&LessPerfectDOSVGA);
  gfx.setTextSize(3);  // big_line
  gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);
  int16_t x1, y1;
  uint16_t w, h;
  gfx.getTextBounds(copy.big_line, 0, 0, &x1, &y1, &w, &h);
  int16_t big_y = BODY_Y + 24;
  set_cursor_top_left(gfx, (PANEL_W - w) / 2, big_y,
                      kCustomFontBaseHeight * 3);
  gfx.print(copy.big_line);

  gfx.setTextSize(2);
  gfx.getTextBounds(copy.detail, 0, 0, &x1, &y1, &w, &h);
  int16_t detail_y = big_y + kCustomFontBaseHeight * 3 + 12;
  set_cursor_top_left(gfx, (PANEL_W - w) / 2, detail_y,
                      kCustomFontBaseHeight * 2);
  gfx.print(copy.detail);
}

void draw_cached_payload_region(Adafruit_GFX& gfx,
                                const Envelope& env,
                                const Payload& cached) {
  // Region: lower body
  const int16_t region_top = BODY_Y + 200;
  const int16_t region_bottom = BODY_Y + BODY_H - BODY_PAD;
  const int16_t left = BODY_PAD;
  const int16_t right = PANEL_W - BODY_PAD;

  // Faint separator
  gfx.drawFastHLine(left, region_top - 8, right - left, COLOR_BLACK);

  // Subheader: "Showing last known data from N min ago"
  gfx.setFont();
  gfx.setTextSize(1);
  gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);
  std::string sub = "Showing last known data from " +
                    format_relative_time(env.seconds_since_last_update);
  gfx.setCursor(left, region_top);
  gfx.print(sub.c_str());

  int16_t y = region_top + 16;
  int idx = env.active_channel_index;
  if (cached.channels.empty()) return;
  if (idx < 0 || idx >= static_cast<int>(cached.channels.size())) idx = 0;
  const auto& ch = cached.channels[idx];

  for (size_t i = 0; i < ch.messages.size() && i < 3; ++i) {
    const auto& m = ch.messages[i];
    gfx.setFont(&LessPerfectDOSVGA);
    gfx.setTextSize(2);
    int16_t sender_h = kCustomFontBaseHeight * 2;
    if (y + sender_h > region_bottom) break;
    set_cursor_top_left(gfx, left, y, sender_h);
    gfx.print(m.sender.c_str());
    y += sender_h + 2;

    gfx.setTextSize(1);
    auto lines = wrap_text(gfx, m.body, right - left, 2);
    int16_t body_line_h = kCustomFontBaseHeight + 2;
    for (const auto& line : lines) {
      if (y + body_line_h > region_bottom) break;
      set_cursor_top_left(gfx, left, y, kCustomFontBaseHeight);
      gfx.print(line.c_str());
      y += body_line_h;
    }
    y += MSG_GAP / 2;
  }
}

}  // namespace

void draw_failure_shell(Adafruit_GFX& gfx, const Envelope& env) {
  gfx.fillScreen(COLOR_WHITE);
  FailureCopy copy = copy_for(env.failure_reason);
  draw_header(gfx, copy);
  draw_upper_body(gfx, copy);
  if (env.has_cached_payload) {
    draw_cached_payload_region(gfx, env, env.cached_payload);
  }
  draw_footer(gfx, env, env.has_cached_payload ? &env.cached_payload : nullptr);
}

}  // namespace screens
}  // namespace render
}  // namespace civicmesh
