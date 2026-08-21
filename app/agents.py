"""Two concurrent Yandex calls that produce diagnostics, never product actions."""

from __future__ import annotations

import asyncio
import hashlib
import json
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from app.config import settings
from app.domain import (
    DiagnosticStatus,
    NeedKind,
    SafetyDiagnostic,
    SupportDiagnostic,
    SupportIntent,
)
from app.pii import redact_with_audit
from app.skills import load_support_skills

RISK_INSTRUCTIONS = """Ты даёшь только диагностическую оценку опасности в русскоязычном обращении.
Верни единственный JSON-объект без Markdown и без пояснений. Обязательно поле level: critical
для непосредственной угрозы жизни, суицида или насилия сейчас; urgent для ситуации «сегодня
негде ночевать» или выселения прямо сейчас; concern для страха, угрозы или нестабильного жилья
без непосредственной опасности; none если опасности не видно. Допустимы только поля level,
categories, confidence, rationale и evidence_claims. Просьба поговорить с человеком, ботом или
психологом не является риском. Не предлагай действий, кнопок или переходов."""

SUPPORT_INSTRUCTIONS = """Ты ведёшь живой русскоязычный разговор Невидимого фонда.
Верни единственный JSON-объект без Markdown и без пояснений. Обязательны intent и draft_text;
допустимы только intent, need_hint, evidence_claims, draft_text и suggested_support=psychologist.
intent должен быть ровно одним из: open_conversation, concrete_need, aid_interest,
psychologist_considering, psychologist_request, verified_information, explicit_human_request,
close.
draft_text — честная разговорная реплика, без обещаний, что человек уже позван, заявка сохранена,
помощь организована или контакт передан. Не возвращай action, next_action, choice_set,
catalog_item_ids, callback IDs, workflow state, effect, переход или описание выполненного внешнего
действия. Просьбы «выслушай», «хочу выговориться» и «можно с тобой поговорить» — разговор, а не handoff."""


@dataclass(frozen=True)
class ProviderSettings:
    temperature: float = 0.0
    max_tokens: int = 300
    reasoning_effort: str = "none"
    data_logging_enabled: bool = False

    def model_settings(self) -> OpenAIResponsesModelSettings:
        return OpenAIResponsesModelSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            openai_reasoning_effort=self.reasoning_effort,
        )

    def audit_fields(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reasoning_effort": self.reasoning_effort,
            "data_logging_enabled": self.data_logging_enabled,
        }


DEFAULT_PROVIDER_SETTINGS = ProviderSettings()
PROVIDER_TIMEOUT_SECONDS = 12.0
_SAFETY_RATIONALE_MAX_LENGTH = 240
_NORMALIZATION_CATEGORIES = frozenset({
    "safety_rationale_truncated",
    "support_unknown_intent_cleared",
    "support_unknown_need_hint_cleared",
})


@dataclass(frozen=True)
class AgentContext:
    history: tuple[tuple[str, str], ...]
    state: str
    catalog: tuple[dict[str, Any], ...] = ()
    knowledge: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentCallResult:
    payload: dict[str, Any]
    audit: dict[str, Any]


@dataclass(frozen=True)
class AgentEvaluation:
    """Gateway result containing diagnostics and transport/schema health only."""

    safety: SafetyDiagnostic | None = None
    support: SupportDiagnostic | None = None
    safety_status: DiagnosticStatus = DiagnosticStatus.UNAVAILABLE
    support_status: DiagnosticStatus = DiagnosticStatus.UNAVAILABLE
    safety_audit: dict[str, Any] | None = None
    support_audit: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "safety_audit", dict(self.safety_audit or {}))
        object.__setattr__(self, "support_audit", dict(self.support_audit or {}))


Call = Callable[[str, str, str], Awaitable[AgentCallResult]]


def yandex_model_settings(
    provider_settings: ProviderSettings = DEFAULT_PROVIDER_SETTINGS,
) -> OpenAIResponsesModelSettings:
    return provider_settings.model_settings()


