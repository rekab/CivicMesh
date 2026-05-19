#include "bulletin_net.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClient.h>

namespace civicmesh {
namespace bulletin {

String parse_host_from_url(const char* url) {
  if (!url) return String();
  String s(url);
  int p = s.indexOf("://");
  int start = (p >= 0) ? p + 3 : 0;
  int end = s.length();
  for (int i = start; i < end; ++i) {
    char c = s[i];
    if (c == '/' || c == '?' || c == '#') { end = i; break; }
  }
  String host = s.substring(start, end);
  int colon = host.indexOf(':');
  if (colon >= 0) host = host.substring(0, colon);
  return host;
}

bool connect_wifi_if_needed(Inkplate& display,
                            const char* ssid,
                            uint8_t timeout_seconds) {
  if (WiFi.status() == WL_CONNECTED) return true;
  // Inkplate's blocking helper: (ssid, pass, timeout_s, isHidden).
  // Empty pass = open network, per Phase 3A PR 1 spec.
  display.connectWiFi(ssid, "", timeout_seconds, false);
  return WiFi.status() == WL_CONNECTED;
}

FetchError fetch_payload(const char* endpoint_url,
                         const char* host_for_dns,
                         uint16_t connect_timeout_ms,
                         uint16_t read_timeout_ms,
                         JsonDocument& out_doc,
                         int* out_http_code,
                         IPAddress* out_resolved) {
  if (out_http_code) *out_http_code = 0;

  // Pre-flight DNS so we can return kDnsFailure distinctly from
  // kPiUnreachable. WiFi.hostByName returns 1 on success.
  IPAddress ip;
  if (WiFi.hostByName(host_for_dns, ip) != 1) {
    return FetchError::kDnsFailure;
  }
  if (out_resolved) *out_resolved = ip;

  HTTPClient http;
  http.setConnectTimeout(connect_timeout_ms);
  http.setTimeout(read_timeout_ms);
  if (!http.begin(endpoint_url)) {
    return FetchError::kPiUnreachable;
  }

  int code = http.GET();
  if (out_http_code) *out_http_code = code;
  if (code <= 0) {
    http.end();
    return FetchError::kPiUnreachable;
  }
  if (code < 200 || code >= 300) {
    http.end();
    return FetchError::kHttpError;
  }

  WiFiClient& stream = http.getStream();
  DeserializationError de = deserializeJson(out_doc, stream);
  http.end();
  if (de) return FetchError::kBadJson;
  return FetchError::kOk;
}

}  // namespace bulletin
}  // namespace civicmesh
