from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from app.config import settings
from app.db import init_db
from app.safety import assess_crisis
from app.service import (
    WELCOME,
    get_history,
    get_or_create_conversation,
    record_event,
    record_message,
    reply_for,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
dp = Dispatcher()


async def notify_staff(bot: Bot, message: Message, reason: str) -> None:
    if not settings.staff_telegram_chat_id:
        return
    # Do not forward the visitor's message automatically: it may contain sensitive data.
    await bot.send_message(
        settings.staff_telegram_chat_id,
        f"Нужна помощь специалистки. Канал: Telegram, диалог: {message.from_user.id}. Причина: {reason}."
    )


async def reply_and_store(
    message: Message, conversation_id: int, text: str, audit: dict | None = None
) -> None:
    await record_message(
        conversation_id,
        "assistant",
        text,
        audit={
            "telegram": {"chat_id": message.chat.id, "in_reply_to_message_id": message.message_id},
            **(audit or {}),
        },
    )
    await message.answer(text)


@dp.message(CommandStart())
async def start(message: Message) -> None:
    conversation = await get_or_create_conversation(message.from_user.id)
    await record_event(conversation.id, "started")
    await record_message(conversation.id, "user", "/start")
    await reply_and_store(
        message,
        conversation.id,
        "Перед началом: Telegram не является экстренной службой и не даёт полной анонимности. "
        "Пожалуйста, не присылайте паспорт, точный адрес или другие документы. "
        "Можно остановиться в любой момент и написать «специалист».\n\n" + WELCOME
    )


@dp.message(Command("delete"))
async def delete_request(message: Message) -> None:
    conversation = await get_or_create_conversation(message.from_user.id)
    await record_event(conversation.id, "deletion_requested")
    await record_message(conversation.id, "user", "/delete")
    await reply_and_store(
        message,
        conversation.id,
        "Запрос на удаление данных принят. Специалистка обработает его по правилам организации. "
        "Если сейчас нужна помощь, можно продолжить писать здесь."
    )


@dp.message(F.text)
async def reply(message: Message, bot: Bot) -> None:
    conversation = await get_or_create_conversation(message.from_user.id)
    assessment = assess_crisis(message.text)
    await record_message(
        conversation.id,
        "user",
        message.text,
        audit={
            "telegram": {
                "chat_id": message.chat.id,
                "message_id": message.message_id,
                "received_at": message.date.isoformat(),
            },
            "risk_assessment": {
                "risk": assessment.risk.value,
                "reason": assessment.reason,
                "detector": "rule_based_v1",
            },
        },
    )
    history = await get_history(conversation.id)
    reply_text, notify, audit = await reply_for(conversation, message.text, history, assessment)
    await reply_and_store(message, conversation.id, reply_text, audit)
    if notify:
        await notify_staff(bot, message, "crisis/concern or human handoff")


async def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before launching the bot.")
    await init_db()
    while True:
        bot = Bot(settings.telegram_bot_token)
        try:
            await dp.start_polling(bot)
            return
        except TelegramNetworkError:
            logger.warning("Telegram network timeout; retrying polling in five seconds.")
            await asyncio.sleep(5)
        finally:
            await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
