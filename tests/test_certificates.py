from datetime import UTC, datetime, timedelta

import pytest

from app.domain import IncomingMessage
from app.service import ConversationService
from app.store import InMemoryConversationStore, StoredCertificate
from scripts.import_certificates import _validated


def incoming(message_id: int) -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=101,
        chat_id=202,
        username="helper_test",
        text="",
        message_id=message_id,
    )


def certificate(code: str, *, expires_in_days: int = 30) -> StoredCertificate:
    return StoredCertificate(
        aid_id="food_card",
        provider="Озон",
        nominal_rubles=300,
        activation_code=code,
        expires_at=datetime.now(UTC) + timedelta(days=expires_in_days),
        serial_number=f"serial-{code}",
    )


@pytest.mark.asyncio
async def test_certificate_is_issued_directly_once_and_never_scheduled_for_usage_check() -> None:
    store = InMemoryConversationStore(certificates=[certificate("FIRST"), certificate("SECOND")])
    service = ConversationService(store=store)

    await service.start(incoming(1))
    await service.handle_callback(incoming(2), "continue")
    await service.handle_callback(incoming(3), "need:food_money")
    first = await service.handle_callback(incoming(4), "aid:food_card")
    replay = await service.handle_callback(incoming(4), "aid:food_card")

    assert "Код активации: FIRST" in first.text
    assert replay.text == first.text
    assert len(store.aid_requests) == 1
    assert store.aid_requests[0].contact_method is None
    assert store.certificates[0].issued_at is not None
    assert store.certificates[1].issued_at is None
    assert store.followup_jobs == []


@pytest.mark.asyncio
async def test_certificate_delivery_is_hidden_from_future_model_context() -> None:
    store = InMemoryConversationStore(certificates=[certificate("BEARER-SECRET")])
    service = ConversationService(store=store)

    await service.start(incoming(11))
    await service.handle_callback(incoming(12), "continue")
    await service.handle_callback(incoming(13), "need:food_money")
    turn = await service.handle_callback(incoming(14), "aid:food_card")
    await service.record_outbound(incoming(14), turn)

    assert store.messages[-1][3]["content_type"] == "certificate"
    isolated = InMemoryConversationStore()
    record = await isolated.ensure(incoming(16))
    await isolated.append_message(
        record,
        "assistant",
        "Code: BEARER-SECRET",
        {"content_type": "certificate"},
    )
    history = await isolated.model_history(record)
    assert ("assistant", "[SENSITIVE_DELIVERY]") in history
    assert all("BEARER-SECRET" not in content for _, content in history)


@pytest.mark.asyncio
async def test_expired_certificate_is_not_issued_and_request_uses_existing_fallback() -> None:
    store = InMemoryConversationStore(certificates=[certificate("EXPIRED", expires_in_days=-1)])
    service = ConversationService(store=store)

    await service.start(incoming(21))
    await service.handle_callback(incoming(22), "continue")
    await service.handle_callback(incoming(23), "need:food_money")
    turn = await service.handle_callback(incoming(24), "aid:food_card")

    assert "запрос сохранён" in turn.text
    assert store.certificates[0].issued_at is None
    assert len(store.followup_jobs) == 1


def test_import_validation_accepts_documented_certificate_shape_without_logging_secrets() -> None:
    values = _validated(
        {
            "aid_id": "food_card",
            "provider": "Озон",
            "nominal_rubles": 300,
            "activation_code": "CODE",
            "activate_by": "03.09.2027",
            "serial_number": "SERIAL",
        }
    )

    assert values["activation_code"] == "CODE"
    assert values["expires_at"].hour == 23
    assert values["expires_at"].minute == 59
