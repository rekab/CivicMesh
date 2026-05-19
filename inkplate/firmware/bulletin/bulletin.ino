// CivicMesh external-display autonomous firmware — Phase 3A PR 1.
//
// Replaces the Phase 2 smoke target: instead of one hardcoded fixture,
// associate WiFi, fetch /api/external-display/state, wrap in a
// firmware-built envelope, dispatch through render_frame, sleep,
// repeat. Fibonacci poll cadence (10s -> 300s cap), independent 5-min
// channel rotation, activity-jump on new server-side messages, and a
// hybrid-refresh signature so e-paper only repaints on visible change.
//
// PR 2 adds: WAKE button, 5-fail deep-sleep escalation, battery
// sampling, dedicated api_mismatch screen. NVS cache, RTC, adaptive
// layouts come in later Phase 3 PRs.

#include <Inkplate.h>
#include <WiFi.h>
#include <render.h>

#include "bulletin_envelope.h"
#include "bulletin_net.h"

// All three of these are injected via -D flags from firmware/Makefile.
// CIVICMESH_SSID is required (the Makefile errors out if unset);
// CIVICMESH_ENDPOINT defaults to http://10.0.0.1/api/external-display/state;
// CIVICMESH_FW_VERSION defaults to 0.1.0.
#ifndef CIVICMESH_SSID
#error "CIVICMESH_SSID must be defined via -D flag in the Makefile"
#endif
#ifndef CIVICMESH_ENDPOINT
#error "CIVICMESH_ENDPOINT must be defined via -D flag in the Makefile"
#endif
#ifndef CIVICMESH_FW_VERSION
#define CIVICMESH_FW_VERSION "0.1.0"
#endif

using civicmesh::bulletin::FetchError;
using civicmesh::bulletin::RenderSig;

// --- Settled parameters ---

static constexpr uint32_t POLL_CADENCE_INIT_MS  = 10000;
static constexpr uint32_t POLL_CADENCE_CAP_MS   = 300000;
static constexpr uint32_t ROTATION_INTERVAL_MS  = 300000;
static constexpr uint8_t  WIFI_CONNECT_TIMEOUT_S = 20;
static constexpr uint16_t HTTP_CONNECT_TIMEOUT_MS = 5000;
static constexpr uint16_t HTTP_READ_TIMEOUT_MS    = 5000;
static constexpr int      EXPECTED_API_VERSION   = 2;
static constexpr float    HARDCODED_BATTERY_VOLTS = 3.95f;  // PR 2 swaps to display.readBattery()

// Fibonacci cadence in ms, terminated by the cap. Reset path returns to fib[0].
static const uint32_t kFib[] = {10000, 20000, 30000, 50000, 80000, 130000, 210000, 300000};
static constexpr size_t kFibLen = sizeof(kFib) / sizeof(kFib[0]);

static uint32_t fib_advance(uint32_t current) {
  for (size_t i = 0; i < kFibLen; ++i) {
    if (kFib[i] == current && i + 1 < kFibLen) return kFib[i + 1];
  }
  return POLL_CADENCE_CAP_MS;
}

// --- Globals (RAM-only; no NVS, no RTC in PR 1) ---

Inkplate display(INKPLATE_1BIT);

static uint16_t  active_channel_index = 0;
static uint16_t  channel_count        = 0;
static uint32_t  last_poll_millis     = 0;
static uint32_t  next_poll_delay_ms   = POLL_CADENCE_INIT_MS;
static uint32_t  next_rotation_at_ms  = 0;
static uint32_t  high_water_mark_ts   = 0;
static RenderSig last_render_sig      = {};

// Cached hostname parsed once at boot from CIVICMESH_ENDPOINT; used for
// pre-flight DNS so failure_shell can show dns_failure vs pi_unreachable.
static String cached_host;

// Last successful server response, retained so the rotation timer can
// re-render a different channel between polls without re-fetching.
// Phase 3B adds NVS persistence; for now this is RAM-only and is
// cleared whenever a poll fails.
static JsonDocument g_last_server_doc;
static bool g_has_last_server_doc = false;

// --- Helpers ---

// Wrap-safe millis() comparison: returns true iff `now` has reached or
// passed `target`. Naive `now >= target` is wrong across millis wraparound.
static inline bool reached(uint32_t now, uint32_t target) {
  return (int32_t)(now - target) >= 0;
}

