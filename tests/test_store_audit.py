from datetime import UTC, datetime, timedelta

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
async def test_in_memory_callback_claim_is_not_repeated_after_completion() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())

    claim = await store.claim_callback(record, "human", 303)
    assert claim
    assert not await store.claim_callback(record, "human", 303)
    await store.complete_callback(record, "human", 303, claim)
    assert not await store.claim_callback(record, "human", 303)
    assert await store.claim_callback(record, "human", 304)


@pytest.mark.asyncio
async def test_in_memory_callback_claim_can_be_reclaimed_after_failure() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())

    failed_claim = await store.claim_callback(record, "human", 303)
    assert failed_claim
    await store.fail_callback(record, "human", 303, failed_claim)

    assert await store.claim_callback(record, "human", 303)


@pytest.mark.asyncio
async def test_in_memory_callback_claim_can_be_reclaimed_after_lease_expiry() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())

    claim = await store.claim_callback(record, "human", 303)
    assert claim
    store.callback_claims[(record.id, "human", "303")].lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

    assert await store.claim_callback(record, "human", 303)


@pytest.mark.asyncio
async def test_postgres_store_forwards_callback_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    async def capture_claim(conversation_id: int, callback_id: str, message_id: int | None) -> str:
        calls.append((conversation_id, callback_id, message_id))
        return "lease-token"

    async def capture_complete(
        conversation_id: int, callback_id: str, message_id: int | None, lease_token: str
    ) -> None:
        calls.append(("complete", conversation_id, callback_id, message_id, lease_token))

    async def capture_failure(
        conversation_id: int, callback_id: str, message_id: int | None, lease_token: str
    ) -> None:
        calls.append(("failed", conversation_id, callback_id, message_id, lease_token))

    monkeypatch.setattr(store_module.db, "claim_callback_execution", capture_claim)
    monkeypatch.setattr(store_module.db, "complete_callback_execution", capture_complete)
    monkeypatch.setattr(store_module.db, "fail_callback_execution", capture_failure)
    record = ConversationRecord(1, "telegram", 101, 202, "helper_test")

    claim = await PostgresConversationStore().claim_callback(record, "human", 303)
    await PostgresConversationStore().complete_callback(record, "human", 303, claim or "")
    await PostgresConversationStore().fail_callback(record, "human", 303, claim or "")

    assert calls == [
        (1, "human", 303),
        ("complete", 1, "human", 303, "lease-token"),
        ("failed", 1, "human", 303, "lease-token"),
    ]
