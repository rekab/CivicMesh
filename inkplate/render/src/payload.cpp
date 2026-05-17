#include "payload.h"

namespace civicmesh {
namespace render {

static std::string str_or_empty(JsonVariantConst v) {
  const char* p = v.as<const char*>();
  return p ? std::string(p) : std::string();
}

bool parse_payload(JsonObjectConst obj, Payload& out, std::string& err) {
  if (obj.isNull()) {
    err = "payload: missing object";
    return false;
  }
  out.api_version = obj["api_version"].as<int>();
  out.server_time = obj["server_time"].as<long>();
  JsonObjectConst hub = obj["hub"].as<JsonObjectConst>();
  out.site_name = str_or_empty(hub["site_name"]);
  out.callsign = str_or_empty(hub["callsign"]);

  JsonArrayConst chans = obj["channels"].as<JsonArrayConst>();
  for (JsonObjectConst c : chans) {
    PayloadChannel pc;
    pc.name = str_or_empty(c["name"]);
    pc.scope = str_or_empty(c["scope"]);
    JsonArrayConst msgs = c["messages"].as<JsonArrayConst>();
    for (JsonObjectConst m : msgs) {
      PayloadMessage pm;
      pm.id = m["id"].as<long>();
      pm.ts = m["ts"].as<long>();
      pm.sender = str_or_empty(m["sender"]);
      pm.body = str_or_empty(m["body"]);
      pc.messages.push_back(std::move(pm));
    }
    out.channels.push_back(std::move(pc));
  }
  return true;
}

}  // namespace render
}  // namespace civicmesh
