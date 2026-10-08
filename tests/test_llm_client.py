import asyncio
import json

import httpx
import pytest

from app.config import LLMRole, parse_config
from app.llm import (
    LLMBadResponse,
    LLMClient,
    LLMConnectionError,
    LLMHTTPError,
    LLMTimeout,
    build_headers,
    build_payload,
    parse_response,
)
from app.summarize.fake import FakeLLM
from app.summarize.prompt import RESPONSE_SCHEMA

MESSAGES = [{"role": "user", "content": "Sources:\n\n[1] Source: A\nHeadline: H\nText: Body."}]

LLAMA = {"base_url": "http://llm.lan:8080/v1/", "api_key": "sk-local-secret", "model": "qwen3"}
CLOUD = {
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-cloud",
    "model": "gpt-4o-mini",
    "temperature": 0,
    "disable_thinking": False,
}


def run(coro):
    return asyncio.run(coro)


def recording(handler):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return httpx.MockTransport(wrapped), seen


def ok_response(content: str = '{"summary": "x.", "citations": [[1]]}') -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": "served-model",
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        },
    )


def test_defaults() -> None:
    role = LLMRole(base_url="http://x/v1", model="m")
    assert (role.temperature, role.max_tokens, role.timeout_seconds) == (0.2, 600, 120)
    assert role.structured_output == "json_schema" and role.disable_thinking


def test_url_headers_and_llama_payload() -> None:
    role = LLMRole(**LLAMA)
    transport, seen = recording(lambda r: ok_response())
    result = run(LLMClient(role, transport=transport).chat(MESSAGES, json_schema=RESPONSE_SCHEMA))
    request = seen[0]
    assert str(request.url) == "http://llm.lan:8080/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-local-secret"
    body = json.loads(request.content)
    assert body["model"] == "qwen3" and body["stream"] is False
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == RESPONSE_SCHEMA
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert "grammar" not in body
    assert result.content.startswith('{"summary"') and result.model == "served-model"
    assert (result.prompt_tokens, result.completion_tokens) == (10, 5)


def test_no_api_key_sends_no_authorization() -> None:
    assert "Authorization" not in build_headers(LLMRole(base_url="http://x/v1", model="m"))


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("json_schema", "json_schema"), ("json_object", "json_object"), ("none", None)],
)
def test_structured_output_modes(mode: str, expected: str | None) -> None:
    role = LLMRole(base_url="http://x/v1", model="m", structured_output=mode)
    payload = build_payload(role, MESSAGES, json_schema=RESPONSE_SCHEMA)
    assert (payload.get("response_format") or {}).get("type") == expected


def test_cloud_and_llama_configs_share_one_code_path() -> None:
    """Providers differ only in settings: same client, same request shape apart from them."""
    text = f"""
profiles:
  - id: local
    name: Local
    llm: {{summarizer: {json.dumps(LLAMA)}}}
  - id: cloud
    name: Cloud
    llm: {{summarizer: {json.dumps(CLOUD)}}}
"""
    config = parse_config(text)
    bodies = {}
    for profile in config.profiles:
        transport, seen = recording(lambda r: ok_response())
        client = LLMClient(profile.llm.summarizer, transport=transport)
        result = run(client.chat(MESSAGES, json_schema=RESPONSE_SCHEMA))
        assert result.content
        bodies[profile.id] = (str(seen[0].url), json.loads(seen[0].content))
    (local_url, local), (cloud_url, cloud) = bodies["local"], bodies["cloud"]
    assert local_url.endswith("/v1/chat/completions") and cloud_url.endswith("/v1/chat/completions")
    assert local.pop("chat_template_kwargs") == {"enable_thinking": False}
    assert "chat_template_kwargs" not in cloud
    differing = {k for k in local | cloud if local.get(k) != cloud.get(k)}
    assert differing == {"model", "temperature"}


def test_parse_response_errors_and_content_parts() -> None:
    with pytest.raises(LLMBadResponse):
        parse_response({"choices": []})
    parts = {"choices": [{"message": {"content": [{"type": "text", "text": " hi "}]}}]}
    assert parse_response(parts, default_model="m").content == "hi"
    assert parse_response(parts, default_model="m").model == "m"


def role(**kwargs) -> LLMRole:
    return LLMRole(**{**LLAMA, **kwargs})


@pytest.mark.parametrize("mode", ["ok", "prose", "malformed", "out_of_range", "invented_number"])
def test_fake_content_modes_return_content(mode: str) -> None:
    result = run(LLMClient(role(), transport=FakeLLM(mode).transport()).chat(MESSAGES))
    assert result.content and result.completion_tokens


def test_fake_empty_content() -> None:
    result = run(LLMClient(role(), transport=FakeLLM("empty").transport()).chat(MESSAGES))
    assert result.content == "" and result.finish_reason == "stop"


def test_http_500_is_typed_and_redacted() -> None:
    def echo_key(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"bad key {request.headers['authorization']}")

    with pytest.raises(LLMHTTPError) as err:
        run(LLMClient(role(), transport=httpx.MockTransport(echo_key)).chat(MESSAGES))
    assert err.value.status_code == 500 and err.value.kind == "http"
    assert "sk-local-secret" not in str(err.value) and "***" in str(err.value)
    with pytest.raises(LLMHTTPError):
        run(LLMClient(role(), transport=FakeLLM("http_500").transport()).chat(MESSAGES))


def test_timeout() -> None:
    with pytest.raises(LLMTimeout, match="timed out after 5s"):
        run(
            LLMClient(role(timeout_seconds=5), transport=FakeLLM("timeout").transport()).chat(
                MESSAGES
            )
        )


def test_connection_error_and_non_json_body() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(LLMConnectionError):
        run(LLMClient(role(), transport=httpx.MockTransport(refuse)).chat(MESSAGES))
    html = httpx.MockTransport(lambda r: httpx.Response(200, text="<html>proxy</html>"))
    with pytest.raises(LLMBadResponse):
        run(LLMClient(role(), transport=html).chat(MESSAGES))