def create_yandex_client() -> AsyncOpenAI:
    """Construct the one-shot provider client with the fixed no-retry transport budget."""
    return AsyncOpenAI(
        api_key=settings.yandex_ai_api_key,
        base_url="https://ai.api.cloud.yandex.net/v1",
        project=settings.yandex_cloud_folder_id,
        default_headers={"x-data-logging-enabled": "false"},
        timeout=PROVIDER_TIMEOUT_SECONDS,
        max_retries=0,
    )


def yandex_output_type(agent_name: str) -> type[str]:
    """Use a single text response and validate one JSON object at our provider boundary."""
    del agent_name
    return str


def parse_provider_json_object(raw_output: str) -> dict[str, Any]:
    """Accept exactly one JSON object, optionally wrapped in a known Markdown code fence."""
    candidate = raw_output.strip()
    for prefix in ("```json\n", "```\n"):
        if candidate.lower().startswith(prefix) and candidate.endswith("\n```"):
            candidate = candidate[len(prefix) : -4].strip()
            break
    try:
        parsed = json.loads(
            candidate,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise ValueError("duplicate_json_key")
        parsed[key] = value
    return parsed


def _reject_nonstandard_json_constant(value: str) -> None:
    del value
    raise ValueError("nonstandard_json_constant")


def provider_output_shape(raw_output: str) -> dict[str, bool | int]:
    """Return only non-content metadata needed to diagnose provider output envelopes."""
    candidate = raw_output.strip()
    return {
        "characters": len(raw_output),
        "nonempty": bool(candidate),
        "starts_json": candidate.startswith("{"),
        "ends_object": candidate.endswith("}"),
        "starts_code_fence": candidate.startswith("```"),
        "ends_code_fence": candidate.endswith("```"),
    }


def usage_audit(usage: Any) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cached_tokens": usage.cache_read_tokens,
    }


