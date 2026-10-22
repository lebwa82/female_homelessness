"""Neutral, offline contracts for speculative button planning."""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from app.chatwoot.client import BinaryAttachment, ChatwootApiError
from app.chatwoot.fast_buttons import (
    CAPABILITY,
    PRIVATE_NOTES_FEATURE,
    BufferedButtonApi,
    UnsupportedFastButton,
)
from app.chatwoot.routing import RouteDecision
from app.chatwoot.service import ChatwootAgentService
from app.domain import Choice
from app.scenario import CERTIFICATES, CONSULTATION_IDS, render
from app.store import CertificateClaimResult
from tests.test_chatwoot_client import RecordingTransport, client
from tests.test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation


@pytest.fixture
def conversation():
    return {
        "id": 23,
        "status": "pending",
        "meta": {"assignee": None},
        "custom_attributes": {
            "workflow_state": "discovering_need",
            "scenario": {"screen": "s2", "variant": 0},
            "workflow_navigation": {"revision": 2, "entries": []},
            "scenario_requests": {"existing": {"state": "requested"}},
            "scenario_pending_input": {"message_id": 40},
        },
    }


async def test_snapshot_and_reads_do_not_alias_caller_data(conversation):
    expected = deepcopy(conversation)
    api = BufferedButtonApi(conversation)
    conversation["custom_attributes"]["scenario"]["screen"] = "changed"
    conversation["meta"]["assignee"] = {"id": 4}
    first = await api.get_conversation(23)
    assert first == expected
    first["custom_attributes"]["scenario"]["screen"] = "also-changed"
    first["custom_attributes"]["workflow_navigation"]["entries"].append({})
    assert await api.get_conversation(23) == expected
    assert api.attributes == {}
    assert api.reply is None


async def test_attribute_updates_are_top_level_merges_without_aliases(conversation):
    api = BufferedButtonApi(conversation)
    update = {"scenario": {"screen": "next-screen"}, "context_epoch": 1}
    await api.set_custom_attributes(23, update)
    update["scenario"]["screen"] = "changed"
    current = await api.get_conversation(23)
    assert current["custom_attributes"] == {
        **conversation["custom_attributes"],
        "scenario": {"screen": "next-screen"},
        "context_epoch": 1,
    }
    assert api.attributes == {"scenario": {"screen": "next-screen"}, "context_epoch": 1}
    assert conversation["custom_attributes"]["scenario"]["screen"] == "s2"


async def test_attribute_delta_omits_unchanged_and_reverted_values(conversation):
    api = BufferedButtonApi(conversation)
    await api.set_custom_attributes(23, {"workflow_state": "discovering_need"})
    assert api.attributes == {}
    await api.set_custom_attributes(23, {"workflow_state": "scenario"})
    assert api.attributes == {"workflow_state": "scenario"}
    await api.set_custom_attributes(23, {"workflow_state": "discovering_need"})
    assert api.attributes == {}


async def test_returned_attribute_delta_cannot_mutate_the_plan(conversation):
    api = BufferedButtonApi(conversation)
    await api.set_custom_attributes(23, {"scenario": {"screen": "next-screen"}})
    delta = api.attributes
    delta["scenario"]["screen"] = "changed"
    assert api.attributes == {"scenario": {"screen": "next-screen"}}


async def test_followup_and_pending_input_changes_are_buffered(conversation):
    api = BufferedButtonApi(conversation)
    update = {
        "scenario_followups": {"idle:0": {"state": "cancelled"}},
        "scenario_pending_input": None,
        "scenario_last_message_id": 41,
        "consultation_reviewed": True,
    }
    await api.set_custom_attributes(23, update)
    assert api.attributes == update
    assert (await api.get_conversation(23))["custom_attributes"] == {
        **conversation["custom_attributes"],
        **update,
    }


@pytest.mark.parametrize("field", ["bot_last_decision", "routing_team_id", "unknown_field"])
async def test_unknown_attribute_rejects_entire_update_before_changes(conversation, field):
    api = BufferedButtonApi(conversation)
    with pytest.raises(UnsupportedFastButton):
        await api.set_custom_attributes(23, {"workflow_state": "scenario", field: {}})
    assert api.attributes == {}
    assert await api.get_conversation(23) == conversation


