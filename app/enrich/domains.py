"""Registrable domains and the social / aggregator hosts enrichment never adds."""

from urllib.parse import urlsplit

_MULTI_PART_SUFFIXES = frozenset(
    {
        *("co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "ltd.uk", "plc.uk", "net.uk"),
        *("com.au", "net.au", "org.au", "gov.au", "edu.au"),
        *("co.nz", "org.nz", "govt.nz", "co.za", "org.za", "gov.za"),
        *("co.jp", "or.jp", "ne.jp", "ac.jp", "go.jp", "co.kr", "or.kr"),
        *("com.br", "com.ar", "com.mx", "com.co", "com.tr", "com.sg", "com.hk", "com.tw"),
        *("com.cn", "com.my", "com.ph", "com.pk", "com.ng", "com.eg", "com.sa"),
        *("co.in", "net.in", "org.in", "gov.in", "co.il", "org.il", "co.id", "or.id"),
    }
)

SOCIAL = frozenset(
    {
        *("twitter.com", "x.com", "t.co", "facebook.com", "fb.com", "instagram.com"),
        *("threads.net", "threads.com", "tiktok.com", "youtube.com", "youtu.be"),
        *("reddit.com", "redd.it", "linkedin.com", "bsky.app", "bsky.social"),
        *("mastodon.social", "mastodon.online", "mstdn.social", "mstdn.jp", "mas.to"),
        *("fosstodon.org", "hachyderm.io", "infosec.exchange", "pawoo.net"),
        *("t.me", "telegram.org", "pinterest.com", "tumblr.com", "snapchat.com"),
        *("discord.com", "vk.com", "weibo.com", "quora.com"),
        *("medium.com", "substack.com", "truthsocial.com", "rumble.com", "twitch.tv"),
    }
)

_SOCIAL_HOST_PARTS = ("mastodon.", "mstdn.")
"""Any host containing one of these counts as a fediverse (mastodon-style) host —
instances live on arbitrary domains, so no fixed list covers them."""

AGGREGATORS = frozenset(
    {
        *("news.google.com", "google.com", "news.yahoo.com", "yahoo.com", "msn.com"),
        *("flipboard.com", "news.ycombinator.com", "ycombinator.com", "apple.news"),
        *("newsbreak.com", "ground.news", "allsides.com", "feedly.com", "inoreader.com"),
        *("digg.com", "smartnews.com", "newsnow.co.uk", "drudgereport.com", "memeorandum.com"),
        *("techmeme.com", "bing.com", "duckduckgo.com", "archive.org", "archive.ph"),
        *("newsnow.com", "pressreader.com", "headtopics.com", "biztoc.com", "newsable.com"),
        *("daily.dev", "app.daily.dev"),
    }
)


def host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".").removeprefix("www.")


def registrable_domain(value: str) -> str:
    """``"https://edition.cnn.com/x"`` / ``"news.bbc.co.uk"`` -> ``"cnn.com"`` / ``"bbc.co.uk"``
    (a suffix heuristic: no public-suffix-list dependency)."""
    name = host(value) if "/" in value else value.lower().rstrip(".").removeprefix("www.")
    labels = [p for p in name.split(".") if p]
    if len(labels) <= 2:
        return ".".join(labels)
    if ".".join(labels[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _matches(name: str, domains) -> bool:
    return any(name == d or name.endswith(f".{d}") for d in domains)


def canonical_domain(value: str, aliases: dict[str, str] | None = None) -> str:
    """The publisher's canonical domain: the registrable domain, then mapped through
    ``aliases`` so editions of the same publisher (``bbc.co.uk`` / ``bbc.com``,
    ``guardian.co.uk`` / ``theguardian.com``) count as one. Subdomains (``amp.``,
    ``m.``, ``uk.``) already collapse to the registrable domain."""
    domain = registrable_domain(value)
    if not aliases:
        return domain
    return aliases.get(domain, domain)


def blocked_reason(
    url: str,
    deny: list[str] | tuple[str, ...] = (),
    *,
    social=(),
    aggregators=(),
) -> str | None:
    """``social`` / ``aggregator`` / ``denied`` when enrichment must not use ``url``.
    ``social`` / ``aggregators``: host lists that extend the built-ins."""
    name = host(url)
    if _matches(name, deny):
        return "denied"
    if (
        _matches(name, SOCIAL)
        or _matches(name, social)
        or any(part in name for part in _SOCIAL_HOST_PARTS)
    ):
        return "social"
    if _matches(name, AGGREGATORS) or _matches(name, aggregators):
        return "aggregator"
    return None
