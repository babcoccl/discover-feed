"""Deterministic demo images so the demo and tests never touch the network.

``/demo-image/<name>`` (demo pages' og:image / icons) and ``picsum.photos/seed/<seed>/<w>/<h>``
(the fixture feeds' media URLs) are generated with Pillow:

- ``<seed>.jpg`` 1200x630 photo-like gradient; ``tiny-<seed>.jpg`` 200x112
- ``icon-<seed>.png`` 180x180 publisher icon; ``pixel.gif`` 1x1; ``<seed>.svg``
"""

import io
import re
import zlib
from functools import lru_cache

from PIL import Image, ImageDraw

_NAME = re.compile(r"^(?:(tiny|icon)-)?([a-z0-9-]{1,60})\.(jpg|png|gif|svg)$")
_PICSUM = re.compile(r"^/seed/([A-Za-z0-9_-]{1,60})/(\d{1,4})/(\d{1,4})$")


def _palette(seed: str) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    h = zlib.crc32(seed.encode())
    a = (40 + h % 180, 40 + (h >> 8) % 180, 40 + (h >> 16) % 180)
    b = (255 - a[1] // 2, 60 + (h >> 4) % 150, 255 - a[0] // 2)
    return a, b


def _photo(seed: str, width: int, height: int) -> bytes:
    a, b = _palette(seed)
    img = Image.new("RGB", (width, height), a)
    draw = ImageDraw.Draw(img)
    for y in range(0, height, 4):
        t = y / max(1, height - 1)
        draw.rectangle(
            (0, y, width, y + 4), fill=tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
        )
    h = zlib.crc32(seed.encode("utf-8") + b"shape")
    for i in range(4):
        r = (h >> (i * 5)) % (height // 3) + height // 8
        cx = (h >> (i * 3)) % width
        cy = (h >> (i * 7)) % height
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=tuple(255 - c for c in a))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=70)
    return out.getvalue()


def _icon(seed: str) -> bytes:
    a, _ = _palette(seed + "icon")
    img = Image.new("RGBA", (180, 180), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((0, 0, 179, 179), radius=36, fill=a)
    draw.rounded_rectangle((52, 52, 128, 128), radius=14, fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


@lru_cache(maxsize=256)
def demo_image(name: str) -> tuple[bytes, str] | None:
    m = _NAME.match(name)
    if not m:
        return None
    variant, seed, ext = m.groups()
    if ext == "svg":
        svg = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="630">'
            f"<text>{seed}</text></svg>"
        )
        return svg.encode(), "image/svg+xml"
    if seed == "pixel" and ext == "gif":
        out = io.BytesIO()
        Image.new("RGB", (1, 1), (255, 255, 255)).save(out, "GIF")
        return out.getvalue(), "image/gif"
    if variant == "icon":
        return _icon(seed), "image/png"
    if variant == "tiny":
        return _photo(seed, 200, 112), "image/jpeg"
    return _photo(seed, 1200, 630), "image/jpeg"


@lru_cache(maxsize=256)
def picsum_image(path: str) -> bytes | None:
    m = _PICSUM.match(path)
    if not m:
        return None
    seed, width, height = m.group(1), int(m.group(2)), int(m.group(3))
    if not (1 <= width <= 2000 and 1 <= height <= 2000):
        return None
    return _photo(seed, width, height)