async def test_planner_never_loads_history_or_claims_persisted_deduplication(conversation):
    api = BufferedButtonApi(conversation)
    assert await api.get_messages(23) == ()
    assert await api.get_messages(23, after_id=40) == ()
    assert not await api.has_reply_for_turn(23, "message:41")
    assert not await api.has_reply_for_turn(23, "message:41", messages=(), after_id=40)
    assert api.attributes == {}


async def test_exactly_one_reply_is_buffered_without_external_delivery(conversation):
    api = BufferedButtonApi(conversation)
    choices = (Choice(id="continue", label="Continue"),)
    assert (
        await api.send_reply(
            23,
            text="Neutral reply",
            choices=choices,
            turn_key="message:41",
            sensitive_content="neutral-marker",
        )
        is None
    )
    assert api.reply["text"] == "Neutral reply"
    assert api.reply["choices"] == choices
    assert api.reply["turn_key"] == "message:41"
    assert api.reply["sensitive_content"] == "neutral-marker"
    expected = deepcopy(api.reply)
    with pytest.raises(UnsupportedFastButton):
        await api.send_reply(23, text="Second reply", choices=(), turn_key="message:42")
    assert api.reply == expected
    assert api.attributes == {}


async def test_attachments_are_rejected_before_buffering_reply(conversation):
    api = BufferedButtonApi(conversation)
    attachment = BinaryAttachment(filename="neutral.txt", content_type="text/plain", data=b"test")
    with pytest.raises(UnsupportedFastButton):
        await api.send_reply(
            23,
            text="Neutral reply",
            choices=(),
            turn_key="message:41",
            attachment=attachment,
        )
    assert api.reply is None
    assert api.attributes == {}


@pytest.mark.parametrize(
    "method,args",
    [
        ("set_status", (23, "open")),
        ("assign_human", (23, 4)),
        ("unassign_human", (23,)),
        ("assign_team", (23, 9)),
        ("get_teams", ()),
        ("get_team_members", (9,)),
        ("wait_for_external_delivery", (23, 51)),
        ("unknown_method", ()),
    ],
)
async def test_external_capabilities_are_unavailable(conversation, method, args):
    api = BufferedButtonApi(conversation)
    with pytest.raises(UnsupportedFastButton):
        await getattr(api, method)(*args)
    assert api.attributes == {}
    assert api.reply is None


@pytest.mark.parametrize(
    "method,args,kwargs",
    [
        ("get_conversation", (99,), {}),
        ("get_messages", (99,), {}),
        ("has_reply_for_turn", (99, "message:41"), {}),
        ("set_custom_attributes", (99, {"workflow_state": "scenario"}), {}),
        ("send_reply", (99,), {"text": "Neutral", "choices": (), "turn_key": "message:41"}),
    ],
)
async def test_conversation_identity_mismatch_cannot_change_plan(
    conversation, method, args, kwargs
):
    api = BufferedButtonApi(conversation)
    with pytest.raises(UnsupportedFastButton):
        await getattr(api, method)(*args, **kwargs)
    assert api.attributes == {}
    assert api.reply is None
    assert await api.get_conversation(23) == conversation


class FastChatwoot(FakeChatwoot):
    """Record native commits separately from the established API fallback."""

    def __init__(self):
        super().__init__()
        self.fast_buttons = False
        self.snapshot_calls = []
        self.commits = []
        self.legacy_calls = []
        self.capability = CAPABILITY
        self.eligible = True
        self.duplicate_reply_id = None
        self.commit_mode = "success"
        self.features = [PRIVATE_NOTES_FEATURE]
        self.commit_notes = []

    async def button_snapshot(self, cid, message_id):
        self.snapshot_calls.append((cid, message_id))
        return {
            "capability": self.capability,
            "eligible": self.eligible,
            "reply_id": self.duplicate_reply_id,
            "conversation": deepcopy(self.conversation),
            "version": "snapshot-version",
            "features": deepcopy(self.features),
        }

    async def commit_button(self, cid, message_id, version, attributes, reply, *, notes=None):
        self.commits.append((cid, message_id, version, deepcopy(attributes), deepcopy(reply)))
        self.commit_notes.append(deepcopy(notes or []))
        if self.commit_mode == "conflict":
            return None
        if self.commit_mode == "preflight":
            raise UnsupportedFastButton("unsupported_commit_plan")
        if self.commit_mode == "unknown":
            raise ConnectionError("unknown atomic commit outcome")
        for note in notes or []:
            await FakeChatwoot.add_private_note(
                self,
                cid,
                note["content"],
                event_key=note["event_key"],
            )
        await FakeChatwoot.set_custom_attributes(self, cid, attributes)
        return await FakeChatwoot.send_reply(self, cid, **reply)

    async def get_conversation(self, cid):
        self.legacy_calls.append("conversation")
        return await super().get_conversation(cid)

    async def get_messages(self, cid, *, after_id=None):
        self.legacy_calls.append("history")
        return await super().get_messages(cid, after_id=after_id)

    async def set_custom_attributes(self, cid, attributes):
        self.legacy_calls.append("attributes")
        await super().set_custom_attributes(cid, attributes)

    async def send_reply(self, cid, **kwargs):
        self.legacy_calls.append("reply")
        return await super().send_reply(cid, **kwargs)

    async def add_private_note(self, cid, content, *, event_key=None):
        self.legacy_calls.append("note")
        await super().add_private_note(cid, content, event_key=event_key)


