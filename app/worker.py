from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import and_, or_, select

from app import db
from app.config import settings
from app.db import Conversation, FollowupJob, purge_expired_content
from app.domain import DELIVERY_AMBIGUOUS_CATEGORY, ConversationState, DeliveryAuthorization

logger = logging.getLogger(__name__)
FOLLOWUP_PROCESSING_LEASE = timedelta(minutes=5)


@dataclass(frozen=True)
class DueJob:
    id: int
    conversation_id: int
    chat_id: int
    kind: str
    conversation_generation: int = 0
    lease_token: str | None = None


class JobRepository(Protocol):
    async def claim_due_jobs(self, now: datetime) -> list[DueJob]: ...

    async def can_deliver(self, job: DueJob) -> bool: ...

    async def complete_job(self, job: DueJob, success: bool) -> None: ...

    async def discard_job(self, job: DueJob) -> None: ...


def followup_claim_statement(now: datetime):
    """Production claim query, exposed so its reclaim predicate is auditable."""
    return (
        select(FollowupJob, Conversation.chat_id, FollowupJob.conversation_generation)
        .join(Conversation, Conversation.id == FollowupJob.conversation_id)
        .where(
            FollowupJob.due_at <= now,
            or_(
                FollowupJob.status == "pending",
                and_(
                    FollowupJob.status == "processing",
                    or_(
                        FollowupJob.lease_expires_at.is_(None),
                        FollowupJob.lease_expires_at <= now,
                    ),
                ),
            ),
        )
        .with_for_update(skip_locked=True)
    )


class PostgresJobRepository:
    async def claim_due_jobs(self, now: datetime) -> list[DueJob]:
        async with db.repository_session() as session:
            result = await session.execute(followup_claim_statement(now))
            rows = result.all()
            jobs: list[DueJob] = []
            for job, chat_id, generation in rows:
                job.status = "processing"
                job.attempts += 1
                job.lease_token = uuid4().hex
                job.lease_expires_at = now + FOLLOWUP_PROCESSING_LEASE
                if chat_id is None:
                    job.status = "cancelled"
                    job.lease_token = None
                    job.lease_expires_at = None
                else:
                    jobs.append(DueJob(job.id, job.conversation_id, chat_id, job.kind, generation, job.lease_token))
            await db.finish_repository_write(session)
            return jobs

    async def complete_job(self, job: DueJob, success: bool) -> None:
        async with db.repository_session() as session:
            row = await session.get(FollowupJob, job.id)
            if (
                row is None
                or row.status != "processing"
                or row.lease_token != job.lease_token
            ):
                return
            if not success:
                row.status = "pending"
                row.lease_token = None
                row.lease_expires_at = None
                await db.finish_repository_write(session)
                return
            row.status = "completed"
            row.lease_token = None
            row.lease_expires_at = None
            row.sent_at = datetime.now(UTC)
            if row.kind == "followup":
                session.add(
                    FollowupJob(
                        conversation_id=row.conversation_id,
                        conversation_generation=job.conversation_generation,
                        aid_request_id=row.aid_request_id,
                        kind="followup_reminder",
                        due_at=datetime.now(UTC) + timedelta(seconds=settings.followup_reminder_seconds),
                    )
                )
                conversation = await session.get(Conversation, row.conversation_id)
                if conversation is not None:
                    conversation.state = "followup_sent"
            await db.finish_repository_write(session)

    async def can_deliver(self, job: DueJob) -> bool:
        """Check the claimed job immediately before delivery without recreating state."""
        async with db.repository_session() as session:
            row = await session.get(FollowupJob, job.id)
            if (
                row is None
                or row.status != "processing"
                or row.lease_token != job.lease_token
                or row.lease_expires_at is None
                or row.lease_expires_at <= datetime.now(UTC)
                or row.conversation_id != job.conversation_id
            ):
                return False
            conversation = await session.get(Conversation, job.conversation_id)
            return (
                conversation is not None
                and conversation.chat_id == job.chat_id
                and row.conversation_generation == job.conversation_generation == conversation.generation
                and conversation.state not in {ConversationState.CLOSED.value, "cancelled"}
            )

    @asynccontextmanager
    async def delivery_authorization(self, job: DueJob) -> AsyncIterator[bool]:
        """Hold the conversation and job rows through final authorization and send.

        Cancellation/deletion takes the conversation lock before touching its jobs,
        so it linearizes entirely before this context or entirely after its send.
        """
        async with db.repository_session() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.id == job.conversation_id).with_for_update()
            )
            row = await session.scalar(select(FollowupJob).where(FollowupJob.id == job.id).with_for_update())
            allowed = (
                row is not None
                and conversation is not None
                and row.status == "processing"
                and row.lease_token == job.lease_token
                and row.lease_expires_at is not None
                and row.lease_expires_at > datetime.now(UTC)
                and row.conversation_id == job.conversation_id
                and row.conversation_generation == job.conversation_generation == conversation.generation
                and conversation.chat_id == job.chat_id
                and conversation.state not in {ConversationState.CLOSED.value, "cancelled"}
            )
            if not allowed:
                owns_lease = (
                    row is not None
                    and row.status == "processing"
                    and row.lease_token == job.lease_token
                    and row.conversation_id == job.conversation_id
                )
                terminal_denial = owns_lease and (
                    conversation is None
                    or conversation.chat_id != job.chat_id
                    or row.conversation_generation
                    != job.conversation_generation
                    or conversation.generation != job.conversation_generation
                    or conversation.state in {ConversationState.CLOSED.value, "cancelled"}
                )
                if terminal_denial:
                    row.status = "cancelled"
                    row.lease_token = None
                    row.lease_expires_at = None
                    try:
                        yield False
                    finally:
                        await db.finish_repository_write(session)
                    return
                if owns_lease and (
                    row.lease_expires_at is None
                    or row.lease_expires_at <= datetime.now(UTC)
                ):
                    row.status = "pending"
                    row.lease_token = None
                    row.lease_expires_at = None
                    try:
                        yield False
                    finally:
                        await db.finish_repository_write(session)
                    return
                yield False
                return
            try:
                yield True
            except Exception:
                row.status = "pending"
                row.lease_token = None
                row.lease_expires_at = None
                await db.finish_repository_write(session)
                raise
            else:
                row.status = "completed"
                row.lease_token = None
                row.lease_expires_at = None
                row.sent_at = datetime.now(UTC)
                if row.kind == "followup":
                    session.add(
                        FollowupJob(
                            conversation_id=row.conversation_id,
                            conversation_generation=job.conversation_generation,
                            aid_request_id=row.aid_request_id,
                            kind="followup_reminder",
                            due_at=datetime.now(UTC) + timedelta(seconds=settings.followup_reminder_seconds),
                        )
                    )
                    conversation.state = "followup_sent"
                await db.finish_repository_write(session)

    async def discard_job(self, job: DueJob) -> None:
        async with db.repository_session() as session:
            row = await session.get(FollowupJob, job.id)
            if row is not None and row.status == "processing" and row.lease_token == job.lease_token:
                row.status = "cancelled"
                row.lease_token = None
                row.lease_expires_at = None
                await db.finish_repository_write(session)


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
        delivery_authorization = getattr(repository, "delivery_authorization", None)
        if delivery_authorization is not None:
            try:
                async with delivery_authorization(job) as authorized:
                    if not authorized:
                        continue
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
            except Exception as error:  # noqa: BLE001 - the context releases a failed leased job
                logger.warning("Follow-up delivery failed for job %s: %s", job.id, type(error).__name__)
                continue
            sent += 1
            continue
        try:
            can_deliver = getattr(repository, "can_deliver", None)
            if can_deliver is not None and not await can_deliver(job):
                discard = getattr(repository, "discard_job", None)
                if discard is not None:
                    await discard(job)
                continue
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
        except Exception as error:  # noqa: BLE001 - a leased job must always be released or reclaimed
            logger.warning("Follow-up delivery failed for job %s: %s", job.id, type(error).__name__)
            try:
                await repository.complete_job(job, False)
            except Exception as completion_error:  # noqa: BLE001 - expired lease is the recovery path
                logger.warning("Follow-up recovery failed for job %s: %s", job.id, type(completion_error).__name__)
        else:
            try:
                await repository.complete_job(job, True)
            except Exception as error:  # noqa: BLE001 - do not abort later due jobs
                logger.warning("Follow-up completion failed for job %s: %s", job.id, type(error).__name__)
            else:
                sent += 1
    return sent


