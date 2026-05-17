#include "relative_time.h"

#include <cstdio>

namespace civicmesh {
namespace render {

std::string format_relative_time(uint32_t seconds) {
  char buf[32];
  if (seconds < 60) {
    return "just now";
  }
  if (seconds < 3600) {
    std::snprintf(buf, sizeof(buf), "%u min ago", seconds / 60);
    return buf;
  }
  if (seconds < 86400) {
    std::snprintf(buf, sizeof(buf), "%u hr ago", seconds / 3600);
    return buf;
  }
  std::snprintf(buf, sizeof(buf), "%u days ago", seconds / 86400);
  return buf;
}

}  // namespace render
}  // namespace civicmesh
