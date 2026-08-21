"""Two independent, structured Yandex AI Studio calls for one user message."""

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
from app.domain import RiskAssessment, RiskLevel, SupportPlan
from app.pii import redact_with_audit
from app.skills import load_support_skills

RISK_INSTRUCTIONS = """Ты классифицируешь только опасность в русскоязычном обращении.
Верни JSON: critical — непосредственная угроза жизни, суицид или насилие сейчас;
urgent — сегодня негде ночевать или выселение прямо сейчас; concern — страх,
угроза или нестабильное жильё без непосредственной опасности; none — опасности
не видно. Просьба поговорить с человеком не является риском. Не выводи concern из
одиночества, усталости, горя, просьбы выслушать, просьбы о человеке или психологе,
либо обычной потребности в помощи. Без прямого указания на угрозу, страх насилия или
нестабильное жильё выбирай none. Верни поля level, categories, confidence и rationale;
не используй rationale_short. Нехватка еды или денег сама по себе — none, а не concern.
Urgent выбирай только при явно негде ночевать сегодня или выселении прямо сейчас.
Страх или нестабильное жильё без этого — concern."""

SUPPORT_INSTRUCTIONS = """Ты ведёшь живой русскоязычный разговор Невидимого фонда.
Верни SupportPlan. Просьбы «выслушай», «хочу выговориться» и «можно с тобой
поговорить» — open_conversation/continue_conversation, не handoff. Только явные
«позовите человека», «хочу живого специалиста», «не хочу говорить с ботом» —
explicit_human_request/request_human. Не показывай need_categories в обычном
разговоре. Психолога сначала мягко предложи текстом с offered_support=psychologist;
при осторожном интересе используй psychologist_considering, а при однозначном
согласии — psychologist_request/start_psychologist_request. Если пользователь
спрашивает о психологе или отвечает осторожным интересом, используй
psychologist_considering/clarify и choice_set=psychologist_interest. Для concrete_need
или aid_interest обязательно укажи need: housing, food_money, legal, support, children
или other; без need не выбирай offer_aid. Выраженная потребность в помощи или интерес
к доступным вариантам — concrete_need либо aid_interest с offer_aid и подходящим need,
а не open_conversation. Даже при urgent жилье верни concrete_need/offer_aid с
need=housing. Вопрос об условиях или возможности психолога не возвращай как
open_conversation. Описание опасности без практической просьбы о помощи остаётся
open_conversation. Не создавай callback ID."""


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
    risk: RiskAssessment
    plan: SupportPlan | None
    risk_audit: dict[str, Any]
    support_audit: dict[str, Any]

    def __post_init__(self) -> None:
        if self.plan is not None and not isinstance(self.plan, SupportPlan):
            raise TypeError("plan must be a SupportPlan or None")


Call = Callable[[str, str, str], Awaitable[AgentCallResult]]


def yandex_model_settings() -> OpenAIResponsesModelSettings:
    """Use Qwen without reasoning to preserve a bounded conversational response."""
    return OpenAIResponsesModelSettings(
        temperature=0.0,
        max_tokens=300,
        openai_reasoning_effort="none",
    )


