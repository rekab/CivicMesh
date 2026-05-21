// CivicMesh external-display autonomous firmware — Phase 3A PR 1 + PR 2.
//
// PR 1 shipped the online happy path: WiFi associate, fetch
// /api/external-display/state, wrap in a firmware-built envelope,
// dispatch through render_frame, on a Fibonacci poll cadence with
// 5-min channel rotation, activity-jump on new server-side messages,
// and a hybrid-refresh signature so the e-paper only repaints on
// visible change.
//
// PR 2 adds the off-paths and the one user-input surface:
//   - WAKE button (GPIO 36, active-LOW): ISR force-poll during normal
//     op; ext0 source for waking from deep sleep.
//   - 5-strike failure streak → deep sleep until WAKE.
//   - Battery sampling on every failure (with WiFi quiesced). If
//     <3.6V, render critical_battery + sleep instead of failure_shell.
//   - Battery sample in setup() before WiFi powers up (catches
//     dead-battery wake-from-sleep before burning RF cycles).
//   - api_mismatch detection routes to the dedicated api_mismatch
//     screen, then sleeps. Permanent state, not transient.
//
// State across deep sleep is intentionally none: WAKE reboots the
// ESP32 and setup() runs fresh. NVS persistence is Phase 3B; RTC is
// Phase 3D; adaptive layouts are 3C.

#include <Inkplate.h>
#include <WiFi.h>
#include <driver/gpio.h>     // gpio_get_level for ISR-safe pin read
#include <esp_sleep.h>
#include <render.h>

#include "bulletin_envelope.h"
#include "bulletin_net.h"

// All three of these are injected via -D flags from firmware/Makefile.
// CIVICMESH_SSID is required (the Makefile errors out if unset);
// CIVICMESH_ENDPOINT defaults to http://10.0.0.1/api/external-display/state;
// CIVICMESH_FW_VERSION defaults to 0.1.0.
//
// /api/stats and /api/status URLs are DERIVED from CIVICMESH_ENDPOINT
// at setup (cached in stats_url / status_url). They're used to render
// UI_SPEC §5's bottom nerd strip — uptime, CPU, mem, sparkline, radio
// status. See inkplate/README.md "Server endpoints consumed".
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

static constexpr uint32_t POLL_CADENCE_INIT_MS    = 10000;
static constexpr uint32_t POLL_CADENCE_CAP_MS     = 300000;
static constexpr uint32_t ROTATION_INTERVAL_MS    = 300000;
static constexpr uint8_t  WIFI_CONNECT_TIMEOUT_S  = 20;
static constexpr uint16_t HTTP_CONNECT_TIMEOUT_MS = 5000;
static constexpr uint16_t HTTP_READ_TIMEOUT_MS    = 5000;
// Shorter timeouts for best-effort telemetry fetches so a stalled
// /api/stats doesn't add 10 s of WiFi-on time to every poll. The
// failure path here is just "log + keep last-known-good", so we'd
// rather give up early.
static constexpr uint16_t TELEMETRY_CONNECT_TIMEOUT_MS = 2000;
static constexpr uint16_t TELEMETRY_READ_TIMEOUT_MS    = 2000;
static constexpr int      EXPECTED_API_VERSION    = 2;

// Phase 3D will replace this with display.readBattery() on every poll.
// Until then, the bulletin success path passes this placeholder; only
// the failure / api_mismatch / setup-time paths sample for real.
static constexpr float    SUCCESS_PATH_BATTERY_PLACEHOLDER = 3.95f;

// PR 2 additions.
static constexpr uint8_t  WAKE_BUTTON_PIN     = 36;        // GPIO 36, active-LOW, RTC-capable
static constexpr uint32_t WAKE_DEBOUNCE_MS    = 250;
static constexpr uint32_t BATTERY_SETTLE_MS   = 100;       // after WiFi off, before readBattery
static constexpr uint8_t  FAILURE_STREAK_MAX  = 5;
static constexpr float    BATT_CRIT_V         = 3.6f;      // matches render/src/layout.h:22

// Fibonacci cadence in ms, terminated by the cap. Reset path returns to fib[0].
static const uint32_t kFib[] = {10000, 20000, 30000, 50000, 80000, 130000, 210000, 300000};
static constexpr size_t kFibLen = sizeof(kFib) / sizeof(kFib[0]);

