"""Pick a story's card image from its members' validated images (pure; read-only DB)."""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from urllib.parse import urlsplit

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import ImagesConfig
from app.images.meta import domain_icon_urls
from app.models import Article, ImageAsset, ImageSource


def site_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.")


@dataclass(frozen=True)
class Candidate:
    article_id: int
    url: str
    kind: str
    """``feed`` | ``page``."""
    width: int = 0
    height: int = 0
    validated: bool = False
    generic: bool = False
    tiny_feed: bool = False
    representative: bool = False
    key: str | None = None
    cached: bool = False

    def rank(self) -> tuple:
        return (
            not self.generic,
            self.validated,
            not self.tiny_feed,
            self.width * self.height,
            self.representative,
            self.kind == "page",
            -self.article_id,
        )


def generic_urls(session: Session, urls: Iterable[str], threshold: int) -> set[str]:
    """URLs used by ``threshold`` or more articles of one site (a publisher default)."""
    urls = {u for u in urls if u}
    if not urls:
        return set()
    rows = session.execute(
        select(Article.id, Article.url, Article.image_url, Article.page_image_url).where(
            or_(Article.image_url.in_(urls), Article.page_image_url.in_(urls))
        )
    )
    seen: dict[tuple[str, str], set[int]] = defaultdict(set)
    for aid, page, feed, image in rows:
        for u in {feed, image} & urls:
            seen[(u, site_of(page))].add(aid)
    return {u for (u, _site), ids in seen.items() if len(ids) >= threshold}


def candidates(
    session: Session, members: Sequence[Article], representative_id: int, config: ImagesConfig
) -> list[Candidate]:
    urls = {u for m in members for u in (m.image_url, m.page_image_url) if u}
    assets = (
        {a.url: a for a in session.scalars(select(ImageAsset).where(ImageAsset.url.in_(urls)))}
        if urls
        else {}
    )
    generic = generic_urls(session, urls, config.generic_threshold)
    found = []
    for m in members:
        for kind, url in (("feed", m.image_url), ("page", m.page_image_url)):
            if not url:
                continue
            asset = assets.get(url)
            if asset is not None and asset.status != "ok":
                continue
            if asset is None and (kind == "page" or m.image_checked_at is not None):
                continue  # only an unchecked feed image is shown provisionally
            width = asset.width or 0 if asset else 0
            found.append(
                Candidate(
                    article_id=m.id,
                    url=url,
                    kind=kind,
                    width=width,
                    height=(asset.height or 0) if asset else 0,
                    validated=asset is not None,
                    generic=url in generic or bool(asset and asset.generic),
                    tiny_feed=kind == "feed" and bool(asset) and width < config.tiny_feed_width,
                    representative=m.id == representative_id,
                    key=asset.key if asset else None,
                    cached=bool(asset and asset.cached_bytes),
                )
            )
    return found


def best(found: Sequence[Candidate]) -> Candidate | None:
    return max(found, key=Candidate.rank, default=None)


def logo(session: Session, members: Sequence[Article], representative_id: int) -> dict | None:
    """The first valid publisher icon: the representative's first, the page's declared icon
    before the domain's ``/apple-touch-icon.png`` and ``/favicon.ico``."""
    ordered = sorted(members, key=lambda m: m.id != representative_id)
    urls: list[str] = []
    for m in ordered:
        for u in ([m.icon_url] if m.icon_url else []) + domain_icon_urls(m.url):
            if u not in urls:
                urls.append(u)
    if not urls:
        return None
    assets = {
        a.url: a
        for a in session.scalars(
            select(ImageAsset).where(ImageAsset.url.in_(urls), ImageAsset.status == "ok")
        )
    }
    for u in urls:
        asset = assets.get(u)
        if asset is not None:
            return {
                "url": u,
                "key": asset.key if asset.cached_bytes else None,
                "tint": asset.tint,
                "tint_dark": asset.tint_dark,
            }
    return None


def story_image(
    session: Session, members: Sequence[Article], representative_id: int, config: ImagesConfig
) -> tuple[dict | None, dict | None]:
    """``(image, logo)`` for the published doc. ``image["source"]`` is an ``ImageSource``."""
    choice = best(candidates(session, members, representative_id, config))
    icon = logo(session, members, representative_id)
    if choice is None:
        source = ImageSource.LOGO if icon else ImageSource.NONE
        return {"url": None, "source": source.value}, icon
    source = choice.kind if choice.representative else ImageSource.MEMBER.value
    image = {
        "url": choice.url,
        "key": choice.key if choice.cached else None,
        "source": source,
        "article_id": choice.article_id,
        "width": choice.width or None,
        "height": choice.height or None,
        "generic": choice.generic,
    }
    return image, icon
