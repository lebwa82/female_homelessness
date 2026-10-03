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

from app.config import settings
from app.domain import (
    DiagnosticStatus,
    NeedKind,
    SafetyDiagnostic,
    SupportDiagnostic,
    SupportIntent,
)
from app.pii import redact_with_audit

RISK_INSTRUCTIONS = """Ты даёшь только диагностическую оценку опасности в русскоязычном обращении.
Верни единственный JSON-объект без Markdown и без пояснений. Обязательны level и escalation.
level — срочность: critical для непосредственной угрозы жизни, суицида или насилия сейчас;
urgent для ситуации «сегодня негде ночевать» или выселения прямо сейчас; concern для
конкретной угрозы, опасности для детей или нестабильного жилья без опасности прямо сейчас;
none если таких оснований нет. Само слово «страшно» не является основанием для concern.
escalation — маршрут продукта, независимо от level: suicide при суицидальном смысле; handoff
при угрозе или насилии, остром бездомье, опасности для детей или эмоциональном кризисе;
none иначе. Возможные categories: violence_threat, acute_homelessness, child_safety,
emotional_crisis, suicide. Указывай только подтверждённые смыслом
сообщения категории. Например, страх, что партнёр заберёт детей — child_safety и handoff даже
при level concern; «сегодня ночую на улице» — acute_homelessness и handoff; «хочу исчезнуть» —
suicide и suicide.
Допустимы только поля level, escalation, categories, confidence, rationale и evidence_claims.
Не предлагай действий, кнопок или переходов.
Оцени только текущее сообщение пользователя, не текст этих инструкций.
Перечень categories — допустимые значения, а не готовый ответ: если основания
отсутствуют, верни пустой список. Обычный запрос ресурса, совета или беседы
не является опасностью. Просьбы подключить сотрудника, психолога или юриста обрабатывает
отдельный классификатор намерений: здесь оценивай только независимые признаки риска.
Если опасности нет, level=none, escalation=none, categories=[].
evidence_claims — только точные цитаты из текущего сообщения; если цитат нет, список пустой."""

RISK_BOUNDARIES = """Границы оценки: неприятные чувства, одиночество, усталость или нехватка
обычных вещей сами по себе не доказывают угрозу, потерю безопасности или эмоциональный кризис.
Если нет указания на опасность, угрозы со стороны людей, острое отсутствие жилья или утрату
способности справляться, level=none и escalation=none. Оцени смысл, а не наличие эмоциональных
слов. Не понижай подтверждённую опасность из-за спокойного тона. Ни просьба о разговоре,
ни запись на консультацию сами по себе не свидетельствуют о риске.
Подтверждённая нестабильность жилья, даже на перспективу, требует внимания дежурной:
level=concern, escalation=handoff. Это не срочная угроза жизни; не повышай level до critical.
rationale — одно короткое объяснение длиной не более 240 символов; evidence_claims — до 5 цитат.
"""

SUPPORT_INSTRUCTIONS = """Ты ведёшь живой русскоязычный разговор Невидимого фонда.
Верни единственный JSON-объект без Markdown и без пояснений. Обязательны intent и draft_text;
допустимы только intent, need_hints, evidence_claims, draft_text и suggested_support=psychologist.
intent должен быть ровно одним из: open_conversation, concrete_need, aid_interest,
psychologist_considering, psychologist_request, verified_information, explicit_human_request,
close.
need_hints — список из нуля или нескольких значений housing, food_money, legal, support,
children, other. Указывай в нём только конкретные виды помощи, которые действительно уместно
показать сейчас отдельными кнопками. Можно вернуть несколько значений; пустой список, если
человеку сейчас важнее просто разговор.
draft_text — честная разговорная реплика, без обещаний, что человек уже позван, заявка сохранена,
помощь организована или контакт передан. Не возвращай action, next_action, choice_set,
catalog_item_ids, callback IDs, workflow state, effect, переход или описание выполненного внешнего
действия. Просьбы «выслушай», «хочу выговориться» и «можно с тобой поговорить» — разговор, а не handoff."""


