"""Source-quality heuristics: flag likely content farms at judging time.

A candidate whose domain looks like a scraper or SEO rewrite — a throwaway TLD
(``suspicious_tlds``) or a generic news-word name (``suspicious_patterns``) — is
*flagged*, never rejected: the relevance checks already said it is on topic, so it
is still added and cited, but marked ``flagged`` in ``enrichment_candidates``,
counted in Settings > Pipeline and badged "source quality uncertain" on the story
page. Review the flagged domains there and move confirmed farms to
``enrichment.quality.deny_domains`` (or the story-page block action);
``DEFAULT_DENY_DOMAINS`` seeds it with farms confirmed in evaluations.
``seo_title_reason`` catches the other farm tell: a trailing ``[20xx]`` year tag
on a comparison/guide title (used by the context link finder).
"""

import re
from collections.abc import Sequence

from app.enrich.domains import registrable_domain

DEFAULT_SUSPICIOUS_TLDS = (
    "edu.pl",  # Polish academia; seen serving English SEO rewrites (qwe.edu.pl)
    *("club", "shop", "online", "site", "xyz", "top", "icu", "info", "biz"),
    *("click", "link", "work", "buzz", "vip", "website", "space", "today", "live"),
)
"""Suffixes commonly used by scraper/SEO domains (name.endswith check)."""

_NEWS_WORDS = ("news", "wire", "daily", "hub", "insider", "feed")

DEFAULT_SUSPICIOUS_PATTERNS = (rf"(^|[.-])({'|'.join(_NEWS_WORDS)})\d*([.-]|$)",)
"""Regexes on the registrable domain: a news-word as a separate token (hyphen/dot
separated or at an edge, digits allowed) — ``tech-insider.org``, ``news24.site``.
Fused names like ``thenews.com.pk``, ``wired.com`` or ``dailynews.com`` do not
match: they are real publications, not keyword-stuffed farm names."""

DEFAULT_DENY_DOMAINS = (
    "shattered.io",  # SEO rewrite templates ("Full Spec Comparison [2026]") seen in the
    # 30-story context eval: 3 accepted links, all same-event coverage
    "finance.biggo.com",  # biggo.com content-farm network (4 accepted links in the eval)
    "vocal.media",  # user-generated content platform, not reported news
    "newscord.org",  # SEO news-site farm ("Elon Musk Pledges One SpaceX Share...")
    "orcarouter.ai",  # SEO blog (AI keyword posts), seen in enrichment + the context eval
)
"""Confirmed content farms hard-denied by default; YAML ``quality.deny_domains``
replaces this list and the DB block list overrides both."""

_SEO_YEAR = re.compile(r"[\[(]20\d\d[\])]\s*$")
"""A ``[2026]`` / ``(2026)`` year tag at the END of the title (SEO freshness dressing)."""

_SEO_WORDS = re.compile(r"\b(comparison|specs?|guide|everything you need)\b", re.IGNORECASE)
"""Words SEO template titles pair with a trailing year tag."""


def seo_title_reason(title: str | None) -> str | None:
    """``title:seo`` when the title follows a farm template — a trailing ``[20xx]`` /
    ``(20xx)`` year tag combined with ``comparison``/``specs``/``guide``/
    ``everything you need`` (``"iPhone vs Pixel Full Spec Comparison [2026]"``)."""
    if title and _SEO_YEAR.search(title) and _SEO_WORDS.search(title):
        return "title:seo"
    return None


def flag_reason(
    url_or_domain: str,
    *,
    tlds: Sequence[str] = DEFAULT_SUSPICIOUS_TLDS,
    patterns: Sequence[str] = DEFAULT_SUSPICIOUS_PATTERNS,
) -> str | None:
    """``tld:<suffix>`` / ``name:<token>`` when the domain looks like a content farm;
    None otherwise. Applied only to candidates that passed every other check."""
    domain = registrable_domain(url_or_domain)
    for tld in tlds:
        if domain == tld or domain.endswith(f".{tld}"):
            return f"tld:{tld}"
    for pattern in patterns:
        match = re.search(pattern, domain)
        if match:
            token = match.group(0).strip("-.")
            return f"name:{token}"[:40]
    return None
