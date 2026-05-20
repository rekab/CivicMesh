#pragma once

#include <ArduinoJson.h>
#include <cstdint>
#include <string>

namespace civicmesh {
namespace render {

// Subset of /api/status the Inkplate footer renders. /api/status is
// captive-portal-facing first; the Inkplate is a secondary consumer.
// See inkplate/README.md for the full consumer map.
//
// Optional: has_status=false means the firmware didn't fetch
// /api/status this cycle, and the footer falls back to envelope's
// firmware-side radio_state.
struct Status {
  bool has_status = false;
  std::string radio_status;  // "online" | "offline" | "recovering" | "needs_human"
  // -1 sentinel: mesh_bot row absent (the empty-branch /api/status
  // doesn't include last_seen_ts, so age_sec is undefined). Footer
  // checks this rather than printing a "0s ago" lie.
  int age_sec = -1;
};

// Returns true if the field was present. Missing/wrong-shape produces a
// default Status with has_status=false and is NOT an error.
bool parse_status(JsonVariantConst v, Status& out);

}  // namespace render
}  // namespace civicmesh
