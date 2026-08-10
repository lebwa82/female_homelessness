from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.config import settings
from app.db import Conversation, ConversationMessage, Event, Session
from app.domain import CRISIS_REPLY, Risk, Stage
from app.knowledge import find_verified_answer
from app.llm import BridgeResult, compassionate_bridge
from app.pii import redact_with_audit
from app.safety import assess_crisis

WELCOME = (
    "Здравствуйте. Я рядом, чтобы спокойно помочь с первым шагом. "
    "Можно не называть имя и не рассказывать всё сразу. Что вам сейчас нужнее всего?\n\n"
    "1. Продукты или гигиена\n2. Безопасное место / разговор со специалисткой\n3. Вопрос о документах или правах\n4. Другое\n\n"
    "В любой момент напишите «специалист», и я передам диалог человеку."
)


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
) -> tuple[str, bool, dict[str, Any]]:
    """Returns (reply, notify_staff) after the latest user message is saved."""
    assessment = assessment or assess_crisis(text)
    if assessment.risk is Risk.ACUTE:
        await request_human(conversation, assessment.reason or "acute")
        return CRISIS_REPLY, True, {"routing": {"action": "crisis_handoff", "reason": assessment.reason}}

    normalized = text.lower().strip()
    if any(term in normalized for term in ("специалист", "человек", "оператор", "помогите")):
        return await request_human(conversation, "user_request"), True, {
            "routing": {"action": "human_handoff", "reason": "user_request"}
        }

    bridge: BridgeResult = await compassionate_bridge(history)
    if bridge.text == "Нужна помощь специалистки.":
        return await request_human(conversation, "model_escalation"), True, {
            "llm": bridge.audit,
            "routing": {"action": "human_handoff", "reason": "model_escalation"},
        }
    prefix = f"{bridge.text}\n\n" if bridge.text else ""
    message_audit = {"llm": bridge.audit}

    answer = find_verified_answer(normalized)
    if answer:
        return prefix + answer, assessment.risk is Risk.CONCERN, message_audit

    if conversation.stage == Stage.WELCOME.value:
        async with Session() as session:
            item = await session.get(Conversation, conversation.id)
            item.requested_help = text[:64]
            item.stage = Stage.DELIVERY.value
            await session.commit()
        prefix = prefix or "Спасибо, что написали.\n\n"
        return (
            prefix + "Для базовой помощи можно выбрать: \n"
            "1. Самовывоз в безопасной точке выдачи\n2. Электронный сертификат\n\n"
            "Ответьте 1 или 2. Если сейчас важнее поговорить — напишите «специалист»."
        ), assessment.risk is Risk.CONCERN, message_audit

    if conversation.stage == Stage.DELIVERY.value and normalized in {"1", "самовывоз", "пвз", "2", "сертификат"}:
        method = "pickup" if normalized in {"1", "самовывоз", "пвз"} else "certificate"
        async with Session() as session:
            item = await session.get(Conversation, conversation.id)
            item.delivery_method = method
            item.stage = Stage.FOLLOW_UP.value
            session.add(Event(conversation_id=conversation.id, kind="delivery_choice", payload=method))
            await session.commit()
        return (
            prefix
            + "Приняла. Сейчас это прототип: специалистка уточнит безопасный способ получения и не будет запрашивать лишние данные. "
            "Я напишу позже, чтобы спросить, стало ли немного легче."
        ), True, message_audit

    return (
        prefix
        + "Я вас слышу. Могу помочь с базовой помощью или позвать специалистку — напишите «специалист». "
    ), assessment.risk is Risk.CONCERN, message_audit
