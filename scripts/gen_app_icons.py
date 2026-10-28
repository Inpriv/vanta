#!/usr/bin/env python3
"""Build every Vanta app icon from the pixel logo.

Sources are the 16 px and 24 px pixel logos in crates/vanta-app/ui/icons/.
Each size scales one of them by a whole number (nearest neighbour), so every
logo pixel stays a sharp square, and centres it at about half the icon's size.

Writes packaging/icons/ (Linux, Windows and the app window), the app's
ui/icons/app-icon.png and the icons/ folder at the repository root.

Usage: python3 scripts/gen_app_icons.py
Needs: Pillow.
"""

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
LOGO16 = ROOT / "crates/vanta-app/ui/icons/logo-px16.png"
LOGO24 = ROOT / "crates/vanta-app/ui/icons/logo-px24.png"

# Icon size -> (logo, scale). The glyph is 14 px wide in the 16 px logo and
# 22 px in the 24 px one; small sizes fill more of the square to stay legible.
PLAN = {
    16: (LOGO16, 1),
    24: (LOGO16, 1),
    32: (LOGO24, 1),
    48: (LOGO24, 1),
    64: (LOGO16, 2),
    128: (LOGO24, 3),
    256: (LOGO24, 6),
    512: (LOGO24, 12),
}
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

WHITE = (255, 255, 255, 255)
DARK = (20, 21, 24, 255)
BLACK = (0, 0, 0, 255)


def glyph(logo: Path, scale: int) -> Image.Image:
    """The logo's alpha mask, cropped to the glyph and scaled up."""
    alpha = Image.open(logo).convert("RGBA").split()[3]
    alpha = alpha.crop(alpha.getbbox())
    return alpha.resize((alpha.width * scale, alpha.height * scale), Image.NEAREST)


def icon(size: int, fg, bg=None) -> Image.Image:
    mask = glyph(*PLAN[size])
    img = Image.new("RGBA", (size, size), bg or (0, 0, 0, 0))
    pos = ((size - mask.width) // 2, (size - mask.height) // 2)
    img.paste(Image.new("RGBA", mask.size, fg), pos, mask)
    return img


def save_ico(path: Path, fg, bg=None) -> None:
    frames = [icon(s, fg, bg) for s in ICO_SIZES]
    frames[-1].save(path, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=frames[:-1])


def main() -> None:
    pkg = ROOT / "packaging/icons"
    for size in PLAN:
        if size != 24:  # 24 px only goes into the .ico files
            icon(size, WHITE).save(pkg / f"vanta-{size}.png", optimize=True)
    icon(512, WHITE).save(pkg / "vanta.png", optimize=True)
    icon(512, DARK).save(pkg / "vanta-dark.png", optimize=True)
    icon(512, WHITE, BLACK).convert("RGB").save(pkg / "vanta-bg.png", optimize=True)
    save_ico(pkg / "vanta.ico", WHITE)
    save_ico(pkg / "vanta-bg.ico", WHITE, BLACK)
    icon(256, WHITE).save(ROOT / "crates/vanta-app/ui/icons/app-icon.png", optimize=True)

    out = ROOT / "icons"
    out.mkdir(exist_ok=True)
    icon(512, WHITE).save(out / "icon.png", optimize=True)
    icon(512, DARK).save(out / "icon-dark.png", optimize=True)
    icon(512, WHITE, BLACK).convert("RGB").save(out / "icon-bg.png", optimize=True)
    save_ico(out / "icon.ico", WHITE)
    save_ico(out / "icon-bg.ico", WHITE, BLACK)
    for f in sorted([*pkg.glob("vanta*"), *out.glob("icon*")]):
        print(f.relative_to(ROOT))


if __name__ == "__main__":
    main()
