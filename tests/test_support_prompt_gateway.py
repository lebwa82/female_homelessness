import json
from types import SimpleNamespace

import pytest

from app import agents
from app.domain import DiagnosticStatus
from app.support_prompt import PromptStore


@pytest.fixture
def prompt_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(agents.settings, "support_prompt_dir", tmp_path)
    monkeypatch.setattr(agents, "format_redacted_transcript", lambda history: ("Synthetic.", {}))
    return PromptStore(tmp_path)


def diagnostic(name):
    payload = (
        {"level": "none", "categories": [], "confidence": 1.0, "rationale": "synthetic"}
        if name == "risk"
        else {"intent": "open_conversation", "draft_text": "Synthetic reply."}
    )
    return agents.AgentCallResult(payload=payload, audit={"status": "completed"})


@pytest.mark.asyncio
async def test_prompt_hot_reload_is_one_snapshot_per_turn_and_only_changes_style(prompt_runtime):
    store = prompt_runtime
    first = store.publish("FIRST SYNTHETIC STYLE", actor_id=1, expected_revision="builtin")
    calls = []
    second = None

    async def call(name, instructions, input_text):
        nonlocal second
        calls.append((name, instructions))
        if name == "risk" and second is None:
            # Publishing after the event began must affect only the next event.
            second = store.publish("SECOND SYNTHETIC STYLE", 1, first.revision)
        return diagnostic(name)

    gateway = agents.YandexAgentGateway(call=call)
    context = agents.AgentContext(history=(("user", "Synthetic check."),), state="open_conversation")
    result = await gateway.evaluate(context)
    assert result.support_status is DiagnosticStatus.COMPLETED
    assert result.support_audit["prompt_revision"] == first.revision
    assert result.support_audit["prompt_status"] == "custom"
    assert "FIRST SYNTHETIC STYLE" not in json.dumps(result.support_audit)
    assert dict(calls)["risk"] == agents.RISK_INSTRUCTIONS + "\n\n" + agents.RISK_BOUNDARIES
    support = dict(calls)["support"]
    assert "FIRST SYNTHETIC STYLE" in support and "SECOND SYNTHETIC STYLE" not in support
    assert agents.SUPPORT_INSTRUCTIONS in support
    assert support.endswith(agents.SUPPORT_CLASSIFICATION_CONTRACT)
    calls.clear()
    result = await gateway.evaluate(context)
    assert result.support_audit["prompt_revision"] == second.revision
    assert "SECOND SYNTHETIC STYLE" in dict(calls)["support"]
    restored = store.rollback(1, second.revision)
    calls.clear()
    result = await gateway.evaluate(context)
    assert result.support_audit["prompt_revision"] == restored.revision
    assert "FIRST SYNTHETIC STYLE" in dict(calls)["support"]


@pytest.mark.asyncio
async def test_invalid_prompt_falls_back_without_disabling_diagnostics(prompt_runtime):
    (prompt_runtime.directory / "support_prompt.json").write_text("invalid-json", encoding="utf-8")
    calls = {}

    async def call(name, instructions, input_text):
        calls[name] = instructions
        return diagnostic(name)

    result = await agents.YandexAgentGateway(call=call).evaluate(
        agents.AgentContext(history=(("user", "Synthetic check."),), state="open_conversation")
    )
    assert result.safety_status is DiagnosticStatus.COMPLETED
    assert result.support_status is DiagnosticStatus.COMPLETED
    assert result.support_audit["prompt_status"] == "fallback_invalid"
    assert result.support_audit["prompt_revision"] == "builtin"
    assert calls["support"] == agents.support_instructions()


def test_unconfigured_style_keeps_original_prompt():
    assert agents.support_instructions() == (
        f"{agents.SUPPORT_INSTRUCTIONS}\n\n{agents.SUPPORT_TONE}\n\n"
        f"{agents.SUPPORT_CLASSIFICATION_CONTRACT}"
    )


@pytest.mark.asyncio
async def test_live_format_retry_keeps_the_original_prompt_snapshot(prompt_runtime, monkeypatch):
    first = prompt_runtime.publish("FIRST SYNTHETIC STYLE", 1, "builtin")
    monkeypatch.setattr(agents.settings, "llm_enabled", True)
    monkeypatch.setattr(agents, "llm_is_configured", lambda: True)
    support_calls = []
    closed = []

    class Client:
        async def close(self):
            closed.append(True)

    monkeypatch.setattr(agents, "create_llm_client", Client)

    async def request(client, *, instructions, input_text, response_format, provider_settings):
        name = response_format["name"]
        payload = diagnostic(name).payload
        if name == "support":
            support_calls.append(instructions)
            if len(support_calls) == 1:
                prompt_runtime.publish("SECOND SYNTHETIC STYLE", 1, first.revision)
                payload = {}  # A malformed provider answer triggers the one format retry.
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))],
            usage=SimpleNamespace(total_tokens=0),
        )

    monkeypatch.setattr(agents, "create_json_response", request)
    result = await agents.YandexAgentGateway().evaluate(
        agents.AgentContext(history=(("user", "Synthetic check."),), state="open_conversation")
    )
    assert result.safety_status is DiagnosticStatus.COMPLETED
    assert result.support_status is DiagnosticStatus.COMPLETED
    assert result.support_audit["prompt_revision"] == first.revision
    assert result.support_audit["format_retry_count"] == 1
    assert len(support_calls) == 2
    assert all("FIRST SYNTHETIC STYLE" in value for value in support_calls)
    assert all("SECOND SYNTHETIC STYLE" not in value for value in support_calls)
    assert len(closed) == 2
