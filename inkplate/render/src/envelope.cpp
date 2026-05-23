#include "envelope.h"

namespace civicmesh {
namespace render {

static std::string str_or_empty(JsonVariantConst v) {
  const char* p = v.as<const char*>();
  return p ? std::string(p) : std::string();
}

bool parse_envelope(JsonObjectConst obj, Envelope& out, std::string& err) {
  if (obj.isNull()) {
    err = "envelope: missing object";
    return false;
  }
  out.status = str_or_empty(obj["status"]);
  out.radio_state = str_or_empty(obj["radio_state"]);
  out.portal_state = str_or_empty(obj["portal_state"]);
  out.battery_volts = obj["battery_volts"].as<float>();
  out.seconds_since_last_update = obj["seconds_since_last_update"].as<uint32_t>();
  out.active_channel_index = obj["active_channel_index"].as<int16_t>();
  out.failure_reason = str_or_empty(obj["failure_reason"]);
  out.http_code = obj["http_code"].as<int>();  // 0 when absent/null
  out.firmware_version = str_or_empty(obj["firmware_version"]);
  out.expected_api_version = obj["expected_api_version"].as<int>();

  JsonVariantConst cached = obj["cached_payload"];
  out.has_cached_payload = !cached.isNull() && cached.is<JsonObjectConst>();
  if (out.has_cached_payload) {
    if (!parse_payload(cached.as<JsonObjectConst>(), out.cached_payload, err)) {
      return false;
    }
  }
  return true;
}

}  // namespace render
}  // namespace civicmesh
