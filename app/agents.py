"""Two concurrent Yandex calls that produce diagnostics, never product actions."""

from __future__ import annotations

import asyncio
import hashlib
import traceback
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any

from openai import AsyncOpenAI
from pydantic import ValidationError
from pydantic_ai import Agent, PromptedOutput
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider

from app.config import settings
from app.domain import (
    DiagnosticStatus,
    RiskAssessment,
    RiskLevel,
    SafetyDiagnostic,
    SupportDiagnostic,
    SupportPlan,
)
from app.pii import redact_with_audit
from app.skills import load_support_skills

RISK_INSTRUCTIONS = """Ты даёшь только диагностическую оценку опасности в русскоязычном обращении.
Верни JSON с level: critical для непосредственной угрозы жизни, суицида или насилия сейчас;
urgent для ситуации «сегодня негде ночевать» или выселения прямо сейчас; concern для страха,
угрозы или нестабильного жилья без непосредственной опасности; none если опасности не видно.
Просьба поговорить с человеком, ботом или психологом не является риском. Верни только поля
level, categories, confidence, rationale и optional evidence_claims. evidence_claims — точные
короткие фрагменты текущего сообщения, без пересказа. Не предлагай действий, кнопок или переходов."""

SUPPORT_INSTRUCTIONS = """Ты ведёшь живой русскоязычный разговор Невидимого фонда.
Верни только диагностический JSON: intent, optional need_hint, optional evidence_claims,
draft_text и optional suggested_support=psychologist. draft_text — честная разговорная реплика,
без обещаний, что человек уже позван, заявка сохранена, помощь организована или контакт передан.
Не возвращай action, next_action, choice_set, catalog_item_ids, callback IDs, workflow state,
effect, переход или описание выполненного внешнего действия. Просьбы «выслушай», «хочу
выговориться» и «можно с тобой поговорить» — разговор, а не handoff."""


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


@dataclass(frozen=True, init=False)
class AgentEvaluation:
    """Gateway result with transport health isolated from semantic model labels.

    Legacy fixture inputs are converted at construction, but the result exposes only
    diagnostics and their statuses. Production text handling consumes those values only.
    """

    safety: SafetyDiagnostic | None
    support: SupportDiagnostic | None
    safety_status: DiagnosticStatus
    support_status: DiagnosticStatus
    safety_audit: dict[str, Any]
    support_audit: dict[str, Any]

    def __init__(
        self,
        *,
        safety: SafetyDiagnostic | None = None,
        support: SupportDiagnostic | None = None,
        safety_status: DiagnosticStatus | None = None,
        support_status: DiagnosticStatus | None = None,
        safety_audit: dict[str, Any] | None = None,
        support_audit: dict[str, Any] | None = None,
        risk: RiskAssessment | None = None,
        plan: SupportPlan | None = None,
        risk_audit: dict[str, Any] | None = None,
    ) -> None:
        if safety is not None and not isinstance(safety, SafetyDiagnostic):
            raise TypeError("safety must be a SafetyDiagnostic or None")
        if support is not None and not isinstance(support, SupportDiagnostic):
            raise TypeError("support must be a SupportDiagnostic or None")
        if risk is not None and not isinstance(risk, RiskAssessment):
            raise TypeError("risk must be a RiskAssessment or None")
        if plan is not None and not isinstance(plan, SupportPlan):
            raise TypeError("plan must be a SupportPlan or None")
        if safety is None and risk is not None:
            safety = SafetyDiagnostic(
                level=risk.level,
                categories=risk.categories,
                confidence=risk.confidence,
                rationale=risk.rationale or "legacy diagnostic",
            )
        if support is None and plan is not None:
            support = SupportDiagnostic(
                intent=plan.intent,
                need_hint=plan.need,
                draft_text=plan.text,
                suggested_support=plan.offered_support,
            )
        object.__setattr__(self, "safety", safety)
        object.__setattr__(self, "support", support)
        object.__setattr__(
            self,
            "safety_status",
            safety_status or (DiagnosticStatus.COMPLETED if safety is not None else DiagnosticStatus.UNAVAILABLE),
        )
        object.__setattr__(
            self,
            "support_status",
            support_status or (DiagnosticStatus.COMPLETED if support is not None else DiagnosticStatus.UNAVAILABLE),
        )
        object.__setattr__(self, "safety_audit", dict(safety_audit or risk_audit or {}))
        object.__setattr__(self, "support_audit", dict(support_audit or {}))

    @property
    def risk_audit(self) -> dict[str, Any]:
        return self.safety_audit


