from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select

from app.config import settings
from app.db import Conversation, FollowupJob, Session, purge_expired_content

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DueJob:
    id: int
    conversation_id: int
    chat_id: int
    kind: str


class JobRepository(Protocol):
    async def claim_due_jobs(self, now: datetime) -> list[DueJob]: ...

    async def complete_job(self, job: DueJob, success: bool) -> None: ...


class PostgresJobRepository:
    async def claim_due_jobs(self, now: datetime) -> list[DueJob]:
        async with Session() as session:
            result = await session.execute(
                select(FollowupJob, Conversation.chat_id)
                .join(Conversation, Conversation.id == FollowupJob.conversation_id)
                .where(FollowupJob.status == "pending", FollowupJob.due_at <= now)
                .with_for_update(skip_locked=True)
            )
            rows = result.all()
            jobs: list[DueJob] = []
            for job, chat_id in rows:
                job.status = "processing"
                job.attempts += 1
                if chat_id is not None:
                    jobs.append(DueJob(job.id, job.conversation_id, chat_id, job.kind))
            await session.commit()
            return jobs

    async def complete_job(self, job: DueJob, success: bool) -> None:
        async with Session() as session:
            row = await session.get(FollowupJob, job.id)
            if row is None:
                return
            if not success:
                row.status = "pending"
                await session.commit()
                return
            row.status = "completed"
            row.sent_at = datetime.now(UTC)
            if row.kind == "followup":
                session.add(
                    FollowupJob(
                        conversation_id=row.conversation_id,
                        aid_request_id=row.aid_request_id,
                        kind="followup_reminder",
                        due_at=datetime.now(UTC) + timedelta(seconds=settings.followup_reminder_seconds),
                    )
                )
                conversation = await session.get(Conversation, row.conversation_id)
                if conversation is not None:
                    conversation.state = "followup_sent"
            await session.commit()


def followup_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Лучше", callback_data="followup:better")],
            [InlineKeyboardButton(text="Примерно так же", callback_data="followup:same")],
            [InlineKeyboardButton(text="Сложнее", callback_data="followup:worse")],
            [InlineKeyboardButton(text="Поговорить с живым человеком", callback_data="human")],
        ]
    )


async def run_due_jobs(bot: Bot, repository: JobRepository, now: datetime | None = None) -> int:
    sent = 0
    for job in await repository.claim_due_jobs(now or datetime.now(UTC)):
        try:
            if job.kind == "followup":
                await bot.send_message(
                    job.chat_id,
                    "Привет. Просто хотела узнать — как вы сейчас по сравнению с тогда, когда мы впервые написали?",
                    reply_markup=followup_markup(),
                )
            else:
                await bot.send_message(
                    job.chat_id,
                    "Всё нормально, если не хочется отвечать. Мы здесь, если что.",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [InlineKeyboardButton(text="Поговорить с живым человеком", callback_data="human")]
                        ]
                    ),
                )
        except (OSError, RuntimeError, TelegramAPIError) as error:
            logger.warning("Follow-up delivery failed for job %s: %s", job.id, type(error).__name__)
            await repository.complete_job(job, False)
        else:
            await repository.complete_job(job, True)
            sent += 1
    return sent


async def worker_loop(bot: Bot, repository: JobRepository | None = None) -> None:
    repository = repository or PostgresJobRepository()
    while True:
        await run_due_jobs(bot, repository)
        await purge_expired_content()
        await asyncio.sleep(settings.worker_poll_seconds)
