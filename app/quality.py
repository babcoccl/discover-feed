"""Generation quality metrics from attempt rows: first-attempt pass rate, attempts per
success, end-to-end seconds per story and failure reasons (Settings > Pipeline and
``python -m app.summarize.compare``). Pure functions; no I/O, no summarization code."""

import statistics
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

KINDS = ("brief", "report")


@dataclass
class KindQuality:
    runs: int = 0
    """Generations (one brief or report request chain each)."""
    first_pass: int = 0
    """Generations whose first answer passed validation."""
    attempts_to_success: list[int] = field(default_factory=list)
    failures: Counter = field(default_factory=Counter)
    """Failed attempts by reason (``validation:verbatim_overlap``, ``timeout``...)."""

    @property
    def first_pass_rate(self) -> float | None:
        return self.first_pass / self.runs if self.runs else None

    @property
    def median_attempts(self) -> float | None:
        return statistics.median(self.attempts_to_success) if self.attempts_to_success else None

    def top_failures(self, n: int = 5) -> list[tuple[str, int]]:
        return self.failures.most_common(n)


@dataclass
class Quality:
    kinds: dict[str, KindQuality]
    story_seconds: float | None
    """Median seconds per story of model time, brief + report."""
    stories: int

    @property
    def empty(self) -> bool:
        return not any(k.runs or k.failures for k in self.kinds.values())


def quality(attempts: Iterable[Mapping[str, Any]]) -> Quality:
    """Metrics from attempt entries in request order (``GenerationAttempt`` rows or
    ``Generation.attempt_log`` dicts with ``story_id``). Entries share a ``run_id`` per
    generation; ``prompt`` is what was asked (a brief's summary fallback is ``summary``)."""
    kinds = {k: KindQuality() for k in KINDS}
    runs: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for a in attempts:
        kind = a.get("kind")
        if kind not in kinds:
            continue
        if a.get("outcome") == "failed" and a.get("reason"):
            kinds[kind].failures[a["reason"]] += 1
        if a.get("run_id"):
            runs[a["run_id"]].append(a)
    story_runs: dict[Any, dict[str, float]] = defaultdict(dict)
    for entries in runs.values():
        kind = entries[0]["kind"]
        main = [e for e in entries if (e.get("prompt") or kind) == kind]
        if not main:
            continue
        q = kinds[kind]
        q.runs += 1
        q.first_pass += main[0].get("outcome") == "ok"
        ok = next((n for n, e in enumerate(main, 1) if e.get("outcome") == "ok"), None)
        if ok is not None:
            q.attempts_to_success.append(ok)
        if entries[0].get("story_id") is not None:
            seconds = sum((e.get("latency_ms") or 0) for e in entries) / 1000
            story_runs[entries[0]["story_id"]][kind] = seconds  # the newest run wins
    totals = [sum(v.values()) for v in story_runs.values() if "brief" in v]
    return Quality(
        kinds=kinds,
        story_seconds=round(statistics.median(totals), 1) if totals else None,
        stories=len(totals),
    )


def percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"