async def run_pending_outcomes(bot: Bot, service: Any) -> int:
    """Deliver committed text outcomes without re-running model diagnostics."""
    sent = 0
    for pending in await service.store.pending_text_outcomes():
        send_succeeded = False
        try:
            async with service.delivery_authorization(pending.incoming, pending.turn) as authorization:
                if authorization is not DeliveryAuthorization.ALLOW:
                    continue
                await bot.send_message(
                    pending.incoming.chat_id,
                    pending.turn.text,
                    reply_markup=(
                        InlineKeyboardMarkup(
                            inline_keyboard=[
                                [InlineKeyboardButton(text=choice.label, callback_data=choice.id)]
                                for choice in pending.turn.choices
                            ]
                        )
                        if pending.turn.choices
                        else None
                    ),
                )
                send_succeeded = True
        except Exception as error:  # noqa: BLE001 - the delivery context releases the lease
            if send_succeeded:
                try:
                    await service.record_delivery_ambiguity(pending.incoming, pending.turn)
                except Exception as audit_error:  # noqa: BLE001 - log is the fallback metric
                    logger.warning(
                        "Pending outcome category=%s audit unavailable: %s",
                        DELIVERY_AMBIGUOUS_CATEGORY,
                        type(audit_error).__name__,
                    )
            logger.warning("Pending outcome delivery failed: %s", type(error).__name__)
            continue
        try:
            await service.record_outbound(pending.incoming, pending.turn)
        except Exception as error:  # noqa: BLE001 - acknowledgement already committed
            logger.warning("Pending outcome audit failed: %s", type(error).__name__)
        sent += 1
    return sent


async def purge_expired_content_safely() -> bool:
    """Keep the worker alive when retention maintenance has a transient DB failure."""
    try:
        await purge_expired_content()
    except Exception as error:  # noqa: BLE001 - maintenance must retry on the next poll
        logger.warning("Retention purge failed: %s", type(error).__name__)
        return False
    return True


async def worker_loop(bot: Bot, repository: JobRepository | None = None, outbox_service: Any | None = None) -> None:
    production_repository = repository is None
    repository = repository or PostgresJobRepository()
    if production_repository and outbox_service is None:
        from app.service import ConversationService

        outbox_service = ConversationService()
    while True:
        if outbox_service is not None:
            try:
                await run_pending_outcomes(bot, outbox_service)
            except Exception as error:  # noqa: BLE001 - one outbox scan cannot stop later work
                logger.warning("Pending outcome iteration failed: %s", type(error).__name__)
        try:
            await run_due_jobs(bot, repository)
        except Exception as error:  # noqa: BLE001 - one transient claim must not stop retention or later polls
            logger.warning("Follow-up iteration failed: %s", type(error).__name__)
        await purge_expired_content_safely()
        await asyncio.sleep(settings.worker_poll_seconds)
