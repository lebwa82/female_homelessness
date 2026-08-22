from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.config import settings
from app.db import init_db
from app.domain import AgentTurn, Choice, IncomingMessage
from app.service import PERSISTENCE_UNAVAILABLE_PROMPT, ConversationService
from app.worker import worker_loop

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
dp = Dispatcher()
conversation_service = ConversationService()


def create_bot() -> Bot:
    proxy_url = settings.resolved_telegram_proxy_url()
    if proxy_url:
        logger.info("Telegram Bot API proxy is enabled (%s).", urlparse(proxy_url).scheme)
    return Bot(settings.telegram_bot_token, session=AiohttpSession(proxy=proxy_url))


def incoming_from_message(message: Message, text: str | None = None, user: object | None = None) -> IncomingMessage:
    sender = user or message.from_user
    return IncomingMessage(
        platform_user_id=sender.id,
        chat_id=message.chat.id,
        username=getattr(sender, "username", None),
        text=message.text if text is None else text,
        message_id=message.message_id,
        received_at=message.date.isoformat(),
    )


def render_keyboard(turn: AgentTurn) -> InlineKeyboardMarkup | None:
    if not turn.choices:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=choice.label, callback_data=choice.id)] for choice in turn.choices
        ]
    )


async def send_turn(message: Message, incoming: IncomingMessage, turn: AgentTurn) -> None:
    if turn.audit.get("suppress_delivery"):
        return
    await message.answer(turn.text, reply_markup=render_keyboard(turn))
    if not turn.audit.get("skip_outbound_persistence"):
        try:
            await conversation_service.record_outbound(incoming, turn)
        except Exception as error:  # noqa: BLE001 - audit degradation cannot suppress user-facing safety copy
            logger.warning("Outbound audit unavailable: %s", type(error).__name__)


async def claim_stateless_update(incoming: IncomingMessage) -> bool:
    """Avoid repeating stateless command/media effects on a redelivered update."""
    claim = getattr(conversation_service, "claim_inbound", None)
    return True if claim is None else await claim(incoming)


def persistence_unavailable_turn() -> AgentTurn:
    return AgentTurn(text=PERSISTENCE_UNAVAILABLE_PROMPT).with_human_choice().model_copy(
        update={"audit": {"skip_outbound_persistence": True}}
    )


async def claim_stateless_update_or_reply(message: Message, incoming: IncomingMessage) -> bool:
    try:
        return await claim_stateless_update(incoming)
    except Exception:  # noqa: BLE001 - failed persistence gets an honest local retry/handoff response
        await send_turn(message, incoming, persistence_unavailable_turn())
        return False


@dp.message(CommandStart())
async def start(message: Message) -> None:
    incoming = incoming_from_message(message, text="")
    turn = await conversation_service.start(incoming)
    await send_turn(message, incoming, turn)


@dp.message(Command("delete"))
async def delete_request(message: Message) -> None:
    incoming = incoming_from_message(message, text="/delete")
    turn = await conversation_service.delete(incoming)
    await send_turn(message, incoming, turn)


@dp.message(Command("system_info"))
async def system_info(message: Message) -> None:
    incoming = incoming_from_message(message, text="/system_info")
    if not await claim_stateless_update_or_reply(message, incoming):
        return
    llm_status = "включена" if settings.llm_enabled else "выключена"
    turn = AgentTurn(
        text=(
            "🛠 Служебная информация\n"
            f"ENV: {settings.app_env}\n"
            f"Сборка: {settings.build_version}\n"
            f"LLM: {llm_status}"
        )
    ).with_human_choice()
    await send_turn(message, incoming, turn)


@dp.callback_query(F.data)
async def callback(query: CallbackQuery) -> None:
    if query.message is None or query.data is None:
        await query.answer()
        return
    incoming = incoming_from_message(query.message, text=query.data, user=query.from_user)
    try:
        turn = await conversation_service.handle_callback(incoming, query.data)
    except Exception:  # noqa: BLE001 - callback lease remains retryable; user gets an honest fallback
        turn = persistence_unavailable_turn()
    await query.answer()
    await send_turn(query.message, incoming, turn)


@dp.message(F.text)
async def reply(message: Message) -> None:
    incoming = incoming_from_message(message)
    turn = await conversation_service.handle_text(incoming)
    await send_turn(message, incoming, turn)


@dp.message()
async def unsupported_content(message: Message) -> None:
    incoming = incoming_from_message(message, text="[non-text content]")
    if not await claim_stateless_update_or_reply(message, incoming):
        return
    turn = AgentTurn(
        text="Пока я могу общаться только текстом. Можно написать несколькими словами, что сейчас важнее всего.",
        choices=(Choice(id="human", label="Поговорить с живым человеком"),),
    )
    await send_turn(message, incoming, turn)


async def poll_once(bot: Bot) -> None:
    worker_task = asyncio.create_task(worker_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        worker_task.cancel()
        await asyncio.gather(worker_task, return_exceptions=True)
        await bot.session.close()


async def main() -> None:
    if not settings.telegram_bot_token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env before launching the bot.")
    await init_db()
    while True:
        bot = create_bot()
        try:
            await poll_once(bot)
            return
        except TelegramNetworkError:
            logger.warning("Telegram network timeout; retrying polling in five seconds.")
            await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
