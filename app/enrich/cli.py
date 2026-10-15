"""Shared setup for the enrichment command-line tools (``diagnose`` / ``evaluate``)."""

import math
import sys
import time

from sqlalchemy.orm import sessionmaker

from app.config import load_config
from app.db import init_db, make_engine, make_session_factory
from app.enrich.service import Enricher
from app.settings import get_settings


def build() -> tuple[sessionmaker, Enricher]:
    from app.pipeline import enricher_from_settings

    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    config = load_config(settings.config_path)
    return factory, enricher_from_settings(factory, settings, config)


def only_providers(enricher: Enricher, names: list[str] | None) -> None:
    """Restrict the run to these providers (``--provider``); unknown names exit."""
    if not names:
        return
    known = {p.name: p for p in enricher.config.providers}
    missing = [n for n in names if n not in known]
    if missing:
        raise SystemExit(
            f"provider(s) not configured: {', '.join(missing)} (configured: "
            f"{', '.join(known) or 'none'})"
        )
    enricher.config = enricher.config.model_copy(update={"providers": [known[n] for n in names]})


def respect_cooldowns(enricher: Enricher, *, wait: bool, out=sys.stderr) -> bool:
    """The providers' cooldowns are shared with the app (``search_provider_state``): refuse
    (or ``--wait``) instead of sending a request that would only earn another 429. Pacing
    (``min_interval_seconds``) is waited out automatically."""
    for provider in enricher.config.providers:
        remaining = enricher.search.cooldown_remaining(provider.name)
        if remaining <= 0:
            continue
        seconds = math.ceil(remaining)
        if not wait:
            print(
                f"{provider.name} is cooling down for {seconds} s after a rate limit "
                "(shared with the app). Try again then, or pass --wait.",
                file=out,
            )
            return False
        print(f"{provider.name} is cooling down; waiting {seconds} s ...", file=out)
        time.sleep(seconds)
    return True