def yandex_output_type(agent_name: str) -> PromptedOutput[Any]:
    """Keep Pydantic validation while avoiding Qwen's unsupported native JSON schema mode."""
    return PromptedOutput(
        RiskAssessment if agent_name == "risk" else SupportPlan,
        name=f"{agent_name}_result",
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
    """Owns provider transport; product state and side effects remain outside it."""

    def __init__(self, call: Call | None = None) -> None:
        self._call = call or self._call_live

    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        transcript, pii_audit = format_redacted_transcript(context.history)
        context_text = format_agent_context(context, transcript)
        risk_task = asyncio.create_task(self._run("risk", RISK_INSTRUCTIONS, context_text, pii_audit))
        support_task = asyncio.create_task(
            self._run(
                "support",
                f"{SUPPORT_INSTRUCTIONS}\n\n{load_support_skills()}",
                context_text,
                pii_audit,
            )
        )
        risk_result, support_result = await asyncio.gather(risk_task, support_task)
        risk, risk_audit = parse_risk(risk_result)
        plan, support_audit = parse_support_plan(support_result)
        return AgentEvaluation(
            risk=risk,
            plan=plan,
            risk_audit=risk_audit,
            support_audit=support_audit,
        )

    async def _run(
        self, agent_name: str, instructions: str, input_text: str, pii_audit: dict[str, Any]
    ) -> AgentCallResult:
        input_hash = hashlib.sha256(input_text.encode()).hexdigest()
        try:
            result = await self._call(agent_name, instructions, input_text)
        except Exception as error:  # noqa: BLE001 - provider boundary must degrade without exposing content
            result = AgentCallResult(
                payload={},
                audit={"status": "error", "error_type": type(error).__name__},
            )
        audit = {
            "provider": "yandex_ai_studio",
            "agent": agent_name,
            "input_hash": input_hash,
            "request": {
                "temperature": 0.0,
                "max_tokens": 300,
                "reasoning_effort": "none",
                "data_logging_enabled": False,
            },
            "pii_redaction": pii_audit,
            **result.audit,
        }
        return AgentCallResult(payload=result.payload, audit=audit)

    async def _call_live(self, agent_name: str, instructions: str, input_text: str) -> AgentCallResult:
        if not settings.llm_enabled or not settings.yandex_ai_api_key:
            return AgentCallResult(payload={}, audit={"status": "not_configured"})
        client = AsyncOpenAI(
            api_key=settings.yandex_ai_api_key,
            base_url="https://ai.api.cloud.yandex.net/v1",
            project=settings.yandex_cloud_folder_id,
            default_headers={"x-data-logging-enabled": "false"},
            timeout=12.0,
            max_retries=1,
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
                model_settings=yandex_model_settings(),
                retries=0,
            )
            result = await agent.run(input_text)
            output = result.output
            payload = output.model_dump(mode="json")
            usage = result.usage
            response = result.response
            return AgentCallResult(
                payload=payload,
                audit={
                    "status": "completed",
                    "response_id": getattr(response, "provider_response_id", None),
                    "model": getattr(response, "model_name", None),
                    "latency_ms": round((perf_counter() - started) * 1000),
                    "usage": usage_audit(usage),
                },
            )
        except Exception as error:  # noqa: BLE001 - SDK/provider errors share no stable base exception
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


def format_agent_context(context: AgentContext, transcript: str) -> str:
    catalog = "\n".join(f"- {item}" for item in context.catalog) or "- каталог пока не нужен"
    knowledge = "\n".join(f"- {item}" for item in context.knowledge) or "- проверенной справки нет"
    return (
        f"Состояние диалога: {context.state}\n\n"
        f"Доступная помощь:\n{catalog}\n\n"
        f"Проверенная информация:\n{knowledge}\n\n"
        f"История:\n{transcript}"
    )


def parse_risk(result: AgentCallResult) -> tuple[RiskAssessment, dict[str, Any]]:
    payload = dict(result.payload)
    rationale_short = payload.pop("rationale_short", None)
    if "rationale" not in payload and rationale_short is not None:
        payload["rationale"] = rationale_short
    try:
        assessment = RiskAssessment.model_validate({**payload, "detector": "model"})
    except ValidationError:
        return (
            RiskAssessment(level=RiskLevel.UNKNOWN, detector="model", rationale="model response unavailable"),
            {**result.audit, "status": "validation_error"},
        )
    return assessment, result.audit


def parse_support_plan(result: AgentCallResult) -> tuple[SupportPlan | None, dict[str, Any]]:
    try:
        return SupportPlan.model_validate(result.payload), result.audit
    except ValidationError:
        return None, {**result.audit, "status": "validation_error"}
