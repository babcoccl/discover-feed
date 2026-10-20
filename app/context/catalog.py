"""The retrieval catalog: one deterministic recipe per planner task type
(``app.context.rules.CONTEXT_TASK_TYPES``). A recipe turns the planner's slots
(subject, question — the question is reader intent for the extractor, never a query)
into a small list of ``RetrievalQuery`` objects: query text plus the provider filters
(window, domain_type, exclusions). The LLM never touches these."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.config import ContextActionConfig, ContextConfig

PAGES_PER_QUERY = 2
"""Hits kept per query (spec: at most 2 pages per query)."""

# default recent windows per type (days back from `now`)
RECENT_DAYS = {"quotes": 90, "financials": 180, "related_developments": 30}
HISTORY_MIN_AGE_DAYS = 3
"""A dated page must be at least this much older than the story to count as history."""

FINANCIAL_TERMS = "funding OR valuation OR revenue OR market"


@dataclass
class RetrievalQuery:
    """One search a recipe wants run: text plus TinyFish-style filters. ``window``
    distinguishes archive queries (older than the story's recent past) from current
    web queries — it decides the item's ``origin`` (archive|web)."""

    task_type: str
    subject: str
    question: str
    query: str
    after: datetime | None = None
    before: datetime | None = None
    domain_type: str = "news"
    pub_year_min: int | None = None
    pub_year_max: int | None = None
    exclude: tuple[str, ...] = ()
    window: str = "recent"  # recent | earlier | long_ago | none

    @property
    def archive(self) -> bool:
        return self.window in ("earlier", "long_ago")


@dataclass
class TaskQueries:
    task_type: str
    subject: str
    question: str
    queries: list[RetrievalQuery] = field(default_factory=list)


def _terms(terms: list[str], subject: str, n: int) -> list[str]:
    return [t for t in terms if t.casefold() != subject.casefold()][:n]


def _recent(after_days: int, now: datetime) -> tuple[datetime, None]:
    return now - timedelta(days=after_days), None


def recipes(
    task,
    *,
    terms: list[str],
    own_domains: tuple[str, ...],
    first_at: datetime,
    now: datetime,
    cfg: ContextConfig,
) -> TaskQueries:
    """The queries for one planned task. ``task`` is a ``planner.ContextTask``; ``terms``
    are the story's topic terms; ``own_domains`` the story's own publisher domains
    (excluded for history/related_developments so context never cites its coverage)."""
    action = cfg.actions.get(task.type) or ContextActionConfig()
    if not action.enabled:
        return TaskQueries(task.type, task.subject, task.question)
    fn = _RECIPES[task.type]
    queries = fn(task, terms, own_domains, first_at, now, cfg, action)
    if not cfg.archive:
        queries = [q for q in queries if not q.archive]
    return TaskQueries(task.type, task.subject, task.question, queries)


def _history(task, terms, own, first_at, now, cfg, action) -> list[RetrievalQuery]:
    """Earlier reporting on the subject: three windows before the story — recent
    (first_at-30d .. first_at-3d), earlier (180d..30d back), long-ago (730d..180d) —
    news first, then web, excluding the story's own domains."""
    base = " ".join([task.subject, *_terms(terms, task.subject, 2)]).strip()
    windows = [
        ("recent", first_at - timedelta(days=30), first_at - timedelta(days=HISTORY_MIN_AGE_DAYS)),
        ("earlier", first_at - timedelta(days=180), first_at - timedelta(days=30)),
        ("long_ago", first_at - timedelta(days=730), first_at - timedelta(days=180)),
    ]
    out = [
        RetrievalQuery(
            task_type="history",
            subject=task.subject,
            question=task.question,
            query=base,
            after=after,
            before=before,
            domain_type="news",
            exclude=own,
            window=name,
        )
        for name, after, before in windows
    ]
    out += [
        RetrievalQuery(
            task_type="history",
            subject=task.subject,
            question=task.question,
            query=base,
            after=after,
            before=before,
            domain_type="web",
            exclude=own,
            window=name,
        )
        for name, after, before in windows
    ]
    return out


def _quotes(task, terms, own, first_at, now, cfg, action) -> list[RetrievalQuery]:
    """What the named person said: ``"Person"`` + one topic term, last 90 days, news."""
    days = action.window_days or RECENT_DAYS["quotes"]
    after, _ = _recent(days, now)
    query = " ".join([f'"{task.subject}"', *_terms(terms, task.subject, 1)]).strip()
    return [
        RetrievalQuery(
            task_type="quotes",
            subject=task.subject,
            question=task.question,
            query=query,
            after=after,
            domain_type="news",
            window="recent",
        )
    ]


def _financials(task, terms, own, first_at, now, cfg, action) -> list[RetrievalQuery]:
    """Company money facts: ``"Company"`` + funding/valuation/revenue/market, 180d."""
    days = action.window_days or RECENT_DAYS["financials"]
    after, _ = _recent(days, now)
    return [
        RetrievalQuery(
            task_type="financials",
            subject=task.subject,
            question=task.question,
            query=f'"{task.subject}" {FINANCIAL_TERMS}',
            after=after,
            domain_type="news",
            window="recent",
        )
    ]


def _related(task, terms, own, first_at, now, cfg, action) -> list[RetrievalQuery]:
    """Adjacent recent news on the subject, excluding the story's own domains."""
    days = action.window_days or RECENT_DAYS["related_developments"]
    after, _ = _recent(days, now)
    base = " ".join([task.subject, *_terms(terms, task.subject, 2)]).strip()
    return [
        RetrievalQuery(
            task_type="related_developments",
            subject=task.subject,
            question=task.question,
            query=base,
            after=after,
            domain_type="news",
            exclude=own,
            window="recent",
        )
    ]


def _background(task, terms, own, first_at, now, cfg, action) -> list[RetrievalQuery]:
    """Reference/explainer material on the topic: undated web search, or
    research_paper (pub_year_min/max) when the action opts into it."""
    query = " ".join(_terms(terms, task.subject, 2) or terms[:2]).strip()
    if action.research:
        return [
            RetrievalQuery(
                task_type="background",
                subject=task.subject,
                question=task.question,
                query=query,
                domain_type="research_paper",
                pub_year_min=now.year - 10,
                pub_year_max=now.year,
                window="none",
            )
        ]
    return [
        RetrievalQuery(
            task_type="background",
            subject=task.subject,
            question=task.question,
            query=query,
            domain_type="web",
            window="none",
        )
    ]


_RECIPES = {
    "history": _history,
    "quotes": _quotes,
    "financials": _financials,
    "related_developments": _related,
    "background": _background,
}
