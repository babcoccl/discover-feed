"""Local image cache: validated images resized to ``cache_width`` WebP files under
``images_dir`` keyed by a hash of the original URL, evicted least-recently-used."""

import contextlib
import hashlib
import io
import logging
import os
import threading
import warnings
from pathlib import Path

from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

ICON_SIZE = 96


def image_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def is_key(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def _rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGBA", img.size, (255, 255, 255, 0))
        background.alpha_composite(img)
        return background
    return img.convert("RGB")


def render_webp(data: bytes, width: int, *, max_pixels: int, square: bool = False) -> bytes:
    """Decode (bomb-guarded), shrink to ``width`` px wide (never enlarge), encode WebP."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        previous = Image.MAX_IMAGE_PIXELS
        Image.MAX_IMAGE_PIXELS = max_pixels
        try:
            with Image.open(io.BytesIO(data)) as src:
                if getattr(src, "n_frames", 1) > 1:
                    src.seek(0)
                img = ImageOps.exif_transpose(src) if hasattr(src, "getexif") else src
                img = _rgb(img)
        finally:
            Image.MAX_IMAGE_PIXELS = previous
    if square:
        img.thumbnail((width, width), Image.Resampling.LANCZOS)
    elif img.width > width:
        img = img.resize((width, max(1, round(img.height * width / img.width))), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "WEBP", quality=80, method=4)
    return out.getvalue()


def icon_tints(data: bytes, max_pixels: int) -> tuple[str, str]:
    """A light and a dark background tint from the icon's average opaque colour."""
    previous = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = max_pixels
    try:
        with Image.open(io.BytesIO(data)) as src:
            img = src.convert("RGBA")
    finally:
        Image.MAX_IMAGE_PIXELS = previous
    img.thumbnail((32, 32))
    raw = img.tobytes()
    pixels = [raw[i : i + 4] for i in range(0, len(raw), 4) if raw[i + 3] > 128]
    if not pixels:
        r = g = b = 128
    else:
        r, g, b = (sum(p[i] for p in pixels) // len(pixels) for i in range(3))

    def mix(target: int, amount: float) -> str:
        return "#" + "".join(f"{round(c + (target - c) * amount):02x}" for c in (r, g, b))

    return mix(255, 0.82), mix(0, 0.72)


class ImageCache:
    def __init__(self, root: Path, max_bytes: int) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.webp"

    def has(self, key: str) -> bool:
        return self.path(key).is_file()

    def put(self, key: str, data: bytes) -> int:
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return len(data)

    def delete(self, key: str) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.path(key).unlink()

    def size(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(p.stat().st_size for p in self.root.glob("*/*.webp"))