static void push_to_panel(const String& combined_json, const char* reason) {
  bool ok = civicmesh::render::render_frame(display, combined_json.c_str());
  Serial.printf("[bulletin] render reason=%s render_frame=%d\n",
                reason, ok ? 1 : 0);
  display.display();
  Serial.println("[bulletin] display.display() done");
}

static void render_combined_ok(const JsonDocument& server_doc,
                               const char* reason) {
  String s = civicmesh::bulletin::build_combined_ok_json(
      server_doc, active_channel_index, HARDCODED_BATTERY_VOLTS,
      /*seconds_since_last_update=*/0, EXPECTED_API_VERSION,
      CIVICMESH_FW_VERSION);
  push_to_panel(s, reason);
  RenderSig sig;
  sig.failure_kind = civicmesh::bulletin::kFailureNone;
  sig.active_channel = active_channel_index;
  sig.max_msg_id = civicmesh::bulletin::max_msg_id_in_channel(
      server_doc, active_channel_index);
  last_render_sig = sig;
  Serial.printf("[bulletin] sig=(%u,%u,%u)\n",
                (unsigned)sig.failure_kind,
                (unsigned)sig.active_channel,
                (unsigned)sig.max_msg_id);
}

static void render_if_sig_changed(const JsonDocument& server_doc,
                                  const char* reason) {
  RenderSig sig;
  sig.failure_kind = civicmesh::bulletin::kFailureNone;
  sig.active_channel = active_channel_index;
  sig.max_msg_id = civicmesh::bulletin::max_msg_id_in_channel(
      server_doc, active_channel_index);
  if (sig == last_render_sig) {
    Serial.printf("[bulletin] sig unchanged, skip render reason=%s\n", reason);
    return;
  }
  render_combined_ok(server_doc, reason);
}

static void render_failure(const char* reason) {
  String s = civicmesh::bulletin::build_combined_failure_json(
      reason, active_channel_index, HARDCODED_BATTERY_VOLTS,
      EXPECTED_API_VERSION, CIVICMESH_FW_VERSION);
  char tag[48];
  snprintf(tag, sizeof(tag), "failure(%s)", reason);
  push_to_panel(s, tag);
  RenderSig sig;
  sig.failure_kind = civicmesh::bulletin::failure_kind_from_reason(reason);
  sig.active_channel = active_channel_index;
  sig.max_msg_id = 0;
  last_render_sig = sig;
}

static void fail_path() {
  next_poll_delay_ms = POLL_CADENCE_INIT_MS;  // fast retry
}

