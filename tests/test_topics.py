import pytest

from app.topics import TopicRule, contains_keyword, matches, normalize_keywords, split_keywords


def m(rule: TopicRule, title: str = "", summary: str = "", source_id: str = "src") -> bool:
    return matches(rule, title=title, summary=summary, source_id=source_id)


def test_include_matches_title_or_summary() -> None:
    rule = TopicRule(include=("AI", "robots"))
    assert m(rule, title="New AI model released")
    assert m(rule, summary="Factories add more robots")
    assert not m(rule, title="Gardening tips", summary="Grow tomatoes")


def test_exclude_wins_over_include() -> None:
    rule = TopicRule(include=("travel",), exclude=("time travel",))
    assert m(rule, title="Budget travel in Portugal")
    assert not m(rule, title="Travel guide", summary="Why time travel paradoxes fail")


def test_exclude_only_rule_excludes() -> None:
    rule = TopicRule(exclude=("crypto",))
    assert m(rule, title="Stocks rally")
    assert not m(rule, title="Crypto lender collapses")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("AI is everywhere", True),
        ("The AI.", True),
        ("(AI)", True),
        ("open-AI style", True),
        ("Officials said the plan works", False),  # "said" contains "ai"
        ("Thailand travel", False),
        ("AIs", False),
        ("MAIN street", False),
    ],
)
def test_whole_word_only(text: str, expected: bool) -> None:
    assert m(TopicRule(include=("AI",)), title=text) is expected


def test_case_insensitive() -> None:
    rule = TopicRule(include=("Machine Learning",))
    assert m(rule, title="MACHINE LEARNING at scale")
    assert m(rule, summary="advances in machine learning")
    assert not m(TopicRule(include=("x",), exclude=("SPAM",)), title="x spam")


def test_multi_word_keyword_tolerates_whitespace_but_not_partial() -> None:
    rule = TopicRule(include=("heat pump",))
    assert m(rule, summary="a new heat\n  pump design")
    assert not m(rule, summary="heat pumps are popular")
    assert not m(rule, summary="heat and pump")


def test_keywords_with_punctuation() -> None:
    assert m(TopicRule(include=("S&P 500",)), title="S&P 500 hits record")
    assert m(TopicRule(include=("8-K",)), title="files an 8-K today")
    assert not m(TopicRule(include=("8-K",)), title="files an 18-K today")
    assert m(TopicRule(include=("C++",)), title="Learning C++ in 2026")


def test_empty_include_matches_everything_from_topic_sources() -> None:
    assert m(TopicRule(), title="anything")
    assert m(TopicRule(), title="")
    restricted = TopicRule(source_ids=("fed",))
    assert m(restricted, title="anything", source_id="fed")
    assert not m(restricted, title="anything", source_id="sec")


def test_source_restriction_combines_with_keywords() -> None:
    rule = TopicRule(include=("rates",), source_ids=("fed",))
    assert m(rule, title="Fed holds rates", source_id="fed")
    assert not m(rule, title="Fed holds rates", source_id="cnbc")


def test_none_text_is_handled() -> None:
    assert not contains_keyword("", "ai")
    assert matches(TopicRule(), title=None, summary=None, source_id="s")  # type: ignore[arg-type]


def test_keyword_parsing_normalizes_and_dedupes() -> None:
    assert split_keywords(" AI, machine   learning,\nai ,, LLM ") == [
        "AI",
        "machine learning",
        "LLM",
    ]
    assert normalize_keywords(["  ", "x", "X"]) == ["x"]
    assert split_keywords("") == []
