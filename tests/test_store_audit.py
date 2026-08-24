from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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
async def test_in_memory_model_history_keeps_only_the_current_context_epoch() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())

    await store.append_message(record, "user", "before-reset")
    await store.update(record, context_epoch=record.context_epoch + 1)
    await store.append_message(record, "assistant", "after-reset")

    assert await store.history(record) == (("user", "before-reset"), ("assistant", "after-reset"))
    assert await store.model_history(record) == (("assistant", "after-reset"),)


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
async def test_in_memory_escalation_reuses_callback_request_key() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())
    request = EscalationRequest(
        cause=EscalationCause.HUMAN_REQUEST,
        reason="button",
        request_key="callback:human:303",
    )

    first = await store.create_escalation(record, request)
    second = await store.create_escalation(record, request)

    assert first is second
    assert len(store.escalations) == 1


@pytest.mark.asyncio
async def test_in_memory_aid_request_reuses_callback_request_key_without_followup_duplication() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())

    first = await store.create_aid_request(
        record,
        "food_card",
        "current_telegram",
        "@helper_test",
        request_key="callback:contact:303",
    )
    duplicate = await store.create_aid_request(
        record,
        "food_card",
        "current_telegram",
        "@helper_test",
        request_key="callback:contact:303",
    )
    later = await store.create_aid_request(
        record,
        "food_card",
        "current_telegram",
        "@helper_test",
        request_key="callback:contact:304",
    )

    assert duplicate is first
    assert len(store.aid_requests) == 2
    assert len(store.followup_jobs) == 2
    assert first.request_key != later.request_key


@pytest.mark.asyncio
async def test_postgres_update_mutates_the_supplied_record_in_place(monkeypatch: pytest.MonkeyPatch) -> None:
    row = SimpleNamespace(
        id=1,
        channel="telegram",
        channel_user_id=101,
        chat_id=202,
        username="helper_test",
        state="followup_sent",
        requested_help=None,
        pending_aid_id=None,
        pending_contact_method=None,
        pending_city=None,
        pending_district=None,
        pending_offer=None,
        generation=0,
        context_epoch=0,
        version=0,
    )

    class SessionDouble:
        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *args):  # type: ignore[no-untyped-def]
            return None

        async def execute(self, statement):  # type: ignore[no-untyped-def]
            assert "FOR UPDATE" in str(statement)
            return SimpleNamespace(scalar_one_or_none=lambda: row)

        async def commit(self) -> None:
            return None

        async def refresh(self, refreshed_row) -> None:  # type: ignore[no-untyped-def]
            assert refreshed_row is row

    monkeypatch.setattr(store_module.db, "Session", SessionDouble)
    record = ConversationRecord(1, "telegram", 101, 202, "helper_test")

    updated = await PostgresConversationStore().update(
        record,
        state="open_conversation",
        need="housing",
        pending_aid_id="hostel_3_nights",
        pending_contact_method="phone",
        pending_city="Москва",
        pending_district="ЦАО",
        pending_offer="psychologist",
    )

    assert updated is record
    assert record.version == 1
    assert record.state == "open_conversation"
    assert record.need == "housing"
    assert record.pending_aid_id == "hostel_3_nights"
    assert record.pending_contact_method == "phone"
    assert record.pending_city == "Москва"
    assert record.pending_district == "ЦАО"
    assert record.pending_offer == "psychologist"


@pytest.mark.asyncio
async def test_delete_data_removes_the_conversation_and_pending_offer() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())
    await store.update(record, pending_offer="psychologist")

    await store.delete_data(record)

    assert record.platform_user_id not in store.conversations


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
    store.callback_claims[(record.id, "keyboard-slot", "303")].lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)

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
