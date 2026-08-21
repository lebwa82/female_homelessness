import asyncio
from types import SimpleNamespace

import pytest
from pydantic_ai import PromptedOutput

from app.agents import (
    AgentCallResult,
    AgentContext,
    AgentEvaluation,
    YandexAgentGateway,
    parse_risk,
    usage_audit,
    yandex_model_settings,
    yandex_output_type,
)
from app.domain import ChoiceSet, RiskAssessment, RiskLevel, SupportIntent, SupportPlan


def test_qwen_uses_deterministic_prompted_typed_output() -> None:
    assert isinstance(yandex_output_type("risk"), PromptedOutput)
    assert yandex_output_type("support").outputs is SupportPlan
    assert yandex_model_settings()["temperature"] == 0.0
    assert yandex_model_settings()["openai_reasoning_effort"] == "none"


def test_usage_audit_reads_the_pydantic_ai_usage_object() -> None:
    usage = SimpleNamespace(input_tokens=11, output_tokens=7, total_tokens=18, cache_read_tokens=3)

    assert usage_audit(usage) == {
        "input_tokens": 11,
        "output_tokens": 7,
        "total_tokens": 18,
        "cached_tokens": 3,
    }


def test_parse_risk_accepts_the_model_rationale_short_alias() -> None:
    risk, audit = parse_risk(
        AgentCallResult(
            payload={
                "level": "none",
                "categories": [],
                "confidence": 0.98,
                "rationale_short": "no concrete danger",
            },
            audit={"status": "completed"},
        )
    )

    assert risk.level is RiskLevel.NONE
    assert risk.rationale == "no concrete danger"
    assert audit["status"] == "completed"


def test_agent_evaluation_rejects_non_support_plan() -> None:
    with pytest.raises(TypeError, match="plan must be a SupportPlan or None"):
        AgentEvaluation(
            risk=RiskAssessment(level=RiskLevel.NONE),
            plan=object(),  # type: ignore[arg-type]
            risk_audit={"status": "completed"},
            support_audit={"status": "completed"},
        )


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
            else {
                "intent": "open_conversation",
                "next_action": "continue_conversation",
                "text": "Я могу вас выслушать. Что сейчас особенно тяжело?",
                "choice_set": "none",
                "catalog_item_ids": [],
            }
        )
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(history=(("user", "Мне нужна еда"),), state="discovering_need")
    )

    assert started == {"risk", "support"}
    assert max_in_flight == 2
    assert result.risk.level is RiskLevel.NONE
    assert result.plan.intent is SupportIntent.OPEN_CONVERSATION
    assert result.plan.choice_set is ChoiceSet.NONE


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
            payload = {
                "intent": "open_conversation",
                "next_action": "continue_conversation",
                "text": "Я рядом.",
                "choice_set": "none",
                "catalog_item_ids": [],
            }
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(history=(("user", "хочу поговорить с человеком"),), state="open_conversation")
    )

    assert "human_requested" not in captured_risk_instruction
    assert "Просьба поговорить с человеком не является риском." in captured_risk_instruction
    assert result.risk.level is RiskLevel.NONE


@pytest.mark.asyncio
async def test_gateway_instructions_keep_safety_and_support_plans_high_precision() -> None:
    captured: dict[str, str] = {}

    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        del input_text
        captured[agent_name] = " ".join(instructions.split())
        if agent_name == "risk":
            payload = {"level": "none", "categories": [], "confidence": 0.98, "rationale": "safe"}
        else:
            payload = {
                "intent": "open_conversation",
                "next_action": "continue_conversation",
                "text": "Я рядом.",
                "choice_set": "none",
                "catalog_item_ids": [],
            }
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(history=(("user", "мне нужна поддержка"),), state="open_conversation")
    )

    assert result.risk_audit["status"] == "completed"
    assert result.support_audit["status"] == "completed"
    assert result.risk_audit["request"]["temperature"] == 0.0
    assert result.support_audit["request"]["temperature"] == 0.0
    assert result.plan is not None
    assert "Не выводи concern из одиночества, усталости, горя, просьбы выслушать" in captured["risk"]
    assert "Без прямого указания на угрозу, страх насилия или нестабильное жильё выбирай none." in captured["risk"]
    assert "Верни поля level, categories, confidence и rationale; не используй rationale_short." in captured["risk"]
    assert "Нехватка еды или денег сама по себе — none, а не concern." in captured["risk"]
    assert "Urgent выбирай только при явно негде ночевать сегодня или выселении прямо сейчас." in captured["risk"]
    assert "без need не выбирай offer_aid" in captured["support"]
    assert "psychologist_considering/clarify и choice_set=psychologist_interest" in captured["support"]
    assert "Выраженная потребность в помощи или интерес к доступным вариантам" in captured["support"]
    assert "Даже при urgent жилье верни concrete_need/offer_aid с need=housing." in captured["support"]
    assert "Вопрос об условиях или возможности психолога не возвращай как open_conversation." in captured["support"]
    assert "Описание опасности без практической просьбы о помощи остаётся open_conversation." in captured["support"]


@pytest.mark.asyncio
async def test_gateway_masks_transcript_and_audit_excludes_raw_message() -> None:
    captured: list[str] = []

    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        captured.append(input_text)
        payload = (
            {"level": "none", "categories": [], "confidence": 0.98, "rationale": "safe"}
            if agent_name == "risk"
            else {
                "intent": "open_conversation",
                "next_action": "continue_conversation",
                "text": "Что сейчас важнее всего?",
                "choice_set": "none",
                "catalog_item_ids": [],
            }
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
    assert all("input_text" not in audit for audit in (result.risk_audit, result.support_audit))
    assert result.risk_audit["input_hash"]
    assert result.support_audit["input_hash"]
    assert result.support_audit["request"]["max_tokens"] == 300
    assert result.support_audit["request"]["reasoning_effort"] == "none"
    assert result.support_audit["request"]["data_logging_enabled"] is False


@pytest.mark.asyncio
async def test_invalid_support_payload_is_reported_without_exposing_provider_error() -> None:
    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        payload = {"level": "none"} if agent_name == "risk" else {"intent": "invented", "text": "x"}
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(history=(("user", "еда"),), state="discovering_need")
    )

    assert result.plan is None
    assert result.support_audit["status"] == "validation_error"
    assert "error_message" not in result.support_audit
