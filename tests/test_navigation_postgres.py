"""Opt-in real PostgreSQL test; all scenario rows are rolled back."""

import os
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app import db
from app.domain import IncomingMessage
from app.service import ConversationService
from app.store import PostgresConversationStore


@pytest.mark.skipif(os.getenv("TEST_POSTGRES_NAVIGATION") != "1", reason="requires local PostgreSQL")
@pytest.mark.asyncio
async def test_navigation_survives_database_reload_without_removing_business_records(monkeypatch) -> None:
    await db.init_db()
    await db.init_db()
    user_id = -(uuid4().int % (2**62))

    def incoming(number: int) -> IncomingMessage:
        return IncomingMessage(platform_user_id=user_id, chat_id=user_id, text="", message_id=number)

    try:
        async with db.engine.connect() as connection:
            transaction = await connection.begin()
            monkeypatch.setattr(db, "Session", async_sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint",
            ))
            try:
                service = ConversationService(PostgresConversationStore())
                await service.start(incoming(1))
                await service.handle_callback(incoming(2), "continue")
                await service.handle_callback(incoming(3), "need:food_money")
                await service.handle_callback(incoming(4), "aid:food_card")
                done = await service.handle_callback(incoming(5), "contact:later")
                callback = next(choice.id for choice in done.choices if choice.id.startswith("back:"))

                # New store and service must load checkpoints from JSONB, not process memory.
                store = PostgresConversationStore()
                service = ConversationService(store)
                record = await store.get(incoming(6))
                assert record is not None
                history = await store.history(record)
                original_entries = record.navigation["entries"]
                await service.handle_callback(incoming(6), callback)
                reloaded = await store.get(incoming(7))
                assert reloaded is not None
                assert reloaded.state == "collecting_contact_method"
                assert reloaded.pending_aid_id == "food_card"
                assert reloaded.navigation["entries"] == original_entries
                assert (await store.history(reloaded))[:len(history)] == history
                requests = await connection.execute(db.select(db.func.count()).select_from(db.AidRequest).where(
                    db.AidRequest.conversation_id == record.id,
                ))
                assert requests.scalar_one() == 1
            finally:
                await transaction.rollback()
    finally:
        await db.engine.dispose()