class NeutralRouter:
    async def choose(self, history, teams, default_id):
        return RouteDecision(default_id, {"reason": "test"})


@pytest.fixture
async def fast_runtime():
    api = FastChatwoot()
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(
        api,
        gateway=gateway,
        duty_team_id=9,
        queue_router=NeutralRouter(),
    )
    await service.process(event("/start", 41))
    await service.process(event("continue", 42))
    api.legacy_calls.clear()
    api.fast_buttons = True
    return api, service, gateway


@pytest.mark.parametrize("action", ["need:children", "need:food_money", "need:legal"])
async def test_existing_callback_plan_uses_one_commit_without_history_or_model(
    fast_runtime, action
):
    api, service, gateway = fast_runtime
    before_replies = len(api.replies)
    assert await service.process(event(action, 43))
    assert api.snapshot_calls == [(23, 43)]
    assert len(api.commits) == 1
    cid, message_id, version, attributes, reply = api.commits[0]
    assert (cid, message_id, version) == (23, 43, "snapshot-version")
    assert attributes["scenario_last_message_id"] == 43
    assert attributes["workflow_need"] == action.removeprefix("need:")
    assert api.conversation["custom_attributes"]["scenario_pending_input"] is None
    assert reply["turn_key"] == "message:43"
    assert reply["choices"]
    assert len(api.replies) == before_replies + 1
    assert not api.legacy_calls
    assert gateway.calls == 0
    assert not api.notes


@pytest.mark.parametrize(
    "capability,eligible", [(None, True), ("other-version", True), (CAPABILITY, False)]
)
async def test_unsupported_snapshot_uses_normal_fallback(fast_runtime, capability, eligible):
    api, service, gateway = fast_runtime
    api.capability = capability
    api.eligible = eligible
    before_replies = len(api.replies)
    assert await service.process(event("need:legal", 43))
    assert api.snapshot_calls == [(23, 43)]
    assert not api.commits
    assert "history" in api.legacy_calls
    assert api.legacy_calls.count("reply") == 1
    assert len(api.replies) == before_replies + 1
    assert gateway.calls == 0


async def test_duplicate_snapshot_does_not_plan_commit_or_fall_back(fast_runtime):
    api, service, gateway = fast_runtime
    api.duplicate_reply_id = 101
    before = deepcopy(api.conversation)
    before_replies = len(api.replies)
    assert not await service.process(event("need:legal", 43))
    assert not api.commits
    assert not api.legacy_calls
    assert api.conversation == before
    assert len(api.replies) == before_replies
    assert gateway.calls == 0


async def test_proven_commit_conflict_rereads_through_normal_fallback(fast_runtime):
    api, service, gateway = fast_runtime
    api.commit_mode = "conflict"
    before_replies = len(api.replies)
    assert await service.process(event("need:legal", 43))
    assert len(api.commits) == 1
    assert "conversation" in api.legacy_calls
    assert "history" in api.legacy_calls
    assert api.legacy_calls.count("reply") == 1
    assert len(api.replies) == before_replies + 1
    assert gateway.calls == 0


async def test_preflight_rejection_falls_back_before_live_effects(fast_runtime):
    api, service, gateway = fast_runtime
    api.commit_mode = "preflight"
    before_replies = len(api.replies)
    assert await service.process(event("need:legal", 43))
    assert len(api.commits) == 1
    assert api.legacy_calls.count("reply") == 1
    assert len(api.replies) == before_replies + 1
    assert gateway.calls == 0


