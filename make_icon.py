"""Generate ``icon.ico`` for the app window and the built .exe.

Draws the same mark as the in-app logo — a violet rounded tile with a white
"extract" arrow onto a baseline — and writes a multi-resolution Windows icon.
This is a *build-time* helper (needs Pillow); the repository stays pure source
and the generated ``icon.ico`` is git-ignored.

    python make_icon.py            # -> icon.ico
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

ACCENT = (124, 108, 255, 255)  # #7c6cff
WHITE = (255, 255, 255, 255)
SCALE = 8  # supersample, then downscale for smooth edges
BASE = 256


def _draw(size: int) -> Image.Image:
    s = size * SCALE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=ACCENT)

    cx = s / 2
    w = max(1, int(s * 0.055))
    def stroke(points):
        d.line(points, fill=WHITE, width=w, joint="curve")
        for x, y in points:
            r = w / 2
            d.ellipse((x - r, y - r, x + r, y + r), fill=WHITE)

    # Rounded caps and joins remain smooth even at title-bar sizes.
    stroke([(cx, s * 0.26), (cx, s * 0.60)])
    # arrow head (two strokes meeting at the tip)
    stroke([(cx - s * 0.13, s * 0.46), (cx, s * 0.60), (cx + s * 0.13, s * 0.46)])
    # baseline (the "tray")
    stroke([(s * 0.30, s * 0.72), (s * 0.70, s * 0.72)])

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    sizes = [16, 24, 32, 48, 64, 128, 256]
    base = _draw(BASE)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    base.save(out, format="ICO", sizes=[(s, s) for s in sizes])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
