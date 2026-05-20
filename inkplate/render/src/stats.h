#pragma once

#include <ArduinoJson.h>
#include <cstdint>
#include <vector>

namespace civicmesh {
namespace render {

// Subset of /api/stats the Inkplate footer actually renders. Mirrors the
// shape returned by compute_stats() in database.py — see
// inkplate/README.md for the full consumer map. Anything else in the
// payload is ignored at parse time; this keeps the renderer narrowly
// coupled to just the fields it draws.
//
// All fields are optional. has_stats=false means the firmware didn't
// fetch /api/stats this cycle (or the harness didn't fabricate it), and
// the footer should fall back to the firmware-version / api-version
// chrome from older builds.
struct Stats {
  bool has_stats = false;

  // system block
  uint32_t uptime_s = 0;
  float cpu_load_1m = 0.0f;
  float cpu_temp_c = 0.0f;
  int mem_available_mb = 0;
  int outbox_depth_now = 0;

  // wifi_sessions block
  int wifi_sessions_now = 0;
  int wifi_sessions_day = 0;

  // messages_seen.hour — 5-min buckets × 12 bars = 1 hour window.
  // Per the UI iteration ToorCamp discussion: this window reads better
  // than the §5 straw-man (30 min / 60 buckets) and is what /api/stats
  // already pre-bucketed for free.
  std::vector<int> messages_seen_hour_bars;
};

// Returns true if the field was present. Missing/wrong-shape produces a
// default Stats with has_stats=false and is NOT an error — telemetry is
// strictly additive and the footer renders without it.
bool parse_stats(JsonVariantConst v, Stats& out);

}  // namespace render
}  // namespace civicmesh
