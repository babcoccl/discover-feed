"""Shared setup for the context command-line tools (``diagnose`` / ``evaluate``)."""

import math
import sys
import time

from sqlalchemy.orm import sessionmaker

from app.config import load_config
from app.context.service import ContextFinder
from app.db import init_db, make_engine, make_session_factory
from app.settings import get_settings


def build() -> tuple[sessionmaker, ContextFinder]:
    from app.pipeline import context_from_settings

    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    factory = make_session_factory(engine)
    config = load_config(settings.config_path)
    return factory, context_from_settings(factory, settings, config)


def only_providers(finder: ContextFinder, names: list[str] | None) -> None:
    """Restrict the run to these providers (``--provider``); unknown names exit."""
    if not names:
        return
    known = {p.name: p for p in finder.config.providers}
    missing = [n for n in names if n not in known]
    if missing:
        raise SystemExit(
            f"provider(s) not configured: {', '.join(missing)} (configured: "
            f"{', '.join(known) or 'none'})"
        )
    finder.config = finder.config.model_copy(update={"providers": [known[n] for n in names]})


def respect_cooldowns(finder: ContextFinder, *, wait: bool, out=sys.stderr) -> bool:
    """Same cooldown contract as ``app.enrich.cli.respect_cooldowns`` — provider
    cooldowns/budgets are shared with enrichment under ``search:<name>``."""
    providers = finder.config.providers
    cooling = {p.name: r for p in providers if (r := finder.search.cooldown_remaining(p.name)) > 0}
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
    wait_for_cooldowns(finder, out=out)
    return True


def wait_for_cooldowns(finder: ContextFinder, *, out=sys.stderr, sleep=time.sleep) -> float:
    """Sleep until no configured provider is cooling down; returns the seconds waited."""
    waited = 0.0
    for provider in finder.config.providers:
        remaining = finder.search.cooldown_remaining(provider.name)
        if remaining <= 0:
            continue
        seconds = math.ceil(remaining)
        print(f"waiting for {provider.name} (cooling down, {seconds} s) ...", file=out)
        sleep(seconds)
        waited += seconds
    return waited
