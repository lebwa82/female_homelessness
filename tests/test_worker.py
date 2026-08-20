from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from app.worker import DueJob, run_due_jobs


@dataclass
class FakeJobRepository:
    jobs: list[DueJob]
    completed: list[tuple[int, bool]] = field(default_factory=list)

    async def claim_due_jobs(self, now: datetime) -> list[DueJob]:
        return self.jobs

    async def complete_job(self, job: DueJob, success: bool) -> None:
        self.completed.append((job.id, success))


@pytest.mark.asyncio
async def test_due_followup_is_sent_once_with_buttons_and_marked_complete() -> None:
    repository = FakeJobRepository(
        [DueJob(id=1, conversation_id=2, chat_id=3, kind="followup")]
    )
    bot = AsyncMock()

    sent = await run_due_jobs(bot, repository, datetime(2026, 8, 20, tzinfo=UTC))

    assert sent == 1
    assert "как вы сейчас" in bot.send_message.await_args.args[1].lower()
    markup = bot.send_message.await_args.kwargs["reply_markup"]
    assert [button.callback_data for row in markup.inline_keyboard for button in row] == [
        "followup:better",
        "followup:same",
        "followup:worse",
        "human",
    ]
    assert repository.completed == [(1, True)]


@pytest.mark.asyncio
async def test_failed_followup_delivery_is_not_marked_complete() -> None:
    repository = FakeJobRepository(
        [DueJob(id=1, conversation_id=2, chat_id=3, kind="followup_reminder")]
    )
    bot = AsyncMock()
    bot.send_message.side_effect = RuntimeError("network unavailable")

    sent = await run_due_jobs(bot, repository, datetime(2026, 8, 20, tzinfo=UTC))

    assert sent == 0
    assert repository.completed == [(1, False)]

