"""Web search providers used by story enrichment (``app.enrich``)."""

from app.search.base import SearchHit, SearchProvider, create_provider
from app.search.service import SearchOutcome, SearchService

__all__ = [
    "SearchHit",
    "SearchOutcome",
    "SearchProvider",
    "SearchService",
    "create_provider",
]
