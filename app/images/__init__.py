"""Card images: page image discovery, validation, SSRF-safe fetch, WebP cache, selection.

``configure()`` is called once at startup (app / demo) with the YAML ``images:`` block;
publishing reads it through ``config()``.
"""

from app.config import ImagesConfig, NoImageStyle

_config = ImagesConfig()
_style_override: NoImageStyle | None = None


def configure(config: ImagesConfig, style: str | None = None) -> None:
    global _config, _style_override
    _config = config
    _style_override = NoImageStyle(style) if style else None


def config() -> ImagesConfig:
    return _config


def no_image_style() -> NoImageStyle:
    return _style_override or _config.no_image_style
