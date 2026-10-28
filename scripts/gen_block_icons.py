#!/usr/bin/env python3
"""Generate Vanta's original 16x16 pixel-art block icons.

The textures are drawn procedurally here (no game assets are used), so they
can be regenerated or tweaked: `python3 scripts/gen_block_icons.py OUT_DIR`.
Requires Pillow.
"""
import random
import sys
from pathlib import Path

from PIL import Image

N = 16


def hexc(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def shade(c, f):
    return tuple(max(0, min(255, int(v * f))) for v in c)


def pick(rng, palette):
    colors, weights = zip(*palette)
    return hexc(rng.choices(colors, weights)[0])


def noise_block(seed, palette, blobs=None):
    rng = random.Random(seed)
    px = [[pick(rng, palette) for _ in range(N)] for _ in range(N)]
    if blobs:
        color, count, size = blobs
        for _ in range(count):
            x, y = rng.randrange(N), rng.randrange(N)
            for dx in range(size):
                for dy in range(size):
                    if rng.random() < 0.75:
                        px[(y + dy) % N][(x + dx) % N] = shade(hexc(color), rng.uniform(0.85, 1.1))
    return px


def ore(seed, colors):
    px = stone(seed)
    rng = random.Random(seed * 7 + 1)
    for _ in range(5):
        cx, cy = rng.randrange(1, N - 2), rng.randrange(1, N - 2)
        for dx, dy in [(0, 0), (1, 0), (0, 1), (1, 1), (-1, 0), (0, -1)]:
            if rng.random() < 0.8:
                px[cy + dy][cx + dx] = hexc(rng.choice(colors))
        px[cy][cx] = shade(hexc(colors[0]), 1.2)
    return px


def stone(seed=2):
    return noise_block(seed, [("#7f7f80", 5), ("#8b8b8c", 4), ("#727273", 3), ("#999a9b", 1)], ("#68686a", 6, 2))


def dirt(seed=1):
    return noise_block(seed, [("#7a5434", 5), ("#6a472a", 4), ("#8b6340", 3), ("#58391f", 2)], ("#9b7350", 5, 1))


def grass():
    px = dirt(11)
    rng = random.Random(12)
    greens = ["#5ea63a", "#6cb846", "#4f9230", "#7cc552"]
    for x in range(N):
        depth = rng.choice([3, 4, 4, 5])
        for y in range(depth):
            px[y][x] = hexc(rng.choice(greens))
        if rng.random() < 0.5:
            px[depth][x] = hexc("#4f9230")
    return px


def cobblestone():
    rng = random.Random(4)
    seeds = [(rng.uniform(0, N), rng.uniform(0, N), rng.choice(["#8d8d8e", "#7a7a7b", "#9d9d9e", "#6f6f70"])) for _ in range(9)]
    px = [[(0, 0, 0)] * N for _ in range(N)]
    for y in range(N):
        for x in range(N):
            d = sorted(((min(abs(x - sx), N - abs(x - sx)) ** 2 + min(abs(y - sy), N - abs(y - sy)) ** 2, c) for sx, sy, c in seeds))
            if d[1][0] - d[0][0] < 6:
                px[y][x] = hexc("#4c4c4d")
            else:
                px[y][x] = shade(hexc(d[0][1]), rng.uniform(0.92, 1.06))
    return px


def oak_log():
    rng = random.Random(5)
    px = []
    cols = [rng.choice(["#5c4326", "#6c5131", "#4b361d", "#755838"]) for _ in range(N)]
    for y in range(N):
        row = []
        for x in range(N):
            c = hexc(cols[x])
            if rng.random() < 0.18:
                c = shade(c, 0.8)
            row.append(c)
        px.append(row)
    return px


def planks():
    rng = random.Random(6)
    base = ["#a8804a", "#b48b52", "#9b7440", "#a07b46"]
    px = []
    for y in range(N):
        board = y // 4
        row = []
        for x in range(N):
            if y % 4 == 3:
                row.append(hexc("#6e5029"))
            elif x == (board * 7 + 3) % N:
                row.append(hexc("#7d5c31"))
            else:
                row.append(shade(hexc(base[board]), rng.uniform(0.94, 1.05)))
        px.append(row)
    return px


def tnt():
    letters = {  # 3x5 pixel glyphs
        "T": ["###", ".#.", ".#.", ".#.", ".#."],
        "N": ["#.#", "###", "###", "#.#", "#.#"],
    }
    rng = random.Random(7)
    px = []
    for y in range(N):
        row = []
        for x in range(N):
            if 5 <= y <= 10:
                row.append(hexc("#ece8e0"))
            else:
                red = ["#d13b2c", "#b8301f", "#e04a38"][(x // 2) % 3]
                row.append(shade(hexc(red), 0.8 if x % 4 == 0 else rng.uniform(0.95, 1.05)))
        px.append(row)
    x0 = 2
    for ch in "TNT":
        for dy, line in enumerate(letters[ch]):
            for dx, v in enumerate(line):
                if v == "#":
                    px[6 + dy - 0][x0 + dx] = hexc("#2b2b2b")
        x0 += 4
    # keep glyphs inside the white band
    for x in range(N):
        px[11][x] = px[11][x]
    return px


def ice():
    rng = random.Random(8)
    px = [[shade(hexc(rng.choice(["#9cc4f5", "#a9cdf7", "#8fb9ef"])), 1.0) for _ in range(N)] for _ in range(N)]
    for start in (2, 9):
        for i in range(N):
            x, y = (start + i) % N, i
            if rng.random() < 0.8:
                px[y][x] = hexc("#e2f0ff")
    return px


def glowstone():
    rng = random.Random(9)
    px = [[(0, 0, 0)] * N for _ in range(N)]
    for by in range(0, N, 4):
        for bx in range(0, N, 4):
            c = hexc(rng.choice(["#f7d36b", "#e6b04a", "#ffe9a3", "#cf9233"]))
            for y in range(by, by + 4):
                for x in range(bx, bx + 4):
                    px[y][x] = shade(c, rng.uniform(0.88, 1.08)) if (x - bx) and (y - by) else shade(c, 0.7)
    return px


BLOCKS = {
    "grass": grass,
    "dirt": dirt,
    "stone": stone,
    "cobblestone": cobblestone,
    "bedrock": lambda: noise_block(3, [("#2c2c2c", 4), ("#494949", 4), ("#6a6a6a", 2), ("#161616", 3)]),
    "sand": lambda: noise_block(13, [("#dccf8f", 5), ("#e5da9c", 4), ("#cdbf7d", 3)], ("#f0e6b5", 4, 1)),
    "gravel": lambda: noise_block(14, [("#837d78", 4), ("#6e6864", 4), ("#9a938d", 3), ("#7a6a5c", 2)], ("#5a5450", 6, 2)),
    "oak_log": oak_log,
    "planks": planks,
    "coal_ore": lambda: ore(21, ["#2a2a2a", "#3b3b3b", "#1d1d1d"]),
    "iron_ore": lambda: ore(22, ["#e0b79a", "#c9997a", "#f0cfb8"]),
    "gold_ore": lambda: ore(23, ["#f6d65a", "#e5b72f", "#fff09a"]),
    "diamond_ore": lambda: ore(24, ["#5fe3e8", "#3cc6cf", "#b8fbff"]),
    "emerald_ore": lambda: ore(25, ["#42d27b", "#23a85a", "#9cf5bf"]),
    "redstone_ore": lambda: ore(26, ["#e23b2b", "#b51e12", "#ff8a7a"]),
    "netherrack": lambda: noise_block(31, [("#7b2d2c", 4), ("#8e3c37", 4), ("#5f1f1f", 3), ("#a24a42", 1)], ("#4d1717", 5, 2)),
    "end_stone": lambda: noise_block(32, [("#e5e7a7", 5), ("#d8db96", 4), ("#eef0bd", 2)], ("#bfc27f", 7, 1)),
    "obsidian": lambda: noise_block(33, [("#1b1427", 5), ("#271c3a", 4), ("#120d1b", 3)], ("#4a3570", 5, 2)),
    "ice": ice,
    "snow": lambda: noise_block(34, [("#f4f8fc", 6), ("#e8eff6", 4), ("#ffffff", 2)]),
    "glowstone": glowstone,
    "tnt": tnt,
}


def bevel(px):
    """Light the top/left edge and shade the bottom/right edge."""
    for i in range(N):
        px[0][i] = shade(px[0][i], 1.18)
        px[i][0] = shade(px[i][0], 1.12)
        px[N - 1][i] = shade(px[N - 1][i], 0.72)
        px[i][N - 1] = shade(px[i][N - 1], 0.78)
    return px


def main(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in BLOCKS.items():
        px = bevel(fn())
        img = Image.new("RGB", (N, N))
        img.putdata([c for row in px for c in row])
        img.save(out / f"{name}.png", optimize=True)
    print(f"wrote {len(BLOCKS)} icons to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "crates/vanta-app/ui/blocks")
