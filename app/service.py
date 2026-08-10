from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db import Conversation, ConversationMessage, Event, Session
from app.domain import CRISIS_REPLY, Risk, Stage
from app.knowledge import find_verified_answer
from app.llm import BridgeResult, compassionate_bridge
from app.pii import redact_with_audit
from app.safety import assess_crisis

MAIN_OPTIONS = (
    "Продукты и гигиена",
    "Безопасное место / специалистка",
    "Вопрос о документах",
    "Другое",
)
DELIVERY_OPTIONS = ("Самовывоз в ПВЗ", "Электронный сертификат", "Связаться со специалисткой")
HELP_OPTIONS = ("Получить базовую помощь", "Связаться со специалисткой")

WELCOME = (
    "Здравствуйте. Я рядом, чтобы спокойно помочь с первым шагом. "
    "Можно не называть имя и не рассказывать всё сразу. Выберите то, что сейчас ближе всего."
)


@dataclass(frozen=True)
class BotReply:
    text: str
    notify_staff: bool = False
    audit: dict[str, Any] | None = None
    buttons: tuple[str, ...] = ()


async def get_or_create_conversation(channel_user_id: int) -> Conversation:
    async with Session() as session:
        result = await session.execute(select(Conversation).where(Conversation.channel_user_id == channel_user_id))
        conversation = result.scalar_one_or_none()
        if conversation is None:
            conversation = Conversation(channel_user_id=channel_user_id)
            session.add(conversation)
            await session.commit()
            await session.refresh(conversation)
        return conversation


async def record_event(
    conversation_id: int, kind: str, payload: str = "", audit: dict[str, Any] | None = None
) -> None:
    async with Session() as session:
        session.add(Event(conversation_id=conversation_id, kind=kind, payload=payload[:1200], audit=audit or {}))
        await session.commit()


async def record_message(
    conversation_id: int, role: str, content: str, audit: dict[str, Any] | None = None
) -> None:
    redaction = redact_with_audit(content)
    async with Session() as session:
        session.add(
            ConversationMessage(
                conversation_id=conversation_id,
                role=role,
                content=content,
                redacted_content=redaction.text,
                audit={"pii_redaction": redaction.audit, **(audit or {})},
            )
        )
        await session.commit()


async def get_history(conversation_id: int) -> list[tuple[str, str]]:
    async with Session() as session:
        result = await session.execute(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.id)
        )
        return [(item.role, item.content) for item in result.scalars()]


async def request_human(conversation: Conversation, reason: str) -> str:
    async with Session() as session:
        item = await session.get(Conversation, conversation.id)
        item.human_handoff = True
        item.stage = Stage.HUMAN_HANDOFF.value
        session.add(
            Event(
                conversation_id=conversation.id,
                kind="human_handoff",
                payload=reason,
                audit={"reason": reason, "staff_chat_configured": bool(settings.staff_telegram_chat_id)},
            )
        )
        await session.commit()
    if settings.staff_telegram_chat_id:
        return "Я передала ваш запрос специалистке. Пока ждёте, можете написать только то, чем готовы поделиться."
    return (
        "Ситуация выглядит критичной. Зову человека. Если есть непосредственная опасность, позвоните 112."
    )


async def reply_for(
    conversation: Conversation, text: str, history: list[tuple[str, str]], assessment=None
) -> BotReply:
    """Build a response whose available actions are controlled by the backend."""
    assessment = assessment or assess_crisis(text)
    if assessment.risk is Risk.ACUTE:
        await request_human(conversation, assessment.reason or "acute")
        return BotReply(
            CRISIS_REPLY,
            notify_staff=True,
            audit={"routing": {"action": "crisis_handoff", "reason": assessment.reason}},
        )

    normalized = text.lower().strip()
    if any(term in normalized for term in ("специалист", "человек", "оператор", "помогите")):
        return BotReply(
            await request_human(conversation, "user_request"),
            notify_staff=True,
            audit={"routing": {"action": "human_handoff", "reason": "user_request"}},
        )

    bridge: BridgeResult = await compassionate_bridge(history)
    if bridge.text == "Нужна помощь специалистки.":
        return BotReply(
            await request_human(conversation, "model_escalation"),
            notify_staff=True,
            audit={
                "llm": bridge.audit,
                "routing": {"action": "human_handoff", "reason": "model_escalation"},
            },
        )
    prefix = f"{bridge.text}\n\n" if bridge.text else ""
    message_audit = {"llm": bridge.audit}

    answer = find_verified_answer(normalized)
    if answer:
        return BotReply(
            prefix + answer,
            notify_staff=assessment.risk is Risk.CONCERN,
            audit=message_audit,
            buttons=HELP_OPTIONS,
        )

    if conversation.stage == Stage.WELCOME.value:
        async with Session() as session:
            item = await session.get(Conversation, conversation.id)
            item.requested_help = text[:64]
            item.stage = Stage.DELIVERY.value
            await session.commit()
        prefix = prefix or "Спасибо, что написали.\n\n"
        return BotReply(
            prefix + "Для базовой помощи выберите подходящий способ получения.",
            notify_staff=assessment.risk is Risk.CONCERN,
            audit=message_audit,
            buttons=DELIVERY_OPTIONS,
        )

    if conversation.stage == Stage.DELIVERY.value and normalized in {
        "1",
        "самовывоз",
        "самовывоз в пвз",
        "пвз",
        "2",
        "сертификат",
        "электронный сертификат",
    }:
        method = "pickup" if normalized in {"1", "самовывоз", "самовывоз в пвз", "пвз"} else "certificate"
        async with Session() as session:
            item = await session.get(Conversation, conversation.id)
            item.delivery_method = method
            item.stage = Stage.FOLLOW_UP.value
            session.add(Event(conversation_id=conversation.id, kind="delivery_choice", payload=method))
            await session.commit()
        return BotReply(
            prefix
            + "Приняла. Сейчас это прототип: специалистка уточнит безопасный способ получения и не будет запрашивать лишние данные. "
            "Я напишу позже, чтобы спросить, стало ли немного легче.",
            notify_staff=True,
            audit=message_audit,
        )

    return BotReply(
        prefix
        + "Я вас слышу. Выберите следующий шаг.",
        notify_staff=assessment.risk is Risk.CONCERN,
        audit=message_audit,
        buttons=HELP_OPTIONS,
    )
