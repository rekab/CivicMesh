#include "screens.h"

#include <algorithm>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

#include "../fonts/LessPerfectDOSVGA.h"
#include "../fonts/LessPerfectDOSVGA24.h"
#include "../layout.h"
#include "../relative_time.h"
#include "../text_wrap.h"

namespace civicmesh {
namespace render {
namespace screens {

namespace {

constexpr int kCustomFontBaseHeight = 16;    // LessPerfectDOSVGA native height
constexpr int kCustomFont24BaseHeight = 24;  // LessPerfectDOSVGA24 native height
constexpr int kBuiltinFontBaseHeight = 8;    // 5x7 + 1px line space

// Bulletin-local header + footer heights. Smaller header than layout.h's
// global HEADER_H (50) because this screen uses size-1 text (16 px)
// where the failure_shell family still uses size-2 (32 px) for
// visibility-at-distance. Footer is slightly larger than the global
// FOOTER_H (30) to fit UI_SPEC §5's nerd strip — the sparkline wants
// ~14 px vertical room above the 8-px built-in text. Per-screen
// overrides avoid regenerating failure_shell goldens; those templates
// aren't changing in this rework.
constexpr int16_t BUL_HEADER_H = 30;
constexpr int16_t BUL_FOOTER_H = 36;
constexpr int16_t BUL_BODY_Y = BUL_HEADER_H;
constexpr int16_t BUL_BODY_H = PANEL_H - BUL_HEADER_H - BUL_FOOTER_H;

// Right-edge channel rail. Tabs stack vertically at the top of the rail
// at a fixed height each (about three size-1 rows). The active tab
// merges with the chat pane (no border on its left side) and is framed
// in a double line for emphasis; inactive tabs get a single-line
// outline. The rail's right edge runs as a single continuous line from
// the body top to the body bottom regardless of how many tabs fill it,
// so the rail reads as a bounded column even when there's only one
// channel.
//
// RAIL_W sized to fit a size-2 (heading-size) label at ~10 chars
// (#civicmesh = 10 chars × ~16 px = 160 px) plus TAB_PAD on both sides.
// Labels longer than that clip at the right edge of the tab.
//
// Deliberate deviation from UI_SPEC §5, which currently specs the rail
// on the left. Right-side rail keeps it visually separated from the
// sender/body column on the left so it reads as less distracting chrome.
constexpr int16_t RAIL_W = 180;
constexpr int16_t TAB_PAD = 6;
constexpr int16_t TAB_H = 54;  // ~3 size-1 rows; comfortably fits size-2 label
// Inset between the outer and inner stroke of the active tab's double-
// line border. Two pixels is the smallest gap that reads cleanly as two
// separate lines at 1-bit display density.
constexpr int16_t DOUBLE_LINE_GAP = 2;

// Sender + body line height at setTextSize(1). The font's yAdvance is 16;
// we use the same value as a row pitch so successive lines stack cleanly.
constexpr int16_t LINE_H = kCustomFontBaseHeight;

// Body indent for continuation lines. Two size-1 cells (~8 px each)
// is enough to signal visual hierarchy without consuming pane width.
// Deviates from UI_SPEC §5's literal "6 cols" (which was sized to
// align with "HH:MM "); the new server-formatted "YYYY-MM-DD HH:MM "
// prefix is 17 cells, and indenting body that far eats too much
// wrap width. The hanging indent is small enough that body still
// reads as belonging to the message above.
constexpr int16_t BODY_INDENT_PX = 16;

// Hard caps on visible content. MAX_MESSAGES_SHOWN matches the server's
// _PER_CHANNEL_LIMIT so the renderer can display everything the payload
// carries; the per-pane height check below is what actually clips. The
// smaller sender line in the new layout means we can fit many more
// messages than the previous 5-row layout did.
constexpr size_t MAX_MESSAGES_SHOWN = 15;
constexpr int kBodyMaxLines = 3;

void draw_inverted_bar(Adafruit_GFX& gfx, int16_t y, int16_t h) {
  gfx.fillRect(0, y, PANEL_W, h, COLOR_BLACK);
}

// WiFi glyph — three nested upper-half arcs above a single-pixel dot,
// the classic "signal radiating outward from a source" shape. Arcs
// stand at r/3, 2r/3, r (equispaced); glyph is (2r+1) wide × (r+1)
// tall. `cx`, `cy` is the position of the dot; arcs render above it.
void draw_wifi_glyph(Adafruit_GFX& gfx,
                     int16_t cx, int16_t cy, int16_t r_outer,
                     uint16_t color) {
  gfx.drawPixel(cx, cy, color);
  // cornername bitmask: 0x1 = upper-left, 0x2 = upper-right;
  // 0x3 = full upper semicircle.
  gfx.drawCircleHelper(cx, cy, r_outer / 3, 0x3, color);
  gfx.drawCircleHelper(cx, cy, (r_outer * 2) / 3, 0x3, color);
  gfx.drawCircleHelper(cx, cy, r_outer, 0x3, color);
}

// Battery glyph — outlined rectangle body + small "nub" on the right
// (the positive terminal), filled internally proportional to charge.
// `pct` is 0.0..1.0; clamped to that range. The glyph is sized (w × h)
// excluding the nub; the nub adds ~2 px on the right.
void draw_battery_glyph(Adafruit_GFX& gfx,
                        int16_t x, int16_t y, int16_t w, int16_t h,
                        float pct, uint16_t color) {
  if (pct < 0.0f) pct = 0.0f;
  if (pct > 1.0f) pct = 1.0f;
  // Body outline
  gfx.drawRect(x, y, w, h, color);
  // Nub: 2 px wide, centered vertically, sits flush against right side
  const int16_t nub_h = h - 4;
  const int16_t nub_y = y + 2;
  gfx.fillRect(x + w, nub_y, 2, nub_h, color);
  // Interior fill (1 px inset from outline so the outline stays visible)
  const int16_t inset = 1;
  const int16_t fill_max = w - 2 * inset;
  const int16_t fill_w = static_cast<int16_t>(fill_max * pct + 0.5f);
  if (fill_w > 0) {
    gfx.fillRect(x + inset, y + inset, fill_w, h - 2 * inset, color);
  }
}

// Map Li-ion volts to a 0..1 charge fraction for the icon's fill bar.
// Battery curves aren't linear, but for a coarse 4-state visual the
// linear approximation over [BATT_CRIT_V .. 4.20 V] is honest enough
// and matches what the operator sees on the LOW threshold (≈3.9 V
// shows roughly half-full).
float battery_pct(float volts) {
  constexpr float V_MAX = 4.20f;
  constexpr float V_MIN = BATT_CRIT_V;  // 3.60
  if (volts <= V_MIN) return 0.0f;
  if (volts >= V_MAX) return 1.0f;
  return (volts - V_MIN) / (V_MAX - V_MIN);
}

// Draw the active tab's double-line border: outer stroke on top, right,
// and bottom (no left — that side merges with the pane), and a second
// inner stroke inset by DOUBLE_LINE_GAP. The inner top/bottom lines
// stop at the inner vertical so the top-right and bottom-right form
// the ═╗ / ═╝ join cleanly without crossing strokes.
void draw_double_border_no_left(Adafruit_GFX& gfx,
                                int16_t x, int16_t y,
                                int16_t w, int16_t h) {
  const int16_t x_outer_right = x + w - 1;
  const int16_t x_inner_right = x_outer_right - DOUBLE_LINE_GAP;
  const int16_t y_outer_top = y;
  const int16_t y_outer_bot = y + h - 1;
  const int16_t y_inner_top = y_outer_top + DOUBLE_LINE_GAP;
  const int16_t y_inner_bot = y_outer_bot - DOUBLE_LINE_GAP;

  // Outer stroke (3 sides).
  gfx.drawFastHLine(x, y_outer_top, w, COLOR_BLACK);
  gfx.drawFastHLine(x, y_outer_bot, w, COLOR_BLACK);
  gfx.drawFastVLine(x_outer_right, y_outer_top, h, COLOR_BLACK);

  // Inner stroke (3 sides). Top/bottom stop at the inner vertical's x
  // so the corner forms a clean ╗/╝ join.
  const int16_t inner_h_w = w - DOUBLE_LINE_GAP;
  const int16_t inner_v_h = h - 2 * DOUBLE_LINE_GAP;
  gfx.drawFastHLine(x, y_inner_top, inner_h_w, COLOR_BLACK);
  gfx.drawFastHLine(x, y_inner_bot, inner_h_w, COLOR_BLACK);
  gfx.drawFastVLine(x_inner_right, y_inner_top, inner_v_h, COLOR_BLACK);
}

// Fallback: format an epoch-seconds timestamp as "YYYY-MM-DD HH:MM" in
// UTC.
//
// Per UI_SPEC §5 the firmware does not compute time — the server
// supplies a pre-formatted `ts_str` on each message (formatted in
// cfg.node.timezone via Python's zoneinfo, with DST). This helper
// only fires when ts_str is missing: legacy fixtures or an older
// server that doesn't carry the field. UTC is chosen because the
// firmware has no timezone context of its own; the wall-clock time
// will be wrong by the local tz offset, but the relative ordering,
// the date, and the minute boundaries are still correct.
std::string format_ts_utc_fallback(long ts) {
  std::time_t t = static_cast<std::time_t>(ts);
  std::tm tm_buf{};
  gmtime_r(&t, &tm_buf);
  // 64 bytes leaves headroom for the compiler's defensive estimate
  // (it assumes %d can swing wider than four digits) while still being
  // well under any reasonable stack budget.
  char buf[64];
  std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d",
                tm_buf.tm_year + 1900, tm_buf.tm_mon + 1, tm_buf.tm_mday,
                tm_buf.tm_hour, tm_buf.tm_min);
  return std::string(buf);
}

void draw_header(Adafruit_GFX& gfx,
                 const Envelope& env,
                 const Payload& payload) {
  draw_inverted_bar(gfx, 0, BUL_HEADER_H);
  // Top bar uses the 16-px LessPerfectDOSVGA at size 1: a single chat-
  // body row of text. The bar height (BUL_HEADER_H) budgets vertical
  // padding around it so the inverted strip reads as deliberate chrome
  // rather than text-on-black. The channel name + (n/m) indicator that
  // used to sit on the right has been retired — the active tab on the
  // right rail already shows the channel identity, and the position
  // info reads visually from the tab stack.
  gfx.setFont(&LessPerfectDOSVGA);
  gfx.setTextSize(1);
  gfx.setTextColor(COLOR_WHITE, COLOR_BLACK);

  const int16_t text_top = (BUL_HEADER_H - kCustomFontBaseHeight) / 2;

  // --- Left: SSID label + wifi glyph + site name ---
  // Format: "SSID: <wifi-glyph> <site_name>" where the glyph is sized
  // to fill most of the bar height (r_outer=18 → 19 px tall in a 30 px
  // bar). We use site_name as the "SSID" value since that's the
  // closest network-identity field the payload carries; if the firmware
  // grows a true SSID envelope field later, it slots in here verbatim.
  constexpr int16_t WIFI_R = 18;
  constexpr int16_t WIFI_GAP = 8;
  const int16_t wifi_glyph_h = WIFI_R + 1;        // 19
  const int16_t wifi_glyph_w = WIFI_R * 2 + 1;    // 37
  const int16_t wifi_dot_y =
      (BUL_HEADER_H - wifi_glyph_h) / 2 + wifi_glyph_h - 1;

  const std::string ssid_value = payload.site_name.empty()
                                     ? std::string("CivicMesh")
                                     : payload.site_name;

  // "SSID: " prefix
  const char* prefix = "SSID: ";
  set_cursor_top_left(gfx, CHROME_PAD, text_top, kCustomFontBaseHeight);
  gfx.print(prefix);

  int16_t x1, y1;
  uint16_t pw, ph;
  gfx.getTextBounds(prefix, 0, 0, &x1, &y1, &pw, &ph);

  // Wifi glyph centered vertically in the bar, just after the prefix
  const int16_t wifi_cx = CHROME_PAD + static_cast<int16_t>(pw) +
                          WIFI_R;  // leftmost arc col is at cx - r
  draw_wifi_glyph(gfx, wifi_cx, wifi_dot_y, WIFI_R, COLOR_WHITE);

  // Site name to the right of the glyph
  const int16_t ssid_x =
      CHROME_PAD + static_cast<int16_t>(pw) + wifi_glyph_w + WIFI_GAP;
  set_cursor_top_left(gfx, ssid_x, text_top, kCustomFontBaseHeight);
  gfx.print(ssid_value.c_str());

  // --- Right: battery indicator (glyph + voltage) ---
  // Critical battery has its own takeover screen; this draws for the
  // "good" (>= BATT_LOW_V) and "low" (BATT_CRIT_V .. BATT_LOW_V) cases.
  // The renderer assumes env.battery_volts > 0 means a valid reading;
  // a zero/negative reading hides the indicator (firmware booting,
  // host_render fixture without battery data).
  if (env.battery_volts > 0.0f) {
    const bool is_low = env.battery_volts < BATT_LOW_V;
    char volt_buf[16];
    std::snprintf(volt_buf, sizeof(volt_buf), "%.2fV",
                  static_cast<double>(env.battery_volts));
    if (is_low) {
      // Low-battery suffix per UI_SPEC §5; reads as a single chunk
      // (battery icon + voltage + "LOW") rather than three separate
      // segments.
      std::strncat(volt_buf, " LOW", sizeof(volt_buf) - std::strlen(volt_buf) - 1);
    }
    int16_t vx1, vy1;
    uint16_t vw, vh;
    gfx.getTextBounds(volt_buf, 0, 0, &vx1, &vy1, &vw, &vh);

    constexpr int16_t BATT_W = 18;
    constexpr int16_t BATT_H = 10;
    // Layout from right to left: chrome pad, voltage text, gap, battery
    // glyph (incl. nub width = 2).
    const int16_t volt_x = PANEL_W - CHROME_PAD - static_cast<int16_t>(vw);
    set_cursor_top_left(gfx, volt_x, text_top, kCustomFontBaseHeight);
    gfx.print(volt_buf);
    const int16_t batt_x = volt_x - 6 - (BATT_W + 2);
    const int16_t batt_y = (BUL_HEADER_H - BATT_H) / 2;
    draw_battery_glyph(gfx, batt_x, batt_y, BATT_W, BATT_H,
                       battery_pct(env.battery_volts), COLOR_WHITE);
  }
}

// Right-edge file-folder channel tabs. Drawn AFTER the body so a tab's
// border paints cleanly over any pane content that would otherwise bleed
// into the rail column.
//
// Tabs are vertically centered in the rail area, with the surrounding
// rail painted inverted (black) above and below. A "CHANNELS" header
// in white sits just above the topmost tab on the black backing. The
// black bars do double duty as the pane/rail boundary - no explicit
// drawFastVLine needed, since white pane ↔ black rail is its own
// implicit edge.
void draw_channel_rail(Adafruit_GFX& gfx,
                       const Envelope& env,
                       const Payload& payload) {
  if (payload.channels.empty()) return;
  const size_t n = payload.channels.size();
  const int16_t rail_x = PANEL_W - RAIL_W;
  const int16_t rail_y = BUL_BODY_Y;
  const int16_t rail_h_total = BUL_BODY_H;

  // Reserve vertical room above the tab block for the "CHANNELS"
  // header. Tabs + header center together as one block in the rail.
  constexpr int16_t HDR_TXT_H = 16;     // size-1 LessPerfectDOSVGA
  constexpr int16_t HDR_GAP = 10;       // gap between header and first tab

  const size_t max_visible = static_cast<size_t>(
      (rail_h_total - HDR_TXT_H - HDR_GAP) / TAB_H);
  const size_t visible = std::min(n, max_visible);

  int active_idx = env.active_channel_index;
  if (active_idx < 0 || active_idx >= static_cast<int>(n)) active_idx = 0;

  const int16_t block_h =
      HDR_TXT_H + HDR_GAP + static_cast<int16_t>(visible) * TAB_H;
  const int16_t hdr_top = rail_y + (rail_h_total - block_h) / 2;
  const int16_t tabs_top = hdr_top + HDR_TXT_H + HDR_GAP;
  const int16_t tabs_bot =
      tabs_top + static_cast<int16_t>(visible) * TAB_H;

  // Inverted (black) backing above the tab block - covers the header
  // strip AND the empty space above it. The pane (white) at x < rail_x
  // gives an implicit boundary at x=rail_x without needing a drawn line.
  gfx.fillRect(rail_x, rail_y, RAIL_W,
               tabs_top - rail_y, COLOR_BLACK);
  gfx.fillRect(rail_x, tabs_bot, RAIL_W,
               rail_y + rail_h_total - tabs_bot, COLOR_BLACK);

  // "▼ CHANNELS ▼" header in white on the black backing, centered.
  // Arrows are filled triangles (no Unicode arrow in LessPerfectDOSVGA;
  // drawn triangles give crisper edges than ASCII proxies).
  gfx.setFont(&LessPerfectDOSVGA);
  gfx.setTextSize(1);
  gfx.setTextColor(COLOR_WHITE, COLOR_BLACK);
  const char* hdr_label = "CHANNELS";
  int16_t hx1, hy1;
  uint16_t hw, hh;
  gfx.getTextBounds(hdr_label, 0, 0, &hx1, &hy1, &hw, &hh);

  constexpr int16_t ARROW_W = 9;
  constexpr int16_t ARROW_H = 6;
  constexpr int16_t ARROW_GAP = 8;
  const int16_t total_label_w =
      ARROW_W + ARROW_GAP + static_cast<int16_t>(hw) + ARROW_GAP + ARROW_W;
  const int16_t label_x = rail_x + (RAIL_W - total_label_w) / 2;
  const int16_t arrow_y = hdr_top + (HDR_TXT_H - ARROW_H) / 2;

  gfx.fillTriangle(label_x, arrow_y,
                   label_x + ARROW_W - 1, arrow_y,
                   label_x + ARROW_W / 2, arrow_y + ARROW_H - 1,
                   COLOR_WHITE);
  set_cursor_top_left(gfx, label_x + ARROW_W + ARROW_GAP,
                      hdr_top, kCustomFontBaseHeight);
  gfx.print(hdr_label);
  const int16_t right_arrow_x =
      label_x + ARROW_W + ARROW_GAP + static_cast<int16_t>(hw) + ARROW_GAP;
  gfx.fillTriangle(right_arrow_x, arrow_y,
                   right_arrow_x + ARROW_W - 1, arrow_y,
                   right_arrow_x + ARROW_W / 2, arrow_y + ARROW_H - 1,
                   COLOR_WHITE);

  // Tabs starting at tabs_top. Interiors remain white (nothing painted
  // black over them); the active tab's punch-through merge with the
  // pane works the same as before.
  for (size_t i = 0; i < visible; ++i) {
    const int16_t y_top = tabs_top + static_cast<int16_t>(i) * TAB_H;
    const bool is_active = (static_cast<int>(i) == active_idx);

    if (is_active) {
      // Active: double-line border on top/right/bottom; no left border
      // so the tab and pane read as one continuous piece of paper.
      draw_double_border_no_left(gfx, rail_x, y_top, RAIL_W, TAB_H);
    } else {
      // Inactive: plain single-line rectangle. No fill, no shading.
      gfx.drawRect(rail_x, y_top, RAIL_W, TAB_H, COLOR_BLACK);
    }

    // Tab label. Active = 32 px (LessPerfectDOSVGA at size 2) for
    // emphasis; inactive = 24 px (native LessPerfectDOSVGA24 at size 1)
    // so inactive tabs read as less prominent without the blocky-pixel
    // look of an integer-scaled small font. Both centered horizontally
    // and vertically inside their tab. Labels too long for RAIL_W still
    // clip at the right edge — measured via getTextBounds.
    if (is_active) {
      gfx.setFont(&LessPerfectDOSVGA);
      gfx.setTextSize(2);
    } else {
      gfx.setFont(&LessPerfectDOSVGA24);
      gfx.setTextSize(1);
    }
    gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);
    const int16_t label_h =
        is_active ? kCustomFontBaseHeight * 2 : kCustomFont24BaseHeight;
    const char* label = payload.channels[i].name.c_str();
    int16_t bx, by;
    uint16_t bw, bh;
    gfx.getTextBounds(label, 0, 0, &bx, &by, &bw, &bh);
    const int16_t label_x =
        rail_x + (RAIL_W - static_cast<int16_t>(bw)) / 2;
    const int16_t label_top = y_top + (TAB_H - label_h) / 2;
    set_cursor_top_left(gfx, label_x, label_top, label_h);
    gfx.print(label);
  }

