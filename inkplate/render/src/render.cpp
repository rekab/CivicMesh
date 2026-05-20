#include "render.h"

#include <ArduinoJson.h>
#include <string>

#include "envelope.h"
#include "layout.h"
#include "payload.h"
#include "stats.h"
#include "status.h"
#include "screens/screens.h"

// The renderer trusts server-side NFKD-to-ASCII normalization
// (external_display.py:_normalize_text). Any byte outside 0x20-0x7E
// reaching here will render as the font's empty glyph — visible
// bug-report behavior, intentional. No runtime validation.

namespace civicmesh {
namespace render {

namespace {

enum class ScreenChoice {
  kBulletin,
  kFailureShell,
  kApiMismatch,
  kCriticalBattery,
};

ScreenChoice choose_screen(const Envelope& env,
                           bool payload_parsed_ok,
                           const Payload& payload) {
  if (env.battery_volts > 0.0f && env.battery_volts < BATT_CRIT_V) {
    return ScreenChoice::kCriticalBattery;
  }
  if (env.status == "error") {
    return ScreenChoice::kFailureShell;
  }
  if (payload_parsed_ok &&
      payload.api_version != env.expected_api_version) {
    return ScreenChoice::kApiMismatch;
  }
  return ScreenChoice::kBulletin;
}

std::string extract_payload_json(const char* combined_json) {
  // Cheap brace-walk to extract the raw "payload" sub-object as a
  // string, for the api_mismatch screen's diagnostic dump. We do not
  // need perfect correctness — only a representative snippet.
  std::string s(combined_json);
  size_t key = s.find("\"payload\"");
  if (key == std::string::npos) return "";
  size_t colon = s.find(':', key);
  if (colon == std::string::npos) return "";
  size_t i = colon + 1;
  while (i < s.size() && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n')) ++i;
  if (i >= s.size() || s[i] != '{') return "";
  int depth = 0;
  size_t start = i;
  for (; i < s.size(); ++i) {
    if (s[i] == '{') ++depth;
    else if (s[i] == '}') {
      --depth;
      if (depth == 0) return s.substr(start, i - start + 1);
    }
  }
  return "";
}

}  // namespace

bool render_frame(Adafruit_GFX& gfx, const char* combined_json) {
  JsonDocument doc;
  DeserializationError de = deserializeJson(doc, combined_json);
  if (de) return false;

  Envelope env;
  std::string err;
  JsonObjectConst env_obj = doc["envelope"].as<JsonObjectConst>();
  if (!parse_envelope(env_obj, env, err)) return false;

  Payload payload;
  bool payload_parsed_ok = false;
  JsonVariantConst payload_v = doc["payload"];
  if (!payload_v.isNull() && payload_v.is<JsonObjectConst>()) {
    if (parse_payload(payload_v.as<JsonObjectConst>(), payload, err)) {
      payload_parsed_ok = true;
    }
  }

  // Stats and Status are additive, optional top-level fields the
  // firmware composes from /api/stats and /api/status. Parse failures
  // (missing field, wrong shape) are NOT fatal — the renderer falls
  // back to envelope-only chrome. See inkplate/README.md for the
  // consumer map and stats.h / status.h for the field subset.
  Stats stats;
  parse_stats(doc["stats"], stats);
  Status status;
  parse_status(doc["status"], status);

  ScreenChoice choice = choose_screen(env, payload_parsed_ok, payload);
  switch (choice) {
    case ScreenChoice::kCriticalBattery:
      screens::draw_critical_battery(gfx, env);
      return true;
    case ScreenChoice::kFailureShell:
      screens::draw_failure_shell(gfx, env);
      return true;
    case ScreenChoice::kApiMismatch:
      screens::draw_api_mismatch(gfx, env, payload,
                                 extract_payload_json(combined_json));
      return true;
    case ScreenChoice::kBulletin:
      screens::draw_bulletin(gfx, env, payload, stats, status);
      return true;
  }
  return false;
}

}  // namespace render
}  // namespace civicmesh
