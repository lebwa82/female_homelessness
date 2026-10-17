from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import agents
from app.config import Settings


def test_existing_provider_remains_the_default(monkeypatch):
    for name in ("LLM_PROVIDER", "TIMEWEB_AI_API_KEY", "TIMEWEB_AI_MODEL"):
        monkeypatch.delenv(name, raising=False)
    config = Settings(_env_file=None)
    assert config.llm_provider == "yandex"
    assert not config.timeweb_ai_api_key
    assert not config.timeweb_ai_model


def test_timeweb_client_has_fixed_endpoint_and_no_transport_retries(monkeypatch):
    monkeypatch.setattr(agents.settings, "llm_provider", "timeweb")
    monkeypatch.setattr(agents.settings, "timeweb_ai_api_key", "test-only-key")
    captured = {}

    def client(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(agents, "AsyncOpenAI", client)
    agents.create_llm_client()
    assert captured["base_url"] == "https://api.timeweb.ai/v1"
    assert captured["max_retries"] == 0
    assert captured["timeout"] == agents.PROVIDER_TIMEOUT_SECONDS
    assert "project" not in captured


def test_timeweb_requires_its_own_key_and_explicit_model(monkeypatch):
    monkeypatch.setattr(agents.settings, "llm_provider", "timeweb")
    monkeypatch.setattr(agents.settings, "timeweb_ai_api_key", "")
    monkeypatch.setattr(agents.settings, "timeweb_ai_model", "deepseek/deepseek-flash")
    assert not agents.llm_is_configured()
    monkeypatch.setattr(agents.settings, "timeweb_ai_api_key", "test-only-key")
    assert agents.llm_is_configured()
    monkeypatch.setattr(agents.settings, "timeweb_ai_model", "")
    assert not agents.llm_is_configured()


@pytest.mark.asyncio
async def test_timeweb_uses_chat_completions_and_keeps_json_contract(monkeypatch):
    monkeypatch.setattr(agents.settings, "llm_provider", "timeweb")
    monkeypatch.setattr(agents.settings, "timeweb_ai_model", "deepseek/deepseek-flash")
    expected = object()
    create = AsyncMock(return_value=expected)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = await agents.create_json_response(
        client,
        instructions="Return a technical result.",
        input_text="Synthetic connectivity check.",
        response_format={"type": "json_schema", "schema": {"required": ["ok"]}},
        provider_settings=agents.ProviderSettings(max_tokens=64),
    )
    assert result is expected
    request = create.call_args.kwargs
    assert request["model"] == "deepseek/deepseek-flash"
    assert request["response_format"] == {"type": "json_object"}
    assert request["max_tokens"] == 64
    assert request["reasoning_effort"] == "none"
    assert request["temperature"] == 0.0
    assert "JSON schema" in request["messages"][0]["content"]
    assert '"required": ["ok"]' in request["messages"][0]["content"]
    assert request["messages"][1]["content"] == "Synthetic connectivity check."


def test_chat_response_reads_final_content_not_reasoning():
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='{"ok":true}', reasoning_content="must not be returned",
    ))])
    assert agents.response_output_text(response) == '{"ok":true}'


def test_chat_usage_uses_prompt_completion_and_cached_tokens():
    usage = SimpleNamespace(
        prompt_tokens=12, completion_tokens=3, total_tokens=15,
        prompt_tokens_details=SimpleNamespace(cached_tokens=4),
    )
    assert agents.usage_audit(usage) == {
        "input_tokens": 12, "output_tokens": 3, "total_tokens": 15, "cached_tokens": 4,
    }


@pytest.mark.asyncio
async def test_timeweb_audit_does_not_claim_provider_logging_is_disabled(monkeypatch):
    monkeypatch.setattr(agents.settings, "llm_provider", "timeweb")
    call = AsyncMock(return_value=agents.AgentCallResult(payload={}, audit={"status": "test"}))
    result = await agents.YandexAgentGateway(call=call)._run(
        "risk", "Synthetic instruction.", "Synthetic check.", {}
    )
    assert result.audit["provider"] == "timeweb_ai_gateway"
    assert result.audit["request"]["data_logging_enabled"] is None


@pytest.mark.asyncio
async def test_timeweb_missing_key_never_falls_back_to_yandex(monkeypatch):
    monkeypatch.setattr(agents.settings, "llm_provider", "timeweb")
    monkeypatch.setattr(agents.settings, "llm_enabled", True)
    monkeypatch.setattr(agents.settings, "timeweb_ai_api_key", "")
    monkeypatch.setattr(agents.settings, "timeweb_ai_model", "deepseek/deepseek-flash")
    client = AsyncMock()
    monkeypatch.setattr(agents, "create_llm_client", client)
    result = await agents.YandexAgentGateway()._call_live("risk", "Synthetic.", "Synthetic.")
    assert result.audit["status"] == "not_configured"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_timeweb_invalid_json_retry_stays_bounded_and_closes_client(monkeypatch):
    monkeypatch.setattr(agents.settings, "llm_provider", "timeweb")
    monkeypatch.setattr(agents.settings, "llm_enabled", True)
    monkeypatch.setattr(agents.settings, "timeweb_ai_api_key", "test-only-key")
    monkeypatch.setattr(agents.settings, "timeweb_ai_model", "deepseek/deepseek-flash")
    response = SimpleNamespace(
        id="synthetic", model="deepseek/deepseek-flash",
        choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7),
    )
    create = AsyncMock(return_value=response)
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=AsyncMock(),
    )
    monkeypatch.setattr(agents, "create_llm_client", lambda: client)
    result = await agents.YandexAgentGateway()._call_live("risk", "Synthetic.", "Synthetic.")
    assert create.await_count == 2
    assert result.audit["format_retry_count"] == 1
    client.close.assert_awaited_once()
