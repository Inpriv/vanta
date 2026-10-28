#!/usr/bin/env python3
"""Make rounded-corner versions of the block textures for instance icons.

Slint's software renderer ignores the corner radius when clipping, so a
16x16 block drawn inside a rounded tile shows square corners there (and in
the headless screenshots). These copies are scaled up 8x with hard pixels and
have transparent, anti-aliased corners matching InstanceIcon's radius, so
every renderer shows the same rounded tile.

Usage: scripts/gen_rounded_blocks.py [blocks-dir]
Needs: Pillow.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SCALE = 8          # 16 px texture -> 128 px
RADIUS = 0.26      # InstanceIcon: border-radius: size * 0.26
SUPERSAMPLE = 4


def rounded(src: Path, dst: Path) -> None:
    img = Image.open(src).convert("RGBA")
    big = img.resize((img.width * SCALE, img.height * SCALE), Image.NEAREST)
    w, h = big.size
    mask = Image.new("L", (w * SUPERSAMPLE, h * SUPERSAMPLE), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * SUPERSAMPLE - 1, h * SUPERSAMPLE - 1),
        radius=round(min(w, h) * RADIUS * SUPERSAMPLE),
        fill=255,
    )
    big.putalpha(mask.resize((w, h), Image.LANCZOS))
    big.save(dst, optimize=True)


def main(blocks: str) -> None:
    src_dir = Path(blocks)
    out = src_dir / "rounded"
    out.mkdir(exist_ok=True)
    for src in sorted(src_dir.glob("*.png")):
        rounded(src, out / src.name)
        print(out / src.name)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "crates/vanta-app/ui/blocks")
