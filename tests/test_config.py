import pytest
from pydantic import ValidationError

from app.config import AlertRule, MatchMode, SourceType, load_config, parse_config
from tests.conftest import EXAMPLE_CONFIG

MINIMAL_LLM = "llm: {base_url: 'http://localhost:8080/v1', model: m}"


def test_example_config_loads_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "LOCAL_LLM_BASE_URL", "LOCAL_LLM_API_KEY", "LOCAL_LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)

    config = load_config(EXAMPLE_CONFIG)

    assert [p.id for p in config.profiles] == ["personal-reader", "market-monitor"]
    reader = config.get_profile("personal-reader")
    market = config.get_profile("market-monitor")
    assert reader is not None and market is not None
    assert reader.sources and market.sources
    assert all(s.type is SourceType.RSS for s in reader.sources)
    assert market.alert_rules[0].match is MatchMode.ANY
    assert str(reader.llm.base_url) == "https://api.openai.com/v1"
    assert reader.llm.api_key is None
    assert market.llm.model == "llama3.1:8b"


def test_env_vars_are_interpolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen2.5")

    config = load_config(EXAMPLE_CONFIG)

    reader = config.get_profile("personal-reader")
    market = config.get_profile("market-monitor")
    assert reader.llm.api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(reader.llm)
    assert market.llm.model == "qwen2.5"


def test_empty_env_var_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "")

    market = load_config(EXAMPLE_CONFIG).get_profile("market-monitor")

    assert str(market.llm.base_url) == "http://localhost:11434/v1"
    assert market.llm.model == "llama3.1:8b"


def test_empty_config_is_valid() -> None:
    assert parse_config("").profiles == []


def test_duplicate_profile_ids_rejected() -> None:
    text = f"""
profiles:
  - {{id: a, name: A, {MINIMAL_LLM}}}
  - {{id: a, name: B, {MINIMAL_LLM}}}
"""
    with pytest.raises(ValidationError, match="duplicate profile ids"):
        parse_config(text)


def test_alert_rule_unknown_source_rejected() -> None:
    text = f"""
profiles:
  - id: a
    name: A
    {MINIMAL_LLM}
    sources: [{{name: feed, url: 'https://example.com/rss'}}]
    alert_rules: [{{name: r, keywords: [x], sources: [nope]}}]
"""
    with pytest.raises(ValidationError, match="unknown sources"):
        parse_config(text)


def test_unknown_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_config(f"profiles: [{{id: a, name: A, colour: red, {MINIMAL_LLM}}}]")


def test_alert_rule_requires_keywords() -> None:
    with pytest.raises(ValidationError):
        AlertRule(name="r", keywords=[])
