#include "text_wrap.h"

#include <sstream>

namespace civicmesh {
namespace render {

static int16_t measure_width(Adafruit_GFX& gfx, const std::string& s) {
  int16_t x1, y1;
  uint16_t w, h;
  gfx.getTextBounds(s.c_str(), 0, 0, &x1, &y1, &w, &h);
  return static_cast<int16_t>(w);
}

std::vector<std::string> wrap_text(Adafruit_GFX& gfx,
                                   const std::string& text,
                                   int16_t max_width,
                                   int max_lines) {
  std::vector<std::string> lines;
  if (text.empty() || max_lines <= 0 || max_width <= 0) return lines;

  // Tokenize by whitespace.
  std::vector<std::string> words;
  {
    std::istringstream iss(text);
    std::string w;
    while (iss >> w) words.push_back(std::move(w));
  }
  if (words.empty()) return lines;

  std::string cur;
  for (size_t i = 0; i < words.size(); ++i) {
    std::string candidate = cur.empty() ? words[i] : cur + " " + words[i];
    if (measure_width(gfx, candidate) <= max_width) {
      cur = std::move(candidate);
      continue;
    }
    // Word doesn't fit on current line. Close current and start new.
    if (!cur.empty()) {
      if (static_cast<int>(lines.size()) + 1 == max_lines) {
        // This would be the last allowed line, but we still have words
        // left. Force truncation with "..." appended.
        std::string truncated = cur;
        while (!truncated.empty() &&
               measure_width(gfx, truncated + "...") > max_width) {
          truncated.pop_back();
        }
        lines.push_back(truncated + "...");
        return lines;
      }
      lines.push_back(std::move(cur));
      cur.clear();
    }
    // Try the word alone on a fresh line; if even alone it overflows,
    // hard-break at character boundary.
    if (measure_width(gfx, words[i]) <= max_width) {
      cur = words[i];
    } else {
      std::string piece;
      for (char c : words[i]) {
        std::string next = piece + c;
        if (measure_width(gfx, next) > max_width) {
          if (static_cast<int>(lines.size()) + 1 == max_lines) {
            lines.push_back(piece + "...");
            return lines;
          }
          lines.push_back(std::move(piece));
          piece = std::string(1, c);
        } else {
          piece = std::move(next);
        }
      }
      cur = piece;
    }
  }
  if (!cur.empty()) {
    if (static_cast<int>(lines.size()) >= max_lines) return lines;
    lines.push_back(std::move(cur));
  }
  return lines;
}

}  // namespace render
}  // namespace civicmesh
