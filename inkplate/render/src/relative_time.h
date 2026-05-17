#pragma once

#include <cstdint>
#include <string>

namespace civicmesh {
namespace render {

// Format an elapsed-time count for footer display.
//   < 60 s        → "just now"
//   1..59 min     → "N min ago"
//   1..23 hr      → "N hr ago"
//   24+ hr        → "N days ago"
std::string format_relative_time(uint32_t seconds);

}  // namespace render
}  // namespace civicmesh
