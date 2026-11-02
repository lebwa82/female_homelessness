from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.certificate_admin import BatchSummary, CertificateAdmin, ImportBatch
from app.certificate_admin_bot import _batch_text, _confirmation_text, build_dispatcher


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(self.messages))


def summary(*, status="awaiting_confirmation", ready=4, duplicates=1, invalid=0):
    return BatchSummary(
        batch_id=7,
        pool_slug="ozon",
        status=status,
        ready=ready,
        duplicates=duplicates,
        invalid=invalid,
    )


def test_confirmation_separates_received_files_from_certificates_to_add():
    assert _confirmation_text(summary()) == (
        "Ozon\n\nБудет добавлено сертификатов: 4\n"
        "Получено PDF-файлов: 5\n"
        "Дубликаты (не добавятся): 1\n"
        "Ошибки (не добавятся): 0\n"
        "Категории: лекарства, хостел.\n\n"
        "Добавить 4 сертификата?"
    )


@pytest.mark.parametrize(
    ("count", "noun"),
    [
        (1, "сертификат"),
        (2, "сертификата"),
        (4, "сертификата"),
        (5, "сертификатов"),
        (11, "сертификатов"),
        (12, "сертификатов"),
        (14, "сертификатов"),
        (21, "сертификат"),
        (22, "сертификата"),
        (25, "сертификатов"),
        (111, "сертификатов"),
    ],
)
def test_confirmation_uses_russian_certificate_plural(count, noun):
    assert _confirmation_text(summary(ready=count)).endswith(f"Добавить {count} {noun}?")


def test_collecting_summary_does_not_claim_import_or_ask_for_confirmation():
    rendered = _confirmation_text(summary(status="collecting"))
    assert "Будет добавлено сертификатов: 4" in rendered
    assert "Добавить 4" not in rendered
    assert "Импортировано" not in rendered


def test_imported_summary_does_not_offer_another_import_or_invent_counts():
    rendered = _confirmation_text(summary(status="imported", ready=0))
    assert rendered == "Ozon\n\nИмпорт завершён.\nКатегории: лекарства, хостел."
    assert "Будет добавлено" not in rendered
    assert "Получено" not in rendered


def test_zero_ready_summary_has_no_import_question():
    rendered = _confirmation_text(summary(ready=0))
    assert "Будет добавлено сертификатов: 0" in rendered
    assert "?" not in rendered


def dispatcher_fixture(batch_summary):
    repository = AsyncMock()
    repository.admin.return_value = CertificateAdmin(10, "Admin", "admin", False)
    repository.active_batch.return_value = ImportBatch(7, 10, "ozon", batch_summary.status)
    repository.summary.return_value = batch_summary
    repository.finish_batch.return_value = batch_summary
    inventory = AsyncMock()
    dispatcher = build_dispatcher(repository, inventory, AsyncMock())
    handlers = {handler.callback.__name__: handler.callback for handler in dispatcher.message.handlers}
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=10, full_name="Admin"),
        chat=SimpleNamespace(type="private"),
    )
    return handlers, message, repository, inventory


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["finish_batch_message", "menu_command"])
async def test_finish_and_menu_show_same_confirmation_before_import(handler_name):
    handlers, message, repository, inventory = dispatcher_fixture(summary())
    bot = FakeBot()

    await handlers[handler_name](message, bot)

    assert bot.messages[0][1] == _confirmation_text(summary())
    buttons = bot.messages[0][2]["reply_markup"].keyboard
    assert buttons[0][0].text == "Импортировать сертификаты"
    repository.ready_documents.assert_not_awaited()
    inventory.import_admin_batch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("handler_name", ["finish_batch_message", "menu_command"])
async def test_zero_ready_batch_does_not_offer_import(handler_name):
    handlers, message, _, inventory = dispatcher_fixture(summary(ready=0))
    bot = FakeBot()

    await handlers[handler_name](message, bot)

    assert "?" not in bot.messages[0][1]
    buttons = bot.messages[0][2]["reply_markup"].keyboard
    assert all(button.text != "Импортировать сертификаты" for row in buttons for button in row)
    inventory.import_admin_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_is_reported_only_after_inventory_import():
    handlers, message, repository, inventory = dispatcher_fixture(summary())
    repository.ready_documents.return_value = (object(),) * 4
    bot = FakeBot()

    async def import_batch(*args, **kwargs):
        assert not bot.messages
        return 4

    inventory.import_admin_batch.side_effect = import_batch
    await handlers["confirm_batch_message"](message, bot)

    inventory.import_admin_batch.assert_awaited_once()
    assert bot.messages[0][1] == "Импортировано сертификатов: 4."


def test_received_count_includes_rejections():
    rendered = _batch_text(summary(ready=4, duplicates=1, invalid=2))
    assert "Будет добавлено сертификатов: 4" in rendered
    assert "Получено PDF-файлов: 7" in rendered
    assert "Ошибки (не добавятся): 2" in rendered
