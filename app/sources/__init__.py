"""Source adapters. Importing this package registers all built-in adapters."""

from app.sources.base import (
    FetchError,
    FetchState,
    RawArticle,
    SourceAdapter,
    UnsupportedSourceType,
    create_adapter,
    register,
)
from app.sources.rss import RSSAdapter

__all__ = [
    "FetchError",
    "FetchState",
    "RSSAdapter",
    "RawArticle",
    "SourceAdapter",
    "UnsupportedSourceType",
    "create_adapter",
    "register",
]