@dataclass(frozen=True)
class ProviderSettings:
    # These two calls make product decisions as well as draft prose: prefer
    # stable intent/risk boundaries over variation of otherwise identical input.
    temperature: float = 0.0
    max_tokens: int = 1500
    reasoning_effort: str = "none"
    data_logging_enabled: bool = False

    def model_settings(self) -> dict[str, Any]:
        return {
            "temperature": self.temperature,
            "max_output_tokens": self.max_tokens,
            "reasoning": {"effort": self.reasoning_effort},
        }

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
    "direct_human_request_level_normalized",
    "safety_rationale_truncated",
    "support_unknown_intent_cleared",
    "support_unknown_need_hints_cleared",
})

SUPPORT_CLASSIFICATION_CONTRACT = """Контракт классификации важнее примеров и описаний навыков.
Классифицируй намерение последней реплики с учётом истории и pending_offer, а не тему вообще.
intent — намерение ПОЛЬЗОВАТЕЛЬНИЦЫ, а не твой план оказать помощь или вызвать специалиста.
Определяй его независимо от текущего state и доступности услуги в каталоге.
Даже если услуга недоступна, конкретный запрос остаётся concrete_need, а просьба
подключить человека — explicit_human_request. Не заменяй их open_conversation
из-за того, что сам не выполняешь действия: их выполняет backend по твоей диагностике.
Сначала выдели факты и потребности, затем выбери intent, затем составь ответ.
Выбери ОДИН intent по смыслу:
- explicit_human_request: просьба связать с живым сотрудником/специалисткой вместо бота,
  в том числе отказ общаться именно с ботом. Просто выговориться боту — open_conversation.
  Отказ от БОТА означает выбор другого собеседника даже без слова «человек»:
  этот случай имеет приоритет над close, не завершай разговор за пользовательницу.
  Опасное состояние без просьбы подключить человека не является explicit_human_request:
  необходимую эскалацию выполнит отдельная диагностика, здесь остаётся open_conversation.
- psychologist_request: человек хочет поговорить с психологом, записаться или принимает
  такое предложение. Сомнения или вопросы о процессе — psychologist_considering.
  Предположение, что психолог мог бы помочь, ещё не согласие записаться.
- psychologist_considering: интерес, сомнения, запрос подробностей о психологе; при
  pending_offer=psychologist короткий вопрос о предложении тоже относится сюда.
  Без упоминания психолога самой пользовательницей или такого предложения в истории
  этот intent не подходит. Эмоциональная поддержка сама по себе — open_conversation.
- concrete_need: конкретная практическая проблема/потребность (жильё, еда, вещи для детей,
  документы, семейные права, оплата необходимых расходов и транспорта), даже если она
  описана через переживания, без прямой просьбы. Денежные расходы — food_money.
  Вопросы опеки, утраты доступа к детям, семейных прав — concrete_need с children и legal;
  волнение не отменяет практическую потребность. Поддержку можно добавить одновременно.
  Это правило действует и когда человек только сообщает о страхе утраты детей, не просит
  юридической консультации явно. Предмет сообщения — дети и семейные права, не только эмоции.
- aid_interest: общий вопрос о доступной помощи, когда потребность ещё не названа.
  Не приписывай все категории: need_hints=[] до уточнения потребности.
- verified_information: запрос проверенной правовой/справочной информации, не вопрос о
  предложенной консультации и не просьба связать со специалистом.
- close: явное завершение разговора вообще, а не отказ от его автоматического формата.
- open_conversation: остальной свободный разговор, в том числе эмоциональная поддержка.
  Одиночество, грусть или желание выговориться без конкретной практической просьбы
  относятся сюда; это не concrete_need и не психологическая заявка. need_hints=[].
Уровень опасности определяет отдельный классификатор: crisis, emergency, suicide, handoff
НЕ являются допустимыми intent. Даже при опасности верни intent из списка и короткую
бережную draft_text; не добавляй поля уровня риска и не обещай действий системы.
suggested_support можно опустить, поставить null или psychologist, других значений нет.
draft_text — 1–1200 символов. Не придумывай ресурсы или детали услуг. Не предлагай список
выбора, не отражённый в need_hints. need_hints — только реальные актуальные потребности,
без повторов и без вывода потребности только из названия категории в сообщениях бота.
"""