  // (Previously: drawFastVLine below the last tab to extend the pane/
  // rail boundary to body bottom. With the black-backed rail above and
  // below the tab block, the boundary is implicit — white pane meets
  // black rail at x=rail_x for the entire body height except where the
  // active tab punches through with a white interior.)
}

// Center a small message in the pane area. Used for the two empty states.
void draw_pane_message(Adafruit_GFX& gfx, const char* msg,
                       int16_t pane_left, int16_t pane_right) {
  gfx.setFont(&LessPerfectDOSVGA);
  gfx.setTextSize(2);
  gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);
  int16_t x1, y1;
  uint16_t w, h;
  gfx.getTextBounds(msg, 0, 0, &x1, &y1, &w, &h);
  const int16_t cx = pane_left + (pane_right - pane_left - static_cast<int16_t>(w)) / 2;
  set_cursor_top_left(gfx, cx,
                      BUL_BODY_Y + (BUL_BODY_H - kCustomFontBaseHeight * 2) / 2,
                      kCustomFontBaseHeight * 2);
  gfx.print(msg);
}

void draw_body_messages(Adafruit_GFX& gfx,
                        const Envelope& env,
                        const Payload& payload) {
  // Pane region: full width minus the right rail (unless there are no
  // channels, in which case the rail isn't drawn and the pane spans the
  // full screen width).
  const bool has_channels = !payload.channels.empty();
  const int16_t pane_left = BODY_PAD;
  const int16_t pane_right = has_channels ? (PANEL_W - RAIL_W) : PANEL_W;
  const int16_t pane_top = BUL_BODY_Y;
  const int16_t pane_bot = BUL_BODY_Y + BUL_BODY_H;
  const int16_t pane_text_right = pane_right - BODY_PAD;
  const int16_t pane_inner_w = pane_text_right - pane_left;
  const int16_t body_wrap_w = pane_inner_w - BODY_INDENT_PX;

  if (!has_channels) {
    draw_pane_message(gfx, "No channels configured.", 0, PANEL_W);
    return;
  }

  int idx = env.active_channel_index;
  if (idx < 0 || idx >= static_cast<int>(payload.channels.size())) idx = 0;
  const auto& ch = payload.channels[idx];

  if (ch.messages.empty()) {
    draw_pane_message(gfx, "No messages yet.", 0, pane_right);
    return;
  }

  gfx.setFont(&LessPerfectDOSVGA);
  gfx.setTextSize(1);
  gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);

