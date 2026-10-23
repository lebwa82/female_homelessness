"""Offline demand tracking without changing the paused certificate response."""

from unittest.mock import AsyncMock

import pytest

from app.chatwoot.service import ChatwootAgentService
from app.scenario import CERTIFICATE_PAUSED_TEXT
from tests.test_chatwoot_fast_buttons import FastChatwoot
from tests.test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation


def runtime(api=None):
    api = api or FakeChatwoot()
    recorder = AsyncMock(return_value=True)
    service = ChatwootAgentService(
        api, gateway=StubGateway(ordinary_evaluation()), certificate_request=recorder,
    )
    api.conversation["custom_attributes"].update({
        "workflow_state": "choosing_aid", "workflow_need": "food_money",
        "scenario": {"screen": "s22", "variant": 0},
    })
    return api, service, recorder


@pytest.mark.parametrize("aid_id,need,screen", (
    ("food_card", "food_money", "s22"), ("medicine_card", "food_money", "s22"),
    ("hostel_3_nights", "housing", "s21"), ("children_card", "children", "s25"),
))
async def test_specific_selection_is_recorded_before_paused_reply_and_replay_is_ignored(
    aid_id, need, screen,
):
    api, service, recorder = runtime()
    api.conversation["custom_attributes"].update({
        "workflow_need": need, "scenario": {"screen": screen, "variant": 0},
    })

    async def record(*args):
        assert not api.replies
        assert api.conversation["custom_attributes"]["scenario"]["screen"] == screen
        return True

    recorder.side_effect = record
    assert await service.process(event(f"aid:{aid_id}"))
    recorder.assert_awaited_once()
    selected_aid, request_key, contact_id, conversation_id, message_id, reason = recorder.await_args.args
    assert (selected_aid, contact_id, conversation_id, message_id, reason) == (
        aid_id, 7, 23, 41, "paused",
    )
    assert len(request_key) == 24
    assert api.replies[0]["text"] == CERTIFICATE_PAUSED_TEXT
    assert api.conversation["custom_attributes"]["pending_aid_id"] is None
    assert not await service.process(event(f"aid:{aid_id}"))
    recorder.assert_awaited_once()


@pytest.mark.parametrize("callback", (
    "aid:children_card", "aid:hostel_3_nights", "certificate:request", "certificate:confirm",
    "sc:i8:0:0",
))
async def test_unrelated_or_generic_certificate_buttons_do_not_record_demand(callback):
    _api, service, recorder = runtime()
    await service.process(event(callback))
    recorder.assert_not_awaited()


async def test_generic_paused_screen_with_old_aid_context_does_not_record_demand():
    api, service, recorder = runtime()
    api.conversation["custom_attributes"].update({
        "workflow_state": "scenario",
        "scenario": {"screen": "i8", "variant": 0, "aid_id": "food_card"},
    })
    assert await service.process(event("certificate:request"))
    recorder.assert_not_awaited()
    assert api.replies[0]["text"] == CERTIFICATE_PAUSED_TEXT


@pytest.mark.parametrize("fast", (False, True))
@pytest.mark.parametrize("scenario", (
    {"screen": "i8", "variant": 0, "aid_id": "food_card"},
    {"screen": "i8", "variant": 1, "aid_id": "food_card", "submitted": True},
    {"screen": "i9", "variant": 1},
    {"screen": "s32", "variant": 0, "draft_key": "old-draft"},
))
@pytest.mark.parametrize("callback", (
    "aid:medicine_card", "certificate:request", "sc:i8:0:0", "sc:i8:0:999",
))
async def test_legacy_certificate_buttons_never_replay_promises_or_submit_again(
    fast, scenario, callback,
):
    api = FastChatwoot() if fast else FakeChatwoot()
    api.fast_buttons = fast
    api, service, recorder = runtime(api)
    api.conversation["custom_attributes"].update({
        "workflow_state": "scenario", "scenario": scenario,
        "scenario_certificate": {"aid_id": "food_card", "issuance_key": "previous-send"},
    })
    service._certificate_claim = AsyncMock(side_effect=AssertionError("unexpected new claim"))
    service._certificate_resume = AsyncMock(side_effect=AssertionError("unexpected resume"))

    assert await service.process(event(callback))

    assert len(api.replies) == 1
    assert api.replies[0]["text"] == CERTIFICATE_PAUSED_TEXT
    assert api.replies[0]["attachment"] is None
    assert not api.notes
    recorder.assert_not_awaited()
    service._certificate_claim.assert_not_awaited()
    service._certificate_resume.assert_not_awaited()
    if fast:
        api.duplicate_reply_id = api.replies[0]["message_id"]
    assert not await service.process(event(callback))
    assert len(api.replies) == 1


async def test_speculative_fast_plan_does_not_write_request_and_fallback_records_once():
    api, service, recorder = runtime(FastChatwoot())
    api.fast_buttons = True
    assert await service._process_fast_button(event("aid:food_card")) is None
    recorder.assert_not_awaited()
    assert not api.commits
    assert not api.replies

    assert await service.process(event("aid:food_card"))
    recorder.assert_awaited_once()
    assert not api.commits
    assert api.replies[0]["text"] == CERTIFICATE_PAUSED_TEXT


async def test_persistence_failure_retains_source_screen_for_durable_retry():
    api, service, recorder = runtime()
    recorder.side_effect = [RuntimeError("database temporarily unavailable"), True]
    with pytest.raises(RuntimeError, match="database temporarily unavailable"):
        await service.process(event("aid:food_card"))
    assert not api.replies
    assert await service.process(event("aid:food_card"))
    assert recorder.await_count == 2
    assert recorder.await_args_list[0] == recorder.await_args_list[1]
    assert len(api.replies) == 1


async def test_navigation_reset_preserves_recorded_demand_and_does_not_create_another():
    _api, service, recorder = runtime()
    assert await service.process(event("aid:food_card"))
    assert await service.process(event("/clear", 42))
    recorder.assert_awaited_once()


async def test_reply_failure_replays_same_request_key_from_original_workflow():
    api, service, recorder = runtime()
    original_send = api.send_reply
    api.send_reply = AsyncMock(side_effect=ConnectionError("reply unavailable"))
    with pytest.raises(ConnectionError, match="reply unavailable"):
        await service.process(event("aid:food_card"))
    recorder.assert_awaited_once()
    assert api.conversation["custom_attributes"]["scenario"]["screen"] == "i8"

    recorder.return_value = False  # Durable recorder recognizes the source event.
    api.send_reply = original_send
    assert await service.process(event("aid:food_card"))
    assert recorder.await_count == 2
    assert recorder.await_args_list[0] == recorder.await_args_list[1]
    assert api.replies[0]["text"] == CERTIFICATE_PAUSED_TEXT


async def test_human_ownership_prevents_demand_recording():
    api, service, recorder = runtime()
    api.conversation["assignee_id"] = 4
    await service.process(event("aid:food_card"))
    recorder.assert_not_awaited()