async def test_duplicate_legacy_pending_checkpoint_is_cleaned_by_fallback(fast_runtime):
    api, service, gateway = fast_runtime
    api.eligible = False
    api.duplicate_reply_id = api.replies[-1]["message_id"]
    api.conversation["custom_attributes"]["scenario_pending_input"] = {"message_id": 42}
    before_replies = len(api.replies)
    assert not await service.process(event("continue", 42))
    assert not api.commits
    assert "history" in api.legacy_calls
    assert "reply" not in api.legacy_calls
    assert api.conversation["custom_attributes"]["scenario_pending_input"] is None
    assert len(api.replies) == before_replies
    assert gateway.calls == 0


async def test_unknown_commit_outcome_propagates_without_legacy_send(fast_runtime):
    api, service, gateway = fast_runtime
    api.commit_mode = "unknown"
    before = deepcopy(api.conversation)
    before_replies = len(api.replies)
    with pytest.raises(ConnectionError, match="unknown atomic commit outcome"):
        await service.process(event("need:legal", 43))
    assert len(api.commits) == 1
    assert not api.legacy_calls
    assert api.conversation == before
    assert len(api.replies) == before_replies
    assert gateway.calls == 0


async def test_private_notification_falls_back_without_speculative_live_effects(fast_runtime):
    api, service, gateway = fast_runtime
    before_replies = len(api.replies)
    assert await service.process(event("human", 43))
    assert api.snapshot_calls == [(23, 43)]
    assert not api.commits
    assert api.legacy_calls.count("reply") == 1
    assert api.legacy_calls.count("note") == 1
    assert len(api.notes) == 1
    assert len(api.replies) == before_replies + 1
    assert api.conversation["custom_attributes"]["handoff_requested"] is True
    assert gateway.calls == 0


async def test_certificate_claim_occurs_only_once_in_normal_fallback(fast_runtime):
    api, service, gateway = fast_runtime
    api.fast_buttons = False
    await service.process(event("need:food_money", 43))
    await service.process(event("aid:food_card", 44))
    api.legacy_calls.clear()
    api.fast_buttons = True
    claims = []

    async def claim(aid_id, issuance_key, recipient_id):
        assert "history" in api.legacy_calls  # The established path has begun.
        claims.append((aid_id, issuance_key, recipient_id))
        return CertificateClaimResult("unavailable")

    service._certificate_claim = claim
    before_replies = len(api.replies)
    assert await service.process(event("certificate:confirm", 45))
    assert api.snapshot_calls == [(23, 45)]
    assert not api.commits
    assert len(claims) == 1
    assert claims[0][0] == "food_card" and claims[0][2] == 7
    assert api.legacy_calls.count("reply") == 1
    assert len(api.replies) == before_replies + 1
    assert gateway.calls == 0


async def test_disabled_fast_buttons_does_not_request_snapshot(fast_runtime):
    api, service, gateway = fast_runtime
    api.fast_buttons = False
    assert await service.process(event("need:legal", 43))
    assert not api.snapshot_calls
    assert not api.commits
    assert api.legacy_calls.count("reply") == 1
    assert gateway.calls == 0


async def test_client_reads_compact_state_with_existing_read_identity():
    path = "/api/v1/accounts/12/conversations/23?nevidimy_turn_state=41"
    snapshot = {"capability": CAPABILITY, "eligible": False}
    transport = RecordingTransport(responses={("GET", path): snapshot})
    assert await client(transport).button_snapshot(23, 41) == snapshot
    assert transport.calls == [("GET", path, "read-token", None)]


@pytest.mark.parametrize("outcome", ["committed", "duplicate"])
async def test_client_commits_one_message_and_stable_receipt_with_bot_identity(outcome):
    path = "/api/v1/accounts/12/conversations/23/messages"
    transport = RecordingTransport(responses={("POST", path): {"outcome": outcome, "id": 51}})
    attributes = {"workflow_state": "scenario", "scenario_last_message_id": 41}
    reply = {
        "text": "Neutral reply",
        "choices": (Choice(id="continue", label="Continue"),),
        "turn_key": "message:41",
    }
    assert await client(transport).commit_button(23, 41, "version", attributes, reply) == 51
    assert len(transport.calls) == 1
    method, called_path, token, payload = transport.calls[0]
    assert (method, called_path, token) == ("POST", path, "bot-token")
    assert payload["nevidimy_atomic_turn"] == {
        "input_id": 41,
        "version": "version",
        "attributes": attributes,
    }
    assert payload["content_attributes"]["bot_turn_key"] == "message:41"
    assert payload["content_attributes"]["items"] == [{"title": "Continue", "value": "continue"}]
    assert payload["message_type"] == "outgoing" and payload["private"] is False