  // database.get_messages orders pinned-first then ts DESC, so a pinned
  // message can appear at index 0 of ch.messages with an old ts. For a
  // chat-transcript display we want strict newest-on-bottom by ts; sort
  // the indices ourselves and ignore pinned-ness here. A future pinned-
  // band treatment (UI_SPEC §5) can pull pinned out separately; that's
  // out of scope for this PR.
  std::vector<size_t> sorted(ch.messages.size());
  for (size_t i = 0; i < sorted.size(); ++i) sorted[i] = i;
  std::sort(sorted.begin(), sorted.end(), [&](size_t a, size_t b) {
    return ch.messages[a].ts > ch.messages[b].ts;  // newest first
  });
  const size_t candidate_count = std::min(sorted.size(), MAX_MESSAGES_SHOWN);

  struct Block {
    const PayloadMessage* m;
    std::string hhmm;
    std::vector<std::string> body_lines;
    int16_t height;
  };
  std::vector<Block> blocks;
  blocks.reserve(candidate_count);
  for (size_t i = 0; i < candidate_count; ++i) {
    const auto& m = ch.messages[sorted[i]];
    Block b;
    b.m = &m;
    // Prefer the server-formatted "YYYY-MM-DD HH:MM" (in the hub's
    // configured tz). Fall back to a UTC format only when the server
    // didn't supply one — exists for legacy fixtures and old-server
    // compatibility.
    b.hhmm = m.ts_str.empty() ? format_ts_utc_fallback(m.ts) : m.ts_str;
    b.body_lines = wrap_text(gfx, m.body, body_wrap_w, kBodyMaxLines);
    // 1 sender line + N body lines + 1 blank line between messages.
    b.height = LINE_H * (1 + static_cast<int16_t>(b.body_lines.size()) + 1);
    blocks.push_back(std::move(b));
  }

