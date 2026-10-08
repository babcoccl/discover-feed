import pytest
from pydantic import ValidationError

from app.config import AlertRule, MatchMode, SourceType, load_config, parse_config
from tests.conftest import EXAMPLE_CONFIG

MINIMAL_LLM = "llm: {summarizer: {base_url: 'http://localhost:8080/v1', model: m}}"


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
    llm = reader.llm.summarizer
    assert str(llm.base_url) == "http://localhost:8080/v1"
    assert llm.api_key is None
    assert llm.model == "local"
    assert llm.structured_output == "json_schema" and llm.disable_thinking
    assert (llm.temperature, llm.max_tokens, llm.timeout_seconds) == (0.2, 600, 120)
    assert reader.llm.chat is not None
    assert market.llm.summarizer.model == "gpt-4o-mini"
    assert not market.llm.summarizer.disable_thinking


def test_env_vars_are_interpolated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://10.0.0.5:8080/v1")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "local-key")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen2.5")

    config = load_config(EXAMPLE_CONFIG)

    reader = config.get_profile("personal-reader").llm.summarizer
    market = config.get_profile("market-monitor").llm.summarizer
    assert market.api_key.get_secret_value() == "sk-test"
    assert "sk-test" not in repr(market)
    assert str(reader.base_url) == "http://10.0.0.5:8080/v1"
    assert reader.api_key.get_secret_value() == "local-key"
    assert reader.model == "qwen2.5"


def test_empty_env_var_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "")
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "")

    reader = load_config(EXAMPLE_CONFIG).get_profile("personal-reader").llm.summarizer

    assert str(reader.base_url) == "http://localhost:8080/v1"
    assert reader.api_key is None
    assert reader.model == "local"


def test_flat_llm_block_is_rejected() -> None:
    text = "profiles: [{id: a, name: A, llm: {base_url: 'http://x/v1', model: m}}]"
    with pytest.raises(ValidationError):
        parse_config(text)


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


def test_source_id_defaults_to_slug_and_refresh_defaults_to_30() -> None:
    from app.config import Source

    src = Source(name="Federal Reserve: Press Releases", url="https://fed.example/rss")
    assert src.id == "federal-reserve-press-releases"
    assert src.refresh_minutes == 30
    with pytest.raises(ValidationError):
        Source(name="x", url="https://x.example/", poll_interval_minutes=5)


def test_all_sources_are_unique_across_profiles() -> None:
    text = f"""
profiles:
  - id: a
    name: A
    {MINIMAL_LLM}
    sources:
      - {{name: Feed, url: 'https://example.com/rss'}}
      - {{name: Paused, url: 'https://p.example/', enabled: false}}
  - id: b
    name: B
    {MINIMAL_LLM}
    sources: [{{name: Feed, url: 'https://example.com/rss'}}]
"""
    config = parse_config(text)
    assert [s.id for s in config.all_sources()] == ["feed"]
    assert config.get_source("paused") is not None


def test_conflicting_source_ids_rejected() -> None:
    text = f"""
profiles:
  - id: a
    name: A
    {MINIMAL_LLM}
    sources: [{{name: Feed, url: 'https://example.com/rss'}}]
  - id: b
    name: B
    {MINIMAL_LLM}
    sources: [{{name: Feed, url: 'https://other.example/rss'}}]
"""
    with pytest.raises(ValidationError, match="source id 'feed'"):
        parse_config(text)
