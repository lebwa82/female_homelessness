import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai import PromptedOutput

from app.agents import (
    AgentCallResult,
    AgentContext,
    ProviderSettings,
    YandexAgentGateway,
    parse_safety_diagnostic,
    parse_support_diagnostic,
    usage_audit,
    yandex_model_settings,
    yandex_output_type,
)
from app.domain import DiagnosticStatus, SafetyDiagnostic, SupportDiagnostic


def test_qwen_uses_typed_diagnostics_and_one_provider_settings_source() -> None:
    provider_settings = ProviderSettings(temperature=0.2, max_tokens=111, reasoning_effort="low")

    assert isinstance(yandex_output_type("risk"), PromptedOutput)
    assert yandex_output_type("risk").outputs is SafetyDiagnostic
    assert yandex_output_type("support").outputs is SupportDiagnostic
    assert yandex_model_settings(provider_settings) == {
        "temperature": 0.2,
        "max_tokens": 111,
        "openai_reasoning_effort": "low",
    }
    assert provider_settings.audit_fields()["max_tokens"] == 111


def test_usage_audit_reads_the_pydantic_ai_usage_object() -> None:
    usage = SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18, cache_read_tokens=3)

    assert usage_audit(usage) == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "cached_tokens": 3,
    }


def test_safety_alias_is_one_way_and_evidence_audit_never_retains_quotes() -> None:
    diagnostic, status, audit = parse_safety_diagnostic(
        AgentCallResult(
            payload={
                "level": "none",
                "categories": [],
                "confidence": 0.98,
                "rationale": "canonical rationale",
                "rationale_short": "ignored provider alias",
                "evidence_claims": ["не хочу жить", "invented quote"],
            },
            audit={"status": "completed"},
        ),
        "не хочу жить",
    )

    assert status is DiagnosticStatus.COMPLETED
    assert diagnostic is not None and diagnostic.rationale == "canonical rationale"
    assert diagnostic.evidence_claims == ()
    assert audit["rationale_alias_used"] is False
    assert audit["evidence"]["claims"] == 2
    assert audit["evidence"]["valid"] == 1
    assert audit["evidence"]["invalid"] == 1
    assert "invented quote" not in repr(audit)
    assert "canonical rationale" not in repr(audit)


def test_safety_alias_audit_flag_does_not_require_the_excluded_provider_field() -> None:
    """A live typed output may retain the alias audit flag but exclude its source field."""
    diagnostic, status, audit = parse_safety_diagnostic(
        AgentCallResult(
            payload={
                "level": "none",
                "categories": [],
                "confidence": 0.98,
                "rationale": "normalized rationale",
            },
            audit={"status": "completed", "rationale_alias_used": True},
        ),
        "безопасно",
    )

    assert status is DiagnosticStatus.COMPLETED
    assert diagnostic is not None and diagnostic.rationale == "normalized rationale"
    assert audit["rationale_alias_used"] is True


@pytest.mark.parametrize(
    ("payload", "expected"),
    (
        ({}, DiagnosticStatus.INVALID),
        ({"intent": "open_conversation", "draft_text": "Я рядом.", "choice_set": "none"}, DiagnosticStatus.INVALID),
    ),
)
def test_invalid_support_diagnostic_never_becomes_a_semantic_risk(
    payload: dict[str, object], expected: DiagnosticStatus
) -> None:
    diagnostic, status, audit = parse_support_diagnostic(
        AgentCallResult(payload=payload, audit={"status": "completed"}),
        "мне нужно выговориться",
    )

    assert diagnostic is None
    assert status is expected
    assert audit["diagnostic_status"] == "invalid"


def test_transport_failure_is_unavailable_not_a_synthetic_unknown_risk() -> None:
    diagnostic, status, audit = parse_safety_diagnostic(
        AgentCallResult(payload={}, audit={"status": "error", "error_type": "TimeoutError"}),
        "мне нужна еда",
    )

    assert diagnostic is None
    assert status is DiagnosticStatus.UNAVAILABLE
    assert audit["diagnostic_status"] == "unavailable"


@pytest.mark.asyncio
async def test_evaluate_starts_exactly_two_calls_concurrently_and_keeps_actions_out_of_schema() -> None:
    started: set[str] = set()
    in_flight = 0
    max_in_flight = 0

    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        nonlocal in_flight, max_in_flight
        del instructions, input_text
        started.add(agent_name)
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        payload = (
            {"level": "none", "categories": [], "confidence": 0.98, "rationale": "safe"}
            if agent_name == "risk"
            else {"intent": "open_conversation", "draft_text": "Я могу вас выслушать."}
        )
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    gateway = YandexAgentGateway(
        call=call,
        provider_settings=ProviderSettings(max_tokens=271, data_logging_enabled=False),
    )
    result = await gateway.evaluate(
        AgentContext(history=(("user", "Мне нужна еда"),), state="open_conversation")
    )

    assert started == {"risk", "support"}
    assert max_in_flight == 2
    assert result.safety_status is DiagnosticStatus.COMPLETED
    assert result.support_status is DiagnosticStatus.COMPLETED
    assert result.support is not None and result.support.draft_text == "Я могу вас выслушать."
    assert result.safety_audit["request"]["max_tokens"] == 271
    assert result.support_audit["request"] == result.safety_audit["request"]


@pytest.mark.asyncio
async def test_gateway_masks_transcript_and_audit_excludes_raw_message() -> None:
    captured: list[str] = []

    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        del instructions
        captured.append(input_text)
        payload = (
            {"level": "none", "categories": [], "confidence": 0.98, "rationale": "safe"}
            if agent_name == "risk"
            else {"intent": "open_conversation", "draft_text": "Я рядом."}
        )
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(
            history=(("user", "Меня зовут Анна Иванова, телефон +7 999 123-45-67"),),
            state="open_conversation",
        )
    )

    assert len(captured) == 2
    assert all("Анна" not in text and "999" not in text for text in captured)
    assert all("input_text" not in audit for audit in (result.safety_audit, result.support_audit))
    assert result.safety_audit["input_hash"]
    assert result.support_audit["input_hash"]