  // blocks[0] is the newest, blocks.back() is the oldest of the slice.
  // Greedily keep newest blocks until adding one more would overflow the
  // pane height. Then reverse for top-down drawing.
  const int16_t pane_avail_h = pane_bot - pane_top - 2 * BODY_PAD;
  int16_t acc = 0;
  size_t fit_count = 0;
  for (const auto& b : blocks) {
    if (acc + b.height > pane_avail_h) break;
    acc += b.height;
    fit_count++;
  }
  if (fit_count == 0) return;
  blocks.resize(fit_count);
  std::reverse(blocks.begin(), blocks.end());

  int16_t y = pane_top + BODY_PAD;
  for (const auto& b : blocks) {
    // Sender line: "HH:MM sender" at setTextSize(1). UI_SPEC §5 wants
    // sender at body-size, not heading-size; reverse-video is the
    // emphasis tool we reach for later, not larger text.
    std::string line = b.hhmm;
    line += ' ';
    line += b.m->sender;
    set_cursor_top_left(gfx, pane_left, y, kCustomFontBaseHeight);
    gfx.print(line.c_str());
    y += LINE_H;
    // Body, indented BODY_INDENT_PX so wrapped lines hang under the
    // sender column.
    for (const auto& body_line : b.body_lines) {
      set_cursor_top_left(gfx, pane_left + BODY_INDENT_PX, y,
                          kCustomFontBaseHeight);
      gfx.print(body_line.c_str());
      y += LINE_H;
    }
    // Blank line between messages — the visual divider, replacing the
    // explicit horizontal rule the old layout drew.
    y += LINE_H;
  }
}

