"""Dev only: draw DupeHunter's icon with Pillow (no runtime dependency)."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

TEAL = (13, 148, 136)
TEAL_DARK = (15, 118, 110)
WHITE = (255, 255, 255)


def draw(size: int) -> Image.Image:
    scale = 8
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=TEAL)

    # back card
    d.rounded_rectangle(
        [int(s * 0.30), int(s * 0.20), int(s * 0.76), int(s * 0.64)],
        radius=int(s * 0.05), fill=WHITE,
    )
    # front card
    d.rounded_rectangle(
        [int(s * 0.20), int(s * 0.30), int(s * 0.66), int(s * 0.74)],
        radius=int(s * 0.05), fill=WHITE,
    )
    # text lines on the front card
    for i, y in enumerate((0.40, 0.48, 0.56)):
        width = 0.34 - i * 0.06
        d.rounded_rectangle(
            [int(s * 0.27), int(s * y), int(s * (0.27 + width)), int(s * (y + 0.035))],
            radius=int(s * 0.015), fill=TEAL_DARK,
        )

    # magnifier over the bottom-right corner
    cx, cy, r = int(s * 0.70), int(s * 0.70), int(s * 0.13)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=WHITE, width=int(s * 0.045))
    d.line(
        [(cx + int(r * 0.7), cy + int(r * 0.7)), (cx + int(r * 1.55), cy + int(r * 1.55))],
        fill=WHITE, width=int(s * 0.05),
    )
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    root = Path(__file__).resolve().parent.parent
    assets = root / "assets"
    assets.mkdir(exist_ok=True)
    master = draw(1024)
    master.save(assets / "dupehunter.ico",
                sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    (root / "docs").mkdir(exist_ok=True)
    master.resize((256, 256), Image.LANCZOS).save(root / "docs" / "icon.png")
    print("icon written to assets/dupehunter.ico and docs/icon.png")


if __name__ == "__main__":
    main()
