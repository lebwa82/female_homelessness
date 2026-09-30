"""Private Telegram interface for importing PDF bearer certificates."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

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


def _main_keyboard(admin: CertificateAdmin) -> InlineKeyboardMarkup:
    rows = [
        [("Загрузить сертификаты", "menu:upload")],
        [("Остатки", "menu:stock")],
    ]
    if admin.role == "owner":
        rows.append([("Администраторы", "menu:admins")])
    return _keyboard(rows)


def _batch_text(summary: BatchSummary) -> str:
    provider, aids = POOL_AIDS[summary.pool_slug]
    categories = ", ".join(AID_LABELS[aid] for aid in aids)
    return (
        f"{provider}: получено {summary.received} из ожидаемых {summary.target_count}.\n"
        f"Готово: {summary.ready}; дубликаты: {summary.duplicates}; "
        f"ошибки: {summary.invalid}.\nКатегории: {categories}."
    )


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
        await bot.send_message(
            user_id,
            "Управление сертификатами",
            reply_markup=_main_keyboard(admin),
        )

    async def show_stock(bot: Bot, admin: CertificateAdmin) -> None:
        rows = await inventory.stock()
        by_slug = {row.slug: row for row in rows}
        lines = ["Остатки сертификатов"]
        for slug in ("ozon", "pyaterochka"):
            row = by_slug.get(slug)
            available = row.available if row is not None else 0
            lines.append(
                f"{POOL_LABELS[slug]}: доступно {available}; "
                f"ориентир {settings.certificate_admin_target_batch_size}"
            )
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

    @dispatcher.message(Command("cancel"))
    async def cancel_command(message: Message) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        admin = await admin_for(message.from_user.id, message.from_user.full_name)
        if admin is None:
            return
        batch = await repository.active_batch(admin.telegram_user_id)
        if batch is None:
            await message.answer("Активной загрузки нет.")
            return
        refs = await repository.cancel_batch(admin.telegram_user_id, batch.id)
        await _delete_refs(object_store, refs)
        await message.answer("Загрузка отменена.", reply_markup=_main_keyboard(admin))

    @dispatcher.message(Command("stock"))
    async def stock_command(message: Message, bot: Bot) -> None:
        if message.from_user is None or message.chat.type != "private":
            return
        admin = await admin_for(message.from_user.id, message.from_user.full_name)
        if admin is not None:
            await show_stock(bot, admin)

    private_callback = F.message.chat.type == "private"

    @dispatcher.callback_query(private_callback & (F.data == "menu:upload"))
    async def choose_pool(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None:
            return
        active = await repository.active_batch(admin.telegram_user_id)
        if active is not None:
            summary = await repository.summary(admin.telegram_user_id, active.id)
            if summary is not None:
                await bot.send_message(
                    admin.telegram_user_id,
                    _batch_text(summary),
                    reply_markup=_keyboard([[
                        ("Завершить", f"batch:finish:{active.id}"),
                        ("Отменить", f"batch:cancel:{active.id}"),
                    ]]),
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
            batch = await repository.start_batch(
                admin.telegram_user_id,
                pool_slug,
                target_count=settings.certificate_admin_target_batch_size,
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
            f"Ожидается файлов: {batch.target_count}. Отправляйте PDF по одному или пачкой.",
            reply_markup=_keyboard([[
                ("Завершить", f"batch:finish:{batch.id}"),
                ("Отменить", f"batch:cancel:{batch.id}"),
            ]]),
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
            await message.answer("Сначала начните новую загрузку через меню.")
            return
        filename = message.document.file_name or "certificate.pdf"
        if (
            message.document.file_size is not None
            and message.document.file_size > MAX_CERTIFICATE_PDF_BYTES
        ):
            await repository.add_rejected(batch.id, filename, "invalid", "file_too_large")
            await message.answer("Файл отклонён: размер превышает 10 MiB.")
            return
        try:
            downloaded = await bot.download(message.document)
            if downloaded is None:
                raise ValueError("download returned no data")
            pdf_bytes = downloaded.read()
            parsed = await asyncio.to_thread(parse_certificate_pdf, pdf_bytes, filename)
        except Exception as error:  # noqa: BLE001 - parser inputs and SDK payloads are sensitive
            logger.info("Certificate PDF rejected kind=%s", type(error).__name__)
            await repository.add_rejected(batch.id, filename, "invalid", "invalid_pdf")
            await message.answer("Файл не распознан как поддерживаемый сертификат.")
            return
        if parsed.provider_slug != batch.pool_slug:
            await repository.add_rejected(batch.id, filename, "invalid", "provider_mismatch")
            await message.answer("Тип сертификата не совпадает с выбранной загрузкой.")
            return
        if await inventory.contains_document(parsed):
            await repository.add_rejected(batch.id, filename, "duplicate", "already_imported")
            await message.answer("Этот сертификат уже есть в инвентаре.")
            return
        ref: CertificateObjectRef | None = None
        try:
            ref = await object_store.upload(parsed)
            added = await repository.add_ready(batch.id, filename, parsed, ref)
            if not added:
                await object_store.delete(ref)
                await repository.add_rejected(batch.id, filename, "duplicate", "batch_duplicate")
                await message.answer("Этот сертификат уже есть в текущей пачке.")
                return
        except Exception as error:  # noqa: BLE001 - never log certificate or object details
            if ref is not None:
                with suppress(Exception):
                    await object_store.delete(ref)
            logger.warning("Certificate staging failed kind=%s", type(error).__name__)
            await message.answer("Не удалось безопасно сохранить файл. Попробуйте ещё раз.")
            return
        summary = await repository.summary(admin.telegram_user_id, batch.id)
        if summary is not None:
            await message.answer(_batch_text(summary))

    @dispatcher.callback_query(private_callback & F.data.startswith("batch:finish:"))
    async def finish_batch(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None or query.data is None:
            return
        batch_id = int(query.data.rsplit(":", 1)[1])
        summary = await repository.finish_batch(admin.telegram_user_id, batch_id)
        if summary is None or summary.status != "awaiting_confirmation":
            await bot.send_message(admin.telegram_user_id, "В пачке нет готовых сертификатов.")
            return
        await bot.send_message(
            admin.telegram_user_id,
            _batch_text(summary) + "\n\nИмпортировать готовые сертификаты?",
            reply_markup=_keyboard([[
                (f"Импортировать {summary.ready}", f"batch:confirm:{batch_id}"),
                ("Отменить", f"batch:cancel:{batch_id}"),
            ]]),
        )

    @dispatcher.callback_query(private_callback & F.data.startswith("batch:confirm:"))
    async def confirm_batch(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None or query.data is None:
            return
        batch_id = int(query.data.rsplit(":", 1)[1])
        documents = await repository.ready_documents(admin.telegram_user_id, batch_id)
        if not documents:
            await bot.send_message(admin.telegram_user_id, "Пачка уже обработана или недоступна.")
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
            )
            return
        await bot.send_message(
            admin.telegram_user_id,
            f"Импортировано сертификатов: {imported}.",
            reply_markup=_main_keyboard(admin),
        )

    @dispatcher.callback_query(private_callback & F.data.startswith("batch:cancel:"))
    async def cancel_batch(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None or query.data is None:
            return
        batch_id = int(query.data.rsplit(":", 1)[1])
        refs = await repository.cancel_batch(admin.telegram_user_id, batch_id)
        await _delete_refs(object_store, refs)
        await bot.send_message(
            admin.telegram_user_id, "Загрузка отменена.", reply_markup=_main_keyboard(admin)
        )

    @dispatcher.callback_query(private_callback & (F.data == "menu:stock"))
    async def stock(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None:
            return
        await show_stock(bot, admin)

    @dispatcher.callback_query(private_callback & (F.data == "menu:admins"))
    async def admins(query: CallbackQuery, bot: Bot) -> None:
        admin = await admin_for(query.from_user.id, query.from_user.full_name)
        await query.answer()
        if admin is None or admin.role != "owner":
            return
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
        await bot.send_message(
            candidate_id,
            "Доступ предоставлен." if approved else "Запрос на доступ отклонён.",
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