static uint32_t fib_advance(uint32_t current) {
  for (size_t i = 0; i < kFibLen; ++i) {
    if (kFib[i] == current && i + 1 < kFibLen) return kFib[i + 1];
  }
  return POLL_CADENCE_CAP_MS;
}

enum class PollOutcome { kOk, kFail };

// --- Globals (RAM-only; no NVS, no RTC_DATA_ATTR) ---

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

// Telemetry endpoints derived from CIVICMESH_ENDPOINT at boot. Empty if
// CIVICMESH_ENDPOINT doesn't contain "/api/" in a recognizable form;
// the telemetry-fetch code skips the call when empty.
static String stats_url;
static String status_url;

// Last successful server response, retained so the rotation timer can
// re-render a different channel between polls without re-fetching, and
// so handle_wake_press can re-render the chosen next channel without
// burning a fetch on every press.
static JsonDocument g_last_server_doc;
static bool g_has_last_server_doc = false;

// Best-effort telemetry fetches. Cached so a fetch failure on poll N
// doesn't blank the nerd strip — the renderer keeps showing whatever
// the last successful fetch returned. Cleared only when never set.
static JsonDocument g_last_stats_doc;
static bool g_has_last_stats_doc = false;
static JsonDocument g_last_status_doc;
static bool g_has_last_status_doc = false;

// PR 2 globals.
volatile bool wake_flag = false;      // set by ISR; cleared in loop()
static uint32_t last_wake_handled_ms = 0;
static uint8_t  failure_streak = 0;

// --- ISR ---

// IRAM_ATTR keeps the handler in IRAM so it's reachable when flash is
// paged out. Body must be minimal: no Serial, no heap, no delay() —
// just gate on the pin's actual level and set the flag.
//
// The ISR-side gpio_get_level filters out EMI transients from the
// e-paper's TPS65186 PMIC switching (±15V rails). Transients are
// µs-scale; by the time the ISR dispatches (~1-3µs after the edge),
// the pin is already back HIGH and we don't latch the flag. Real
// button presses are 10ms+, so they pass the check easily. Loop
// adds a second digitalRead confirm after the debounce window as
// belt-and-suspenders against anything slower that slips through.
void IRAM_ATTR wake_isr() {
  if (gpio_get_level((gpio_num_t)WAKE_BUTTON_PIN) == 0) {
    wake_flag = true;
  }
}

// --- Helpers ---

// Wrap-safe millis() comparison: returns true iff `now` has reached or
// passed `target`. Naive `now >= target` is wrong across millis wraparound.
static inline bool reached(uint32_t now, uint32_t target) {
  return (int32_t)(now - target) >= 0;
}

// Quiesce the RF rail before readBattery(). Per inkplate-research.md:144,
// active WiFi sags the rail and biases readings low. The library's own
// internal 5ms is for MOSFET divider settle (Inkplate6Driver.cpp:987) —
// the 100ms here is the post-RF-off rail recovery, a separate concern.
// Safe to call before WiFi has ever been started: WiFi.mode(WIFI_OFF) is
// a no-op in that case.
static float sample_battery_with_wifi_off() {
  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  delay(BATTERY_SETTLE_MS);
  return (float)display.readBattery();
  // WiFi.setAutoReconnect(true) from setup() remains in effect, so the
  // next poll's connect_wifi_if_needed() will re-associate.
}

