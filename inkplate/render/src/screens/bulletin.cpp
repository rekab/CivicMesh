#include "screens.h"

#include <algorithm>
#include <cstdio>
#include <ctime>
#include <string>
#include <vector>

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

// Body indent for continuation lines, per UI_SPEC §5. Six size-1 cells
// of ~8 px = 48 px. Aligns body lines under the sender column ("HH:MM "
// is exactly 6 cells, so the body hangs under the sender name).
constexpr int16_t BODY_INDENT_PX = 48;

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

// Fallback: format an epoch-seconds timestamp as "HH:MM" in UTC.
//
// Per UI_SPEC §5 the firmware does not compute time — the server
// supplies a pre-formatted `ts_str` on each message (formatted in
// cfg.node.timezone via Python's zoneinfo, with DST). This helper
// only fires when ts_str is missing: legacy fixtures or an older
// server that doesn't carry the field. UTC is chosen because the
// firmware has no timezone context of its own; the wall-clock time
// will be wrong by the local tz offset, but the relative ordering
// and the minute boundaries are still correct.
std::string format_hhmm_utc_fallback(long ts) {
  std::time_t t = static_cast<std::time_t>(ts);
  std::tm tm_buf{};
  gmtime_r(&t, &tm_buf);
  char buf[8];
  std::snprintf(buf, sizeof(buf), "%02d:%02d", tm_buf.tm_hour, tm_buf.tm_min);
  return std::string(buf);
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

// Right-edge file-folder channel tabs. Drawn AFTER the body so a tab's
// border paints cleanly over any pane content that would otherwise bleed
// into the rail column. Tabs are fixed-height (TAB_H) and stack from the
// top of the rail. The area below the last tab is still bounded by the
// rail's continuous right edge — the rail reads as a column, not a
// loose stack of tabs.
void draw_channel_rail(Adafruit_GFX& gfx,
                       const Envelope& env,
                       const Payload& payload) {
  if (payload.channels.empty()) return;
  const size_t n = payload.channels.size();
  const int16_t rail_x = PANEL_W - RAIL_W;
  const int16_t rail_y = BODY_Y;
  const int16_t rail_h_total = BODY_H;
  // Clamp visible tab count to what fits in the rail (defensive — with
  // TAB_H=54 and BODY_H=520 the rail holds ~9 tabs, more than the ~6 we
  // ever see in practice).
  const size_t max_visible = static_cast<size_t>(rail_h_total / TAB_H);
  const size_t visible = std::min(n, max_visible);

  int active_idx = env.active_channel_index;
  if (active_idx < 0 || active_idx >= static_cast<int>(n)) active_idx = 0;

  for (size_t i = 0; i < visible; ++i) {
    const int16_t y_top = rail_y + static_cast<int16_t>(i) * TAB_H;
    const bool is_active = (static_cast<int>(i) == active_idx);

    if (is_active) {
      // Active: double-line border on top/right/bottom; no left border
      // so the tab and pane read as one continuous piece of paper.
      draw_double_border_no_left(gfx, rail_x, y_top, RAIL_W, TAB_H);
    } else {
      // Inactive: plain single-line rectangle. No fill, no shading.
      gfx.drawRect(rail_x, y_top, RAIL_W, TAB_H, COLOR_BLACK);
    }

    // Tab label at heading size (setTextSize(2), same as the top status
    // bar's site_name / channel-name). Vertically centered in the tab,
    // left-aligned with TAB_PAD; labels too long for RAIL_W clip at the
    // right edge of the tab.
    gfx.setFont(&LessPerfectDOSVGA);
    gfx.setTextSize(2);
    gfx.setTextColor(COLOR_BLACK, COLOR_WHITE);
    const int16_t label_h = kCustomFontBaseHeight * 2;  // 32 px
    const int16_t label_top = y_top + (TAB_H - label_h) / 2;
    set_cursor_top_left(gfx, rail_x + TAB_PAD, label_top, label_h);
    gfx.print(payload.channels[i].name.c_str());
  }

  // Rail/pane boundary line, on the right edge of the pane (= left edge
  // of the rail). Inactive tabs already supply this line via their own
  // left border; the active tab leaves it open (the "gap" — its merge
  // with the pane). This extension covers the empty area below the last
  // tab so the boundary continues to the body bottom and the rail still
  // reads as a bounded column even when only a few tabs fill it.
  const int16_t last_tab_bot = rail_y + static_cast<int16_t>(visible) * TAB_H;
  const int16_t rail_bot = rail_y + rail_h_total;
  if (last_tab_bot < rail_bot) {
    gfx.drawFastVLine(rail_x, last_tab_bot,
                      rail_bot - last_tab_bot, COLOR_BLACK);
  }
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
                      BODY_Y + (BODY_H - kCustomFontBaseHeight * 2) / 2,
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
  const int16_t pane_top = BODY_Y;
  const int16_t pane_bot = BODY_Y + BODY_H;
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
    // Prefer the server-formatted HH:MM (in the hub's configured tz).
    // Fall back to a UTC format only when the server didn't supply one
    // — exists for legacy fixtures and old-server compatibility.
    b.hhmm = m.ts_str.empty() ? format_hhmm_utc_fallback(m.ts) : m.ts_str;
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
  // Rail drawn after the body: its borders should paint over anything
  // that might bleed into the right column from earlier passes.
  draw_channel_rail(gfx, env, payload);
  draw_footer(gfx, env, payload);
}

}  // namespace screens
}  // namespace render
}  // namespace civicmesh
