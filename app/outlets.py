"""Outlet metadata for enrichment coverage display and diversity.

``config/outlets.yaml`` is a git-tracked starter list of widely known outlets:
``name``, ``domains`` (all editions), ``country`` (ISO 3166-1 alpha-2), ``kind``
(wire | national | local | trade | international | state | blog) and ``groups``
(free-form, user-defined tags — the list asserts no political bias). Enrichment
uses it to label members in the Coverage section, the API and the summarize
source diversity rule; unknown domains get ``kind = unknown`` and a country
guessed from the public suffix where possible.

The registry is a process-wide singleton (``outlets()``): the YAML path comes
from ``DISCOVER_OUTLETS_PATH`` or defaults to ``config/outlets.yaml`` beside the
repo root. A missing or empty file means no labels — everything is unknown.
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config" / "outlets.yaml"
PATH_ENV = "DISCOVER_OUTLETS_PATH"

KINDS = ("wire", "international", "national", "local", "trade", "state", "blog", "unknown")
"""Display order for the Coverage section."""

_COUNTRY_TLDS = {
    "ac": "AC",
    "ae": "AE",
    "ar": "AR",
    "at": "AT",
    "au": "AU",
    "be": "BE",
    "bg": "BG",
    "br": "BR",
    "ca": "CA",
    "ch": "CH",
    "cl": "CL",
    "cn": "CN",
    "co": "CO",
    "cz": "CZ",
    "de": "DE",
    "dk": "DK",
    "ee": "EE",
    "eg": "EG",
    "es": "ES",
    "eu": "EU",
    "fi": "FI",
    "fr": "FR",
    "gr": "GR",
    "hk": "HK",
    "hr": "HR",
    "hu": "HU",
    "id": "ID",
    "ie": "IE",
    "il": "IL",
    "in": "IN",
    "ir": "IR",
    "is": "IS",
    "it": "IT",
    "jp": "JP",
    "ke": "KE",
    "kr": "KR",
    "lt": "LT",
    "lu": "LU",
    "lv": "LV",
    "mx": "MX",
    "my": "MY",
    "ng": "NG",
    "nl": "NL",
    "no": "NO",
    "nz": "NZ",
    "pe": "PE",
    "ph": "PH",
    "pk": "PK",
    "pl": "PL",
    "pt": "PT",
    "ro": "RO",
    "rs": "RS",
    "ru": "RU",
    "sa": "SA",
    "se": "SE",
    "sg": "SG",
    "si": "SI",
    "sk": "SK",
    "th": "TH",
    "tr": "TR",
    "tw": "TW",
    "ua": "UA",
    "uk": "GB",
    "us": "US",
    "uy": "UY",
    "ve": "VE",
    "vn": "VN",
    "za": "ZA",
}
"""Public suffixes that name a country (``bbc.co.uk`` -> GB, ``spiegel.de`` -> DE)."""


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def country_from_tld(domain: str) -> str | None:
    """Country code from a country-code TLD (``elmundo.es`` -> ES); None elsewhere."""
    for part in reversed(domain.split(".")):
        if part in _COUNTRY_TLDS:
            return _COUNTRY_TLDS[part]
    return None


@dataclass
class Outlet:
    name: str
    domains: list[str] = field(default_factory=list)
    country: str | None = None
    kind: str = "unknown"
    groups: list[str] = field(default_factory=list)


class OutletRegistry:
    """Loaded ``config/outlets.yaml``: domain (incl. subdomain suffix) -> Outlet."""

    def __init__(self, outlets: list[Outlet]) -> None:
        self.outlets = outlets
        self._by_domain: dict[str, Outlet] = {}
        for o in outlets:
            for d in o.domains:
                self._by_domain.setdefault(d, o)

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PATH) -> "OutletRegistry":
        path = Path(path)
        if not path.exists():
            logger.info("no outlets file at %s; coverage labels will be unknown", path)
            return cls([])
        try:
            rows = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid outlets file {path}: {exc}") from None
        out = []
        seen_domains: dict[str, str] = {}
        for row in rows.get("outlets") or []:
            if not isinstance(row, dict) or not row.get("name"):
                raise ValueError(f"invalid outlets entry {row!r}: needs at least name")
            name = str(row["name"])
            kind = str(row.get("kind") or "unknown").lower()
            if kind not in KINDS:
                raise ValueError(f"outlet {name!r}: unknown kind {kind!r} ({'/'.join(KINDS)})")
            country = (row.get("country") or None) and str(row["country"]).strip().upper()
            domains = [domain_of(f"https://{d}/") for d in row.get("domains") or []]
            for d in domains:
                other = seen_domains.get(d)
                if other is not None:
                    raise ValueError(f"domain {d!r} listed for both {other!r} and {name!r}")
                seen_domains[d] = name
            groups = [str(g).strip() for g in row.get("groups") or [] if str(g).strip()]
            out.append(
                Outlet(
                    name=name,
                    domains=domains,
                    country=country
                    or next((country_from_tld(d) for d in domains if country_from_tld(d)), None),
                    kind=kind,
                    groups=groups,
                )
            )
        return cls(out)

    def for_url(self, url: str) -> Outlet:
        """The listed outlet for ``url``, or a synthesized unknown one."""
        host = domain_of(url)
        return self.for_domain(host, url)

    def for_domain(self, domain: str, url: str = "") -> Outlet:
        host = domain.lower().removeprefix("www.")
        parts = host.split(".")
        for i in range(len(parts)):
            hit = self._by_domain.get(".".join(parts[i:]))
            if hit is not None:
                return hit
        return Outlet(
            name=self.name_of(url) if url else host,
            domains=[host] if host else [],
            country=country_from_tld(host),
            kind="unknown",
        )

    @staticmethod
    def name_of(url: str) -> str:
        """A readable fallback name for an unknown host (``news.express.co.uk`` ->
        ``express.co.uk``), so chips show a domain rather than a raw URL."""
        from app.enrich.domains import registrable_domain

        return registrable_domain(url) or url

    def group_domains(self, groups: list[str]) -> dict[str, list[str]]:
        """``{group: [domains]}`` for the configured names (empty entries for unknown
        groups so the caller can log them once)."""
        wanted = set(groups)
        return {
            g: [d for o in self.outlets if g in o.groups for d in o.domains]
            for g in groups
            if g in wanted
        }

    def groups_covered(self, outlets: list[Outlet], groups: list[str]) -> set[str]:
        """The configured groups that already have at least one listed outlet."""
        return {g for o in outlets for g in o.groups if g in groups}


_registry: OutletRegistry | None = None


def outlets() -> OutletRegistry:
    """The process-wide registry (reads ``DISCOVER_OUTLETS_PATH`` or the repo file once)."""
    global _registry
    if _registry is None:
        path = os.environ.get(PATH_ENV, "")
        _registry = OutletRegistry.load(Path(path) if path else DEFAULT_PATH)
    return _registry


def _reset() -> None:  # tests only
    global _registry
    _registry = None