SUPPORT_TONE = """Отвечай спокойно, коротко и по существу последней реплики.
Не требуй имени, адреса или объяснения всей ситуации. Не ставь диагнозов, не спорь,
не оценивай человека. Можно задать один мягкий открытый вопрос, если он действительно
поможет продолжить разговор. Если человек просит выслушать — выслушай, не переключай
на анкету или услуги. Конкретные меню, запись, контакты и опросы ведёт приложение
по утверждённому HTML-сценарию, а не ты. Не сочиняй юридические рекомендации,
номера, адреса или условия помощи. Не утверждай, что связаться с человеком невозможно:
для этого у приложения есть отдельное действие. Справочная информация допустима только из
переданных проверенных материалов; если их нет, честно обозначь ограничение.
Предложение рассказать об услуге и просьба рассказать — обмен информацией,
не согласие на запись. psychologist_request требует просьбы именно о встрече,
записи или разговоре С психологом, не разговора О психологе.
"""


def support_instructions() -> str:
    # The old workflow skill bundle contains imperative handoff/intake/menu
    # instructions. Those now belong to the HTML runtime, not to a classifier.
    return f"{SUPPORT_INSTRUCTIONS}\n\n{SUPPORT_TONE}\n\n{SUPPORT_CLASSIFICATION_CONTRACT}"


@dataclass(frozen=True)
class AgentContext:
    history: tuple[tuple[str, str], ...]
    state: str
    catalog: tuple[dict[str, Any], ...] = ()
    knowledge: tuple[str, ...] = ()
    pending_offer: str | None = None


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


def yandex_model_settings(provider_settings: ProviderSettings = DEFAULT_PROVIDER_SETTINGS) -> dict[str, Any]:
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


def yandex_response_format(agent_name: str) -> dict[str, Any]:
    """Constrain provider generation, while retaining defensive local validation."""
    if agent_name not in {"risk", "support"}:
        raise ValueError(f"unknown_agent:{agent_name}")
    model = SafetyDiagnostic if agent_name == "risk" else SupportDiagnostic
    schema = model.model_json_schema()
    if agent_name == "support":
        schema["properties"]["intent"] = {"$ref": "#/$defs/SupportIntent"}
        order = ("evidence_claims", "need_hints", "intent", "draft_text", "suggested_support")
    else:
        schema["properties"].pop("rationale_alias_used", None)
        # Human/psychologist requests belong to the intent classifier. Keep the
        # legacy domain value readable, but never ask this model to infer it.
        categories = schema["$defs"]["SafetyCategory"]["enum"]
        schema["$defs"]["SafetyCategory"]["enum"] = [c for c in categories if c != "direct_human_request"]
        order = ("evidence_claims", "categories", "level", "escalation", "confidence", "rationale")
    # Generate observations before the decision instead of committing to an
    # enum before looking at the supporting facts. No extra model round trip.
    schema["properties"] = {name: schema["properties"][name] for name in order}
    schema["required"] = list(schema["properties"])
    return {"type": "json_schema", "name": agent_name, "schema": schema, "strict": True}


def response_output_text(response: Any) -> str:
    """Read Responses API message content without retaining the full provider response."""
    texts = [
        content.text
        for item in getattr(response, "output", ())
        for content in getattr(item, "content", ())
        if isinstance(getattr(content, "text", None), str)
    ]
    return "\n".join(texts)


def provider_payload_is_valid(agent_name: str, payload: dict[str, Any]) -> bool:
    """Check the typed diagnostic contract before deciding whether to retry its transport."""
    try:
        if agent_name == "risk":
            SafetyDiagnostic.model_validate(payload)
        elif agent_name == "support":
            if SupportDiagnostic.model_validate(payload).intent is None:
                return False
        else:
            return False
    except ValidationError:
        return False
    return True


def parse_provider_json_object(raw_output: str) -> dict[str, Any]:
    """Parse the one provider object even if Qwen wraps it in harmless prose."""
    candidate = raw_output.strip()
    for prefix in ("```json\n", "```\n"):
        if candidate.lower().startswith(prefix) and candidate.endswith("\n```"):
            candidate = candidate[len(prefix) : -4].strip()
            break
    parsed = _decode_json_object(candidate)
    if parsed is not None:
        return parsed

    # Qwen occasionally puts an otherwise valid response after a short prose
    # preface.  This only recovers one strict JSON object; schema validation
    # below still rejects unknown or missing product fields.
    for start in (index for index, char in enumerate(candidate) if char == "{"):
        parsed = _decode_json_object_prefix(candidate[start:])
        if parsed is not None:
            return parsed
    return {}


