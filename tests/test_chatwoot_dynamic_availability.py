"""Offline adapter contracts for read-only stock and guarded issuance."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.chatwoot.service import ChatwootAgentService
from app.store import CertificateClaimResult, StoredCertificate
from tests.test_chatwoot_fast_buttons import FastChatwoot, NeutralRouter
from tests.test_chatwoot_service import StubGateway, event, ordinary_evaluation


@pytest.mark.parametrize("fast_buttons", [False, True])
@pytest.mark.parametrize("in_stock", [False, True])
async def test_menu_reads_current_availability_without_reserving(fast_buttons, in_stock):
    api = FastChatwoot()
    gateway = StubGateway(ordinary_evaluation())
    available = AsyncMock(return_value=frozenset({"food_card"}) if in_stock else frozenset())
    claim = AsyncMock(side_effect=AssertionError("menu must not reserve inventory"))
    resume = AsyncMock(side_effect=AssertionError("menu must not resume delivery"))
    request = AsyncMock(side_effect=AssertionError("menu must not persist a request"))
    service = ChatwootAgentService(
        api,
        gateway=gateway,
        queue_router=NeutralRouter(),
        certificate_available=available,
        certificate_claim=claim,
        certificate_resume=resume,
        certificate_request=request,
    )
    await service.process(event("/start", 41))
    await service.process(event("continue", 42))
    available.reset_mock()
    api.legacy_calls.clear()
    api.fast_buttons = fast_buttons

    assert await service.process(event("need:food_money", 43))

    assert available.await_count >= 1
    assert ("aid:food_card" in {choice.id for choice in api.replies[-1]["choices"]}) == in_stock
    claim.assert_not_awaited()
    resume.assert_not_awaited()
    request.assert_not_awaited()
    assert gateway.calls == 0
    if fast_buttons:
        assert len(api.commits) == 1
        assert not api.legacy_calls
    else:
        assert not api.commits


@pytest.mark.parametrize("fast_buttons", [False, True])
async def test_confirmation_reserves_once_only_in_normal_delivery_path(fast_buttons):
    api = FastChatwoot()
    api.fast_buttons = fast_buttons
    api.conversation["custom_attributes"].update(
        workflow_state="certificate_preview",
        workflow_need="food_money",
        pending_aid_id="food_card",
        scenario={"screen": "s31", "variant": 0},
    )
    gateway = StubGateway(ordinary_evaluation())
    available = AsyncMock(return_value=frozenset({"food_card"}))

    async def reserve(aid_id, issuance_key, recipient_id):
        assert "history" in api.legacy_calls
        assert (aid_id, recipient_id) == ("food_card", 7)
        return CertificateClaimResult("issued", StoredCertificate(
            aid_id=aid_id,
            provider="Test provider",
            nominal_rubles=3000,
            activation_code="TEST-CODE",
            serial_number="TEST-SERIAL",
            expires_at=datetime.now(UTC) + timedelta(days=30),
            issuance_key=issuance_key,
        ))

    claim = AsyncMock(side_effect=reserve)
    service = ChatwootAgentService(
        api,
        gateway=gateway,
        queue_router=NeutralRouter(),
        certificate_available=available,
        certificate_claim=claim,
    )
    incoming = event("certificate:confirm", 42)

    assert await service.process(incoming)

    claim.assert_awaited_once()
    assert len(api.replies) == 1
    assert api.replies[0]["sensitive_content"] == "certificate"
    assert api.conversation["custom_attributes"]["scenario"]["screen"] == "s31b"
    assert not api.commits
    assert len(api.snapshot_calls) == int(fast_buttons)
    assert gateway.calls == 0
    api.duplicate_reply_id = api.replies[0]["message_id"]
    assert not await service.process(incoming)
    claim.assert_awaited_once()
    assert len(api.replies) == 1


@pytest.mark.parametrize("fast_buttons", [False, True])
@pytest.mark.parametrize("takeover", ["history", "checkpoint", "staff_reply"])
async def test_takeover_before_reservation_aborts_without_claim_or_reply(fast_buttons, takeover):
    api = FastChatwoot()
    api.fast_buttons = fast_buttons
    api.conversation["custom_attributes"].update(
        reply_owner="bot", ownership_version=3,
        workflow_state="certificate_preview", workflow_need="food_money",
        pending_aid_id="food_card", scenario={"screen": "s31", "variant": 0},
    )
    get_messages = api.get_messages
    set_attributes = api.set_custom_attributes

    async def history(conversation_id, *, after_id=None):
        messages = await get_messages(conversation_id, after_id=after_id)
        if after_id is None:
            if takeover == "history":
                api.conversation["assignee_id"] = 4
            elif takeover == "staff_reply":
                api.messages += ({
                    "id": 43, "message_type": 1, "private": False,
                    "sender": {"id": 4, "type": "user"}, "content": "staff reply",
                },)
        return messages

    async def checkpoint(conversation_id, attributes):
        await set_attributes(conversation_id, attributes)
        if takeover == "checkpoint" and attributes.get("scenario_pending_input"):
            api.conversation["assignee_id"] = 4

    api.get_messages = history
    api.set_custom_attributes = checkpoint
    claim = AsyncMock(return_value=CertificateClaimResult("unavailable"))
    request = AsyncMock(return_value=True)
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(
        api, gateway=gateway, queue_router=NeutralRouter(),
        certificate_available=AsyncMock(return_value=frozenset({"food_card"})),
        certificate_claim=claim, certificate_request=request,
    )

    assert not await service.process(event("certificate:confirm", 42))

    claim.assert_not_awaited()
    request.assert_not_awaited()
    assert not api.replies
    assert not api.notes
    assert not api.commits
    assert api.conversation["assignee_id"] == 4
    attributes = api.conversation["custom_attributes"]
    assert attributes["reply_owner"] == "human"
    assert attributes["workflow_state"] == "certificate_preview"
    assert attributes["scenario"] == {"screen": "s31", "variant": 0}
    assert attributes.get("scenario_pending_input") is None
    assert gateway.calls == 0


@pytest.mark.parametrize("fast_buttons", [False, True])
async def test_takeover_during_reservation_keeps_it_for_same_recipient_retry(fast_buttons):
    api = FastChatwoot()
    api.fast_buttons = fast_buttons
    api.conversation["custom_attributes"].update(
        workflow_state="certificate_preview", workflow_need="food_money",
        pending_aid_id="food_card", scenario={"screen": "s31", "variant": 0},
    )
    reservations = {}

    async def reserve(aid_id, issuance_key, recipient_id):
        key = (aid_id, issuance_key, recipient_id)
        if key not in reservations:
            reservations[key] = CertificateClaimResult("issued", StoredCertificate(
                aid_id=aid_id, provider="Test provider", nominal_rubles=3000,
                activation_code="TEST-CODE", serial_number="TEST-SERIAL",
                expires_at=datetime.now(UTC) + timedelta(days=30), issuance_key=issuance_key,
            ))
            api.conversation["assignee_id"] = 4
        return reservations[key]

    claim = AsyncMock(side_effect=reserve)
    failed = AsyncMock()
    service = ChatwootAgentService(
        api, gateway=StubGateway(ordinary_evaluation()), queue_router=NeutralRouter(),
        certificate_available=AsyncMock(return_value=frozenset({"food_card"})),
        certificate_claim=claim, certificate_mark_failed=failed,
    )
    incoming = event("certificate:confirm", 42)

    assert not await service.process(incoming)
    claim.assert_awaited_once()
    assert len(reservations) == 1
    assert not api.replies
    failed.assert_not_awaited()

    await api.unassign_human(23)
    assert await service.process(incoming)
    assert claim.await_count == 2
    assert claim.await_args_list[0] == claim.await_args_list[1]
    assert len(reservations) == 1
    assert len(api.replies) == 1
    assert api.replies[0]["sensitive_content"] == "certificate"
    failed.assert_not_awaited()
