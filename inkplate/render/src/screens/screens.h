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

void draw_failure_shell(Adafruit_GFX& gfx,
                        const Envelope& env);

void draw_api_mismatch(Adafruit_GFX& gfx,
                       const Envelope& env,
                       const Payload& payload,
                       const std::string& raw_payload_json);

void draw_critical_battery(Adafruit_GFX& gfx,
                           const Envelope& env);

}  // namespace screens
}  // namespace render
}  // namespace civicmesh
