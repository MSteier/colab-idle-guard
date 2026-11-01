"""Tray icon images, drawn with Pillow (no image files to ship)."""

from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw

from .presenter import STATE_STYLE

SIZE = 64


@lru_cache(maxsize=None)
def state_icon(state: str) -> Image.Image:
    colour, glyph = STATE_STYLE.get(state, STATE_STYLE["unknown"])
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # Shield-like rounded square so the icon reads at 16x16.
    d.rounded_rectangle((4, 4, SIZE - 4, SIZE - 4), radius=14, fill=colour)
    white = (255, 255, 255, 255)
    c = SIZE // 2
    if glyph == "play":
        d.polygon([(c - 9, c - 13), (c - 9, c + 13), (c + 13, c)], fill=white)
    elif glyph == "pause":
        d.rectangle((c - 12, c - 13, c - 4, c + 13), fill=white)
        d.rectangle((c + 4, c - 13, c + 12, c + 13), fill=white)
    elif glyph == "!":
        d.rectangle((c - 4, c - 17, c + 4, c + 5), fill=white)
        d.rectangle((c - 4, c + 10, c + 4, c + 17), fill=white)
    elif glyph == "?":
        d.arc((c - 11, c - 18, c + 11, c + 4), start=180, end=90, fill=white, width=7)
        d.rectangle((c - 3, c + 1, c + 4, c + 8), fill=white)
        d.rectangle((c - 3, c + 12, c + 4, c + 18), fill=white)
    elif glyph == "x":
        d.line((c - 12, c - 12, c + 12, c + 12), fill=white, width=8)
        d.line((c - 12, c + 12, c + 12, c - 12), fill=white, width=8)
    else:
        # Plain state: a small "C" for Colab.
        d.arc((c - 14, c - 14, c + 14, c + 14), start=45, end=315, fill=white, width=8)
    return img
