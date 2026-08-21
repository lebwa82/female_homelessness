import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai import PromptedOutput

from app.agents import (
    AgentCallResult,
    AgentContext,
    YandexAgentGateway,
    usage_audit,
    yandex_model_settings,
    yandex_output_type,
)
from app.domain import ActionKind, RiskLevel


def test_qwen_uses_prompted_typed_output_with_reasoning_disabled() -> None:
    assert isinstance(yandex_output_type("risk"), PromptedOutput)
    assert yandex_model_settings()["openai_reasoning_effort"] == "none"


def test_usage_audit_reads_the_pydantic_ai_usage_object() -> None:
    usage = SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18, cache_read_tokens=3)

    assert usage_audit(usage) == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "cached_tokens": 3,
    }


@pytest.mark.asyncio
async def test_evaluate_starts_risk_and_support_calls_concurrently() -> None:
    started: set[str] = set()
    in_flight = 0
    max_in_flight = 0

    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        nonlocal in_flight, max_in_flight
        started.add(agent_name)
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        payload = (
            {"level": "none", "categories": [], "confidence": 0.98, "rationale": "safe"}
            if agent_name == "risk"
            else {"kind": "show_choices", "text": "Что сейчас важнее всего?", "choices": []}
        )
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(history=(("user", "Мне нужна еда"),), state="discovering_need")
    )

    assert started == {"risk", "support"}
    assert max_in_flight == 2
    assert result.risk.level is RiskLevel.NONE
    assert result.action.kind is ActionKind.SHOW_CHOICES


@pytest.mark.asyncio
async def test_human_request_is_instructed_and_parsed_as_no_safety_risk() -> None:
    captured_risk_instruction = ""

    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        nonlocal captured_risk_instruction
        if agent_name == "risk":
            captured_risk_instruction = instructions
            assert "хочу поговорить с человеком" in input_text
            payload = {"level": "none", "categories": [], "confidence": 0.98, "rationale": "safe"}
        else:
            payload = {"kind": "reply", "text": "Я рядом."}
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(history=(("user", "хочу поговорить с человеком"),), state="open_conversation")
    )

    assert "human_requested" not in captured_risk_instruction
    assert "не является риском безопасности" in captured_risk_instruction
    assert result.risk.level is RiskLevel.NONE


@pytest.mark.asyncio
async def test_gateway_masks_transcript_and_audit_excludes_raw_message() -> None:
    captured: list[str] = []

    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        captured.append(input_text)
        payload = (
            {"level": "none", "categories": [], "confidence": 0.98, "rationale": "safe"}
            if agent_name == "risk"
            else {"kind": "reply", "text": "Что сейчас важнее всего?"}
        )
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(
            history=(("user", "Меня зовут Анна Иванова, телефон +7 999 123-45-67"),),
            state="discovering_need",
        )
    )

    assert len(captured) == 2
    assert all("Анна" not in text and "999" not in text for text in captured)
    assert all("input_text" not in audit for audit in (result.risk_audit, result.action_audit))
    assert result.risk_audit["input_hash"]
    assert result.action_audit["input_hash"]
    assert result.action_audit["request"]["max_tokens"] == 300
    assert result.action_audit["request"]["reasoning_effort"] == "none"
    assert result.action_audit["request"]["data_logging_enabled"] is False


@pytest.mark.asyncio
async def test_invalid_support_payload_is_reported_without_exposing_provider_error() -> None:
    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        payload = {"level": "none"} if agent_name == "risk" else {"kind": "invented", "text": "x"}
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(history=(("user", "еда"),), state="discovering_need")
    )

    assert result.action is None
    assert result.action_audit["status"] == "validation_error"
    assert "error_message" not in result.action_audit
