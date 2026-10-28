#!/usr/bin/env python3
"""Straighten the middle stroke of the digit 2 in the website's Pixelify Sans.

Pixelify Sans steps the middle of its "2" (the side strokes reach into it at
different heights), so it reads as curved. This keeps the font's own top and
bottom halves and replaces the middle with one even bar; every other glyph is
untouched. The fonts
are OFL-1.1 with no Reserved Font Name, so the family name stays.

Usage: scripts/fix_pixelify_two.py web/assets/fonts/PixelifySans-*.woff2
Needs: fonttools, brotli, skia-pathops.
"""

import sys

import pathops
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

# The middle stroke in each weight, read from the original "2": inner left to
# outer-right-column edge, bottom to top of the stroke. Above it the font's top
# half is kept as is, below it the bottom half; the side strokes that poked
# into the middle at different heights are cut back to the bar.
MIDDLE = {
    "Regular": (151, 260, 433, 359),
    "SemiBold": (150, 251, 446, 374),
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
        x0, y0, x1, y1 = MIDDLE[weight]
        top = pathops.op(orig, rect(-1000, y1, 2000, 2000), pathops.PathOp.INTERSECTION)
        bottom = pathops.op(orig, rect(-1000, -1000, 2000, y0), pathops.PathOp.INTERSECTION)
        path = pathops.op(top, bottom, pathops.PathOp.UNION)
        path = pathops.op(path, rect(x0, y0, x1, y1), pathops.PathOp.UNION)

        pen = TTGlyphPen(glyphs)
        path.draw(pen)
        font["glyf"]["two"] = pen.glyph()
        font["glyf"]["two"].recalcBounds(font["glyf"])
        font.save(p)
        print(f"{p}: straightened the middle of 'two' ({weight})")


if __name__ == "__main__":
    main(sys.argv[1:])
