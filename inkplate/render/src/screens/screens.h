#pragma once

#include <Adafruit_GFX.h>
#include <string>

#include "../envelope.h"
#include "../payload.h"

namespace civicmesh {
namespace render {
namespace screens {

void draw_bulletin(Adafruit_GFX& gfx,
                   const Envelope& env,
                   const Payload& payload);

void draw_critical_battery(Adafruit_GFX& gfx,
                           const Envelope& env);

// draw_failure_shell and draw_api_mismatch declarations land in PR 1B,
// alongside the corresponding screens/failure_shell.cpp and
// screens/api_mismatch.cpp source files. The render.cpp dispatch
// returns false for those states in PR 1A.

}  // namespace screens
}  // namespace render
}  // namespace civicmesh
