// Inkplate 6 fortunes demo — picks a random fortune on each boot, renders
// it full-panel, sleeps 1-5 random minutes, repeats. Proves the toolchain
// can drive a real wake/render/sleep loop and that text layout + the
// hardware RNG + deep sleep all work end-to-end.
//
// Corpus: ~650 one-line fortunes from the BSD fortunes-min package,
// extracted by ../tools/build_fortunes.py. See ../../README.md for the
// regenerator workflow and NOTICE at the repo root for attribution.

#include "Inkplate.h"
#include "fortunes_data.h"

Inkplate display(INKPLATE_1BIT);

// Sleep range, in minutes. 1-5 inclusive per spec.
static const uint32_t SLEEP_MIN_MINUTES = 1;
static const uint32_t SLEEP_MAX_MINUTES = 5;

static const uint64_t US_PER_SECOND = 1000000ULL;
static const uint64_t SECONDS_PER_MINUTE = 60ULL;

void setup() {
    display.begin();
    display.clearDisplay();

    // esp_random() pulls from the ESP32 hardware RNG. Without WiFi/BT
    // running its entropy is degraded but still plenty for picking a
    // fortune and a sleep duration; we never use it for anything
    // cryptographic here.
    uint32_t pick = esp_random() % FORTUNES_COUNT;
    uint32_t sleep_minutes =
        SLEEP_MIN_MINUTES + (esp_random() % (SLEEP_MAX_MINUTES - SLEEP_MIN_MINUTES + 1));

    display.setTextColor(BLACK, WHITE);

    // Main fortune body. drawTextBox word-wraps inside the box and
    // truncates with "..." if it overflows; the corpus is filtered to
    // <=120 chars so truncation is rare in practice.
    display.drawTextBox(
        /*x0=*/40, /*y0=*/120,
        /*x1=*/760, /*y1=*/500,
        FORTUNES[pick],
        /*size=*/4,
        /*font=*/NULL,
        /*vspace=*/6,
        /*border=*/false,
        /*fontSize=*/8);

    // Footer: small diagnostic line so a viewer can see the app is alive
    // and re-seeded across wakes. Uses the built-in 5x7 font at size 1.
    char footer[96];
    snprintf(footer, sizeof(footer),
             "#%u / %u  -  next refresh in %u min",
             (unsigned)pick, (unsigned)FORTUNES_COUNT, (unsigned)sleep_minutes);
    display.setTextSize(1);
    display.setCursor(10, 585);
    display.print(footer);

    display.display();

    // Deep sleep until the timer fires. setup() runs from scratch on
    // wake, picks a fresh fortune, repeats. No partial update; the
    // post-wake _blockPartial reset would silently full-refresh anyway
    // (see docs/inkplate-research.md §C item 1).
    esp_sleep_enable_timer_wakeup(
        (uint64_t)sleep_minutes * SECONDS_PER_MINUTE * US_PER_SECOND);
    esp_deep_sleep_start();
}

void loop() {}
