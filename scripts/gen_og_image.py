#!/usr/bin/env python3
"""Render the 1200x630 social preview image (web/assets/og.png).

Usage: python3 scripts/gen_og_image.py SCREENSHOT.png
SCREENSHOT is a 2x render of the Home page from the `screenshots` example.
Needs Pillow.
"""
import random
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
BG = (11, 12, 14)
TEXT = (236, 237, 240)
MUTED = (154, 157, 166)
ACCENT = (114, 184, 255)
UI = Path("crates/vanta-app/ui")


def font(name, size):
    return ImageFont.truetype(str(UI / "fonts" / name), size)


def main(shot_path):
    img = Image.new("RGB", (W, H), BG)
    px = img.load()
    rng = random.Random(7)
    for y in range(0, H, 2):          # pixel grain in 2x2 cells
        for x in range(0, W, 2):
            r = rng.random()
            if r < 0.05:
                v = 20 if r < 0.025 else 17
                for dy in (0, 1):
                    for dx in (0, 1):
                        px[x + dx, y + dy] = (v, v + 1, v + 4)
    d = ImageDraw.Draw(img)

    # Accent bar along the top edge.
    d.rectangle([0, 0, W, 8], fill=ACCENT)
    d.rectangle([0, 8, W, 11], fill=(43, 95, 158))

    # Pixel V mark, 6x scale of the 24 px logo.
    mark = Image.open(UI / "icons" / "logo-px24.png").convert("RGBA").resize((144, 144), Image.NEAREST)
    img.paste(Image.new("RGB", mark.size, TEXT), (72, 92), mark)

    d.text((236, 104), "Vanta", font=font("PixelifySans-SemiBold.ttf", 104), fill=TEXT)
    d.text((76, 274), "A fast, minimal", font=font("Inter-SemiBold.ttf", 44), fill=TEXT)
    d.text((76, 326), "Minecraft launcher.", font=font("Inter-SemiBold.ttf", 44), fill=TEXT)
    d.text((76, 400), "Open source · Windows & Linux", font=font("PixelifySans-Regular.ttf", 28), fill=MUTED)

    # Hotbar of block slots.
    blocks = ["grass", "dirt", "stone", "oak_log", "planks", "diamond_ore", "gold_ore", "tnt", "glowstone"]
    x0, y0, slot, gap = 76, 476, 48, 6
    d.rectangle([x0 - 6, y0 - 6, x0 + len(blocks) * (slot + gap) - gap + 5, y0 + slot + 5], fill=(13, 14, 17), outline=(42, 45, 51), width=3)
    for i, b in enumerate(blocks):
        x = x0 + i * (slot + gap)
        selected = i == 5
        d.rectangle([x, y0, x + slot - 1, y0 + slot - 1], fill=(26, 28, 33), outline=ACCENT if selected else (35, 38, 44), width=3)
        tex = Image.open(UI / "blocks" / f"{b}.png").convert("RGB").resize((32, 32), Image.NEAREST)
        img.paste(tex, (x + 8, y0 + 8))

    # App screenshot on the right with a hard pixel shadow and frame.
    shot = Image.open(shot_path).convert("RGB")
    sw = 560
    sh = round(shot.height * sw / shot.width)
    shot = shot.resize((sw, sh), Image.LANCZOS)
    sx, sy = W - sw - 56, (H - sh) // 2 + 12
    d.rectangle([sx + 8, sy + 8, sx + sw + 8, sy + sh + 8], fill=(0, 0, 0))
    img.paste(shot, (sx, sy))
    d.rectangle([sx - 2, sy - 2, sx + sw + 1, sy + sh + 1], outline=(46, 49, 56), width=2)

    d.text((W - 56, H - 40), "getvanta.xyz", font=font("PixelifySans-Regular.ttf", 22), fill=MUTED, anchor="ra")

    out = Path("web/assets/og.png")
    img.save(out, optimize=True)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(sys.argv[1])
