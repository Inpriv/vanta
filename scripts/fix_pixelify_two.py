#!/usr/bin/env python3
"""Straighten the middle stroke of the digit 2 in the website's Pixelify Sans.

Pixelify Sans steps the middle of its "2" (the left half sits higher than the
right), so it reads as curved. This keeps the font's own outline and fills
that stroke into one straight bar; every other glyph is untouched. The fonts
are OFL-1.1 with no Reserved Font Name, so the family name stays.

Usage: scripts/fix_pixelify_two.py web/assets/fonts/PixelifySans-*.woff2
Needs: fonttools, brotli, skia-pathops.
"""

import sys

import pathops
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

# The middle stroke's full extent in each weight, read from the original "2":
# from the left inner edge to the right outer edge, bottom to top.
MIDDLE = {
    "Regular": (151, 260, 433, 359),
    "SemiBold": (150, 251, 446, 374),
}


def main(paths):
    for p in paths:
        weight = "SemiBold" if "SemiBold" in p else "Regular"
        font = TTFont(p)
        glyphs = font.getGlyphSet()

        path = pathops.Path()
        glyphs["two"].draw(path.getPen())
        x0, y0, x1, y1 = MIDDLE[weight]
        bar = pathops.Path()
        bar.moveTo(x0, y0)
        bar.lineTo(x0, y1)
        bar.lineTo(x1, y1)
        bar.lineTo(x1, y0)
        bar.close()
        path = pathops.op(path, bar, pathops.PathOp.UNION)

        pen = TTGlyphPen(glyphs)
        path.draw(pen)
        font["glyf"]["two"] = pen.glyph()
        font["glyf"]["two"].recalcBounds(font["glyf"])
        font.save(p)
        print(f"{p}: straightened the middle of 'two' ({weight})")


if __name__ == "__main__":
    main(sys.argv[1:])
