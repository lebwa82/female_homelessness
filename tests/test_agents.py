import asyncio
from types import SimpleNamespace

import pytest

from app import agents
from app.agents import (
    SUPPORT_INSTRUCTIONS,
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
from app.domain import DiagnosticStatus, SupportIntent


def test_qwen_uses_text_json_boundary_and_one_provider_settings_source() -> None:
    provider_settings = ProviderSettings(temperature=0.2, max_tokens=111, reasoning_effort="low")

    assert yandex_output_type("risk") is str
    assert yandex_output_type("support") is str
    assert yandex_model_settings(provider_settings) == {
        "temperature": 0.2,
        "max_tokens": 111,
        "openai_reasoning_effort": "low",
    }
    assert provider_settings.audit_fields()["max_tokens"] == 111


def test_yandex_client_uses_fixed_timeout_and_disables_sdk_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class Client:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(agents, "AsyncOpenAI", Client)

    agents.create_yandex_client()

    assert captured["timeout"] == 12.0
    assert captured["max_retries"] == 0


def test_support_text_json_instructions_enumerate_the_only_valid_intents() -> None:
    assert all(intent.value in SUPPORT_INSTRUCTIONS for intent in SupportIntent)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"level":"none"}', {"level": "none"}),
        ('```json\n{"level":"none"}\n```', {"level": "none"}),
        ("not-json", {}),
        ('{"level":"none"}\ntrailing', {}),
        ('{"level":"none","level":"critical"}', {}),
        ('{"confidence":NaN}', {}),
        ('{"confidence":Infinity}', {}),
        ('{"confidence":-Infinity}', {}),
    ],
)
def test_provider_json_parser_accepts_only_one_object_or_known_code_fence(
    raw: str, expected: dict[str, str]
) -> None:
    assert agents.parse_provider_json_object(raw) == expected


def test_provider_output_shape_reports_only_non_content_metadata() -> None:
    shape = agents.provider_output_shape('```json\n{"level":"none"}\n```')

    assert shape == {
        "characters": 28,
        "nonempty": True,
        "starts_json": False,
        "ends_object": False,
        "starts_code_fence": True,
        "ends_code_fence": True,
    }
    assert all("level" not in str(value) for value in shape.values())


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


def test_level_only_safety_diagnostic_is_accepted_for_provider_schema_robustness() -> None:
    """A diagnostic label alone must not make the live structured call retry or fail."""
    diagnostic, status, audit = parse_safety_diagnostic(
        AgentCallResult(payload={"level": "none"}, audit={"status": "completed"}),
        "анонимная health-проверка",
    )

    assert status is DiagnosticStatus.COMPLETED
    assert diagnostic is not None and diagnostic.level.value == "none"
    assert audit["diagnostic_status"] == "completed"


def test_safety_truncates_only_an_oversized_string_rationale() -> None:
    diagnostic, status, audit = parse_safety_diagnostic(
        AgentCallResult(
            payload={
                "level": "none",
                "rationale": "r" * 241,
            },
            audit={"status": "completed"},
        ),
        "anonymized",
    )

    assert status is DiagnosticStatus.COMPLETED
    assert diagnostic is not None and len(diagnostic.rationale) == 240
    assert audit["normalization"] == {"categories": ["safety_rationale_truncated"]}
    assert "r" * 241 not in repr(audit)


def test_support_clears_only_unknown_string_enum_labels() -> None:
    diagnostic, status, audit = parse_support_diagnostic(
        AgentCallResult(
            payload={
                "intent": "unrecognized_intent",
                "need_hint": "unrecognized_need",
                "draft_text": "safe draft",
            },
            audit={"status": "completed"},
        ),
        "anonymized",
    )

    assert status is DiagnosticStatus.COMPLETED
    assert diagnostic is not None
    assert diagnostic.intent is None
    assert diagnostic.need_hint is None
    assert audit["normalization"] == {
        "categories": ["support_unknown_intent_cleared", "support_unknown_need_hint_cleared"]
    }
    assert "unrecognized" not in repr(audit)