// Annotated noreturn so the compiler flags any post-call code; also
// helps callers (on_failure, etc.) reason about control flow.
[[noreturn]] static void enter_deep_sleep_until_wake() {
  Serial.printf("[bulletin] entering deep sleep, WAKE GPIO%u to resume\n",
                (unsigned)WAKE_BUTTON_PIN);
  Serial.flush();
  // Detach before arming ext0 on the same pin (undefined behavior otherwise).
  detachInterrupt(digitalPinToInterrupt(WAKE_BUTTON_PIN));
  // 0 = LOW level wakes (button is active-LOW).
  esp_sleep_enable_ext0_wakeup((gpio_num_t)WAKE_BUTTON_PIN, 0);
  esp_deep_sleep_start();
  // Unreachable.
  while (true) {}
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
  // Pass cached telemetry docs through so the footer's nerd strip
  // renders. Either may be null on the very first poll if its fetch
  // failed; the renderer falls back to the legacy banner in that
  // case (parse_stats / parse_status set has_stats=false).
  const JsonDocument* stats_p =
      g_has_last_stats_doc ? &g_last_stats_doc : nullptr;
  const JsonDocument* status_p =
      g_has_last_status_doc ? &g_last_status_doc : nullptr;
  String s = civicmesh::bulletin::build_combined_ok_json(
      server_doc, stats_p, status_p,
      active_channel_index, SUCCESS_PATH_BATTERY_PLACEHOLDER,
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

static void render_failure(const char* reason, float battery_volts) {
  String s = civicmesh::bulletin::build_combined_failure_json(
      reason, active_channel_index, battery_volts,
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

static void render_critical_battery(float battery_volts) {
  String s = civicmesh::bulletin::build_combined_critical_battery_json(
      battery_volts, EXPECTED_API_VERSION, CIVICMESH_FW_VERSION);
  push_to_panel(s, "critical_battery");
  // Sentinel flavor so any subsequent ok-poll sig will differ and
  // re-render. (After critical_battery we deep-sleep, so this matters
  // only via the post-wake setup path, which re-zeros last_render_sig
  // on reboot anyway. Kept for defense-in-depth.)
  RenderSig sig;
  sig.failure_kind = 0xFF;
  sig.active_channel = active_channel_index;
  sig.max_msg_id = 0;
  last_render_sig = sig;
}

static void render_api_mismatch_screen(const JsonDocument& server_doc,
                                       float battery_volts) {
  String s = civicmesh::bulletin::build_combined_api_mismatch_json(
      server_doc, battery_volts, EXPECTED_API_VERSION, CIVICMESH_FW_VERSION);
  push_to_panel(s, "api_mismatch");
}

// Best-effort telemetry fetch. Logs verbosely so the serial monitor
// shows each attempt and any error. On success, populates `out_doc` and
// returns true; on failure, logs the reason and returns false WITHOUT
// touching failure_streak — telemetry failures must not cascade into
// the chat-fetch failure_shell path. Caller decides whether to swap
// the result into the cached global.
static bool fetch_telemetry(const char* label,
                            const String& url,
                            JsonDocument& out_doc) {
  if (url.length() == 0) {
    Serial.printf("[bulletin] %s skip: url not configured\n", label);
    return false;
  }
  Serial.printf("[bulletin] %s polling url=%s\n", label, url.c_str());
  int code = 0;
  IPAddress resolved;
  civicmesh::bulletin::FetchError err =
      civicmesh::bulletin::fetch_payload(
          url.c_str(), cached_host.c_str(),
          TELEMETRY_CONNECT_TIMEOUT_MS, TELEMETRY_READ_TIMEOUT_MS,
          out_doc, &code, &resolved);
  if (err == civicmesh::bulletin::FetchError::kOk) {
    Serial.printf("[bulletin] %s fetch ok ip=%s http=%d\n",
                  label, resolved.toString().c_str(), code);
    return true;
  }
  const char* err_str = "?";
  switch (err) {
    case civicmesh::bulletin::FetchError::kDnsFailure:
      err_str = "dns_failure"; break;
    case civicmesh::bulletin::FetchError::kPiUnreachable:
      err_str = "pi_unreachable"; break;
    case civicmesh::bulletin::FetchError::kHttpError:
      err_str = "http_error"; break;
    case civicmesh::bulletin::FetchError::kBadJson:
      err_str = "bad_json"; break;
    case civicmesh::bulletin::FetchError::kOk:
      err_str = "ok"; break;  // unreachable
  }
  Serial.printf("[bulletin] %s fetch FAILED err=%s code=%d "
                "(footer will show last-known-good or fall back to "
                "legacy banner)\n",
                label, err_str, code);
  return false;
}

// Centralized failure handler used by every transient-failure case in
// poll(). May not return (battery-critical or streak-max paths sleep).
static void on_failure(const char* reason) {
  next_poll_delay_ms = POLL_CADENCE_INIT_MS;   // fast retry
  float v = sample_battery_with_wifi_off();
  Serial.printf("[bulletin] failure battery_v=%.2f reason=%s\n", v, reason);
  if (v > 0.0f && v < BATT_CRIT_V) {
    render_critical_battery(v);
    enter_deep_sleep_until_wake();             // no return
  }
  failure_streak++;
  render_failure(reason, v);
  Serial.printf("[bulletin] streak=%u/%u\n",
                (unsigned)failure_streak, (unsigned)FAILURE_STREAK_MAX);
  if (failure_streak >= FAILURE_STREAK_MAX) {
    enter_deep_sleep_until_wake();             // no return
  }
}

static PollOutcome poll(bool is_wake_driven = false) {
  last_poll_millis = millis();
  Serial.printf("[bulletin] poll start (next in %us)%s\n",
                (unsigned)(next_poll_delay_ms / 1000),
                is_wake_driven ? " [wake]" : "");

  if (!civicmesh::bulletin::connect_wifi_if_needed(
          display, CIVICMESH_SSID, WIFI_CONNECT_TIMEOUT_S)) {
    Serial.println("[bulletin] wifi associate failed");
    on_failure("ap_unreachable");
    return PollOutcome::kFail;
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
      on_failure("dns_failure"); return PollOutcome::kFail;
    case FetchError::kPiUnreachable:
      Serial.printf("[bulletin] fetch pi_unreachable code=%d\n", http_code);
      on_failure("pi_unreachable"); return PollOutcome::kFail;
    case FetchError::kHttpError:
      Serial.printf("[bulletin] fetch http_error code=%d\n", http_code);
      on_failure("http_error"); return PollOutcome::kFail;
    case FetchError::kBadJson:
      Serial.println("[bulletin] fetch bad_json");
      on_failure("bad_json"); return PollOutcome::kFail;
    case FetchError::kOk:
      Serial.printf("[bulletin] fetch dns_ok ip=%s http_code=%d\n",
                    resolved.toString().c_str(), http_code);
      break;
  }

  int api_v = server_doc["api_version"].as<int>();
  if (api_v != EXPECTED_API_VERSION) {
    Serial.printf("[bulletin] api_mismatch got=%d want=%d\n",
                  api_v, EXPECTED_API_VERSION);
    // Permanent state per spec: dedicated screen + immediate deep sleep.
    // Streak counter NOT touched (api_mismatch is not a transient fault).
    // If battery is also critical, critical_battery wins (dispatch order).
    float v = sample_battery_with_wifi_off();
    if (v > 0.0f && v < BATT_CRIT_V) {
      render_critical_battery(v);
    } else {
      render_api_mismatch_screen(server_doc, v);
    }
    enter_deep_sleep_until_wake();  // no return
  }

  // Success.
  failure_streak = 0;

  // Best-effort telemetry fetches for the §5 nerd strip. Both are
  // independent of the chat path: a failed stats or status fetch must
  // NOT trip failure_streak or render failure_shell. The cached docs
  // hold last-known-good values; if both this fetch fails AND no
  // previous successful fetch, the renderer falls back to the legacy
  // banner. Order: stats first (bigger payload, more likely to fail
  // on a slow link) so its failure log lands before status.
  {
    JsonDocument tmp_stats;
    if (fetch_telemetry("stats", stats_url, tmp_stats)) {
      g_last_stats_doc = tmp_stats;
      g_has_last_stats_doc = true;
    }
    JsonDocument tmp_status;
    if (fetch_telemetry("status", status_url, tmp_status)) {
      g_last_status_doc = tmp_status;
      g_has_last_status_doc = true;
    }
  }

  JsonArrayConst channels = server_doc["channels"].as<JsonArrayConst>();
  channel_count = channels.isNull() ? 0 : channels.size();
  Serial.printf("[bulletin] parse ok channels=%u server_time=%u\n",
                (unsigned)channel_count,
                (unsigned)server_doc["server_time"].as<uint32_t>());

  if (channel_count == 0) {
    active_channel_index = 0;
    if (!is_wake_driven) {
      next_poll_delay_ms = fib_advance(next_poll_delay_ms);
    }
    g_last_server_doc = server_doc;
    g_has_last_server_doc = true;
    Serial.println("[bulletin] zero channels; rendering empty bulletin");
    if (!is_wake_driven) {
      render_if_sig_changed(g_last_server_doc, "quiet_poll_empty");
    }
    return PollOutcome::kOk;
  }
  if (active_channel_index >= channel_count) {
    active_channel_index = channel_count - 1;
  }

  int16_t jump_to = civicmesh::bulletin::select_activity_channel(
      server_doc, high_water_mark_ts);
  uint32_t new_hwm = civicmesh::bulletin::max_ts_across_all_channels(server_doc);
  if (new_hwm > high_water_mark_ts) high_water_mark_ts = new_hwm;

  // Cache before render so rotation between polls and wake-press
  // re-renders have fresh data.
  g_last_server_doc = server_doc;
  g_has_last_server_doc = true;

  if (jump_to >= 0 && !is_wake_driven) {
    active_channel_index = (uint16_t)jump_to;
    uint32_t prev = next_poll_delay_ms;
    next_poll_delay_ms = POLL_CADENCE_INIT_MS;
    next_rotation_at_ms = millis() + ROTATION_INTERVAL_MS;
    Serial.printf("[bulletin] activity jump to channel=%u hwm=%u (cadence reset %u->%u)\n",
                  (unsigned)active_channel_index,
                  (unsigned)high_water_mark_ts,
                  (unsigned)prev, (unsigned)next_poll_delay_ms);
    render_combined_ok(g_last_server_doc, "activity");
  } else if (jump_to >= 0 && is_wake_driven) {
    // WAKE-press is a manual channel selector — handle_wake_press will
    // advance the channel itself. Don't let the passive activity-jump
    // mutate active_channel_index out from under the user.
    Serial.printf("[bulletin] wake-driven: activity in channel=%u detected, deferring to wake handler\n",
                  (unsigned)jump_to);
  } else {
    // Quiet poll: gate fib_advance on !is_wake_driven so handle_wake_press's
    // own reset to POLL_CADENCE_INIT_MS isn't overwritten by fib_advance(10s)=20s.
    if (!is_wake_driven) {
      uint32_t prev = next_poll_delay_ms;
      next_poll_delay_ms = fib_advance(next_poll_delay_ms);
      Serial.printf("[bulletin] cadence advance %ums -> %ums\n",
                    (unsigned)prev, (unsigned)next_poll_delay_ms);
    }
    if (next_rotation_at_ms == 0) {
      next_rotation_at_ms = millis() + ROTATION_INTERVAL_MS;
    }
    if (!is_wake_driven) {
      render_if_sig_changed(g_last_server_doc, "quiet_poll");
    }
  }
  return PollOutcome::kOk;
}

static void handle_wake_press() {
  Serial.println("[bulletin] wake_pressed");
  next_poll_delay_ms = POLL_CADENCE_INIT_MS;   // reset Fibonacci to 10s
  PollOutcome outcome = poll(/*is_wake_driven=*/true);
  if (outcome != PollOutcome::kOk) {
    return;  // poll already rendered failure_shell or slept; nothing more here.
  }
  // poll succeeded; g_last_server_doc is populated but unrendered.
  // WAKE is a manual channel selector — each press advances to the
  // next channel with wrap-around. poll(is_wake_driven=true) deliberately
  // does not touch active_channel_index, so the increment below is the
  // sole source of channel change on a press.
  if (g_has_last_server_doc && channel_count > 0) {
    uint16_t prev = active_channel_index;
    active_channel_index = (uint16_t)((prev + 1) % channel_count);
    Serial.printf("[bulletin] wake -> next channel %u -> %u (of %u)\n",
                  (unsigned)prev, (unsigned)active_channel_index,
                  (unsigned)channel_count);
  }
  next_rotation_at_ms = millis() + ROTATION_INTERVAL_MS;
  // Unconditional: bypass sig check. The press is a UX signal; user
  // expects a visible refresh even if content hasn't changed.
  render_combined_ok(g_last_server_doc, "wake");
}

// --- Arduino entry ---

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.printf("[bulletin] boot fw=%s ssid=%s endpoint=%s\n",
                CIVICMESH_FW_VERSION, CIVICMESH_SSID, CIVICMESH_ENDPOINT);

  // display.begin() MUST run before sample_battery_with_wifi_off(): the
  // battery divider MOSFET is on GPIO 9 of the MCP23017 I/O expander,
  // and the expander is only initialized inside display.begin()
  // (InkplateLibrary/.../Inkplate6Driver.cpp:971-987). Without it,
  // readBattery() silently fails the MOSFET enable and ADC reads ~0V.
  display.begin();
  display.clearDisplay();

  // PSRAM sanity check: the 1-bit framebuffer (~60KB) and the Inkplate
  // library's working buffers live in external PSRAM. If PSRAM init
  // failed (loose solder joint, dead chip), psramFound() returns false
  // and any subsequent display.display() / render_frame call touches
  // unallocated memory — crash or garbage output. Best we can do is
  // shout to serial and sleep. Operator wakes us with WAKE to reboot;
  // if the hardware is genuinely broken we'll sleep again on the next
  // boot, which is the least-bad outcome.
  if (!psramFound()) {
    Serial.println("[bulletin] PSRAM not initialized — display "
                   "framebuffer unallocated. Cannot render. Sleeping.");
    enter_deep_sleep_until_wake();  // no return
  }

  // Setup-time dead-battery check: catches wake-from-deep-sleep with a
  // dead battery before we burn RF cycles trying to associate.
  float v = sample_battery_with_wifi_off();
  Serial.printf("[bulletin] setup battery_v=%.2f\n", v);
  if (v > 0.0f && v < BATT_CRIT_V) {
    render_critical_battery(v);
    enter_deep_sleep_until_wake();  // no return
  }

  // WAKE button on GPIO 36, active-LOW. Attached AFTER the setup-time
  // battery check so a press during the check itself doesn't race the
  // wake_flag handling in loop().
  pinMode(WAKE_BUTTON_PIN, INPUT);
  attachInterrupt(digitalPinToInterrupt(WAKE_BUTTON_PIN), wake_isr, FALLING);

  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);

  cached_host = civicmesh::bulletin::parse_host_from_url(CIVICMESH_ENDPOINT);
  Serial.printf("[bulletin] cached host=%s\n", cached_host.c_str());

  // Derive /api/stats and /api/status URLs from CIVICMESH_ENDPOINT by
  // chopping at "/api/" and pasting the new paths on. If the endpoint
  // doesn't contain "/api/" (operator typoed it, or it's some custom
  // shape), we log loudly and skip telemetry — the renderer still
  // renders the bulletin, just with the legacy footer banner.
  {
    String ep(CIVICMESH_ENDPOINT);
    int api_idx = ep.indexOf("/api/");
    if (api_idx >= 0) {
      String base = ep.substring(0, api_idx);
      stats_url = base + "/api/stats";
      status_url = base + "/api/status";
      Serial.printf("[bulletin] telemetry urls stats=%s status=%s\n",
                    stats_url.c_str(), status_url.c_str());
    } else {
      Serial.printf("[bulletin] WARN: CIVICMESH_ENDPOINT %s does not "
                    "contain '/api/' — telemetry fetches disabled, "
                    "nerd strip will fall back to legacy banner\n",
                    CIVICMESH_ENDPOINT);
    }
  }

  // Schedule first poll immediately so we don't wait an initial 10s.
  last_poll_millis = millis() - next_poll_delay_ms;
}

void loop() {
  uint32_t now = millis();

  // WAKE press handling at the top so a press never gets stuck behind
  // a long poll/rotation cycle. Loop-side confirm with digitalRead
  // kills anything that snuck past the ISR-side filter (mechanical
  // bounce, slower EMI). Don't bump last_wake_handled_ms on spurious
  // — a real press immediately after a spurious shouldn't have its
  // debounce window restarted.
  if (wake_flag &&
      reached(now, last_wake_handled_ms + WAKE_DEBOUNCE_MS)) {
    wake_flag = false;
    if (digitalRead(WAKE_BUTTON_PIN) != LOW) {
      Serial.println("[bulletin] wake spurious (pin high at confirm), ignored");
      return;
    }
    last_wake_handled_ms = millis();
    handle_wake_press();
    return;
  }

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