def _decode_json_object(candidate: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(
            candidate,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _decode_json_object_prefix(candidate: str) -> dict[str, Any] | None:
    try:
        parsed, _ = json.JSONDecoder(
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_nonstandard_json_constant,
        ).raw_decode(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


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
    cached_tokens = getattr(usage, "cache_read_tokens", None)
    if not isinstance(cached_tokens, int):
        cached_tokens = getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0)
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "cached_tokens": cached_tokens if isinstance(cached_tokens, int) else 0,
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
        # Build *both* inputs before scheduling either provider coroutine.  A
        # local preparation failure is a pair of unavailable diagnostics, never
        # one orphan provider request.
        try:
            transcript, pii_audit = format_redacted_transcript(context.history)
            current_user_text = _current_user_text(context.history)
            current_redacted = redact_with_audit(current_user_text).text
            safety_input = format_safety_context(context, current_redacted)
            support_prompt = support_instructions()
            support_input = format_agent_context(context, transcript)
        except Exception:  # noqa: BLE001 - no provider task is safe after local preparation fails
            return AgentEvaluation(
                safety_status=DiagnosticStatus.UNAVAILABLE,
                support_status=DiagnosticStatus.UNAVAILABLE,
                safety_audit={"status": "unavailable", "reason": "preparation_failed"},
                support_audit={"status": "unavailable", "reason": "preparation_failed"},
            )
        safety_task = asyncio.create_task(
            self._run("risk", RISK_INSTRUCTIONS + "\n\n" + RISK_BOUNDARIES, safety_input, pii_audit)
        )
        support_task = asyncio.create_task(
            self._run(
                "support",
                support_prompt,
                support_input,
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
            response = await client.responses.create(
                model=f"gpt://{settings.yandex_cloud_folder_id}/{settings.yandex_ai_model}",
                instructions=instructions,
                input=input_text,
                text={"format": yandex_response_format(agent_name)},
                **yandex_model_settings(self._provider_settings),
            )
            output_text = response_output_text(response)
            output = parse_provider_json_object(output_text)
            format_retry_count = 0
            if not provider_payload_is_valid(agent_name, output):
                format_retry_count = 1
                response = await client.responses.create(
                    model=f"gpt://{settings.yandex_cloud_folder_id}/{settings.yandex_ai_model}",
                    instructions=(
                        f"{instructions}\n\nТехническое повторение: верни полный JSON-объект, "
                        "содержащий все обязательные поля, без любого другого текста."
                    ),
                    input=input_text,
                    text={"format": yandex_response_format(agent_name)},
                    **yandex_model_settings(self._provider_settings),
                )
                output_text = response_output_text(response)
                output = parse_provider_json_object(output_text)
            return AgentCallResult(
                payload=output,
                audit={
                    "status": "completed",
                    "response_id": getattr(response, "id", None),
                    "model": getattr(response, "model", None),
                    "latency_ms": round((perf_counter() - started) * 1000),
                    "usage": usage_audit(response.usage),
                    "format_retry_count": format_retry_count,
                    "output_shape": provider_output_shape(output_text),
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
        f"Состояние диалога: {context.state}\npending_offer: {context.pending_offer or 'none'}\n\n"
        f"Доступная помощь:\n{catalog}\n\n"
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
    raw_categories = payload.get("categories")
    if (
        isinstance(raw_categories, list)
        and set(raw_categories) == {"direct_human_request"}
        and payload.get("level") != "none"
    ):
        payload["level"] = "none"
        categories.add("direct_human_request_level_normalized")
    rationale = payload.get("rationale")
    if isinstance(rationale, str) and len(rationale) > _SAFETY_RATIONALE_MAX_LENGTH:
        payload["rationale"] = rationale[:_SAFETY_RATIONALE_MAX_LENGTH]
        categories.add("safety_rationale_truncated")
    return payload, frozenset(categories)


def _normalize_support_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], frozenset[str]]:
    categories: set[str] = set()
    intent = payload.get("intent")
    if isinstance(intent, str) and intent not in {item.value for item in SupportIntent}:
        payload["intent"] = None
        categories.add("support_unknown_intent_cleared")
    need_hints = payload.get("need_hints")
    if isinstance(need_hints, list):
        allowed = {item.value for item in NeedKind}
        normalized = [need for need in need_hints if isinstance(need, str) and need in allowed]
        if normalized != need_hints:
            categories.add("support_unknown_need_hints_cleared")
        payload["need_hints"] = normalized
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