def test_partial_normalization_does_not_accept_missing_draft_or_invalid_safety_level() -> None:
    safety, safety_status, safety_audit = parse_safety_diagnostic(
        AgentCallResult(
            payload={"level": "unrecognized_level", "rationale": "r" * 241},
            audit={"status": "completed"},
        ),
        "anonymized",
    )
    support, support_status, support_audit = parse_support_diagnostic(
        AgentCallResult(
            payload={"intent": "unrecognized_intent"},
            audit={"status": "completed"},
        ),
        "anonymized",
    )

    assert safety is None and safety_status is DiagnosticStatus.INVALID
    assert support is None and support_status is DiagnosticStatus.INVALID
    assert safety_audit["normalization"] == {"categories": ["safety_rationale_truncated"]}
    assert support_audit["normalization"] == {"categories": ["support_unknown_intent_cleared"]}


@pytest.mark.parametrize("payload", ({}, {"level": 7}, {"level": []}))
def test_partial_normalization_rejects_missing_or_non_string_safety_level(
    payload: dict[str, object],
) -> None:
    diagnostic, status, audit = parse_safety_diagnostic(
        AgentCallResult(payload=payload, audit={"status": "completed"}),
        "anonymized",
    )

    assert diagnostic is None
    assert status is DiagnosticStatus.INVALID
    assert audit["normalization"] == {"categories": []}


@pytest.mark.parametrize(
    ("payload", "categories"),
    (
        ({"intent": 7, "draft_text": "safe draft"}, []),
        ({"intent": "open_conversation", "need_hint": 7, "draft_text": "safe draft"}, []),
        ({"intent": "unknown", "draft_text": ""}, ["support_unknown_intent_cleared"]),
        ({"intent": "unknown", "draft_text": 7}, ["support_unknown_intent_cleared"]),
        ({"intent": "unknown"}, ["support_unknown_intent_cleared"]),
        (
            {"intent": "unknown", "draft_text": "safe draft", "suggested_support": "unknown"},
            ["support_unknown_intent_cleared"],
        ),
        ({"intent": "open_conversation", "draft_text": "safe draft", "suggested_support": 7}, []),
    ),
)
def test_partial_normalization_rejects_non_string_enum_and_invalid_support_fields(
    payload: dict[str, object], categories: list[str]
) -> None:
    diagnostic, status, audit = parse_support_diagnostic(
        AgentCallResult(payload=payload, audit={"status": "completed"}),
        "anonymized",
    )

    assert diagnostic is None
    assert status is DiagnosticStatus.INVALID
    assert audit["normalization"] == {"categories": categories}


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


def test_invalid_diagnostic_audit_contains_only_validation_shape() -> None:
    diagnostic, status, audit = parse_support_diagnostic(
        AgentCallResult(
            payload={
                "intent": "open_conversation",
                "draft_text": "diagnostic",
                "choice_set": "not-authorized",
            },
            audit={"status": "completed"},
        ),
        "anonymized",
    )

    assert diagnostic is None
    assert status is DiagnosticStatus.INVALID
    assert audit["validation_errors"] == {"fields": ["choice_set"], "types": ["extra_forbidden"]}
    assert "not-authorized" not in repr(audit)


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
async def test_invalid_diagnostic_keeps_the_two_call_provider_budget() -> None:
    calls: list[str] = []

    async def call(agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        del instructions, input_text
        calls.append(agent_name)
        payload = (
            {"level": "invalid-label"}
            if agent_name == "risk"
            else {"intent": "open_conversation", "draft_text": "diagnostic"}
        )
        return AgentCallResult(payload=payload, audit={"status": "completed", "agent": agent_name})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(history=(("user", "anonymized"),), state="open_conversation")
    )

    assert sorted(calls) == ["risk", "support"]
    assert result.safety_status is DiagnosticStatus.INVALID
    assert result.support_status is DiagnosticStatus.COMPLETED


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
