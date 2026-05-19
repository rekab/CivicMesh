#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Inkplate.h>
#include <IPAddress.h>

// Network seam for the bulletin sketch: WiFi association (with reuse),
// pre-flight DNS so failure_shell can distinguish dns_failure from
// pi_unreachable, and an HTTPClient GET that streams the body straight
// into the caller's JsonDocument. http.getString() is avoided per
// inkplate/render/NOTES.md guidance on ESP32 heap fragmentation.

namespace civicmesh {
namespace bulletin {

enum class FetchError {
  kOk,
  kDnsFailure,
  kPiUnreachable,   // DNS resolved, TCP/HTTP layer failed before any 2xx
  kHttpError,       // got an HTTP response, but not 2xx
  kBadJson,         // body did not parse as JSON
};

// Returns true iff WiFi.status() == WL_CONNECTED on exit. Reuses an
// existing connection; only re-runs display.connectWiFi() when down.
bool connect_wifi_if_needed(Inkplate& display,
                            const char* ssid,
                            uint8_t timeout_seconds);

// Fetch GET endpoint_url into out_doc. Performs a pre-flight DNS
// resolution against host_for_dns (a bare hostname or IP). On
// kPiUnreachable / kHttpError / kBadJson the out_doc is left in an
// indeterminate state (caller should not read it).
FetchError fetch_payload(const char* endpoint_url,
                         const char* host_for_dns,
                         uint16_t connect_timeout_ms,
                         uint16_t read_timeout_ms,
                         JsonDocument& out_doc,
                         int* out_http_code,        // optional, may be null
                         IPAddress* out_resolved);  // optional, may be null

// Pull the host portion out of an http:// or https:// URL. Returns
// "" on parse error. ":port" suffixes are stripped (DNS resolution
// is hostname-only).
String parse_host_from_url(const char* url);

}  // namespace bulletin
}  // namespace civicmesh
