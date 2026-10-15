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
    """The providers' cooldowns are shared with the app (``search_provider_state``): with
    every provider cooling down, refuse (or ``--wait``) instead of sending requests that
    would only earn another 429; one cooling provider is skipped (its fallback serves) or,
    with ``--wait``, waited for. Pacing (``min_interval_seconds``) is waited out
    automatically."""
    providers = enricher.config.providers
    cooling = {
        p.name: r for p in providers if (r := enricher.search.cooldown_remaining(p.name)) > 0
    }
    if not cooling:
        return True
    if not wait and len(cooling) == len(providers):
        for name, remaining in cooling.items():
            print(
                f"{name} is cooling down for {math.ceil(remaining)} s after a rate limit "
                "(shared with the app). Try again then, or pass --wait.",
                file=out,
            )
        return False
    if not wait:
        for name, remaining in cooling.items():
            print(
                f"{name} is cooling down for {math.ceil(remaining)} s (shared with the app); "
                "its searches are skipped (pass --wait to wait for it).",
                file=out,
            )
        return True
    wait_for_cooldowns(enricher, out=out)
    return True


def wait_for_cooldowns(enricher: Enricher, *, out=sys.stderr, sleep=time.sleep) -> float:
    """Sleep until no configured provider is cooling down; returns the seconds waited."""
    waited = 0.0
    for provider in enricher.config.providers:
        remaining = enricher.search.cooldown_remaining(provider.name)
        if remaining <= 0:
            continue
        seconds = math.ceil(remaining)
        print(f"waiting for {provider.name} (cooling down, {seconds} s) ...", file=out)
        sleep(seconds)
        waited += seconds
    return waited
