"""Download and validate one image: timeout, byte cap, ``image/*`` content type, no SVG/ICO
for photos, no tracking pixels or small images, Pillow decompression-bomb limit."""

import io
import re
import warnings
from dataclasses import dataclass

import httpx
from PIL import Image, UnidentifiedImageError

from app.images.safety import Blocked, Guard, guarded_get_stream

USER_AGENT = "discover-feed/0.1 (+https://github.com/babcoccl/discover-feed; image check)"
TRACKING = re.compile(
    r"(?:^|[/_.-])(?:pixel|spacer|blank|1x1|tracking|beacon|transparent)(?:[/_.-]|$)", re.I
)


@dataclass(frozen=True)
class Fetched:
    """A validated image; ``data`` are the downloaded bytes."""

    url: str
    data: bytes
    content_type: str
    width: int
    height: int


class Rejected(Exception):
    def __init__(self, reason: str, *, failed: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.failed = failed
        """True for fetch errors (maybe transient) rather than an unusable image."""


def is_tracking_url(url: str) -> bool:
    return bool(TRACKING.search(httpx.URL(url).path))


async def fetch_image(
    client: httpx.AsyncClient,
    guard: Guard,
    url: str,
    *,
    max_bytes: int,
    max_pixels: int,
    min_width: int = 1,
    min_height: int = 1,
    icon: bool = False,
) -> Fetched:
    if not icon and is_tracking_url(url):
        raise Rejected("tracking_pixel")
    try:
        response = await guarded_get_stream(client, guard, url)
    except Blocked as exc:
        raise Rejected(exc.reason) from exc
    except httpx.TimeoutException as exc:
        raise Rejected("timeout", failed=True) from exc
    except httpx.HTTPError as exc:
        raise Rejected("network_error", failed=True) from exc
    try:
        if response.status_code >= 400:
            raise Rejected(f"http_{response.status_code}", failed=response.status_code >= 500)
        ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not ctype.startswith("image/"):
            raise Rejected("not_image")
        if "svg" in ctype:
            raise Rejected("svg")
        if not icon and ctype in ("image/x-icon", "image/vnd.microsoft.icon"):
            raise Rejected("ico")
        length = response.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise Rejected("too_large")
        buf = bytearray()
        try:
            async for chunk in response.aiter_bytes():
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise Rejected("too_large")
        except httpx.TimeoutException as exc:
            raise Rejected("timeout", failed=True) from exc
        except httpx.HTTPError as exc:
            raise Rejected("network_error", failed=True) from exc
    finally:
        await response.aclose()
    width, height, fmt = probe(bytes(buf), max_pixels)
    if fmt == "SVG":
        raise Rejected("svg")
    if not icon and fmt == "ICO":
        raise Rejected("ico")
    if width <= 2 and height <= 2:
        raise Rejected("tracking_pixel")
    if width < min_width or height < min_height:
        raise Rejected("too_small")
    return Fetched(str(response.url), bytes(buf), ctype, width, height)


def probe(data: bytes, max_pixels: int) -> tuple[int, int, str]:
    """``(width, height, format)`` without decoding pixels; bombs and junk are Rejected."""
    if data.lstrip()[:5].lower() in (b"<?xml", b"<svg ") or b"<svg" in data[:512].lower():
        return 0, 0, "SVG"
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as img:
                width, height = img.size
                fmt = img.format or ""
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise Rejected("decompression_bomb") from exc
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise Rejected("undecodable") from exc
    if width * height > max_pixels:
        raise Rejected("decompression_bomb")
    return width, height, fmt
