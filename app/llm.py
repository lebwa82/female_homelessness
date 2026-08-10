"""Narrow Yandex AI Studio integration.

The model receives the complete transcript and drafts an empathetic bridging
phrase. State transitions, aid decisions, knowledge answers and crisis
escalation always remain in application code.
"""

from __future__ import annotations

from dataclasses import dataclass
from time import perf_counter
from typing import Any

from openai import AsyncOpenAI, OpenAIError

from app.config import settings
from app.pii import redact_for_model, redact_with_audit

SYSTEM_PROMPT = """Ты помогаешь написать ОДНУ короткую, бережную реплику на русском,
учитывая полный transcript диалога ниже.
Ты не психолог, не юрист и не экстренная служба. Не давай советов, не проси личные
данные, не задавай вопросов, не предлагай варианты помощи, не обещай результат и
не называй организации. Не более 35 слов. Если
сообщение похоже на опасность, верни ровно: Нужна помощь специалистки."""


@dataclass(frozen=True)
class BridgeResult:
    text: str | None
    audit: dict[str, Any]


def is_ready() -> bool:
    return all(
        (
            settings.llm_enabled,
            settings.yandex_ai_api_key,
            settings.yandex_cloud_folder_id,
            settings.yandex_ai_model,
        )
    )


def format_transcript(history: list[tuple[str, str]]) -> str:
    role_names = {"user": "Пользователь", "assistant": "Бот"}
    dialogue = "\n\n".join(
        f"{role_names.get(role, role)}: {redact_for_model(content)}" for role, content in history
    )
    return dialogue + "\n\nСформулируй следующую короткую реплику бота."


def format_transcript_with_audit(history: list[tuple[str, str]]) -> tuple[str, dict[str, Any]]:
    role_names = {"user": "Пользователь", "assistant": "Бот"}
    redactions = [redact_with_audit(content) for _, content in history]
    dialogue = "\n\n".join(
        f"{role_names.get(role, role)}: {redaction.text}"
        for (role, _), redaction in zip(history, redactions, strict=True)
    )
    entity_counts: dict[str, int] = {}
    for redaction in redactions:
        for entity_type, count in redaction.audit["entity_counts"].items():
            entity_counts[entity_type] = entity_counts.get(entity_type, 0) + count
    return (
        dialogue + "\n\nСформулируй следующую короткую реплику бота.",
        {
            "engine": "presidio",
            "messages_processed": len(history),
            "messages_with_pii": sum(redaction.audit["detected"] for redaction in redactions),
            "entities_total": sum(redaction.audit["entities_total"] for redaction in redactions),
            "entity_counts": dict(sorted(entity_counts.items())),
        },
    )


async def compassionate_bridge(history: list[tuple[str, str]]) -> BridgeResult:
    if not is_ready():
        return BridgeResult(None, {"provider": "yandex_ai_studio", "status": "not_configured"})
    client = AsyncOpenAI(
        api_key=settings.yandex_ai_api_key,
        base_url="https://ai.api.cloud.yandex.net/v1",
        project=settings.yandex_cloud_folder_id,
        default_headers={"x-data-logging-enabled": "false"},
        timeout=8.0,
        max_retries=1,
    )
    transcript, pii_audit = format_transcript_with_audit(history)
    started = perf_counter()
    try:
        response = await client.responses.create(
            model=f"gpt://{settings.yandex_cloud_folder_id}/{settings.yandex_ai_model}",
            instructions=SYSTEM_PROMPT,
            input=transcript,
            temperature=0.3,
            max_output_tokens=1500,
        )
        usage = response.usage
        return BridgeResult(
            response.output_text.strip()[:300] if response.output_text else None,
            {
                "provider": "yandex_ai_studio",
                "status": str(response.status),
                "response_id": response.id,
                "model": response.model,
                "latency_ms": round((perf_counter() - started) * 1000),
                "request": {
                    "temperature": 0.3,
                    "max_output_tokens": 1500,
                    "data_logging_enabled": False,
                },
                "usage": {
                    "input_tokens": usage.input_tokens if usage else None,
                    "output_tokens": usage.output_tokens if usage else None,
                    "total_tokens": usage.total_tokens if usage else None,
                    "cached_tokens": usage.input_tokens_details.cached_tokens
                    if usage and usage.input_tokens_details
                    else None,
                },
                "pii_redaction": pii_audit,
            },
        )
    except OpenAIError as error:
        # The user must never see provider error details or lose the help flow.
        return BridgeResult(
            None,
            {
                "provider": "yandex_ai_studio",
                "status": "error",
                "error_type": type(error).__name__,
                "latency_ms": round((perf_counter() - started) * 1000),
                "pii_redaction": pii_audit,
            },
        )
    finally:
        await client.close()
