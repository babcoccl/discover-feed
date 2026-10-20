"""The context planner: one LLM call per story that picks 2-4 retrieval tasks from a
fixed catalog (``app.context.rules.CONTEXT_TASK_TYPES``). The model chooses task types
and fills slots (subject = a named entity in the story, question = the reader-facing
intent); it never writes raw search queries — ``app.context.catalog`` builds those
deterministically. On any failure the planner falls back to ``history`` +
``related_developments`` for the story's top entity."""

import json
import logging
from dataclasses import dataclass, field

from app.context.rules import CONTEXT_TASK_TYPES

logger = logging.getLogger(__name__)


PLANNER_SCHEMA = {
    "type": "object",
    "properties": {
        "tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(CONTEXT_TASK_TYPES)},
                    "subject": {"type": "string"},
                    "question": {"type": "string"},
                    "priority": {"type": "integer", "enum": [1, 2, 3]},
                },
                "required": ["type", "subject", "question", "priority"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["tasks"],
    "additionalProperties": False,
}

PLANNER_CHARS = 2400
"""Characters of brief text sent to the planner."""


@dataclass
class ContextTask:
    """One planned retrieval task: a catalog type plus the slots the recipes need."""

    type: str
    subject: str
    question: str
    priority: int = 2


@dataclass
class PlannerResult:
    tasks: list[ContextTask] = field(default_factory=list)
    raw: str = ""
    """The model's raw answer (persisted in the trace)."""
    error: str | None = None
    fallback: bool = False


def parse_tasks(payload: dict, *, max_tasks: int, known_subjects: list[str]) -> list[ContextTask]:
    """Validate the planner's JSON: catalog types only, a non-empty subject that names
    something in the story, a one-sentence question, priority 1-3. Invalid entries are
    dropped (never guessed at), the rest are capped at ``max_tasks``."""
    known = {s.casefold() for s in known_subjects if s}
    out: list[ContextTask] = []
    for item in payload.get("tasks") or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        subject = " ".join(str(item.get("subject") or "").split())
        question = " ".join(str(item.get("question") or "").split())
        if kind not in CONTEXT_TASK_TYPES or not subject:
            continue
        if kind != "background" and known and subject.casefold() not in known:
            continue  # the subject must name a story entity (background uses topic terms)
        try:
            priority = int(item.get("priority") or 2)
        except (TypeError, ValueError):
            priority = 2
        out.append(
            ContextTask(
                type=kind,
                subject=subject,
                question=question,
                priority=min(max(priority, 1), 3),
            )
        )
    # one task per (type, subject) pair, best priority first
    seen: set[tuple[str, str]] = set()
    deduped = sorted(out, key=lambda t: t.priority)
    keep = []
    for task in deduped:
        key = (task.type, task.subject.casefold())
        if key in seen:
            continue
        seen.add(key)
        keep.append(task)
    return keep[:max_tasks]


def fallback_tasks(subjects: list[str]) -> list[ContextTask]:
    """No usable planner answer: history + related_developments on the top entity."""
    subject = subjects[0] if subjects else ""
    return [
        ContextTask(
            type="history",
            subject=subject,
            question="What led up to this story?",
            priority=1,
        ),
        ContextTask(
            type="related_developments",
            subject=subject,
            question="What else has happened around this recently?",
            priority=2,
        ),
    ]


def _prompt(title: str, brief: str, digests: list[str], subjects: list[str]) -> str:
    lines = [
        f"Story: {title}",
        f"Named entities: {', '.join(subjects) if subjects else '(none)'}",
        "",
        f"Brief:\n{brief[:PLANNER_CHARS]}",
    ]
    if digests:
        lines.append("")
        lines.append("Per-source digests:")
        for d in digests[:4]:
            lines.append(f"- {d[:400]}")
    return "\n".join(lines)


SYSTEM = (
    "You plan background research for a news reader. Pick the most useful tasks from "
    "this fixed catalog only: history (earlier reporting on the subject before this "
    "story), quotes (what a named person said), financials (funding, valuation, "
    "revenue, market for a company), related_developments (recent adjacent news), "
    "background (reference/explainer pages on the topic). For each task give: the "
    "catalog type, the subject (a named entity from the story — never a topic word "
    "except for background), one sentence stating what the reader wants to know, and "
    "priority 1-3 (1 = most valuable). Pick only tasks with a real chance of finding "
    "verifiable page evidence; do not invent subjects."
)


async def plan_tasks(
    title: str,
    brief: str,
    digests: list[str],
    subjects: list[str],
    *,
    max_tasks: int,
    llm_role=None,
    llm_transport=None,
) -> PlannerResult:
    """One LLM call (or none when no role is configured -> the heuristic fallback)."""
    result = PlannerResult()
    if llm_role is None:
        result.tasks = fallback_tasks(subjects)
        result.fallback = True
        return result
    from app.llm.client import LLMClient

    role = llm_role.model_copy(update={"disable_thinking": True, "max_tokens": 500})
    client = LLMClient(role, transport=llm_transport)
    try:
        chat = await client.chat(
            [
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": _prompt(title, brief, digests, subjects)},
            ],
            json_schema=PLANNER_SCHEMA,
            schema_name="context_planner",
        )
        result.raw = chat.content[:2000]
        data = json.loads(chat.content)
    except Exception as exc:  # any LLM/parse failure -> deterministic fallback
        logger.info("context planner failed (%s); using fallback tasks", exc)
        result.error = f"{type(exc).__name__}: {exc}"[:300]
        result.tasks = fallback_tasks(subjects)
        result.fallback = True
        return result
    result.tasks = parse_tasks(
        data if isinstance(data, dict) else {},
        max_tasks=max_tasks,
        known_subjects=subjects,
    )
    if not result.tasks:
        result.tasks = fallback_tasks(subjects)
        result.fallback = True
    return result
