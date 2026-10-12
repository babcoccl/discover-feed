"""Report quality: the verbatim guard, retries, whitespace, glued words, the report source
budget and the quality metrics."""

import asyncio
import json

import pytest

from app.llm import LLMClient
from app.quality import quality
from app.summarize.fake import FakeLLM
from app.summarize.prompt import SourceDoc
from app.summarize.service import Summarizer
from app.summarize.sources import share_budget
from app.summarize.validate import (
    SummaryInvalid,
    VerbatimGuard,
    glued_words,
    parse_json,
    validate_brief,
    validate_report,
)
from tests.test_briefs_reports import LONG, LONG2, demo  # noqa: F401  (fixture)
from tests.test_summarizer import ROLE, TEXT_A, TEXT_B


def _word(*parts: int) -> str:
    """A unique made-up word without digits (digits would count as numbers)."""
    return "q" + "z".join("".join(chr(97 + int(d)) for d in str(n)) for n in parts)


def _filler(p: int, n: int) -> str:
    return " ".join(_word(p, i) for i in range(n))


def _report(paragraphs: list[str]) -> str:
    return json.dumps({"paragraphs": [{"text": t, "citations": [1]} for t in paragraphs]})


DIAGNOSTIC_SOURCE = (
    "The new chips target agentic AI, which needs multi-step execution, long-context "
    "decoding, and tool use. The design increases the system's compute FLOPs by a wide margin."
)


def test_diagnostic_phrases_pass_the_guard() -> None:
    phrases = [
        "Agents rely on multi-step execution, long-context decoding, and tool use.",
        "The vendor says the design increases the system's compute FLOPs by a wide margin.",
    ]
    paragraphs = [f"{_filler(p, 70)} {phrases[p % 2]}" for p in range(4)]
    report = validate_report(_report(paragraphs), [DIAGNOSTIC_SOURCE], duplicate_threshold=0.99)
    assert len(report.paragraphs) == 4


def test_copied_run_of_15_words_fails() -> None:
    copied = "planted trees along the canal path on a cold morning, and organisers said the rowing"
    paragraphs = [_filler(p, 80) for p in range(4)]
    paragraphs[2] += " Volunteers " + copied + "."
    with pytest.raises(SummaryInvalid, match=r"\(15 words\) word for word") as err:
        validate_report(_report(paragraphs), [LONG2])
    assert err.value.phrases == (copied.replace(",", ""),)


def test_title_and_allow_list_words_are_exempt() -> None:
    title = "Planted trees along the canal path on a cold morning and organisers said the rowing"
    copied = title.lower()
    paragraphs = [_filler(p, 80) for p in range(4)]
    paragraphs[1] += " " + copied + "."
    with pytest.raises(SummaryInvalid):
        validate_report(_report(paragraphs), [LONG2])
    assert validate_report(_report(paragraphs), [LONG2], title=title)
    guard = VerbatimGuard(allow=(copied,))
    assert validate_report(_report(paragraphs), [LONG2], guard=guard)


def test_hyphens_case_and_punctuation_do_not_hide_a_copy() -> None:
    source = "The long-context model handles twelve separate tasks with great care and patience"
    paragraphs = [_filler(p, 80) for p in range(4)]
    paragraphs[0] += (
        " THE LONG CONTEXT MODEL: handles twelve separate tasks, with great care and patience."
    )
    with pytest.raises(SummaryInvalid, match="word for word"):
        validate_report(_report(paragraphs), [source])


def test_report_with_more_than_20_percent_copied_phrases_fails() -> None:
    sentences = [s.replace(",", "").split() for s in LONG2.splitlines()]
    paragraphs = [
        " ".join(
            " ".join(sentences[j][p * 4 : p * 4 + 10]) + f" {_word(p, j, 1)} {_word(p, j, 2)}"
            for j in range(8)
        )
        for p in range(4)
    ]
    with pytest.raises(SummaryInvalid, match="phrases appear in the sources") as err:
        validate_report(_report(paragraphs), [LONG2], duplicate_threshold=0.99)
    assert err.value.phrases and all(len(p.split()) == 10 for p in err.value.phrases)
    loose = VerbatimGuard(max_ratio=0.5)
    assert validate_report(_report(paragraphs), [LONG2], duplicate_threshold=0.99, guard=loose)


