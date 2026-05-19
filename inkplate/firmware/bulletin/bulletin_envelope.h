#pragma once

// Builds the combined {envelope, payload} JSON that the renderer expects
// (see ../../render/src/render.h:23 and ../../render/src/render.cpp:70-87),
// plus the small bits of bookkeeping the bulletin sketch needs to keep
// e-paper refreshes minimal and to detect activity-jump opportunities.

#include <Arduino.h>
#include <ArduinoJson.h>
#include <stdint.h>

namespace civicmesh {
namespace bulletin {

// Hybrid-refresh signature. Only the active screen's visible state
// participates: failure_kind catches transitions in/out of failure_shell,
// active_channel catches channel rotation, max_msg_id catches new
// messages in the *currently displayed* channel (activity-jump handles
// new messages in *other* channels).
//
// Layout intentionally not packed; the comparison is field-wise via
// operator==. Sized small so a stale value is cheap to keep in RAM.
struct RenderSig {
  uint8_t  failure_kind = 0;   // see kFailure* below
  uint16_t active_channel = 0;
  uint32_t max_msg_id = 0;
};

bool operator==(const RenderSig& a, const RenderSig& b);
inline bool operator!=(const RenderSig& a, const RenderSig& b) { return !(a == b); }

constexpr uint8_t kFailureNone = 0;
constexpr uint8_t kFailureAp = 1;
constexpr uint8_t kFailureDns = 2;
constexpr uint8_t kFailurePi = 3;
constexpr uint8_t kFailureHttp = 4;
constexpr uint8_t kFailureJson = 5;
constexpr uint8_t kFailureApiVer = 6;

uint8_t failure_kind_from_reason(const char* reason);

// Walk the parsed server response for the max `id` in the channel at
// active_idx. Returns 0 if the index is OOB or the channel is empty.
uint32_t max_msg_id_in_channel(const JsonDocument& server_doc,
                               uint16_t active_idx);

// Walk every message in every channel, return the largest `ts`. Used
// to keep high_water_mark_ts up to date.
uint32_t max_ts_across_all_channels(const JsonDocument& server_doc);

// Activity-jump: find the channel index whose newest message has the
// largest ts > hwm. Ties broken by lower channel index. Returns -1 if
// no channel has any message newer than hwm.
int16_t select_activity_channel(const JsonDocument& server_doc,
                                uint32_t hwm);

// Serialize the success-path combined doc. Attaches `server_doc` as
// the payload by shallow reference; do not free server_doc until the
// returned String is no longer needed.
String build_combined_ok_json(const JsonDocument& server_doc,
                              uint16_t active_channel_index,
                              float battery_volts,
                              uint32_t seconds_since_last_update,
                              int expected_api_version,
                              const char* firmware_version);

// Serialize a failure_shell combined doc with payload: null.
// `reason` is passed straight through to the renderer's
// copy_for() lookup in screens/failure_shell.cpp:25-42 (canonical:
// "ap_unreachable" | "dns_failure" | "pi_unreachable" |
// "http_error" | "bad_json"; any other string renders as
// "ERROR / <reason>").
String build_combined_failure_json(const char* reason,
                                   uint16_t active_channel_index,
                                   float battery_volts,
                                   int expected_api_version,
                                   const char* firmware_version);

}  // namespace bulletin
}  // namespace civicmesh