@pytest.mark.parametrize("status", [409, 401, 500])
async def test_only_proven_http_conflict_allows_legacy_fallback(status):
    transport = RecordingTransport()
    transport.request = AsyncMock(side_effect=ChatwootApiError("atomic_turn", status))
    api = client(transport)
    reply = {"text": "Neutral", "choices": (), "turn_key": "message:41"}
    attributes = {"scenario_last_message_id": 41}
    if status == 409:
        assert await api.commit_button(23, 41, "version", attributes, reply) is None
    else:
        with pytest.raises(ChatwootApiError) as error:
            await api.commit_button(23, 41, "version", attributes, reply)
        assert error.value.status == status
    assert transport.request.await_count == 1


@pytest.mark.parametrize(
    "result",
    [
        {"outcome": "committed"},
        {"outcome": "duplicate", "id": True},
        {"outcome": "committed", "id": "51"},
        {"id": 51},
        {"outcome": "unknown", "id": 51},
    ],
)
async def test_malformed_commit_success_is_unknown_not_a_safe_fallback(result):
    path = "/api/v1/accounts/12/conversations/23/messages"
    transport = RecordingTransport(responses={("POST", path): result})
    reply = {"text": "Neutral", "choices": (), "turn_key": "message:41"}
    with pytest.raises(ChatwootApiError):
        await client(transport).commit_button(
            23,
            41,
            "version",
            {"scenario_last_message_id": 41},
            reply,
        )
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "invalid_part",
    [
        "large_attributes",
        "large_utf8_attributes",
        "large_text",
        "large_utf8_text",
        "unknown_field",
        "pending_input",
        "missing_watermark",
        "wrong_watermark",
    ],
)
async def test_incompatible_commit_plan_is_rejected_before_transport(invalid_part):
    transport = RecordingTransport()
    attributes = {"scenario_last_message_id": 41}
    reply = {"text": "Neutral", "choices": (), "turn_key": "message:41"}
    if invalid_part == "large_attributes":
        attributes["scenario"] = {"payload": "x" * 262_145}
    elif invalid_part == "large_utf8_attributes":
        attributes["scenario"] = {"payload": "я" * 131_073}
    elif invalid_part == "large_text":
        reply["text"] = "x" * 32_769
    elif invalid_part == "large_utf8_text":
        reply["text"] = "я" * 16_385
    elif invalid_part == "unknown_field":
        attributes["routing_team_id"] = 9
    elif invalid_part == "pending_input":
        attributes["scenario_pending_input"] = {"message_id": 41}
    elif invalid_part == "missing_watermark":
        attributes.pop("scenario_last_message_id")
    else:
        attributes["scenario_last_message_id"] = 40
    with pytest.raises(UnsupportedFastButton):
        await client(transport).commit_button(23, 41, "version", attributes, reply)
    assert len(transport.calls) == 0


async def test_private_notes_and_business_receipts_are_buffered_without_aliases(conversation):
    api = BufferedButtonApi(conversation)
    key = "scenario:" + "a" * 24
    await api.add_private_note(23, "Neutral note", event_key=key)
    await api.add_private_note(23, "Repeated note", event_key=key)
    assert api.notes == [{"content": "Neutral note", "event_key": key}]
    exposed = api.notes
    exposed[0]["content"] = "Changed"
    assert api.notes[0]["content"] == "Neutral note"
    update = {
        "scenario_requests": {"request": {"state": "requested"}},
        "scenario_event_receipts": {"a" * 24: "survey_answer"},
    }
    await api.set_custom_attributes(23, update)
    assert api.attributes == update
    assert api.reply is None


@pytest.mark.parametrize("case", ["oversized", "utf8", "missing_key", "long_key", "legacy_key"])
async def test_private_note_rejection_preserves_existing_buffer(conversation, case):
    api = BufferedButtonApi(conversation)
    await api.add_private_note(23, "Neutral", event_key="scenario:" + "a" * 24)
    content, key = "Other", "scenario-alert:" + "b" * 24
    if case == "oversized":
        content = "x" * 32_769
    elif case == "utf8":
        content = "я" * 16_385
    elif case == "missing_key":
        key = None
    elif case == "long_key":
        key = "x" * 257
    else:
        key = "handoff:41"
    with pytest.raises(UnsupportedFastButton):
        await api.add_private_note(23, content, event_key=key)
    assert len(api.notes) == 1
    assert api.notes[0]["content"] == "Neutral"


