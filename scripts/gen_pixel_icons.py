#!/usr/bin/env python3
"""Turn Vanta's line icons into crisp 16x16 pixel icons.

Each SVG in crates/vanta-app/ui/icons is rendered with thicker strokes at
16x16 (via rsvg-convert) and every pixel is snapped to fully on or off, so
the icons sit on the same pixel grid as the block textures.
Usage: python3 scripts/gen_pixel_icons.py   (needs Pillow and rsvg-convert)
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

SRC = Path("crates/vanta-app/ui/icons")
OUT = SRC / "px"
SIZE = 16
THRESHOLD = 96


def pixels(img):
    """All pixels as tuples (Pillow 12+ renamed getdata)."""
    getter = getattr(img, "get_flattened_data", None) or img.getdata
    return list(getter())


def main():
    OUT.mkdir(exist_ok=True)
    count = 0
    for svg in sorted(SRC.glob("*.svg")):
        if svg.stem == "logo":
            continue
        text = svg.read_text()
        # Thicker, square strokes read better once snapped to pixels.
        text = re.sub(r'stroke-width="[^"]+"', 'stroke-width="2.7"', text)
        text = text.replace('stroke-linecap="round"', 'stroke-linecap="square"')
        text = text.replace('stroke-linejoin="round"', 'stroke-linejoin="miter"')
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "i.svg"
            png = Path(tmp) / "i.png"
            src.write_text(text)
            subprocess.run(["rsvg-convert", "-w", str(SIZE), "-h", str(SIZE), str(src), "-o", str(png)], check=True)
            img = Image.open(png).convert("RGBA")
        out = Image.new("RGBA", img.size)
        out.putdata([(255, 255, 255, 255) if a >= THRESHOLD else (0, 0, 0, 0) for (_, _, _, a) in pixels(img)])
        out.save(OUT / f"{svg.stem}.png", optimize=True)
        count += 1
    print(f"wrote {count} pixel icons to {OUT}")


if __name__ == "__main__":
    sys.exit(main())
