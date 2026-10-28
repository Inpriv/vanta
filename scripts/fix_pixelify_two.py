#!/usr/bin/env python3
"""Redraw the digit 2 in the website's Pixelify Sans files.

Pixelify Sans draws "2" with a closed lower bowl, so at stat sizes "28" reads
as "88". This replaces just that glyph with an open, classic pixel 2 built on
the font's own grid and stroke widths; every other glyph is untouched. The
fonts are OFL-1.1 with no Reserved Font Name, so the family name stays.

Usage: scripts/fix_pixelify_two.py web/assets/fonts/PixelifySans-*.woff2
Needs: fonttools, brotli, skia-pathops.
"""

import sys

import pathops
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

# Grid per weight, read from the original "2": outer/inner x edges, the
# vertical stroke and the y lines of the top bar, shoulder and bottom bar.
GRIDS = {
    "Regular": dict(L=60, l=151, li=161, ri=423, r=433, R=525,
                    bot=-12, bar=88, mid_lo=260, mid_hi=359, step=270,
                    sh_lo=350, tip_lo=440, top_lo=540, top=631),
    "SemiBold": dict(L=61, l=150, li=179, ri=417, r=446, R=536,
                     bot=-11, bar=113, mid_lo=251, mid_hi=374, step=262,
                     sh_lo=337, tip_lo=424, top_lo=548, top=636),
}


def rects(g, steps=3):
    stroke = g["li"] - g["L"]
    out = [
        (g["l"], g["top_lo"], g["r"], g["top"]),            # top bar
        (g["L"], g["tip_lo"], g["li"], g["top_lo"] + 1),    # left tip
        (g["ri"], g["mid_hi"], g["R"], g["top_lo"] + 1),    # right shoulder
        (g["L"], g["bot"], g["R"], g["bar"]),               # bottom bar
    ]
    # Diagonal from under the shoulder down to the bottom-left, one stroke
    # wide, as a staircase of equal steps (each overlaps the next by 1 unit).
    h = (g["mid_hi"] - g["bar"]) / steps
    x_top, x_bot = g["r"] - stroke, g["L"]
    for i in range(steps):
        x = round(x_top + (x_bot - x_top) * i / (steps - 1))
        y1 = round(g["mid_hi"] - h * i) + 1
        y0 = round(g["mid_hi"] - h * (i + 1)) - 1
        out.append((x, y0, x + stroke, y1))
    return out


def build(font, g):
    path = pathops.Path()
    for x0, y0, x1, y1 in rects(g):
        r = pathops.Path()
        r.moveTo(x0, y0)
        r.lineTo(x0, y1)
        r.lineTo(x1, y1)
        r.lineTo(x1, y0)
        r.close()
        path = pathops.op(path, r, pathops.PathOp.UNION)
    pen = TTGlyphPen(font.getGlyphSet())
    path.draw(pen)
    return pen.glyph()


def main(paths):
    for p in paths:
        weight = "SemiBold" if "SemiBold" in p else "Regular"
        font = TTFont(p)
        font["glyf"]["two"] = build(font, GRIDS[weight])
        font["glyf"]["two"].recalcBounds(font["glyf"])
        font.save(p)
        print(f"{p}: redrew 'two' ({weight})")


if __name__ == "__main__":
    main(sys.argv[1:])
