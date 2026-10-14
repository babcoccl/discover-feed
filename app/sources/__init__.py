"""Source adapters. Importing this package registers all built-in adapters."""

from app.sources.base import (
    FetchError,
    FetchState,
    Quota,
    QuotaExhausted,
    RateLimited,
    RawArticle,
    SourceAdapter,
    UnsupportedSourceType,
    create_adapter,
    register,
)
from app.sources.guardian import GuardianAdapter
from app.sources.newsdata import NewsDataAdapter
from app.sources.rss import RSSAdapter
from app.sources.x import XAdapter

__all__ = [
    "FetchError",
    "FetchState",
    "GuardianAdapter",
    "NewsDataAdapter",
    "Quota",
    "QuotaExhausted",
    "RSSAdapter",
    "RateLimited",
    "RawArticle",
    "SourceAdapter",
    "UnsupportedSourceType",
    "XAdapter",
    "create_adapter",
    "register",
]