@pytest.mark.parametrize("limit", ["count", "aggregate"])
async def test_private_note_count_and_aggregate_limits_are_effect_free(conversation, limit):
    api = BufferedButtonApi(conversation)
    size = 32_768 if limit == "aggregate" else 1
    count = 2 if limit == "aggregate" else 4
    for index in range(count):
        await api.add_private_note(23, "x" * size, event_key=f"scenario:{index:024x}")
    with pytest.raises(UnsupportedFastButton):
        await api.add_private_note(23, "x", event_key="scenario:" + "f" * 24)
    assert len(api.notes) == count


async def test_private_note_identity_mismatch_is_rejected(conversation):
    api = BufferedButtonApi(conversation)
    with pytest.raises(UnsupportedFastButton):
        await api.add_private_note(99, "Neutral", event_key="scenario:" + "a" * 24)
    assert api.notes == []


def scenario_callback(api, screen, variant, index, aid_id, job_kind=None):
    """Construct neutral persisted metadata and use the real callback renderer."""
    request_key = "a" * 24
    context = {
        "screen": screen,
        "variant": variant,
        "aid_id": aid_id,
        "request_key": request_key,
        "completed": variant == 7 or job_kind == "review",
    }
    attributes = api.conversation["custom_attributes"]
    attributes.update(
        workflow_state="scenario",
        workflow_need="legal",
        scenario=context,
        scenario_requests={
            request_key: {
                "state": "completed" if job_kind == "review" else "requested",
                "aid_id": aid_id,
                "specialist_id": 4,
            }
        },
    )
    if job_kind:
        key = f"{job_kind}:{request_key}"
        context["job_key"] = key
        attributes["scenario_followups"] = {
            key: {"screen": screen, "state": "sent", "context": {}},
            f"reminder:{key}": {"screen": "s59", "state": "pending", "context": {}},
        }
    revision = attributes["workflow_navigation"]["revision"]
    choices = render(screen, revision=revision, context=context, variant=variant).choices
    return next(choice.id for choice in choices if choice.id.startswith(f"sc:{screen}:{index}:"))


SCENARIO_CASES = (
    [
        (
            "s5",
            variant,
            variant * 4 + index,
            (*CERTIFICATES, *CONSULTATION_IDS, CONSULTATION_IDS[-1])[variant],
            "check",
            {"survey_answer", "survey_closed"} if index == 3 else {"survey_answer"},
        )
        for variant in range(8)
        for index in range(4)
    ]
    + [
        (
            "s7",
            variant,
            variant * 2 + index,
            CONSULTATION_IDS[variant],
            "review",
            {"survey_answer", "survey_closed"} if index == 1 else {"survey_answer"},
        )
        for variant in range(3)
        for index in range(2)
    ]
    + [
        ("s35b", 0, index, "legal_consultation", None, {"legal_topic"})
        for index in (0, 1, 2, 3, 4, 6)
    ]
    + [
        ("s34b", 0, 0, "psychologist_3_sessions", None, {"survey_closed"}),
        ("s56", 1, 3, "legal_consultation", "check", {"survey_answer", "consultation_retry"}),
        ("s72", 0, 1, "legal_consultation", "review", {"survey_answer", "negative_feedback"}),
    ]
)