Call = Callable[[str, str, str], Awaitable[AgentCallResult]]


def yandex_model_settings(
    provider_settings: ProviderSettings = DEFAULT_PROVIDER_SETTINGS,
) -> OpenAIResponsesModelSettings:
    return provider_settings.model_settings()


def yandex_output_type(agent_name: str) -> PromptedOutput[Any]:
    output = SafetyDiagnostic if agent_name == "risk" else SupportDiagnostic
    return PromptedOutput(
        output,
        name=f"{agent_name}_diagnostic",
        description="Верни только один JSON-объект, соответствующий этой схеме.",
    )


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
        client = AsyncOpenAI(
            api_key=settings.yandex_ai_api_key,
            base_url="https://ai.api.cloud.yandex.net/v1",
            project=settings.yandex_cloud_folder_id,
            default_headers={"x-data-logging-enabled": "false"},
            timeout=12.0,
            max_retries=0,
        )
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
            output = result.output
            response = result.response
            return AgentCallResult(
                payload=output.model_dump(mode="json"),
                audit={
                    "status": "completed",
                    "rationale_alias_used": bool(
                        getattr(output, "rationale_alias_used", False)
                    ),
                    "response_id": getattr(response, "provider_response_id", None),
                    "model": getattr(response, "model_name", None),
                    "latency_ms": round((perf_counter() - started) * 1000),
                    "usage": usage_audit(result.usage),
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
    try:
        diagnostic = SafetyDiagnostic.model_validate(payload)
    except ValidationError:
        return None, DiagnosticStatus.INVALID, _diagnostic_audit(result.audit, DiagnosticStatus.INVALID)
    audit = _diagnostic_audit(result.audit, DiagnosticStatus.COMPLETED)
    audit["rationale_alias_used"] = alias_used
    audit["evidence"] = _validate_evidence_claims(diagnostic.evidence_claims, current_user_text)
    return diagnostic.model_copy(update={"evidence_claims": ()}), DiagnosticStatus.COMPLETED, audit


def parse_support_diagnostic(
    result: AgentCallResult,
    current_user_text: str,
) -> tuple[SupportDiagnostic | None, DiagnosticStatus, dict[str, Any]]:
    if result.audit.get("status") != "completed":
        return None, DiagnosticStatus.UNAVAILABLE, _diagnostic_audit(result.audit, DiagnosticStatus.UNAVAILABLE)
    payload = dict(result.payload)
    try:
        diagnostic = SupportDiagnostic.model_validate(payload)
    except ValidationError:
        return None, DiagnosticStatus.INVALID, _diagnostic_audit(result.audit, DiagnosticStatus.INVALID)
    audit = _diagnostic_audit(result.audit, DiagnosticStatus.COMPLETED)
    audit["evidence"] = _validate_evidence_claims(diagnostic.evidence_claims, current_user_text)
    return diagnostic.model_copy(update={"evidence_claims": ()}), DiagnosticStatus.COMPLETED, audit


def parse_risk(result: AgentCallResult) -> tuple[RiskAssessment, dict[str, Any]]:
    """Compatibility adapter; production flow uses `parse_safety_diagnostic`."""
    diagnostic, _, audit = parse_safety_diagnostic(result, "")
    if diagnostic is None:
        return RiskAssessment(level=RiskLevel.NONE, detector="diagnostic-unavailable"), audit
    return RiskAssessment(
        level=diagnostic.level,
        categories=diagnostic.categories,
        confidence=diagnostic.confidence,
        rationale=diagnostic.rationale,
        detector="model-diagnostic",
    ), audit


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


def _diagnostic_audit(audit: dict[str, Any], status: DiagnosticStatus) -> dict[str, Any]:
    return {**audit, "status": status.value, "diagnostic_status": status.value}