static void poll() {
  last_poll_millis = millis();
  Serial.printf("[bulletin] poll start (next in %us)\n",
                (unsigned)(next_poll_delay_ms / 1000));

  if (!civicmesh::bulletin::connect_wifi_if_needed(
          display, CIVICMESH_SSID, WIFI_CONNECT_TIMEOUT_S)) {
    Serial.println("[bulletin] wifi associate failed");
    render_failure("ap_unreachable");
    fail_path();
    return;
  }
  Serial.printf("[bulletin] wifi connect ok ip=%s\n",
                WiFi.localIP().toString().c_str());

  JsonDocument server_doc;
  int http_code = 0;
  IPAddress resolved;
  FetchError err = civicmesh::bulletin::fetch_payload(
      CIVICMESH_ENDPOINT, cached_host.c_str(),
      HTTP_CONNECT_TIMEOUT_MS, HTTP_READ_TIMEOUT_MS,
      server_doc, &http_code, &resolved);

  switch (err) {
    case FetchError::kDnsFailure:
      Serial.printf("[bulletin] fetch dns_failed host=%s\n", cached_host.c_str());
      render_failure("dns_failure"); fail_path(); return;
    case FetchError::kPiUnreachable:
      Serial.printf("[bulletin] fetch pi_unreachable code=%d\n", http_code);
      render_failure("pi_unreachable"); fail_path(); return;
    case FetchError::kHttpError:
      Serial.printf("[bulletin] fetch http_error code=%d\n", http_code);
      render_failure("http_error"); fail_path(); return;
    case FetchError::kBadJson:
      Serial.println("[bulletin] fetch bad_json");
      render_failure("bad_json"); fail_path(); return;
    case FetchError::kOk:
      Serial.printf("[bulletin] fetch dns_ok ip=%s http_code=%d\n",
                    resolved.toString().c_str(), http_code);
      break;
  }

  int api_v = server_doc["api_version"].as<int>();
  if (api_v != EXPECTED_API_VERSION) {
    Serial.printf("[bulletin] api_mismatch got=%d want=%d\n",
                  api_v, EXPECTED_API_VERSION);
    // PR 1 treats api_mismatch as a transient failure_shell. The
    // dedicated api_mismatch screen + deep-sleep escalation are PR 2.
    render_failure("api_mismatch");
    fail_path();
    return;
  }

  JsonArrayConst channels = server_doc["channels"].as<JsonArrayConst>();
  channel_count = channels.isNull() ? 0 : channels.size();
  Serial.printf("[bulletin] parse ok channels=%u server_time=%u\n",
                (unsigned)channel_count,
                (unsigned)server_doc["server_time"].as<uint32_t>());

  if (channel_count == 0) {
    // Hub has no configured channels. Render the bulletin anyway —
    // the renderer's bulletin screen tolerates empty input — and
    // skip activity-jump entirely.
    active_channel_index = 0;
    next_poll_delay_ms = fib_advance(next_poll_delay_ms);
    g_last_server_doc = server_doc;
    g_has_last_server_doc = true;
    Serial.println("[bulletin] zero channels; rendering empty bulletin");
    render_if_sig_changed(g_last_server_doc, "quiet_poll_empty");
    return;
  }
  if (active_channel_index >= channel_count) {
    active_channel_index = channel_count - 1;
  }

  int16_t jump_to = civicmesh::bulletin::select_activity_channel(
      server_doc, high_water_mark_ts);
  uint32_t new_hwm = civicmesh::bulletin::max_ts_across_all_channels(server_doc);
  if (new_hwm > high_water_mark_ts) high_water_mark_ts = new_hwm;

  // Cache before render so rotation between polls has fresh data.
  g_last_server_doc = server_doc;
  g_has_last_server_doc = true;

  if (jump_to >= 0) {
    active_channel_index = (uint16_t)jump_to;
    uint32_t prev = next_poll_delay_ms;
    next_poll_delay_ms = POLL_CADENCE_INIT_MS;
    next_rotation_at_ms = millis() + ROTATION_INTERVAL_MS;
    Serial.printf("[bulletin] activity jump to channel=%u hwm=%u (cadence reset %u->%u)\n",
                  (unsigned)active_channel_index,
                  (unsigned)high_water_mark_ts,
                  (unsigned)prev, (unsigned)next_poll_delay_ms);
    render_combined_ok(g_last_server_doc, "activity");
  } else {
    uint32_t prev = next_poll_delay_ms;
    next_poll_delay_ms = fib_advance(next_poll_delay_ms);
    if (next_rotation_at_ms == 0) {
      next_rotation_at_ms = millis() + ROTATION_INTERVAL_MS;
    }
    Serial.printf("[bulletin] cadence advance %ums -> %ums\n",
                  (unsigned)prev, (unsigned)next_poll_delay_ms);
    render_if_sig_changed(g_last_server_doc, "quiet_poll");
  }
}

// --- Arduino entry ---

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("[bulletin] boot fw=%s ssid=%s endpoint=%s\n",
                CIVICMESH_FW_VERSION, CIVICMESH_SSID, CIVICMESH_ENDPOINT);

  display.begin();
  display.clearDisplay();

  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);

  cached_host = civicmesh::bulletin::parse_host_from_url(CIVICMESH_ENDPOINT);
  Serial.printf("[bulletin] cached host=%s\n", cached_host.c_str());

  // Schedule first poll immediately so we don't wait an initial 10s.
  last_poll_millis = millis() - next_poll_delay_ms;
}

void loop() {
  uint32_t now = millis();
  if (reached(now, last_poll_millis + next_poll_delay_ms)) {
    poll();
  } else if (g_has_last_server_doc && channel_count > 0 &&
             reached(now, next_rotation_at_ms)) {
    active_channel_index = (active_channel_index + 1) % channel_count;
    next_rotation_at_ms = millis() + ROTATION_INTERVAL_MS;
    Serial.printf("[bulletin] rotation -> channel=%u\n",
                  (unsigned)active_channel_index);
    render_combined_ok(g_last_server_doc, "rotation");
  } else {
    delay(50);
  }
}
