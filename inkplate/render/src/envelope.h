#pragma once

#include <ArduinoJson.h>
#include <cstdint>
#include <string>

#include "payload.h"

namespace civicmesh {
namespace render {

struct Envelope {
  std::string status;          // "ok" | "stale" | "starting" | "error"
  std::string radio_state;     // "ok" | "degraded" | "down" | "needs_human" | "unknown"
  std::string portal_state;    // "ok" | "down" | "unknown"
  float battery_volts = 0.0f;
  uint32_t seconds_since_last_update = 0;
  int16_t active_channel_index = 0;
  std::string failure_reason;  // empty if absent or null
  int http_code = 0;           // non-2xx status for failure_reason=="http_error"; 0 if absent
  std::string firmware_version;
  int expected_api_version = 0;
  bool has_cached_payload = false;
  Payload cached_payload;
};

bool parse_envelope(JsonObjectConst obj, Envelope& out, std::string& err);

}  // namespace render
}  // namespace civicmesh