@pytest.mark.parametrize("raw", ["\n", "\r\n", "\t", "\n\n  "])
def test_control_characters_inside_strings_keep_word_boundaries(raw: str) -> None:
    content = (
        '{"lead": "Acme Robotics raised $40 million for its AI' + raw + 'workloads.", '
        '"lead_citations": [1], "bullets": ['
        '{"text": "Investors led by Big Fund backed the' + raw + 'startup.", "citations": [2]},'
        '{"text": "The company now employs 350 people.", "citations": [1]},'
        '{"text": "Acme plans to grow its fleet of warehouse machines.", "citations": [1, 2]}]}'
    )
    for wrapped in (content, f"Sure! Here it is:\n```json\n{content}\n```", f"x {content} y"):
        brief = validate_brief(wrapped, [TEXT_A + " AI workloads", TEXT_B])
        assert brief.lead.endswith("for its AI workloads.")
        assert brief.bullets[0].text == "Investors led by Big Fund backed the startup."
    assert parse_json('{"a": "x\\ny"}') == {"a": "x\ny"}  # escaped newlines stay JSON


def test_glued_words_are_warnings_only() -> None:
    text = "Teams moved AIworkloads and tasksMore to iPhone apps at McDonald's and OpenAI."
    assert glued_words(text) == ["AIworkloads", "tasksMore"]
    assert glued_words(text, allow=["AIworkloads"]) == ["tasksMore"]
    paragraphs = [_filler(p, 80) for p in range(4)]
    paragraphs[0] += " The AIworkloads grew."
    report = validate_report(_report(paragraphs), [LONG])
    assert report.warnings == ("paragraph 1 has suspected glued words: AIworkloads",)


def test_glued_word_warning_skips_words_found_in_a_source() -> None:
    text = "Meta released FocusBench and AIworkloads."
    assert glued_words(text) == ["FocusBench", "AIworkloads"]
    assert glued_words(text, sources=["Meta unveils FocusBench, a benchmark."]) == ["AIworkloads"]
    assert glued_words("Focusbench", sources=["FocusBenchmarks"]) == []


def _docs() -> list[SourceDoc]:
    return [
        SourceDoc(i, i, f"Outlet {i}", f"Story {i}", f"https://s{i}.example/", t, True)
        for i, t in enumerate([LONG, LONG2], 1)
    ]


def test_retry_quotes_copied_phrases_and_raises_temperature() -> None:
    fake = FakeLLM(["copied_report", "ok"])
    summ = Summarizer(LLMClient(ROLE, transport=fake.transport()), retry_temperature_bump=0.2)
    gen = asyncio.run(summ.generate_report(_docs()))
    assert gen.status == "ok" and fake.calls == 2
    first, retry = fake.requests
    assert retry["temperature"] == pytest.approx(first["temperature"] + 0.2)
    feedback = retry["messages"][-1]["content"]
    assert "These phrases were copied word for word from the sources" in feedback
    assert "different sentence structure" in feedback
    assert [a["temperature"] for a in gen.attempt_log] == [
        first["temperature"],
        retry["temperature"],
    ]
    assert [a["attempt"] for a in gen.attempt_log] == [1, 2]
    assert len({a["run_id"] for a in gen.attempt_log}) == 1


def test_share_budget() -> None:
    assert share_budget([5000], 2400) == [2400]
    assert share_budget([100, 5000, 5000], 2400) == [100, 1150, 1150]
    assert share_budget([300, 400], 2400) == [300, 400]
    assert share_budget([], 2400) == []


def test_quality_metrics() -> None:
    def a(run, kind, outcome, reason=None, prompt=None, story=1, ms=1000):
        return {
            "run_id": run,
            "kind": kind,
            "prompt": prompt or kind,
            "outcome": outcome,
            "reason": reason,
            "latency_ms": ms,
            "story_id": story,
        }

    q = quality(
        [
            a("b1", "brief", "ok", story=1),
            a("r1", "report", "failed", "validation:verbatim_overlap", story=1, ms=4000),
            a("r1", "report", "ok", story=1, ms=5000),
            a("b2", "brief", "failed", "validation:length", story=2),
            a("b2", "brief", "failed", "validation:length", story=2),
            a("b2", "brief", "failed", "invalid_json", story=2),
            a("b2", "brief", "ok", prompt="summary", story=2),
        ]
    )
    brief, report = q.kinds["brief"], q.kinds["report"]
    assert (brief.runs, brief.first_pass, brief.attempts_to_success) == (2, 1, [1])
    assert brief.top_failures() == [("validation:length", 2), ("invalid_json", 1)]
    assert (report.first_pass_rate, report.median_attempts) == (0, 2)
    assert q.story_seconds == 7.0 and q.stories == 2  # (1 + 4 + 5) and 4 -> median 7


def test_pipeline_panel_shows_quality_metrics(demo) -> None:  # noqa: F811
    panel = demo.get("/p/personal-reader/settings/pipeline").text
    assert "data-quality" in panel and "First-attempt pass" in panel
    assert "Attempts per success" in panel and "per story (brief + report" in panel
