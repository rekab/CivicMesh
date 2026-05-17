// Inkplate 6 fortunes demo — picks a random fortune on each boot, renders
// it at a random position (screensaver-style, also handy for fighting
// e-paper burn-in), sleeps 1-5 random minutes, repeats.
//
// Wrapping is done in software because drawTextBox's word-wrap doesn't
// behave reliably across InkplateLibrary versions; we compute line breaks
// here and render with setCursor + print so layout is fully predictable.
//
// Corpus: ~650 one-line fortunes from the BSD fortunes-min package,
// extracted by ../tools/build_fortunes.py. See ../../README.md for the
// regenerator workflow and NOTICE at the repo root for attribution.

#include "Inkplate.h"
#include "fortunes_data.h"
#include <string.h>

Inkplate display(INKPLATE_1BIT);

// Sleep range, in minutes. 1-5 inclusive per spec.
static const uint32_t SLEEP_MIN_MINUTES = 1;
static const uint32_t SLEEP_MAX_MINUTES = 5;

static const uint64_t US_PER_SECOND = 1000000ULL;
static const uint64_t SECONDS_PER_MINUTE = 60ULL;

// Panel dimensions and layout. Built-in Adafruit GFX 5x7 font advances
// 6*size px per character and 8*size px per line.
static const int SCREEN_W = 800;
static const int SCREEN_H = 600;
static const int MARGIN = 20;
static const int FOOTER_RESERVED_H = 30;  // bottom strip we don't render fortune into

static const int TEXT_SIZE = 3;
static const int CHAR_W = 6 * TEXT_SIZE;
static const int CHAR_H = 8 * TEXT_SIZE;

// Greedy-wrap bounds. Fortunes are filtered to <=120 chars; at ~40 chars
// per line that's at most 3-4 wrapped lines. 8 leaves headroom.
static const int LINE_CHARS = (SCREEN_W - 2 * MARGIN) / CHAR_W;
static const int MAX_LINES = 8;

// wrap_text: greedy word-wrap on space boundaries. Writes up to
// max_lines null-terminated lines into the lines[] buffer. Returns the
// number of lines used. A word longer than line_chars gets hard-broken
// at line_chars to avoid an infinite loop.
static int wrap_text(const char* text,
                     char lines[][LINE_CHARS + 1],
                     int line_chars,
                     int max_lines) {
    int line_idx = 0;
    int col = 0;
    lines[0][0] = '\0';

    const char* p = text;
    while (*p && line_idx < max_lines) {
        while (*p == ' ') p++;
        if (!*p) break;

        const char* word_start = p;
        while (*p && *p != ' ') p++;
        int word_len = p - word_start;

        if (word_len > line_chars) {
            // Word doesn't fit on any line at all; hard-break it.
            if (col > 0) {
                line_idx++;
                if (line_idx >= max_lines) break;
                col = 0;
            }
            int take = line_chars;
            memcpy(lines[line_idx], word_start, take);
            lines[line_idx][take] = '\0';
            line_idx++;
            col = 0;
            // Re-point p so the remainder of the word becomes the next "word".
            p = word_start + take;
            continue;
        }

        int needed = (col == 0) ? word_len : (1 + word_len);  // +1 for leading space
        if (col + needed > line_chars) {
            line_idx++;
            if (line_idx >= max_lines) break;
            col = 0;
        }
        if (col > 0) {
            lines[line_idx][col++] = ' ';
        }
        memcpy(lines[line_idx] + col, word_start, word_len);
        col += word_len;
        lines[line_idx][col] = '\0';
    }

    return line_idx + (col > 0 ? 1 : 0);
}

// pick_in_range: uniform random int in [lo, hi]. Falls back to lo if
// hi < lo (which happens when the wrapped text is wider/taller than the
// safe envelope; the fortune is rendered pinned to the upper-left instead
// of being clipped).
static int pick_in_range(int lo, int hi) {
    if (hi <= lo) return lo;
    return lo + (int)(esp_random() % (uint32_t)(hi - lo + 1));
}

void setup() {
    display.begin();
    display.clearDisplay();

    // esp_random() pulls from the ESP32 hardware RNG. Without WiFi/BT
    // running its entropy is degraded but still plenty for picking a
    // fortune, a sleep duration, and a screen position; we never use it
    // for anything cryptographic here.
    uint32_t pick = esp_random() % FORTUNES_COUNT;
    uint32_t sleep_minutes =
        SLEEP_MIN_MINUTES + (esp_random() % (SLEEP_MAX_MINUTES - SLEEP_MIN_MINUTES + 1));

    // Wrap.
    char lines[MAX_LINES][LINE_CHARS + 1];
    int n_lines = wrap_text(FORTUNES[pick], lines, LINE_CHARS, MAX_LINES);

    // Bounding box of the wrapped text.
    int max_line_chars = 0;
    for (int i = 0; i < n_lines; i++) {
        int len = (int)strlen(lines[i]);
        if (len > max_line_chars) max_line_chars = len;
    }
    int box_w = max_line_chars * CHAR_W;
    int box_h = n_lines * CHAR_H;

    // Random top-left, constrained so the bounding box stays inside the
    // safe envelope (margins + footer reserved at the bottom). New
    // position every wake; this is what gives the screensaver feel and
    // spreads pixel wear around the panel.
    int x0 = pick_in_range(MARGIN, SCREEN_W - box_w - MARGIN);
    int y0 = pick_in_range(MARGIN, SCREEN_H - box_h - FOOTER_RESERVED_H - MARGIN);

    display.setTextColor(BLACK, WHITE);
    display.setTextSize(TEXT_SIZE);
    for (int i = 0; i < n_lines; i++) {
        display.setCursor(x0, y0 + i * CHAR_H);
        display.print(lines[i]);
    }

    // Footer: small diagnostic line at a fixed position so a viewer can
    // see the app is alive and re-seeded across wakes. Uses the built-in
    // 5x7 font at size 1.
    char footer[96];
    snprintf(footer, sizeof(footer),
             "#%u / %u  -  next refresh in %u min",
             (unsigned)pick, (unsigned)FORTUNES_COUNT, (unsigned)sleep_minutes);
    display.setTextSize(1);
    display.setCursor(10, SCREEN_H - 15);
    display.print(footer);

    display.display();

    // Deep sleep until the timer fires. setup() runs from scratch on
    // wake, picks a fresh fortune and position, repeats. No partial
    // update; the post-wake _blockPartial reset would silently
    // full-refresh anyway (see docs/inkplate-research.md §C item 1).
    esp_sleep_enable_timer_wakeup(
        (uint64_t)sleep_minutes * SECONDS_PER_MINUTE * US_PER_SECOND);
    esp_deep_sleep_start();
}

void loop() {}
