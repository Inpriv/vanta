#!/usr/bin/env python3
"""Give the digit 2 in the website's Pixelify Sans a handwritten diagonal.

Pixelify Sans draws "2" with a closed lower bowl, so "28" reads as "88". This
keeps the font's own top curve and bottom bar and replaces the middle with a
stepped diagonal, like a 2 written by hand; every other glyph is untouched. The fonts
are OFL-1.1 with no Reserved Font Name, so the family name stays.

Usage: scripts/fix_pixelify_two.py web/assets/fonts/PixelifySans-*.woff2
Needs: fonttools, brotli, skia-pathops.
"""

import sys

import pathops
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

# Per weight, from the original "2": where the top curve ends (bottom of the
# right column), the top of the bottom bar, the top and inner edge of the
# bottom-right tick, the outer left/right edges and the stroke width.
GRID = {
    "Regular": dict(curve=359, bar=88, tick=179, ri=423, L=60, R=525, stroke=101),
    "SemiBold": dict(curve=374, bar=113, tick=200, ri=417, L=61, R=536, stroke=118),
}


def rect(x0, y0, x1, y1):
    r = pathops.Path()
    r.moveTo(x0, y0)
    r.lineTo(x0, y1)
    r.lineTo(x1, y1)
    r.lineTo(x1, y0)
    r.close()
    return r


def main(paths):
    for p in paths:
        weight = "SemiBold" if "SemiBold" in p else "Regular"
        font = TTFont(p)
        glyphs = font.getGlyphSet()

        orig = pathops.Path()
        glyphs["two"].draw(orig.getPen())
        g = GRID[weight]
        top = pathops.op(orig, rect(-1000, g["curve"], 2000, 2000), pathops.PathOp.INTERSECTION)
        keep = pathops.op(rect(-1000, -1000, 2000, g["bar"]), rect(g["ri"], -1000, 2000, g["tick"]), pathops.PathOp.UNION)
        bottom = pathops.op(orig, keep, pathops.PathOp.INTERSECTION)  # bar + tick
        path = pathops.op(top, bottom, pathops.PathOp.UNION)
        # A stepped diagonal from under the curve down to the bottom-left,
        # like a handwritten 2: three steps, each about one and a half strokes
        # wide so neighbours overlap into one solid stroke.
        steps = 3
        w = round(g["stroke"] * 1.5)
        h = (g["curve"] - g["bar"]) / steps
        x_top = g["R"] - w
        for i in range(steps):
            x = round(x_top + (g["L"] - x_top) * i / (steps - 1))
            y1 = round(g["curve"] - h * i) + 1
            y0 = round(g["curve"] - h * (i + 1)) - 1
            path = pathops.op(path, rect(x, y0, x + w, y1), pathops.PathOp.UNION)

        pen = TTGlyphPen(glyphs)
        path.draw(pen)
        font["glyf"]["two"] = pen.glyph()
        font["glyf"]["two"].recalcBounds(font["glyf"])
        font.save(p)
        print(f"{p}: straightened the middle of 'two' ({weight})")


if __name__ == "__main__":
    main(sys.argv[1:])
