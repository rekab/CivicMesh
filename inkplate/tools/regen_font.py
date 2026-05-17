#!/usr/bin/env python3
# Regenerate inkplate/render/src/fonts/LessPerfectDOSVGA.h from the TTF.
#
# Why Python instead of Adafruit's C fontconvert: the C tool needs
# libfreetype-dev (header package) which isn't on the dev Pi by
# default. Debian's python3-freetype binding ships its own headers
# under the runtime libfreetype6, so we use it directly. Output
# format is byte-identical to fontconvert's: same GFXglyph table,
# same bit-packed-MSB-first bitmap concatenation, same GFXfont struct.
#
# Usage:
#   python3 inkplate/tools/regen_font.py \
#       --ttf /path/to/LessPerfectDOSVGA.ttf \
#       --px 16 \
#       --out inkplate/render/src/fonts/LessPerfectDOSVGA.h
#
# Iterate `--px` to hit the target native glyph height. Doubled to
# ~32px by setTextSize(2) at render time.
#
# Source TTF: http://laemeur.sdf.org/fonts/LessPerfectDOSVGA.ttf
# License: free for all use, commercial and non-commercial.

import argparse
import sys
from pathlib import Path

try:
    import freetype
except ImportError:
    sys.exit("ERROR: python3-freetype not installed. apt install python3-freetype")


FIRST = 0x20  # space
LAST = 0x7E   # tilde
SYMBOL_NAME = "LessPerfectDOSVGA"


def render_glyph(face, codepoint):
    face.load_char(chr(codepoint),
                   freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_MONO)
    g = face.glyph
    bm = g.bitmap
    return {
        "width": bm.width,
        "height": bm.rows,
        "pitch": bm.pitch,
        "buffer": bytes(bm.buffer),
        "x_advance": g.advance.x >> 6,
        "bearing_x": g.bitmap_left,
        "bearing_y": g.bitmap_top,
    }


def pack_glyph(glyph):
    """Pack a glyph's pixels into a tight MSB-first bitstream, padded
    to the next byte boundary at the end of the glyph (not per row)."""
    w, h, pitch, buf = glyph["width"], glyph["height"], glyph["pitch"], glyph["buffer"]
    out_bits = []
    for y in range(h):
        row_start = y * pitch
        for x in range(w):
            byte = buf[row_start + (x // 8)]
            out_bits.append((byte >> (7 - (x % 8))) & 1)
    # Pad to next byte boundary.
    while len(out_bits) % 8 != 0:
        out_bits.append(0)
    packed = bytearray()
    for i in range(0, len(out_bits), 8):
        b = 0
        for j in range(8):
            b = (b << 1) | out_bits[i + j]
        packed.append(b)
    return bytes(packed)


def emit_header(ttf_path, px, out_path):
    face = freetype.Face(str(ttf_path))
    face.set_pixel_sizes(0, px)

    glyphs = []
    bitstream = bytearray()
    bitmap_offset = 0

    for cp in range(FIRST, LAST + 1):
        g = render_glyph(face, cp)
        packed = pack_glyph(g)
        glyphs.append({
            "bitmap_offset": bitmap_offset,
            "width": g["width"],
            "height": g["height"],
            "x_advance": g["x_advance"],
            "x_offset": g["bearing_x"],
            "y_offset": 1 - g["bearing_y"],
            "codepoint": cp,
        })
        bitstream.extend(packed)
        # bitmapOffset tracks ceil(width*height/8) bits.
        bitmap_offset += (g["width"] * g["height"] + 7) // 8

    y_advance = face.size.height >> 6
    if y_advance == 0:
        y_advance = max(g["height"] for g in glyphs)

    lines = []
    lines.append("// Generated from LessPerfectDOSVGA.ttf by"
                 " inkplate/tools/regen_font.py.")
    lines.append("// Source: http://laemeur.sdf.org/fonts/")
    lines.append("// License: free for all use, commercial and"
                 " non-commercial.")
    lines.append("// IBM designed the glyphs; Zeh Fernando did the"
                 " TrueType conversion;")
    lines.append("// Laemeur narrowed the kerning. Author requests no"
                 " specific attribution.")
    lines.append(f"// Pixel size: {px}. Glyph range: 0x{FIRST:02X}-0x{LAST:02X}."
                 f" yAdvance: {y_advance}.")
    lines.append("")
    lines.append("#pragma once")
    lines.append("")
    lines.append("#include \"gfxfont.h\"")
    lines.append("")
    lines.append(f"const uint8_t {SYMBOL_NAME}Bitmaps[] PROGMEM = {{")
    # 12 bytes per line, hex.
    bs = list(bitstream)
    for i in range(0, len(bs), 12):
        chunk = bs[i:i + 12]
        lines.append("  " + ", ".join(f"0x{b:02X}" for b in chunk) +
                     ("," if i + 12 < len(bs) else ""))
    lines.append("};")
    lines.append("")
    lines.append(f"const GFXglyph {SYMBOL_NAME}Glyphs[] PROGMEM = {{")
    for i, g in enumerate(glyphs):
        comma = "," if i < len(glyphs) - 1 else ""
        cp = g["codepoint"]
        c = chr(cp) if 0x21 <= cp <= 0x7E else " "
        lines.append(
            f"  {{ {g['bitmap_offset']:5d}, {g['width']:3d}, {g['height']:3d}, "
            f"{g['x_advance']:3d}, {g['x_offset']:4d}, {g['y_offset']:4d} }}"
            f"{comma}  // 0x{cp:02X} '{c}'"
        )
    lines.append("};")
    lines.append("")
    lines.append(f"const GFXfont {SYMBOL_NAME} PROGMEM = {{")
    lines.append(f"  (uint8_t *){SYMBOL_NAME}Bitmaps,")
    lines.append(f"  (GFXglyph *){SYMBOL_NAME}Glyphs,")
    lines.append(f"  0x{FIRST:02X}, 0x{LAST:02X}, {y_advance}")
    lines.append("};")
    lines.append("")

    out_path.write_text("\n".join(lines))
    print(f"wrote {out_path} ({len(bs)} bytes of bitmap data,"
          f" {len(glyphs)} glyphs, yAdvance={y_advance})", file=sys.stderr)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--ttf", required=True, type=Path)
    p.add_argument("--px", type=int, default=16,
                   help="Target native glyph height in pixels (default 16)")
    p.add_argument("--out", required=True, type=Path)
    args = p.parse_args()

    if not args.ttf.exists():
        sys.exit(f"ERROR: TTF not found: {args.ttf}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    emit_header(args.ttf, args.px, args.out)


if __name__ == "__main__":
    main()
