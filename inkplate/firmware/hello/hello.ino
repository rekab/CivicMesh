#include "Inkplate.h"

Inkplate display(INKPLATE_1BIT);

void setup() {
    display.begin();
    display.clearDisplay();

    display.setTextColor(BLACK, WHITE);
    display.setTextSize(4);

    display.setCursor(40, 80);
    display.print("CivicMesh");

    display.setCursor(40, 160);
    display.print("Hello, Inkplate");

    display.setCursor(40, 240);
    display.print("hardware validation step 1");

    display.display();
}

void loop() {}
