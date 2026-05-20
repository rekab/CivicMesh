#include "stats.h"

namespace civicmesh {
namespace render {

bool parse_stats(JsonVariantConst v, Stats& out) {
  if (v.isNull() || !v.is<JsonObjectConst>()) return false;
  JsonObjectConst obj = v.as<JsonObjectConst>();

  // system block — all sub-fields are .as<T>() which returns the zero
  // value of T when missing or wrong type, so a partial /api/stats
  // (e.g. compute_stats partway through a refactor) just leaves the
  // unfilled fields at their defaults.
  JsonObjectConst sys = obj["system"].as<JsonObjectConst>();
  if (!sys.isNull()) {
    out.uptime_s = sys["uptime_s"].as<uint32_t>();
    JsonObjectConst cpu = sys["cpu"].as<JsonObjectConst>();
    if (!cpu.isNull()) {
      out.cpu_load_1m = cpu["load_1m"].as<float>();
      out.cpu_temp_c = cpu["temp_c"].as<float>();
    }
    JsonObjectConst mem = sys["mem"].as<JsonObjectConst>();
    if (!mem.isNull()) {
      out.mem_available_mb = mem["available_mb"].as<int>();
    }
    JsonObjectConst outbox = sys["outbox"].as<JsonObjectConst>();
    if (!outbox.isNull()) {
      out.outbox_depth_now = outbox["depth_now"].as<int>();
    }
  }

  JsonObjectConst wifi = obj["wifi_sessions"].as<JsonObjectConst>();
  if (!wifi.isNull()) {
    out.wifi_sessions_now = wifi["now"].as<int>();
    out.wifi_sessions_day = wifi["day"].as<int>();
  }

  // messages_seen.hour.bars — pre-bucketed 5-min × 12 bars by the
  // server. Copy into our vector; any other shape is silently empty.
  JsonObjectConst seen = obj["messages_seen"].as<JsonObjectConst>();
  if (!seen.isNull()) {
    JsonObjectConst hour = seen["hour"].as<JsonObjectConst>();
    if (!hour.isNull()) {
      JsonArrayConst bars = hour["bars"].as<JsonArrayConst>();
      if (!bars.isNull()) {
        for (JsonVariantConst b : bars) {
          out.messages_seen_hour_bars.push_back(b.as<int>());
        }
      }
    }
  }

  out.has_stats = true;
  return true;
}

}  // namespace render
}  // namespace civicmesh