@pytest.mark.parametrize("screen,variant,index,aid_id,job_kind,event_kinds", SCENARIO_CASES)
async def test_local_scenario_effects_use_one_commit_without_history_or_model(
    fast_runtime,
    screen,
    variant,
    index,
    aid_id,
    job_kind,
    event_kinds,
):
    api, service, gateway = fast_runtime
    callback = scenario_callback(api, screen, variant, index, aid_id, job_kind)
    requests = deepcopy(api.conversation["custom_attributes"]["scenario_requests"])
    before_replies = len(api.replies)
    assert await service.process(event(callback, 43))
    assert len(api.commits) == 1
    assert not api.legacy_calls
    assert gateway.calls == 0
    assert len(api.replies) == before_replies + 1
    attrs = api.conversation["custom_attributes"]
    assert attrs["scenario_requests"] == requests
    assert set(attrs["scenario_event_receipts"].values()) == event_kinds
    note_count = sum(
        2 if kind in {"negative_feedback", "consultation_retry"} else 1 for kind in event_kinds
    )
    assert len(api.commit_notes[0]) == note_count
    assert len(api.notes) == note_count
    assert len(api.note_keys) == note_count
    if job_kind:
        job = f"{job_kind}:" + "a" * 24
        assert attrs["scenario_followups"][job]["state"] == "answered"
        assert attrs["scenario_followups"][f"reminder:{job}"]["state"] == "cancelled"
    api.duplicate_reply_id = api.replies[-1]["message_id"]
    assert not await service.process(event(callback, 43))
    assert len(api.commits) == 1
    assert len(api.notes) == note_count


@pytest.mark.parametrize("features", [[], None, "private_notes_v1"])
async def test_missing_note_feature_falls_back_without_speculative_live_effects(
    fast_runtime, features
):
    api, service, gateway = fast_runtime
    api.features = features
    callback = scenario_callback(api, "s35b", 0, 0, "legal_consultation")
    assert await service.process(event(callback, 43))
    assert not api.commits
    assert "history" in api.legacy_calls
    assert api.legacy_calls.count("note") == 1
    assert api.legacy_calls.count("reply") == 1
    assert len(api.notes) == 1
    assert gateway.calls == 0


async def test_plain_reply_still_commits_without_private_note_feature(fast_runtime):
    api, service, gateway = fast_runtime
    api.features = []
    assert await service.process(event("need:legal", 43))
    assert len(api.commits) == 1
    assert api.commit_notes == [[]]
    assert not api.legacy_calls
    assert gateway.calls == 0


@pytest.mark.parametrize("notes", [None, []])
async def test_empty_notes_preserve_original_atomic_payload(notes):
    path = "/api/v1/accounts/12/conversations/23/messages"
    transport = RecordingTransport(responses={("POST", path): {"outcome": "committed", "id": 51}})
    reply = {"text": "Neutral", "choices": (), "turn_key": "message:41"}
    assert (
        await client(transport).commit_button(
            23,
            41,
            "version",
            {"scenario_last_message_id": 41},
            reply,
            notes=notes,
        )
        == 51
    )
    assert transport.calls[0][3]["nevidimy_atomic_turn"] == {
        "input_id": 41,
        "version": "version",
        "attributes": {"scenario_last_message_id": 41},
    }


async def test_client_commits_optional_private_notes_inside_atomic_envelope():
    path = "/api/v1/accounts/12/conversations/23/messages"
    transport = RecordingTransport(responses={("POST", path): {"outcome": "committed", "id": 51}})
    notes = [{"content": "Neutral", "event_key": "scenario:" + "a" * 24}]
    reply = {"text": "Neutral", "choices": (), "turn_key": "message:41"}
    assert (
        await client(transport).commit_button(
            23,
            41,
            "version",
            {"scenario_last_message_id": 41},
            reply,
            notes=notes,
        )
        == 51
    )
    assert transport.calls[0][3]["nevidimy_atomic_turn"]["notes"] == notes
    assert len(transport.calls) == 1


@pytest.mark.parametrize("case", ["count", "single", "utf8", "aggregate", "key", "extra_key"])
async def test_incompatible_private_notes_are_rejected_before_transport(case):
    transport = RecordingTransport()
    notes = [{"content": "Neutral", "event_key": "scenario:" + "a" * 24}]
    if case == "count":
        notes *= 5
    elif case == "single":
        notes[0]["content"] = "x" * 32_769
    elif case == "utf8":
        notes[0]["content"] = "я" * 16_385
    elif case == "aggregate":
        notes = [
            {"content": "x" * size, "event_key": f"scenario:{index:024x}"}
            for index, size in enumerate((32_768, 32_768, 1))
        ]
    elif case == "key":
        notes[0]["event_key"] = "handoff:41"
    else:
        notes[0]["private"] = False
    reply = {"text": "Neutral", "choices": (), "turn_key": "message:41"}
    with pytest.raises(UnsupportedFastButton):
        await client(transport).commit_button(
            23,
            41,
            "version",
            {"scenario_last_message_id": 41},
            reply,
            notes=notes,
        )
    assert len(transport.calls) == 0
