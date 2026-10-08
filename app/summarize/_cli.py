"""Shared plumbing for the smoke / compare / capture commands (they never write to the DB)."""

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import profiles as repo
from app.config import LLMRole, load_config
from app.db import init_db, make_engine, make_session_factory
from app.llm import LLMClient
from app.models import Story
from app.settings import Settings
from app.summarize.fake import FakeLLM
from app.summarize.prompt import SourceDoc
from app.summarize.sources import select_sources

EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "profiles.example.yaml"


def parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--profile", help="profile whose llm.summarizer is used (default: first)")
    p.add_argument(
        "--db",
        help="SQLite file to read stories from (default: DISCOVER_DATABASE_URL or "
        "./data/discover.db; the demo's is .demo/demo.db)",
    )
    p.add_argument("--config", help="profiles YAML (default: DISCOVER_CONFIG_PATH)")
    p.add_argument("--fake", action="store_true", help="use the built-in fake LLM (offline)")
    return p


@dataclass
class Env:
    settings: Settings
    role: LLMRole
    session_factory: sessionmaker
    fake: bool

    def client(self, role: LLMRole | None = None) -> LLMClient:
        transport = FakeLLM().transport() if self.fake else None
        return LLMClient(role or self.role, transport=transport)


def load(args: argparse.Namespace) -> Env:
    overrides = {}
    if args.db:
        overrides["database_url"] = f"sqlite:///{os.path.abspath(args.db).replace(os.sep, '/')}"
    if args.config:
        overrides["config_path"] = args.config
    settings = Settings(scheduler_enabled=False, **overrides)
    config_path = settings.config_path
    if not config_path.exists() and not args.config:
        config_path = EXAMPLE_CONFIG
        print(f"{settings.config_path} not found; using {config_path}", file=sys.stderr)
    config = load_config(config_path)
    profile = config.get_profile(args.profile) if args.profile else config.profiles[0]
    if profile is None:
        sys.exit(f"unknown profile {args.profile!r}")
    engine = make_engine(settings.database_url)
    init_db(engine)
    return Env(settings, profile.llm.summarizer, make_session_factory(engine), args.fake)


@dataclass
class StoryInput:
    story_id: int
    title: str
    sources: list[SourceDoc]


def newest_stories(env: Env, limit: int) -> list[StoryInput]:
    """Newest stories, multi-source ones first (they exercise citations best)."""
    with env.session_factory() as session:
        stmt = select(Story.id).order_by(
            (Story.source_count >= 2).desc(), Story.last_updated_at.desc(), Story.id.desc()
        )
        names = repo.source_names(session)
        stories = []
        for story_id in session.scalars(stmt.limit(limit)):
            item = repo.get_story(session, story_id)
            if item is None:
                continue
            sources = select_sources(
                item.members,
                names,
                max_articles=env.settings.summarize_max_articles_per_story,
                max_words=env.settings.summarize_max_words_per_article,
            )
            stories.append(StoryInput(story_id, item.title, sources))
    if not stories:
        sys.exit(f"no stories in {env.settings.database_url}; run the app or demo first")
    return stories


def marked(summary, sources: list[SourceDoc]) -> str:
    """``Sentence. [1][2]`` from a validated summary."""
    del sources
    return " ".join(
        s + " " + "".join(f"[{i}]" for i in c)
        for s, c in zip(summary.sentences, summary.citations, strict=True)
    )


def utf8_stdout() -> None:
    """Windows consoles default to a legacy code page; summaries contain non-ASCII text."""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