// Format `uptime_s` into a compact "UP <d>d<h>h" / "UP <h>h<m>m" / "UP
// <m>m". Drops trailing units below the relevant magnitude so a hub up
// for 3 days reads as "UP 3d14h", not "UP 3d14h22m". Width is bounded
// at ~10 chars even for multi-year uptimes — fits the footer budget.
void format_uptime(uint32_t s, char* buf, size_t cap) {
  const uint32_t d = s / 86400;
  const uint32_t h = (s % 86400) / 3600;
  const uint32_t m = (s % 3600) / 60;
  if (d > 0) {
    std::snprintf(buf, cap, "UP %ud%uh", d, h);
  } else if (h > 0) {
    std::snprintf(buf, cap, "UP %uh%um", h, m);
  } else {
    std::snprintf(buf, cap, "UP %um", m);
  }
}

// Draw the messages_seen.hour sparkline as 12 fillRect bars in white
// over the footer's inverted-bar background. Bars are normalized to the
// max bar in the window; zero-count buckets render as a single-pixel
// baseline so "no activity" still reads as data rather than "missing".
//
// Returns the x-coordinate just past the right edge of the sparkline so
// the caller can resume cursor advance.
int16_t draw_sparkline(Adafruit_GFX& gfx,
                       int16_t x, int16_t footer_top,
                       const std::vector<int>& bars) {
  constexpr int16_t BAR_W = 4;
  constexpr int16_t BAR_GAP = 1;
  constexpr int16_t MAX_H = 14;
  const int16_t spark_top = footer_top + (BUL_FOOTER_H - MAX_H) / 2;

  int max_val = 0;
  for (int v : bars) if (v > max_val) max_val = v;

  for (size_t i = 0; i < bars.size(); ++i) {
    int v = bars[i];
    int16_t h = 1;
    if (max_val > 0 && v > 0) {
      h = static_cast<int16_t>((static_cast<long>(v) * MAX_H) / max_val);
      if (h < 1) h = 1;
    }
    const int16_t bx = x + static_cast<int16_t>(i) * (BAR_W + BAR_GAP);
    const int16_t by = spark_top + (MAX_H - h);
    gfx.fillRect(bx, by, BAR_W, h, COLOR_WHITE);
  }
  if (bars.empty()) return x;
  return x + static_cast<int16_t>(bars.size()) * (BAR_W + BAR_GAP) - BAR_GAP;
}

