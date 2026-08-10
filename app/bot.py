from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove

from app.config import settings
from app.db import init_db
from app.safety import assess_crisis
from app.service import (
    MAIN_OPTIONS,
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


def create_bot() -> Bot:
    proxy_url = settings.resolved_telegram_proxy_url()
    if proxy_url:
        logger.info("Telegram Bot API proxy is enabled (%s).", urlparse(proxy_url).scheme)
    return Bot(settings.telegram_bot_token, session=AiohttpSession(proxy=proxy_url))


async def notify_staff(bot: Bot, message: Message, reason: str) -> None:
    if not settings.staff_telegram_chat_id:
        return
    # Do not forward the visitor's message automatically: it may contain sensitive data.
    await bot.send_message(
        settings.staff_telegram_chat_id,
        f"Нужна помощь специалистки. Канал: Telegram, диалог: {message.from_user.id}. Причина: {reason}."
    )


async def reply_and_store(
    message: Message,
    conversation_id: int,
    text: str,
    audit: dict | None = None,
    buttons: tuple[str, ...] = (),
) -> None:
    await record_message(
        conversation_id,
        "assistant",
        text,
        audit={
            "telegram": {"chat_id": message.chat.id, "in_reply_to_message_id": message.message_id},
            "ui": {"buttons": list(buttons)},
            **(audit or {}),
        },
    )
    reply_markup = (
        ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=button)] for button in buttons],
            resize_keyboard=True,
        )
        if buttons
        else ReplyKeyboardRemove()
    )
    await message.answer(text, reply_markup=reply_markup)


@dp.message(CommandStart())
async def start(message: Message) -> None:
    conversation = await get_or_create_conversation(message.from_user.id)
    await record_event(conversation.id, "started")
    await record_message(conversation.id, "user", "/start")
    test_banner = f"🧪 Тестовый контур: {settings.runtime_label()}.\n\n"
    await reply_and_store(
        message,
        conversation.id,
        test_banner
        + "Перед началом: Telegram не является экстренной службой и не даёт полной анонимности. "
        "Пожалуйста, не присылайте паспорт, точный адрес или другие документы. "
        "Можно остановиться в любой момент и написать «специалист».\n\n" + WELCOME,
        buttons=MAIN_OPTIONS,
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


@dp.message(Command("system_info"))
async def system_info(message: Message) -> None:
    """Return non-secret diagnostic data; this command is intentionally not in the UI."""
    conversation = await get_or_create_conversation(message.from_user.id)
    await record_event(conversation.id, "system_info_requested")
    await record_message(conversation.id, "user", "/system_info")
    llm_status = "включена" if settings.llm_enabled else "выключена"
    await reply_and_store(
        message,
        conversation.id,
        "🛠 Служебная информация\n"
        f"ENV: {settings.app_env}\n"
        f"Сборка: {settings.build_version}\n"
        f"LLM: {llm_status}",
        audit={
            "system_info": {
                "app_env": settings.app_env,
                "build_version": settings.build_version,
                "llm_enabled": settings.llm_enabled,
            }
        },
        buttons=MAIN_OPTIONS,
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
    reply = await reply_for(conversation, message.text, history, assessment)
    await reply_and_store(message, conversation.id, reply.text, reply.audit, reply.buttons)
    if reply.notify_staff:
        await notify_staff(bot, message, "crisis/concern or human handoff")


async def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before launching the bot.")
    await init_db()
    while True:
        bot = create_bot()
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
