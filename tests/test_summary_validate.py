import json

import pytest

from app.summarize.validate import (
    MAX_WORDS,
    SummaryInvalid,
    numbers_in,
    parse_json,
    split_sentences,
    validate_summary,
)

SOURCES = [
    "Acme Corp said revenue rose 12% to $4.5 billion in the third quarter.",
    "The company will cut 1,200 jobs, Acme told employees on Tuesday.",
]


def answer(summary: str, citations) -> str:
    return json.dumps({"summary": summary, "citations": citations})


def test_valid_summary_passes() -> None:
    result = validate_summary(
        answer("Acme revenue rose 12% to $4.5 billion. It will cut 1,200 jobs.", [[1], [1, 2]]),
        SOURCES,
    )
    assert result.sentences == ["Acme revenue rose 12% to $4.5 billion.", "It will cut 1,200 jobs."]
    assert result.citations == [[1], [1, 2]]


@pytest.mark.parametrize(
    "content",
    [
        "Here you go:\n```json\n" + answer("Acme cut jobs.", [[2]]) + "\n```",
        "Sure! " + answer("Acme cut jobs.", [[2]]) + " Let me know if you need more.",
        "```\n" + answer("Acme cut jobs.", [[2]]) + "\n```",
    ],
)
def test_json_in_fences_or_prose_is_parsed(content: str) -> None:
    assert validate_summary(content, SOURCES).summary == "Acme cut jobs."


@pytest.mark.parametrize("content", ["", "   ", '{"summary": "x", ', "not json at all", "[1, 2]"])
def test_invalid_json_rejected(content: str) -> None:
    with pytest.raises(SummaryInvalid):
        parse_json(content)


@pytest.mark.parametrize(
    ("summary", "citations", "message"),
    [
        ("", [], "non-empty string"),
        ("Acme cut jobs.", "1", "list of lists"),
        ("Acme cut jobs.", [[True]], "list of lists"),
        ("Acme cut jobs. Revenue rose.", [[2]], "2 sentence(s) but citations has 1"),
        ("Acme cut jobs. Revenue rose.", [[2], []], "sentence 2 has no citation"),
        ("Acme cut jobs.", [[3]], "valid source indexes are 1..2"),
        ("Acme cut jobs.", [[0]], "valid source indexes are 1..2"),
    ],
)
def test_citation_rules(summary: str, citations, message: str) -> None:
    with pytest.raises(SummaryInvalid, match=message.replace("(", r"\(").replace(")", r"\)")):
        validate_summary(answer(summary, citations), SOURCES)


def test_word_limit() -> None:
    words = " ".join(["word"] * (MAX_WORDS + 1)) + "."
    with pytest.raises(SummaryInvalid, match="words; the limit is 70"):
        validate_summary(answer(words, [[1]]), SOURCES)
    ok = " ".join(["word"] * MAX_WORDS) + "."
    assert validate_summary(answer(ok, [[1]]), SOURCES)


@pytest.mark.parametrize(
    "summary",
    [
        "Acme revenue rose 15%.",  # percentage not in any source
        "Acme revenue reached $5 billion.",  # currency amount
        "Acme will cut 1,300 jobs.",  # digits with separators
        "Acme said 7 executives left.",
    ],
)
def test_invented_numbers_rejected(summary: str) -> None:
    with pytest.raises(SummaryInvalid, match="not found in its cited sources"):
        validate_summary(answer(summary, [[1, 2]]), SOURCES)


def test_number_must_be_in_a_source_the_sentence_cites() -> None:
    with pytest.raises(SummaryInvalid, match=r"\['1200'\]"):
        validate_summary(answer("Acme will cut 1,200 jobs.", [[1]]), SOURCES)
    assert validate_summary(answer("Acme will cut 1200 jobs.", [[2]]), SOURCES)


def test_numbers_are_normalized() -> None:
    assert numbers_in("$4.50, 1,200 and 12%") == {"4.5", "1200", "12"}
    assert numbers_in("3.0 and 03") == {"3"}


def test_sentence_split_keeps_abbreviations_and_initials() -> None:
    text = (
        "The U.S. Federal Reserve held rates. J. Powell spoke at 2 p.m. on Tuesday. Markets rose."
    )
    assert split_sentences(text) == [
        "The U.S. Federal Reserve held rates.",
        "J. Powell spoke at 2 p.m. on Tuesday.",
        "Markets rose.",
    ]
    assert split_sentences("Revenue was $4.5 billion. Shares rose 3.2%.") == [
        "Revenue was $4.5 billion.",
        "Shares rose 3.2%.",
    ]
