#include "bulletin_envelope.h"

#include <string.h>

namespace civicmesh {
namespace bulletin {

bool operator==(const RenderSig& a, const RenderSig& b) {
  return a.failure_kind == b.failure_kind &&
         a.active_channel == b.active_channel &&
         a.max_msg_id == b.max_msg_id;
}

uint8_t failure_kind_from_reason(const char* reason) {
  if (!reason) return kFailureNone;
  if (!strcmp(reason, "ap_unreachable")) return kFailureAp;
  if (!strcmp(reason, "dns_failure")) return kFailureDns;
  if (!strcmp(reason, "pi_unreachable")) return kFailurePi;
  if (!strcmp(reason, "http_error")) return kFailureHttp;
  if (!strcmp(reason, "bad_json")) return kFailureJson;
  if (!strcmp(reason, "api_mismatch")) return kFailureApiVer;
  return kFailureHttp;  // unknown — generic
}

uint32_t max_msg_id_in_channel(const JsonDocument& server_doc,
                               uint16_t active_idx) {
  JsonArrayConst channels = server_doc["channels"].as<JsonArrayConst>();
  if (channels.isNull()) return 0;
  if (active_idx >= channels.size()) return 0;
  JsonObjectConst ch = channels[active_idx].as<JsonObjectConst>();
  if (ch.isNull()) return 0;
  JsonArrayConst msgs = ch["messages"].as<JsonArrayConst>();
  if (msgs.isNull()) return 0;
  uint32_t max_id = 0;
  for (JsonVariantConst m : msgs) {
    uint32_t id = m["id"].as<uint32_t>();
    if (id > max_id) max_id = id;
  }
  return max_id;
}

uint32_t max_ts_across_all_channels(const JsonDocument& server_doc) {
  JsonArrayConst channels = server_doc["channels"].as<JsonArrayConst>();
  if (channels.isNull()) return 0;
  uint32_t max_ts = 0;
  for (JsonVariantConst ch : channels) {
    JsonArrayConst msgs = ch["messages"].as<JsonArrayConst>();
    if (msgs.isNull()) continue;
    for (JsonVariantConst m : msgs) {
      uint32_t ts = m["ts"].as<uint32_t>();
      if (ts > max_ts) max_ts = ts;
    }
  }
  return max_ts;
}

int16_t select_activity_channel(const JsonDocument& server_doc,
                                uint32_t hwm) {
  JsonArrayConst channels = server_doc["channels"].as<JsonArrayConst>();
  if (channels.isNull()) return -1;
  int16_t best_idx = -1;
  uint32_t best_ts = 0;
  uint16_t i = 0;
  for (JsonVariantConst ch : channels) {
    JsonArrayConst msgs = ch["messages"].as<JsonArrayConst>();
    if (!msgs.isNull()) {
      uint32_t newest = 0;
      for (JsonVariantConst m : msgs) {
        uint32_t ts = m["ts"].as<uint32_t>();
        if (ts > newest) newest = ts;
      }
      // Tiebreaker: strictly > best_ts, so the lower index wins on tie.
      if (newest > hwm && newest > best_ts) {
        best_ts = newest;
        best_idx = static_cast<int16_t>(i);
      }
    }
    ++i;
  }
  return best_idx;
}

namespace {

void fill_envelope(JsonObject env,
                   const char* status,
                   const char* radio_state,
                   const char* portal_state,
                   const char* failure_reason,
                   uint16_t active_channel_index,
                   float battery_volts,
                   uint32_t seconds_since_last_update,
                   int expected_api_version,
                   const char* firmware_version) {
  env["status"] = status;
  env["radio_state"] = radio_state;
  env["portal_state"] = portal_state;
  env["battery_volts"] = battery_volts;
  env["seconds_since_last_update"] = seconds_since_last_update;
  env["active_channel_index"] = active_channel_index;
  if (failure_reason && failure_reason[0]) {
    env["failure_reason"] = failure_reason;
  } else {
    env["failure_reason"] = nullptr;
  }
  env["firmware_version"] = firmware_version;
  env["expected_api_version"] = expected_api_version;
  env["cached_payload"] = nullptr;  // Phase 3B fills this from NVS
}

}  // namespace

String build_combined_ok_json(const JsonDocument& server_doc,
                              const JsonDocument* stats_doc,
                              const JsonDocument* status_doc,
                              uint16_t active_channel_index,
                              float battery_volts,
                              int wifi_rssi,
                              uint32_t seconds_since_last_update,
                              int expected_api_version,
                              const char* firmware_version) {
  JsonDocument out;
  JsonObject env = out["envelope"].to<JsonObject>();
  fill_envelope(env, "ok", "ok", "ok", nullptr, active_channel_index,
                battery_volts, seconds_since_last_update,
                expected_api_version, firmware_version);
  // Only the OK path carries a live RSSI; the failure / critical-battery /
  // api_mismatch builders don't render the bulletin header, so they omit
  // the field and the renderer falls back to "unknown".
  env["wifi_rssi"] = wifi_rssi;
  // Shallow attach: out["payload"] references server_doc's tree. Caller
  // must hold server_doc alive until the returned String is built.
  out["payload"] = server_doc.as<JsonVariantConst>();
  if (stats_doc) {
    out["stats"] = stats_doc->as<JsonVariantConst>();
  }
  if (status_doc) {
    out["status"] = status_doc->as<JsonVariantConst>();
  }

  String s;
  serializeJson(out, s);
  return s;
}

String build_combined_failure_json(const char* reason,
                                   uint16_t active_channel_index,
                                   float battery_volts,
                                   int expected_api_version,
                                   const char* firmware_version,
                                   int http_code) {
  JsonDocument out;
  JsonObject env = out["envelope"].to<JsonObject>();
  // For failure_shell, radio/portal go to "unknown" — matches
  // failure_ap_unreachable__no_cache/in.json and lets the renderer's
  // copy_for() pick the right header/big_line/detail strings.
  fill_envelope(env, "error", "unknown", "unknown", reason,
                active_channel_index, battery_volts,
                /*seconds_since_last_update=*/0,
                expected_api_version, firmware_version);
  // Only the http_error screen renders this; 0 elsewhere is harmless
  // (the renderer keeps the generic detail when http_code <= 0).
  env["http_code"] = http_code;
  out["payload"] = nullptr;

  String s;
  serializeJson(out, s);
  return s;
}

String build_combined_critical_battery_json(float battery_volts,
                                            int expected_api_version,
                                            const char* firmware_version) {
  JsonDocument out;
  JsonObject env = out["envelope"].to<JsonObject>();
  // status="ok" — critical_battery is dispatched BEFORE the
  // status=="error" → failure_shell check (render.cpp:28-34), so
  // setting "error" here would short-circuit to failure_shell and
  // the critical_battery screen would never render. The renderer's
  // dispatch trigger is purely battery_volts > 0 && < BATT_CRIT_V.
  fill_envelope(env, "ok", "unknown", "unknown", nullptr,
                /*active_channel_index=*/0, battery_volts,
                /*seconds_since_last_update=*/0,
                expected_api_version, firmware_version);
  out["payload"] = nullptr;

  String s;
  serializeJson(out, s);
  return s;
}

String build_combined_api_mismatch_json(const JsonDocument& server_doc,
                                        float battery_volts,
                                        int expected_api_version,
                                        const char* firmware_version) {
  JsonDocument out;
  JsonObject env = out["envelope"].to<JsonObject>();
  // status="ok" (NOT "error"). render.cpp:34 routes any status=="error"
  // envelope to failure_shell BEFORE the api_version check, so an
  // error-status envelope would render failure_shell instead of the
  // dedicated api_mismatch screen. Dispatch hits api_mismatch when
  // payload is parsable AND payload.api_version != env.expected_api_version
  // (render.cpp:37-40).
  fill_envelope(env, "ok", "unknown", "unknown", nullptr,
                /*active_channel_index=*/0, battery_volts,
                /*seconds_since_last_update=*/0,
                expected_api_version, firmware_version);
  // Payload attached so the renderer can extract the raw JSON dump
  // (render.cpp:97-99 → extract_payload_json from the combined doc).
  out["payload"] = server_doc.as<JsonVariantConst>();

  String s;
  serializeJson(out, s);
  return s;
}

}  // namespace bulletin
}  // namespace civicmesh
