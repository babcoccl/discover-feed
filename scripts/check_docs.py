#!/usr/bin/env python3
"""Fail if the docs site drops anything the old README documented.

Two checks:

1. Coverage of the pre-MkDocs README (``tests/fixtures/old-readme.md``): every
   ``DISCOVER_*`` variable, ``python -m ...`` command, ``/api/...`` endpoint and
   config key mentioned there must appear somewhere under ``docs/``.
2. Configuration reference completeness: every field of ``app.settings.Settings``
   (as ``DISCOVER_<FIELD>``) and of the enrichment / context / search-provider /
   summaries / alerts / images config models must appear in
   ``docs/configuration/reference.md``.

Exits non-zero and prints each missing item.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from pathlib import Path
from typing import Any, get_args, get_origin

ROOT = Path(__file__).resolve().parents[1]
DOCS_DIR = ROOT / "docs"
OLD_README = ROOT / "tests" / "fixtures" / "old-readme.md"
REFERENCE = DOCS_DIR / "configuration" / "reference.md"

# Tokens in the old README that are prose/code, not config keys or endpoints a
# reader needs to look up. Keep small; everything else must be documented.
IGNORE_TOKENS = {
    "localhost:8000",
    "127.0.0.1:8000",
    "python",
    "app.enrich.quality",  # python module path, not a config key
    "image_assets.reason",  # stale name in the old README; the table is `images`
}
# Fields documented under a different spelling or intentionally not user-facing.
IGNORE_FIELDS: set[str] = set()


def doc_corpus() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted(DOCS_DIR.rglob("*.md")))


def readme_tokens(text: str, field_names: set[str]) -> dict[str, set[str]]:
    """Pull the things the old README mentioned that docs must keep covering."""
    env = set(re.findall(r"DISCOVER_[A-Z0-9_]+", text))
    env |= set(
        re.findall(
            r"\b(?:LOCAL_LLM|TINYFISH|GUARDIAN|NEWSDATA|X_|SMTP_|"
            r"WEBHOOK|NTFY_|BRAVE_|SEARXNG)[A-Z0-9_]*\b",
            text,
        )
    )

    modules = set(re.findall(r"python3? -m ([a-zA-Z_][\w.]*)", text))
    modules |= {f"scripts/{m}" for m in re.findall(r"scripts/([\w.]+\.py)", text)}

    endpoints = set()
    for m in re.findall(r"/api/[A-Za-z0-9_/{}/=&?%:.-]+", text):
        endpoints.add(m.rstrip(".,;:*"))

    keys: set[str] = set()
    for tok in re.findall(r"`([^`\n]+)`", text):
        tok = tok.strip()
        if re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z0-9_*]+)+", tok) or tok in field_names:
            keys.add(tok)
    return {"env": env, "commands": modules, "endpoints": endpoints, "config keys": keys}


def _fields(model: Any) -> list[tuple[str, Any]]:
    if dataclasses.is_dataclass(model):
        return [(f.name, f.type) for f in dataclasses.fields(model)]
    return [(n, f.annotation) for n, f in model.model_fields.items()]


def _unwrap(ann: Any) -> Any:
    for a in get_args(ann):
        if isinstance(a, type) and a is not type(None):
            return a
    return ann


def collect_fields(model: Any, seen: set[int] | None = None) -> set[str]:
    """All field names of a pydantic model / dataclass, recursing into nested ones."""
    seen = seen or set()
    if id(model) in seen:
        return set()
    seen.add(id(model))
    names: set[str] = set()
    for name, ann in _fields(model):
        names.add(name)
        sub = _unwrap(ann)
        if isinstance(sub, type) and (
            dataclasses.is_dataclass(sub) or hasattr(sub, "model_fields")
        ):
            names |= collect_fields(sub, seen)
        elif get_origin(sub) in (list, dict, tuple):
            for a in get_args(sub):
                a = _unwrap(a)
                if isinstance(a, type) and (
                    dataclasses.is_dataclass(a) or hasattr(a, "model_fields")
                ):
                    names |= collect_fields(a, seen)
    return names


def main() -> int:
    from app import config as cfg
    from app.context.rules import SameEventRules
    from app.settings import Settings

    models = [
        cfg.EnrichmentConfig,
        cfg.ExtraRulesConfig,
        cfg.EnrichmentQualityConfig,
        cfg.SearchProviderConfig,
        cfg.ContextConfig,
        cfg.ContextActionConfig,
        SameEventRules,
        cfg.SummariesConfig,
        cfg.AlertsConfig,
        cfg.AlertRule,
        cfg.AlertChannel,
        cfg.AlertKeyword,
        cfg.QuietHours,
        cfg.ImagesConfig,
        cfg.PerspectivesConfig,
    ]
    model_fields: set[str] = set()
    for m in models:
        model_fields |= collect_fields(m)
    model_fields -= IGNORE_FIELDS

    corpus = doc_corpus()
    ref_text = REFERENCE.read_text(encoding="utf-8")

    missing: list[str] = []

    # 1. Old README coverage.
    groups = readme_tokens(OLD_README.read_text(encoding="utf-8"), model_fields)
    for kind, tokens in groups.items():
        for tok in sorted(tokens):
            if tok in IGNORE_TOKENS:
                continue
            if tok not in corpus:
                missing.append(f"{kind}: {tok}")

    # 2. Settings fields -> env var name; config fields -> reference.md.
    for name in sorted(Settings.model_fields):
        env = f"DISCOVER_{name.upper()}"
        if env not in corpus and name not in corpus:
            missing.append(f"settings field: {name} (env {env})")
    for name in sorted(model_fields):
        if name not in ref_text:
            missing.append(f"config field: {name}")

    if missing:
        print(f"check_docs: {len(missing)} undocumented item(s):")
        for m in missing:
            print(f"  - {m}")
        return 1
    print(
        f"check_docs: ok ({sum(len(v) for v in groups.values())} README tokens, "
        f"{len(model_fields)} config fields)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
