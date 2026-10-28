#!/usr/bin/env python3
"""Replace the digit 2 in the website's Pixelify Sans with VT323's 2.

Pixelify Sans draws "2" with a closed lower bowl, so "28" reads as "88". This
swaps in the pixel shape of VT323's "2" (a diagonal instead of a bowl), scaled
into Pixelify's own "2" box so size, width and spacing stay the same, with
strokes thickened to match Pixelify's weight. Every other glyph is untouched. Both fonts are OFL-1.1 without Reserved Font Names.

Usage: scripts/fix_pixelify_two.py web/assets/fonts/PixelifySans-*.woff2
Needs: fonttools, brotli, skia-pathops.
"""

import sys

import pathops
from fontTools.pens.boundsPen import BoundsPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

# VT323's "2" as pixel blocks (x0, y0, x1, y1) in its units, without the
# font's scanline rounding. Its box is x 56..344, y 0..560.
VT323_TWO = [
    (100, 480, 300, 560),  # top bar
    (56, 400, 120, 480),   # left tip
    (260, 320, 344, 480),  # right side
    (210, 240, 290, 320),  # diagonal
    (160, 160, 240, 240),
    (110, 80, 190, 160),
    (56, 0, 344, 80),      # bottom bar
]
VT_BOX = (56, 0, 344, 560)

# Extra stroke weight per Pixelify weight, in font units: every block grows by
# this much on each side (kept inside the 2's box) so the 2 is as heavy as
# the digits around it.
GROW = {"Regular": 14, "SemiBold": 24}


def main(paths):
    for p in paths:
        grow = GROW["SemiBold" if "SemiBold" in p else "Regular"]
        font = TTFont(p)
        glyphs = font.getGlyphSet()
        bounds = BoundsPen(glyphs)
        glyphs["two"].draw(bounds)
        bx0, by0, bx1, by1 = bounds.bounds  # Pixelify's own 2 box
        sx = (bx1 - bx0) / (VT_BOX[2] - VT_BOX[0])
        sy = (by1 - by0) / (VT_BOX[3] - VT_BOX[1])

        path = pathops.Path()
        for x0, y0, x1, y1 in VT323_TWO:
            r = pathops.Path()
            X0, X1 = (round(bx0 + (x - VT_BOX[0]) * sx) for x in (x0, x1))
            Y0, Y1 = (round(by0 + (y - VT_BOX[1]) * sy) for y in (y0, y1))
            X0, Y0 = max(bx0, X0 - grow), max(by0, Y0 - grow)
            X1, Y1 = min(bx1, X1 + grow), min(by1, Y1 + grow)
            r.moveTo(X0, Y0 - 1)
            r.lineTo(X0, Y1 + 1)
            r.lineTo(X1, Y1 + 1)
            r.lineTo(X1, Y0 - 1)
            r.close()
            path = pathops.op(path, r, pathops.PathOp.UNION)

        pen = TTGlyphPen(glyphs)
        path.draw(pen)
        font["glyf"]["two"] = pen.glyph()
        font["glyf"]["two"].recalcBounds(font["glyf"])
        font.save(p)
        print(f"{p}: 'two' replaced with VT323's shape")


if __name__ == "__main__":
    main(sys.argv[1:])
