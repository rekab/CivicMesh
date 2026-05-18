// CivicMesh external-display smoke target (Phase 2 PR 2).
//
// Renders one hard-coded fixture's JSON through render_frame(), then
// halts. Proves the renderer (../render/) compiles for ESP32 and the
// Inkplate panel accepts the framebuffer it produces.
//
// Not production firmware. The polling/sleep control loop, WiFi join,
// NVS last-good cache, real envelope sourcing (battery ADC, status),
// and partial refresh all land in Phase 3.

#include <Inkplate.h>
#include <render.h>            // From inkplate/render/, via --libraries .. in firmware/Makefile.
#include "smoke_fixture.h"     // static const char SMOKE_FIXTURE_JSON[] = "...";

Inkplate display(INKPLATE_1BIT);

void setup() {
    Serial.begin(115200);
    delay(200);
    Serial.println("[bulletin] begin");

    display.begin();
    display.clearDisplay();   // framebuffer-only — display.display() below pushes to panel

    bool ok = civicmesh::render::render_frame(display, SMOKE_FIXTURE_JSON);
    Serial.print("[bulletin] render_frame returned ");
    Serial.println(ok ? "true" : "false");

    display.display();
    Serial.println("[bulletin] display() complete; halting");
}

void loop() {
    // Empty. Phase 3 owns the control loop.
}
