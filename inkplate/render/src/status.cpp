#include "status.h"

namespace civicmesh {
namespace render {

bool parse_status(JsonVariantConst v, Status& out) {
  if (v.isNull() || !v.is<JsonObjectConst>()) return false;
  JsonObjectConst obj = v.as<JsonObjectConst>();

  const char* rs = obj["radio_status"].as<const char*>();
  if (rs) out.radio_status = rs;

  // age_sec absent in /api/status's empty branch (mesh_bot row missing);
  // keep the -1 sentinel set in the struct's default so the footer can
  // tell "no row" apart from "row says 0".
  JsonVariantConst age = obj["age_sec"];
  if (!age.isNull() && age.is<int>()) {
    out.age_sec = age.as<int>();
  }

  out.has_status = true;
  return true;
}

}  // namespace render
}  // namespace civicmesh