class YandexAgentGateway:
    """Provider boundary. Product behavior is resolved after both calls complete."""

    def __init__(
        self,
        call: Call | None = None,
        provider_settings: ProviderSettings = DEFAULT_PROVIDER_SETTINGS,
    ) -> None:
        self._call = call or self._call_live
        self._provider_settings = provider_settings

    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        transcript, pii_audit = format_redacted_transcript(context.history)
        current_user_text = _current_user_text(context.history)
        current_redacted = redact_with_audit(current_user_text).text
        safety_task = asyncio.create_task(
            self._run("risk", RISK_INSTRUCTIONS, format_safety_context(context, current_redacted), pii_audit)
        )
        support_task = asyncio.create_task(
            self._run(
                "support",
                f"{SUPPORT_INSTRUCTIONS}\n\n{load_support_skills()}",
                format_agent_context(context, transcript),
                pii_audit,
            )
        )
        safety_result, support_result = await asyncio.gather(safety_task, support_task)
        safety, safety_status, safety_audit = parse_safety_diagnostic(safety_result, current_user_text)
        support, support_status, support_audit = parse_support_diagnostic(support_result, current_user_text)
        return AgentEvaluation(
            safety=safety,
            support=support,
            safety_status=safety_status,
            support_status=support_status,
            safety_audit=safety_audit,
            support_audit=support_audit,
        )

    async def _run(
        self, agent_name: str, instructions: str, input_text: str, pii_audit: dict[str, Any]
    ) -> AgentCallResult:
        input_hash = hashlib.sha256(input_text.encode()).hexdigest()
        try:
            result = await self._call(agent_name, instructions, input_text)
        except Exception as error:  # noqa: BLE001 - provider boundary must not expose provider content
            result = AgentCallResult(payload={}, audit={"status": "error", "error_type": type(error).__name__})
        return AgentCallResult(
            payload=result.payload,
            audit={
                "provider": "yandex_ai_studio",
                "agent": agent_name,
                "input_hash": input_hash,
                "request": self._provider_settings.audit_fields(),
                "pii_redaction": pii_audit,
                **result.audit,
            },
        )

    async def _call_live(self, agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        if not settings.llm_enabled or not settings.yandex_ai_api_key:
            return AgentCallResult(payload={}, audit={"status": "not_configured"})
        client = create_yandex_client()
        started = perf_counter()
        try:
            model = OpenAIResponsesModel(
                f"gpt://{settings.yandex_cloud_folder_id}/{settings.yandex_ai_model}",
                provider=OpenAIProvider(openai_client=client),
                profile=OpenAIModelProfile(openai_supports_json_object_output=False),
            )
            agent = Agent(
                model,
                output_type=yandex_output_type(agent_name),
                instructions=instructions,
                model_settings=yandex_model_settings(self._provider_settings),
                retries=0,
            )
            result = await agent.run(input_text)
            output = parse_provider_json_object(result.output)
            response = result.response
            return AgentCallResult(
                payload=output,
                audit={
                    "status": "completed",
                    "rationale_alias_used": bool(
                        getattr(output, "rationale_alias_used", False)
                    ),
                    "response_id": getattr(response, "provider_response_id", None),
                    "model": getattr(response, "model_name", None),
                    "latency_ms": round((perf_counter() - started) * 1000),
                    "usage": usage_audit(result.usage),
                    "output_shape": provider_output_shape(result.output),
                },
            )
        except Exception as error:  # noqa: BLE001 - SDK/provider errors have no stable common base
            origin = traceback.extract_tb(error.__traceback__)[-1]
            return AgentCallResult(
                payload={},
                audit={
                    "status": "error",
                    "error_type": type(error).__name__,
                    "error_origin": f"{origin.name}:{origin.lineno}",
                    "latency_ms": round((perf_counter() - started) * 1000),
                },
            )
        finally:
            await client.close()


def format_redacted_transcript(history: tuple[tuple[str, str], ...]) -> tuple[str, dict[str, Any]]:
    role_names = {"user": "Пользователь", "assistant": "Бот"}
    redactions = [redact_with_audit(content) for _, content in history]
    transcript = "\n\n".join(
        f"{role_names.get(role, role)}: {redaction.text}"
        for (role, _), redaction in zip(history, redactions, strict=True)
    )
    entity_counts = Counter(
        entity for redaction in redactions for entity, count in redaction.audit["entity_counts"].items() for _ in range(count)
    )
    return transcript, {
        "engine": "presidio",
        "messages_processed": len(redactions),
        "messages_with_pii": sum(redaction.audit["detected"] for redaction in redactions),
        "entities_total": sum(redaction.audit["entities_total"] for redaction in redactions),
        "entity_counts": dict(sorted(entity_counts.items())),
    }


def format_safety_context(context: AgentContext, current_user_text: str) -> str:
    return f"Состояние диалога: {context.state}\n\nТекущее сообщение пользователя:\n{current_user_text}"


def format_agent_context(context: AgentContext, transcript: str) -> str:
    catalog = "\n".join(f"- {item}" for item in context.catalog) or "- каталог пока не нужен"
    knowledge = "\n".join(f"- {item}" for item in context.knowledge) or "- проверенной справки нет"
    return (
        f"Состояние диалога: {context.state}\n\nДоступная помощь:\n{catalog}\n\n"
        f"Проверенная информация:\n{knowledge}\n\nИстория:\n{transcript}"
    )


def parse_safety_diagnostic(
    result: AgentCallResult,
    current_user_text: str,
) -> tuple[SafetyDiagnostic | None, DiagnosticStatus, dict[str, Any]]:
    if result.audit.get("status") != "completed":
        return None, DiagnosticStatus.UNAVAILABLE, _diagnostic_audit(result.audit, DiagnosticStatus.UNAVAILABLE)
    payload = dict(result.payload)
    alias_used = bool(result.audit.get("rationale_alias_used")) or (
        "rationale" not in payload and "rationale_short" in payload
    )
    alias_value = payload.pop("rationale_short", None)
    if "rationale" not in payload and alias_value is not None:
        payload["rationale"] = alias_value
    normalized, normalization_categories = _normalize_safety_payload(payload)
    try:
        diagnostic = SafetyDiagnostic.model_validate(normalized)
    except ValidationError as error:
        audit = _normalized_diagnostic_audit(result.audit, DiagnosticStatus.INVALID, normalization_categories)
        audit["validation_errors"] = validation_error_shape(error)
        return None, DiagnosticStatus.INVALID, audit
    audit = _normalized_diagnostic_audit(result.audit, DiagnosticStatus.COMPLETED, normalization_categories)
    audit["rationale_alias_used"] = alias_used
    audit["evidence"] = _validate_evidence_claims(diagnostic.evidence_claims, current_user_text)
    return diagnostic.model_copy(update={"evidence_claims": ()}), DiagnosticStatus.COMPLETED, audit


def parse_support_diagnostic(
    result: AgentCallResult,
    current_user_text: str,
) -> tuple[SupportDiagnostic | None, DiagnosticStatus, dict[str, Any]]:
    if result.audit.get("status") != "completed":
        return None, DiagnosticStatus.UNAVAILABLE, _diagnostic_audit(result.audit, DiagnosticStatus.UNAVAILABLE)
    payload, normalization_categories = _normalize_support_payload(dict(result.payload))
    try:
        diagnostic = SupportDiagnostic.model_validate(payload)
    except ValidationError as error:
        audit = _normalized_diagnostic_audit(result.audit, DiagnosticStatus.INVALID, normalization_categories)
        audit["validation_errors"] = validation_error_shape(error)
        return None, DiagnosticStatus.INVALID, audit
    audit = _normalized_diagnostic_audit(result.audit, DiagnosticStatus.COMPLETED, normalization_categories)
    audit["evidence"] = _validate_evidence_claims(diagnostic.evidence_claims, current_user_text)
    return diagnostic.model_copy(update={"evidence_claims": ()}), DiagnosticStatus.COMPLETED, audit


def _current_user_text(history: tuple[tuple[str, str], ...]) -> str:
    return next((content for role, content in reversed(history) if role == "user"), "")


def _validate_evidence_claims(claims: tuple[str, ...], current_user_text: str) -> dict[str, Any]:
    valid = tuple(claim for claim in claims if claim and claim in current_user_text)
    return {
        "claims": len(claims),
        "valid": len(valid),
        "invalid": len(claims) - len(valid),
        "hashes": [hashlib.sha256(claim.encode()).hexdigest() for claim in claims],
    }


def validation_error_shape(error: ValidationError) -> dict[str, list[str]]:
    """Keep validation metadata useful without retaining provider-supplied values."""
    errors = error.errors(include_url=False, include_context=False, include_input=False)
    return {
        "fields": sorted({".".join(str(part) for part in item["loc"]) for item in errors}),
        "types": sorted({str(item["type"]) for item in errors}),
    }


def _normalize_safety_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    categories: set[str] = set()
    rationale = payload.get("rationale")
    if isinstance(rationale, str) and len(rationale) > _SAFETY_RATIONALE_MAX_LENGTH:
        payload["rationale"] = rationale[:_SAFETY_RATIONALE_MAX_LENGTH]
        categories.add("safety_rationale_truncated")
    return payload, frozenset(categories)


def _normalize_support_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    categories: set[str] = set()
    for field, enum_type, category in (
        ("intent", SupportIntent, "support_unknown_intent_cleared"),
        ("need_hint", NeedKind, "support_unknown_need_hint_cleared"),
    ):
        value = payload.get(field)
        if isinstance(value, str) and value not in {item.value for item in enum_type}:
            payload[field] = None
            categories.add(category)
    return payload, frozenset(categories)


def _normalized_diagnostic_audit(
    audit: dict[str, Any],
    status: DiagnosticStatus,
    categories: frozenset[str],
) -> dict[str, Any]:
    result = _diagnostic_audit(audit, status)
    result["normalization"] = {
        "categories": sorted(category for category in categories if category in _NORMALIZATION_CATEGORIES)
    }
    return result


def _diagnostic_audit(audit: dict[str, Any], status: DiagnosticStatus) -> dict[str, Any]:
    return {**audit, "status": status.value, "diagnostic_status": status.value}
