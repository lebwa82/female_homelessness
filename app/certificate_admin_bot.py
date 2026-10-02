"""Private Telegram interface for importing PDF bearer certificates."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from time import monotonic

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from app.certificate_admin import (
    BatchSummary,
    CertificateAdmin,
    CertificateAdminRepository,
)
from app.certificate_documents import (
    MAX_CERTIFICATE_PDF_BYTES,
    CertificateObjectRef,
    S3CertificateObjectStore,
    parse_certificate_pdf,
)
from app.chatwoot.certificates import POOL_AIDS, CertificateInventory, database_url
from app.config import settings
from scripts.certificate_pdf_runtime import services

logger = logging.getLogger(__name__)
CLEANUP_INTERVAL_SECONDS = 10 * 60
UPLOAD_DEBOUNCE_SECONDS = 2.0
UPLOAD_PROGRESS_INTERVAL_SECONDS = 5.0
UPLOAD_CONCURRENCY = 4

POOL_LABELS = {"ozon": "Ozon", "pyaterochka": "Пятёрочка"}
AID_LABELS = {
    "medicine_card": "лекарства",
    "hostel_3_nights": "хостел",
    "food_card": "продукты",
    "children_card": "детские товары",
}


def _keyboard(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=data) for label, data in row]
        for row in rows
    ])


def _reply_keyboard(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=label) for label in row] for row in rows],
        resize_keyboard=True,
        is_persistent=True,
    )


def _main_keyboard(admin: CertificateAdmin) -> ReplyKeyboardMarkup:
    rows = [["Загрузить сертификаты"], ["Остатки"]]
    if admin.role == "owner":
        rows.append(["Администраторы"])
    return _reply_keyboard(rows)


def _batch_keyboard() -> ReplyKeyboardMarkup:
    return _reply_keyboard([["Завершить загрузку", "Отменить загрузку"], ["Меню"]])


def _confirmation_keyboard() -> ReplyKeyboardMarkup:
    return _reply_keyboard([["Импортировать сертификаты", "Отменить загрузку"], ["Меню"]])


def _batch_text(summary: BatchSummary) -> str:
    provider, aids = POOL_AIDS[summary.pool_slug]
    categories = ", ".join(AID_LABELS[aid] for aid in aids)
    return (
        f"{provider}\n\nПолучено: {summary.received}\n"
        f"Готово к импорту: {summary.ready}; дубликаты: {summary.duplicates}; "
        f"ошибки: {summary.invalid}.\nКатегории: {categories}."
    )


@dataclass(slots=True)
class _UploadState:
    pending: int = 0
    generation: int = 0
    progress_message_id: int | None = None
    next_progress_at: float = 0.0
    final_task: asyncio.Task[None] | None = None


class UploadProgressCoordinator:
    """Collapse many Telegram document updates into one batch status message."""

    def __init__(self, repository: CertificateAdminRepository) -> None:
        self._repository = repository
        self._states: dict[tuple[int, int], _UploadState] = {}
        self._lock = asyncio.Lock()

    async def begin(self, bot: Bot, admin_id: int, batch_id: int) -> None:
        key = (admin_id, batch_id)
        async with self._lock:
            state = self._states.setdefault(key, _UploadState())
            if state.final_task is not None:
                state.final_task.cancel()
                state.final_task = None
            if state.progress_message_id is None:
                try:
                    sent = await bot.send_message(
                        admin_id,
                        "Получаю и проверяю сертификаты…",
                        reply_markup=_batch_keyboard(),
                    )
                    state.progress_message_id = sent.message_id
                except Exception as error:  # noqa: BLE001 - Telegram errors may contain payloads
                    logger.warning(
                        "Certificate upload progress notification failed kind=%s",
                        type(error).__name__,
                    )
                state.next_progress_at = monotonic() + UPLOAD_PROGRESS_INTERVAL_SECONDS
            state.pending += 1
            state.generation += 1

    async def complete(self, bot: Bot, admin_id: int, batch_id: int) -> None:
        key = (admin_id, batch_id)
        update_progress = False
        progress_message_id: int | None = None
        async with self._lock:
            state = self._states.get(key)
            if state is None:
                return
            state.pending = max(0, state.pending - 1)
            state.generation += 1
            generation = state.generation
            if state.pending == 0:
                state.final_task = asyncio.create_task(
                    self._send_final_after_pause(bot, admin_id, batch_id, generation)
                )
            elif monotonic() >= state.next_progress_at:
                update_progress = True
                progress_message_id = state.progress_message_id
                state.next_progress_at = monotonic() + UPLOAD_PROGRESS_INTERVAL_SECONDS
        if update_progress and progress_message_id is not None:
            summary = await self._repository.summary(admin_id, batch_id)
            if summary is not None:
                with suppress(Exception):
                    await bot.edit_message_text(
                        _batch_text(summary) + "\n\nОбработка продолжается…",
                        chat_id=admin_id,
                        message_id=progress_message_id,
                    )

    async def pending(self, admin_id: int, batch_id: int) -> int:
        async with self._lock:
            state = self._states.get((admin_id, batch_id))
            return state.pending if state is not None else 0

    async def forget(self, admin_id: int, batch_id: int) -> None:
        async with self._lock:
            state = self._states.pop((admin_id, batch_id), None)
            if state is not None and state.final_task is not None:
                state.final_task.cancel()

    async def _send_final_after_pause(
        self, bot: Bot, admin_id: int, batch_id: int, generation: int
    ) -> None:
        try:
            await asyncio.sleep(UPLOAD_DEBOUNCE_SECONDS)
            async with self._lock:
                state = self._states.get((admin_id, batch_id))
                if state is None or state.pending != 0 or state.generation != generation:
                    return
            summary = await self._repository.summary(admin_id, batch_id)
            if summary is not None and summary.status == "collecting":
                await bot.send_message(
                    admin_id,
                    _batch_text(summary),
                    reply_markup=_batch_keyboard(),
                )
            async with self._lock:
                state = self._states.get((admin_id, batch_id))
                if state is not None and state.pending == 0 and state.generation == generation:
                    self._states.pop((admin_id, batch_id), None)
        except asyncio.CancelledError:
            return
        except Exception as error:  # noqa: BLE001 - Telegram errors may contain payloads
            logger.warning(
                "Certificate upload final notification failed kind=%s",
                type(error).__name__,
            )
            async with self._lock:
                state = self._states.get((admin_id, batch_id))
                if state is not None and state.pending == 0 and state.generation == generation:
                    self._states.pop((admin_id, batch_id), None)


async def _delete_refs(
    store: S3CertificateObjectStore, refs: tuple[CertificateObjectRef, ...]
) -> None:
    for ref in refs:
        try:
            await store.delete(ref)
        except Exception as error:  # noqa: BLE001 - never log object keys or SDK payloads
            logger.warning("Certificate staging cleanup failed kind=%s", type(error).__name__)


def build_dispatcher(
    repository: CertificateAdminRepository,
    inventory: CertificateInventory,
    object_store: S3CertificateObjectStore,
) -> Dispatcher:
    dispatcher = Dispatcher()
    bot_username: str | None = None
    progress = UploadProgressCoordinator(repository)
    upload_slots = asyncio.Semaphore(UPLOAD_CONCURRENCY)

    async def admin_for(user_id: int, display_name: str) -> CertificateAdmin | None:
        admin = await repository.admin(user_id)
        if admin is not None:
            await repository.update_identity(user_id, display_name)
        return admin

    async def show_menu(bot: Bot, user_id: int, display_name: str) -> None:
        admin = await admin_for(user_id, display_name)
        if admin is None:
            await bot.send_message(user_id, "Доступ к этому боту не предоставлен.")
            return
        active = await repository.active_batch(admin.telegram_user_id)
        if active is not None:
            summary = await repository.summary(admin.telegram_user_id, active.id)
            if summary is not None:
                if active.status == "awaiting_confirmation":
                    await bot.send_message(
                        user_id,
                        _batch_text(summary) + "\n\nИмпортировать готовые сертификаты?",
                        reply_markup=_confirmation_keyboard(),
                    )
                else:
                    await bot.send_message(
                        user_id, _batch_text(summary), reply_markup=_batch_keyboard()
                    )
                return
        await bot.send_message(
            user_id, "Управление сертификатами", reply_markup=_main_keyboard(admin)
        )

    async def show_stock(bot: Bot, admin: CertificateAdmin) -> None:
        rows = await inventory.stock()
        by_slug = {row.slug: row for row in rows}
        lines = ["Остатки сертификатов"]
        for slug in ("ozon", "pyaterochka"):
            row = by_slug.get(slug)
            available = row.available if row is not None else 0
            lines.append(f"{POOL_LABELS[slug]}: доступно {available}")
        await bot.send_message(
            admin.telegram_user_id, "\n".join(lines), reply_markup=_main_keyboard(admin)
        )

    @dispatcher.message(CommandStart())
    async def start(message: Message, command: CommandObject, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        if command.args and command.args.startswith("invite_"):
            claim = await repository.claim_invite(
                command.args.removeprefix("invite_"),
                message.from_user.id,
                message.from_user.full_name,
            )
            if claim is None:
                await message.answer("Приглашение недействительно или уже использовано.")
                return
            await message.answer("Заявка отправлена владельцу. Дождитесь подтверждения.")
            await bot.send_message(
                claim.created_by,
                f"Новый кандидат: {message.from_user.full_name}\nРоль: {claim.role}",
                reply_markup=_keyboard([[
                    ("Подтвердить", f"invite:approve:{claim.invite_id}"),
                    ("Отклонить", f"invite:reject:{claim.invite_id}"),
                ]]),
            )
            return
        bootstrap_username = settings.certificate_admin_username()
        current_username = (message.from_user.username or "").lower()
        if (
            await repository.admin(message.from_user.id) is None
            and bootstrap_username is not None
            and current_username == bootstrap_username
        ):
            await repository.claim_initial_owner(
                message.from_user.id, message.from_user.full_name
            )
        await show_menu(bot, message.from_user.id, message.from_user.full_name)

    @dispatcher.message(Command("menu"))
    @dispatcher.message(F.text == "Меню")
    async def menu_command(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        await show_menu(bot, message.from_user.id, message.from_user.full_name)

    @dispatcher.message(Command("cancel"))
    async def cancel_command(message: Message) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        admin = await admin_for(message.from_user.id, message.from_user.full_name)
        if admin is None:
            return
        batch = await repository.active_batch(admin.telegram_user_id)
        if batch is None:
            await message.answer("Активной загрузки нет.", reply_markup=_main_keyboard(admin))
            return
        await progress.forget(admin.telegram_user_id, batch.id)
        refs = await repository.cancel_batch(admin.telegram_user_id, batch.id)
        await _delete_refs(object_store, refs)
        await message.answer("Загрузка отменена.", reply_markup=_main_keyboard(admin))

    @dispatcher.message(Command("stock"))
    @dispatcher.message(F.text == "Остатки")
    async def stock_command(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        admin = await admin_for(message.from_user.id, message.from_user.full_name)
        if admin is not None:
            await show_stock(bot, admin)

    private_callback = F.message.chat.type == "private"

    async def choose_pool_for(admin: CertificateAdmin, bot: Bot) -> None:
        active = await repository.active_batch(admin.telegram_user_id)
        if active is not None:
            summary = await repository.summary(admin.telegram_user_id, active.id)
            if summary is not None:
                await bot.send_message(
                    admin.telegram_user_id,
                    _batch_text(summary),
                    reply_markup=(
                        _confirmation_keyboard()
                        if active.status == "awaiting_confirmation"
                        else _batch_keyboard()
                    ),
                )
            return
        await bot.send_message(
            admin.telegram_user_id,
            "Выберите тип сертификатов:",
            reply_markup=_keyboard([[
                ("Ozon", "upload:ozon"),
                ("Пятёрочка", "upload:pyaterochka"),
            ]]),
        )

    @dispatcher.message(F.text == "Загрузить сертификаты")
    async def choose_pool_message(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        admin = await admin_for(message.from_user.id, message.from_user.full_name)
        if admin is not None:
            await choose_pool_for(admin, bot)

    @dispatcher.callback_query(private_callback & (F.data == "menu:upload"))
    async def choose_pool(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None:
            return
        await choose_pool_for(admin, bot)

    @dispatcher.callback_query(
        private_callback & F.data.in_({"upload:ozon", "upload:pyaterochka"})
    )
    async def start_upload(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None or query.data is None:
            return
        pool_slug = query.data.split(":", 1)[1]
        try:
            await repository.start_batch(
                admin.telegram_user_id,
                pool_slug,
                ttl_hours=settings.certificate_admin_batch_ttl_hours,
            )
        except Exception as error:  # noqa: BLE001 - keep database details out of Telegram/logs
            logger.warning("Certificate batch start failed kind=%s", type(error).__name__)
            await bot.send_message(admin.telegram_user_id, "Не удалось начать загрузку.")
            return
        _, aids = POOL_AIDS[pool_slug]
        categories = ", ".join(AID_LABELS[aid] for aid in aids)
        await bot.send_message(
            admin.telegram_user_id,
            f"Загрузка {POOL_LABELS[pool_slug]}.\nКатегории: {categories}.\n"
            "Отправляйте PDF по одному или пачкой. Когда закончите, нажмите "
            "«Завершить загрузку».",
            reply_markup=_batch_keyboard(),
        )

    @dispatcher.message(F.document)
    async def receive_pdf(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private" or message.document is None:
            return
        admin = await admin_for(message.from_user.id, message.from_user.full_name)
        if admin is None:
            return  # Never download an unauthorized user's file.
        batch = await repository.active_batch(admin.telegram_user_id)
        if batch is None or batch.status != "collecting":
            await message.answer(
                "Нет активной загрузки. Выберите «Загрузить сертификаты».",
                reply_markup=_main_keyboard(admin),
            )
            return
        filename = message.document.file_name or "certificate.pdf"
        await progress.begin(bot, admin.telegram_user_id, batch.id)
        try:
            async with upload_slots:
                if (
                    message.document.file_size is not None
                    and message.document.file_size > MAX_CERTIFICATE_PDF_BYTES
                ):
                    await repository.add_rejected(
                        batch.id, filename, "invalid", "file_too_large"
                    )
                    return
                try:
                    downloaded = await bot.download(message.document)
                    if downloaded is None:
                        raise ValueError("download returned no data")
                    pdf_bytes = downloaded.read()
                    parsed = await asyncio.to_thread(parse_certificate_pdf, pdf_bytes, filename)
                except Exception as error:  # noqa: BLE001 - parser inputs may be sensitive
                    logger.info("Certificate PDF rejected kind=%s", type(error).__name__)
                    await repository.add_rejected(batch.id, filename, "invalid", "invalid_pdf")
                    return
                if parsed.provider_slug != batch.pool_slug:
                    await repository.add_rejected(
                        batch.id, filename, "invalid", "provider_mismatch"
                    )
                    return
                if await inventory.contains_document(parsed):
                    await repository.add_rejected(
                        batch.id, filename, "duplicate", "already_imported"
                    )
                    return
                ref: CertificateObjectRef | None = None
                try:
                    ref = await object_store.upload(parsed)
                    added = await repository.add_ready(batch.id, filename, parsed, ref)
                    if not added:
                        await object_store.delete(ref)
                        await repository.add_rejected(
                            batch.id, filename, "duplicate", "batch_duplicate"
                        )
                except Exception as error:  # noqa: BLE001 - never log certificate details
                    if ref is not None:
                        with suppress(Exception):
                            await object_store.delete(ref)
                    logger.warning("Certificate staging failed kind=%s", type(error).__name__)
                    await repository.add_rejected(
                        batch.id, filename, "invalid", "storage_error"
                    )
        finally:
            await progress.complete(bot, admin.telegram_user_id, batch.id)

    async def finish_for(admin: CertificateAdmin, batch_id: int, bot: Bot) -> None:
        pending = await progress.pending(admin.telegram_user_id, batch_id)
        if pending:
            await bot.send_message(
                admin.telegram_user_id,
                f"Ещё обрабатываются файлы: {pending}. Дождитесь завершения проверки.",
                reply_markup=_batch_keyboard(),
            )
            return
        summary = await repository.finish_batch(admin.telegram_user_id, batch_id)
        if summary is None or summary.status != "awaiting_confirmation":
            await bot.send_message(
                admin.telegram_user_id,
                "В пачке нет готовых сертификатов.",
                reply_markup=_batch_keyboard(),
            )
            return
        await progress.forget(admin.telegram_user_id, batch_id)
        await bot.send_message(
            admin.telegram_user_id,
            _batch_text(summary) + "\n\nИмпортировать готовые сертификаты?",
            reply_markup=_confirmation_keyboard(),
        )

    async def confirm_for(admin: CertificateAdmin, batch_id: int, bot: Bot) -> None:
        documents = await repository.ready_documents(admin.telegram_user_id, batch_id)
        if not documents:
            await bot.send_message(
                admin.telegram_user_id,
                "Пачка уже обработана или недоступна.",
                reply_markup=_main_keyboard(admin),
            )
            return
        try:
            imported = await inventory.import_admin_batch(
                documents, batch_id=batch_id, admin_id=admin.telegram_user_id
            )
        except Exception as error:  # noqa: BLE001 - conflict details can contain bearer values
            logger.warning("Certificate batch import failed kind=%s", type(error).__name__)
            refs = await repository.cancel_batch(admin.telegram_user_id, batch_id)
            await _delete_refs(object_store, refs)
            await bot.send_message(
                admin.telegram_user_id,
                "Импорт отменён целиком из-за конфликта или временной ошибки.",
                reply_markup=_main_keyboard(admin),
            )
            return
        await bot.send_message(
            admin.telegram_user_id,
            f"Импортировано сертификатов: {imported}.",
            reply_markup=_main_keyboard(admin),
        )

    async def cancel_for(admin: CertificateAdmin, batch_id: int, bot: Bot) -> None:
        await progress.forget(admin.telegram_user_id, batch_id)
        refs = await repository.cancel_batch(admin.telegram_user_id, batch_id)
        await _delete_refs(object_store, refs)
        await bot.send_message(
            admin.telegram_user_id, "Загрузка отменена.", reply_markup=_main_keyboard(admin)
        )

    @dispatcher.message(F.text == "Завершить загрузку")
    async def finish_batch_message(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        admin = await admin_for(message.from_user.id, message.from_user.full_name)
        if admin is None:
            return
        batch = await repository.active_batch(admin.telegram_user_id)
        if batch is None:
            await show_menu(bot, admin.telegram_user_id, admin.display_name)
            return
        await finish_for(admin, batch.id, bot)

    @dispatcher.message(F.text == "Импортировать сертификаты")
    async def confirm_batch_message(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        admin = await admin_for(message.from_user.id, message.from_user.full_name)
        if admin is None:
            return
        batch = await repository.active_batch(admin.telegram_user_id)
        if batch is None or batch.status != "awaiting_confirmation":
            await show_menu(bot, admin.telegram_user_id, admin.display_name)
            return
        await confirm_for(admin, batch.id, bot)

    @dispatcher.message(F.text == "Отменить загрузку")
    async def cancel_batch_message(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        admin = await admin_for(message.from_user.id, message.from_user.full_name)
        if admin is None:
            return
        batch = await repository.active_batch(admin.telegram_user_id)
        if batch is None:
            await show_menu(bot, admin.telegram_user_id, admin.display_name)
            return
        await cancel_for(admin, batch.id, bot)

    @dispatcher.callback_query(private_callback & F.data.startswith("batch:finish:"))
    async def finish_batch(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is not None and query.data is not None:
            await finish_for(admin, int(query.data.rsplit(":", 1)[1]), bot)

    @dispatcher.callback_query(private_callback & F.data.startswith("batch:confirm:"))
    async def confirm_batch(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is not None and query.data is not None:
            await confirm_for(admin, int(query.data.rsplit(":", 1)[1]), bot)

    @dispatcher.callback_query(private_callback & F.data.startswith("batch:cancel:"))
    async def cancel_batch(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is not None and query.data is not None:
            await cancel_for(admin, int(query.data.rsplit(":", 1)[1]), bot)

    @dispatcher.callback_query(private_callback & (F.data == "menu:stock"))
    async def stock(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None:
            return
        await show_stock(bot, admin)

    async def show_admins(bot: Bot, admin: CertificateAdmin) -> None:
        rows = await repository.admins()
        lines = ["Администраторы"] + [f"{item.display_name} — {item.role}" for item in rows]
        buttons: list[list[tuple[str, str]]] = [
            [("Пригласить оператора", "admin:invite:operator")],
            [("Пригласить владельца", "admin:invite:owner")],
        ]
        for item in rows:
            if not item.is_bootstrap and item.telegram_user_id != admin.telegram_user_id:
                buttons.append([(
                    f"Удалить {item.display_name[:24]}",
                    f"admin:revoke:{item.telegram_user_id}",
                )])
        buttons.append([("В меню", "menu:main")])
        await bot.send_message(admin.telegram_user_id, "\n".join(lines), reply_markup=_keyboard(buttons))

    @dispatcher.message(F.text == "Администраторы")
    async def admins_message(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        admin = await admin_for(message.from_user.id, message.from_user.full_name)
        if admin is not None and admin.role == "owner":
            await show_admins(bot, admin)

    @dispatcher.callback_query(private_callback & (F.data == "menu:admins"))
    async def admins(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is not None and admin.role == "owner":
            await show_admins(bot, admin)

    @dispatcher.callback_query(private_callback & F.data.startswith("admin:invite:"))
    async def invite_admin(query: CallbackQuery, bot: Bot) -> None:
        nonlocal bot_username
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None or admin.role != "owner" or query.data is None:
            return
        role = query.data.rsplit(":", 1)[1]
        _, token = await repository.create_invite(
            admin.telegram_user_id, role, settings.certificate_admin_invite_ttl_hours
        )
        if bot_username is None:
            bot_username = (await bot.get_me()).username
        await bot.send_message(
            admin.telegram_user_id,
            f"Перешлите ссылку новому администратору. Она одноразовая и действует "
            f"{settings.certificate_admin_invite_ttl_hours} ч.\n"
            f"https://t.me/{bot_username}?start=invite_{token}",
        )

    @dispatcher.callback_query(private_callback & F.data.startswith("invite:"))
    async def resolve_invite(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None or admin.role != "owner" or query.data is None:
            return
        _, action, raw_id = query.data.split(":", 2)
        candidate_id = await repository.resolve_invite(
            admin.telegram_user_id, int(raw_id), approve=action == "approve"
        )
        if candidate_id is None:
            await bot.send_message(admin.telegram_user_id, "Приглашение уже обработано.")
            return
        approved = action == "approve"
        candidate = await repository.admin(candidate_id) if approved else None
        await bot.send_message(
            candidate_id,
            "Доступ предоставлен." if approved else "Запрос на доступ отклонён.",
            reply_markup=_main_keyboard(candidate) if candidate is not None else None,
        )
        await bot.send_message(
            admin.telegram_user_id,
            "Администратор добавлен." if approved else "Приглашение отклонено.",
        )

    @dispatcher.callback_query(private_callback & F.data.startswith("admin:revoke:"))
    async def revoke_admin(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None or admin.role != "owner" or query.data is None:
            return
        target_id = int(query.data.rsplit(":", 1)[1])
        removed = await repository.revoke(admin.telegram_user_id, target_id)
        if removed:
            active = await repository.active_batch(target_id)
            if active is not None:
                refs = await repository.cancel_batch(target_id, active.id)
                await _delete_refs(object_store, refs)
        await bot.send_message(
            admin.telegram_user_id,
            "Доступ отозван." if removed else "Этого администратора нельзя удалить.",
        )

    @dispatcher.callback_query(private_callback & (F.data == "menu:main"))
    async def main_menu(query: CallbackQuery, bot: Bot) -> None:
        await query.answer()
        await show_menu(bot, query.from_user.id, query.from_user.full_name)

    return dispatcher


async def cleanup_expired_batches(
    repository: CertificateAdminRepository,
    object_store: S3CertificateObjectStore,
) -> None:
    while True:
        try:
            for admin_id, batch_id in await repository.expired_batches():
                refs = await repository.cancel_batch(admin_id, batch_id, expired=True)
                await _delete_refs(object_store, refs)
        except Exception as error:  # noqa: BLE001 - keep database/S3 details out of logs
            logger.warning("Certificate batch cleanup failed kind=%s", type(error).__name__)
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def run() -> None:
    if not settings.certificate_admin_bot_token:
        raise RuntimeError("CERTIFICATE_ADMIN_BOT_TOKEN is required")
    owner_ids = settings.certificate_admin_owner_ids()
    if not owner_ids and settings.certificate_admin_username() is None:
        raise RuntimeError("certificate administrator bootstrap identity is required")
    inventory, object_store = services()
    repository = CertificateAdminRepository(database_url(settings.certificate_database_password))
    await inventory.initialize()
    await repository.initialize(owner_ids)
    proxy = settings.resolved_telegram_proxy_url()
    dispatcher = build_dispatcher(repository, inventory, object_store)
    try:
        async with Bot(
            settings.certificate_admin_bot_token,
            session=AiohttpSession(proxy=proxy) if proxy else AiohttpSession(),
        ) as bot:
            await bot.delete_webhook(drop_pending_updates=False)
            await bot.set_my_commands([
                BotCommand(command="menu", description="Открыть меню"),
                BotCommand(command="stock", description="Показать остатки"),
                BotCommand(command="cancel", description="Отменить загрузку"),
            ])
            cleanup_task = asyncio.create_task(cleanup_expired_batches(repository, object_store))
            try:
                await dispatcher.start_polling(
                    bot, allowed_updates=["message", "callback_query"]
                )
            finally:
                cleanup_task.cancel()
                await asyncio.gather(cleanup_task, return_exceptions=True)
    finally:
        await repository.close()
        await inventory.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run())
    except Exception as error:  # noqa: BLE001 - SDK exceptions may contain tokens or payloads
        logger.error("Certificate admin bot stopped kind=%s", type(error).__name__)
        raise SystemExit(1) from None
