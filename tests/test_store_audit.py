import pytest

from app import store as store_module
from app.domain import EscalationCause, EscalationRequest, IncomingMessage
from app.store import ConversationRecord, InMemoryConversationStore, PostgresConversationStore


def identity() -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=101,
        chat_id=202,
        username="helper_test",
        text="",
        message_id=303,
    )


@pytest.mark.asyncio
async def test_in_memory_store_keeps_policy_audit_without_message_text() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())

    await store.record_action(
        record,
        "policy_decision",
        "completed",
        {"intent": "open_conversation", "choice_set": "none"},
    )

    assert store.actions[-1] == (
        record.id,
        "policy_decision",
        "completed",
        {"intent": "open_conversation", "choice_set": "none"},
    )


@pytest.mark.asyncio
async def test_postgres_store_forwards_policy_audit_to_database(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str, str, dict[str, str]]] = []

    async def capture_action(
        conversation_id: int, kind: str, status: str, audit: dict[str, str]
    ) -> None:
        calls.append((conversation_id, kind, status, audit))

    monkeypatch.setattr(store_module.db, "record_action", capture_action)
    record = ConversationRecord(1, "telegram", 101, 202, "helper_test")
    audit = {"intent": "open_conversation", "choice_set": "none"}

    await PostgresConversationStore().record_action(record, "policy_decision", "completed", audit)

    assert calls == [(1, "policy_decision", "completed", audit)]


@pytest.mark.asyncio
async def test_escalation_request_keeps_human_cause_without_risk_level() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())

    await store.create_escalation(
        record,
        EscalationRequest(cause=EscalationCause.HUMAN_REQUEST, reason="button"),
    )

    escalation = store.escalations[-1]
    assert escalation.cause is EscalationCause.HUMAN_REQUEST
    assert escalation.level is None
    assert escalation.reason == "button"


@pytest.mark.asyncio
async def test_delete_data_resets_pending_offer() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())
    await store.update(record, pending_offer="psychologist")

    await store.delete_data(record)

    assert record.pending_offer is None


@pytest.mark.asyncio
async def test_in_memory_callback_claim_is_idempotent_per_message() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())

    assert await store.claim_callback(record, "human", 303)
    assert not await store.claim_callback(record, "human", 303)
    assert await store.claim_callback(record, "human", 304)


@pytest.mark.asyncio
async def test_postgres_store_forwards_callback_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, str, int | None]] = []

    async def capture_claim(conversation_id: int, callback_id: str, message_id: int | None) -> bool:
        calls.append((conversation_id, callback_id, message_id))
        return True

    monkeypatch.setattr(store_module.db, "claim_callback_execution", capture_claim)
    record = ConversationRecord(1, "telegram", 101, 202, "helper_test")

    assert await PostgresConversationStore().claim_callback(record, "human", 303)
    assert calls == [(1, "human", 303)]
