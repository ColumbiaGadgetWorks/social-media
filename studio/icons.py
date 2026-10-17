"""Small round social icons for the email footer, drawn here so there are no image files to ship.

Email apps can't use SVG, so they are PNGs served from the public /e/icon/ address. Each is a
coloured disc with a white mark; 2x size so they stay sharp when shown at 32px."""
from __future__ import annotations

import io
from functools import cache

from PIL import Image, ImageDraw, ImageFont

SIZE = 64
# key -> (disc colour, mark). The mark is the letters people recognise each network by.
ICONS = {
    "facebook": ("#1877f2", "f"),
    "instagram": ("#c13584", "IG"),
    "bluesky": ("#1185fe", "Bs"),
    "threads": ("#101010", "@"),
    "youtube": ("#e00000", ">"),
    "tiktok": ("#101010", "Tk"),
    "linkedin": ("#0a66c2", "in"),
    "discord": ("#5865f2", "D"),
}


@cache
def png(key: str) -> bytes | None:
    if key not in ICONS:
        return None
    colour, mark = ICONS[key]
    scale = 4  # draw big, shrink for smooth edges
    img = Image.new("RGBA", (SIZE * scale, SIZE * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, SIZE * scale - 1, SIZE * scale - 1), fill=colour)
    font = ImageFont.load_default(size=(36 if len(mark) == 1 else 28) * scale)
    draw.text((SIZE * scale / 2, SIZE * scale / 2), mark, fill="white", font=font, anchor="mm")
    out = io.BytesIO()
    img.resize((SIZE, SIZE), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()
