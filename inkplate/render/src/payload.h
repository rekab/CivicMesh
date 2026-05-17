#pragma once

#include <ArduinoJson.h>
#include <cstdint>
#include <string>
#include <vector>

namespace civicmesh {
namespace render {

struct PayloadMessage {
  long id = 0;
  long ts = 0;
  std::string sender;
  std::string body;
};

struct PayloadChannel {
  std::string name;
  std::string scope;  // "local" | "mesh"
  std::vector<PayloadMessage> messages;
};

// Mirrors external_display.build_state() at external_display.py:72-85.
struct Payload {
  int api_version = 0;
  long server_time = 0;
  std::string site_name;
  std::string callsign;
  std::vector<PayloadChannel> channels;
};

// Returns true on success. On failure (missing keys, wrong types) returns
// false and leaves out partially-filled. err is set to a short reason.
bool parse_payload(JsonObjectConst obj, Payload& out, std::string& err);

}  // namespace render
}  // namespace civicmesh