void draw_footer(Adafruit_GFX& gfx,
                 const Envelope& env,
                 const Payload& payload,
                 const Stats& stats,
                 const Status& status) {
  const int16_t y = PANEL_H - BUL_FOOTER_H;
  draw_inverted_bar(gfx, y, BUL_FOOTER_H);
  gfx.setFont();  // built-in 5x7
  gfx.setTextSize(1);
  gfx.setTextColor(COLOR_WHITE, COLOR_BLACK);

  const int16_t text_y =
      y + (BUL_FOOTER_H - kBuiltinFontBaseHeight) / 2;

  // Layout is left-to-right segments separated by " | ". Each segment
  // advances `cursor_x`; the sparkline is also a "segment" but drawn as
  // rectangles rather than text. The relative-time string at the end
  // is right-aligned independently.
  int16_t cursor_x = CHROME_PAD;
  bool any_segment = false;

  auto seg = [&](const char* s) {
    if (any_segment) {
      gfx.setCursor(cursor_x, text_y);
      gfx.print(" | ");
      // " | " is 3 chars × 6 px (built-in 5x7 advance) = 18 px.
      cursor_x += 18;
    }
    any_segment = true;
    gfx.setCursor(cursor_x, text_y);
    gfx.print(s);
    int16_t x1, y1; uint16_t w, h;
    gfx.getTextBounds(s, 0, 0, &x1, &y1, &w, &h);
    cursor_x += w;
  };

  char buf[40];
  if (stats.has_stats) {
    format_uptime(stats.uptime_s, buf, sizeof(buf));
    seg(buf);

    std::snprintf(buf, sizeof(buf), "CPU %.2f %.0fC",
                  static_cast<double>(stats.cpu_load_1m),
                  static_cast<double>(stats.cpu_temp_c));
    seg(buf);

    std::snprintf(buf, sizeof(buf), "MEM %dM", stats.mem_available_mb);
    seg(buf);

    std::snprintf(buf, sizeof(buf), "SESS %d/%d",
                  stats.wifi_sessions_now, stats.wifi_sessions_day);
    seg(buf);

    std::snprintf(buf, sizeof(buf), "OUTBOX %d", stats.outbox_depth_now);
    seg(buf);

    if (!stats.messages_seen_hour_bars.empty()) {
      // Separator before the sparkline, then the sparkline itself.
      gfx.setCursor(cursor_x, text_y);
      gfx.print(" | ");
      cursor_x += 18;
      cursor_x = draw_sparkline(gfx, cursor_x, y,
                                stats.messages_seen_hour_bars);
      any_segment = true;
    }
  }

  // RADIO segment from /api/status. Distinct from envelope.radio_state
  // (a firmware-side coarse value); the server's radio_status is the
  // operator-coded truth about mesh_bot liveness.
  if (status.has_status && !status.radio_status.empty()) {
    std::snprintf(buf, sizeof(buf), "RADIO %s",
                  status.radio_status.c_str());
    seg(buf);
  }

  // (Battery warning moved to the top bar's battery indicator. The
  // critical-battery case still takes over the whole screen via
  // draw_critical_battery.)

  // Far-right: how stale this frame is. Lives outside the cursor-advance
  // loop because it's the only right-anchored segment; collisions with
  // the left-aligned run mean the strip overflowed and we lose the
  // relative-time tail (preferable to overwriting nerd-strip data).
  std::string rel = format_relative_time(env.seconds_since_last_update);
  if (env.status == "stale") rel += " (stale)";
  int16_t x1, y1;
  uint16_t w, h;
  gfx.getTextBounds(rel.c_str(), 0, 0, &x1, &y1, &w, &h);
  const int16_t rel_x = PANEL_W - CHROME_PAD - static_cast<int16_t>(w);
  if (rel_x > cursor_x + 6) {
    gfx.setCursor(rel_x, text_y);
    gfx.print(rel.c_str());
  }

  // Fallback when no telemetry: show the old firmware/api banner so the
  // strip doesn't go visibly blank. Fires when /api/stats was
  // unreachable or not fetched (host_render fixtures without stats).
  if (!any_segment) {
    char banner[64];
    std::snprintf(banner, sizeof(banner), "civicmesh-fw %s / api v%d",
                  env.firmware_version.c_str(), payload.api_version);
    gfx.setCursor(CHROME_PAD, text_y);
    gfx.print(banner);
  }
}

}  // namespace

void draw_bulletin(Adafruit_GFX& gfx,
                   const Envelope& env,
                   const Payload& payload,
                   const Stats& stats,
                   const Status& status) {
  gfx.fillScreen(COLOR_WHITE);
  draw_header(gfx, env, payload);
  draw_body_messages(gfx, env, payload);
  // Rail drawn after the body: its borders should paint over anything
  // that might bleed into the right column from earlier passes.
  draw_channel_rail(gfx, env, payload);
  draw_footer(gfx, env, payload, stats, status);
}

}  // namespace screens
}  // namespace render
}  // namespace civicmesh
