import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import certificate_admin_bot
from app.certificate_admin import BatchSummary
from app.certificate_admin_bot import UploadProgressCoordinator, _batch_text
from app.config import Settings


def test_batch_summary_counts_every_received_document() -> None:
    summary = BatchSummary(
        batch_id=1,
        pool_slug="ozon",
        status="collecting",
        ready=12,
        duplicates=2,
        invalid=1,
    )

    assert summary.received == 15
    rendered = _batch_text(summary)
    assert "лекарства, хостел" in rendered
    assert "Получено: 15" in rendered
    assert "ожидаем" not in rendered.lower()


@pytest.mark.asyncio
async def test_bulk_upload_emits_one_start_and_one_final_status(monkeypatch) -> None:
    monkeypatch.setattr(certificate_admin_bot, "UPLOAD_DEBOUNCE_SECONDS", 0.01)
    repository = AsyncMock()
    repository.summary.return_value = BatchSummary(
        batch_id=7,
        pool_slug="ozon",
        status="collecting",
        ready=48,
        duplicates=1,
        invalid=1,
    )
    bot = AsyncMock()
    bot.send_message.side_effect = [
        SimpleNamespace(message_id=101),
        SimpleNamespace(message_id=102),
    ]
    progress = UploadProgressCoordinator(repository)

    for _ in range(50):
        await progress.begin(bot, admin_id=10, batch_id=7)
    for _ in range(50):
        await progress.complete(bot, admin_id=10, batch_id=7)
    await asyncio.sleep(0.03)

    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args_list[0].args[1] == "Получаю и проверяю сертификаты…"
    assert "Получено: 50" in bot.send_message.await_args_list[1].args[1]
    assert "Завершить загрузку" in str(
        bot.send_message.await_args_list[1].kwargs["reply_markup"]
    )


@pytest.mark.parametrize("value", ["-1", "abc", "1,broken", "1.5"])
def test_certificate_admin_rejects_invalid_bootstrap_ids(value: str) -> None:
    settings = Settings(CERTIFICATE_ADMIN_BOOTSTRAP_OWNER_IDS=value)

    with pytest.raises(ValueError, match="positive IDs"):
        settings.certificate_admin_owner_ids()
