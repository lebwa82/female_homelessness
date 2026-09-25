from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.agents import AgentEvaluation
from app.certificate_documents import CertificateObjectRef
from app.chatwoot.client import BinaryAttachment
from app.chatwoot.contracts import (
    ConversationChanged,
    IncomingChatwootMessage,
    MessageDeliveryChanged,
    StaffMessage,
)
from app.chatwoot.service import ChatwootAgentService
from app.domain import (
    DiagnosticStatus,
    RiskLevel,
    SafetyDiagnostic,
    SafetyEscalation,
    SupportDiagnostic,
    SupportIntent,
)
from app.store import CertificateClaimResult, StoredCertificate


@dataclass
class StubGateway:
    result: AgentEvaluation
    calls: int = 0

    async def evaluate(self, context: object) -> AgentEvaluation:
        self.calls += 1
        return self.result


@dataclass
class FakeChatwoot:
    conversation: dict[str, Any] = field(
        default_factory=lambda: {
            "id": 23,
            "status": "pending",
            "assignee_id": None,
            "assignee_team_id": None,
            "custom_attributes": {"reply_owner": "bot", "workflow_state": "open_conversation"},
        }
    )
    messages: tuple[dict[str, Any], ...] = field(
        default_factory=lambda: (
            {"id": 41, "message_type": "incoming", "content": "test input", "private": False},
        )
    )
    conversation_reads: int = 0
    replies: list[dict[str, Any]] = field(default_factory=list)
    attributes: list[dict[str, Any]] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    teams: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    reply_exists: bool = False
    note_keys: set[str] = field(default_factory=set)

    async def get_teams(self):
        return ({"id": 9, "name": "Дежурные", "description": "Общая очередь"},)

    async def get_team_members(self, team_id):
        return (4,)

    async def unassign_human(self, conversation_id):
        self.conversation["assignee_id"] = None
        self.conversation.get("meta", {}).pop("assignee", None)

    async def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        self.conversation_reads += 1
        return self.conversation

    async def get_messages(self, conversation_id: int) -> tuple[dict[str, Any], ...]:
        return self.messages

    async def has_reply_for_turn(self, conversation_id: int, turn_key: str) -> bool:
        return self.reply_exists or any(reply["turn_key"] == turn_key for reply in self.replies)

    async def reply_id_for_turn(self, conversation_id: int, turn_key: str) -> int | None:
        reply = next((item for item in self.replies if item["turn_key"] == turn_key), None)
        return reply["message_id"] if reply else None

    async def set_custom_attributes(self, conversation_id: int, attributes: dict[str, Any]) -> None:
        self.attributes.append(attributes)
        self.conversation["custom_attributes"] = {
            **self.conversation.get("custom_attributes", {}),
            **attributes,
        }

    async def set_status(self, conversation_id: int, status: str) -> None:
        self.statuses.append(status)
        self.conversation["status"] = status

    async def assign_team(self, conversation_id: int, team_id: int) -> None:
        self.teams.append(team_id)
        self.conversation["assignee_team_id"] = team_id

    async def add_private_note(self, conversation_id: int, content: str, *, event_key=None) -> None:
        if event_key in self.note_keys:
            return
        if event_key:
            self.note_keys.add(event_key)
        self.notes.append(content)

    async def send_reply(
        self,
        conversation_id: int,
        *,
        text: str,
        choices: tuple[object, ...],
        turn_key: str,
        sensitive_content: str | None = None,
        attachment: BinaryAttachment | None = None,
    ) -> int:
        message_id = 100 + len(self.replies)
        self.replies.append(
            {
                "message_id": message_id,
                "text": text,
                "choices": choices,
                "turn_key": turn_key,
                "sensitive_content": sensitive_content,
                "attachment": attachment,
            }
        )
        return message_id


def event(content: str = "test input", message_id: int = 41) -> IncomingChatwootMessage:
    return IncomingChatwootMessage(
        message_id=message_id,
        conversation_id=23,
        contact_id=7,
        inbox_id=3,
        content=content,
    )


def ordinary_evaluation() -> AgentEvaluation:
    return AgentEvaluation(
        safety=SafetyDiagnostic(level=RiskLevel.NONE),
        support=SupportDiagnostic(intent=SupportIntent.OPEN_CONVERSATION, draft_text="Я рядом."),
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=DiagnosticStatus.COMPLETED,
    )


@pytest.mark.asyncio
async def test_requested_duty_keeps_conversation_and_commands_available() -> None:
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway, duty_team_id=9)
    assert await service.process(event("human"))
    assert api.conversation["custom_attributes"]["handoff_requested"] is True
    assert await service.process(event("test followup", 42))
    assert gateway.calls == 1
    assert len(api.notes) == 1
    assert await service.process(event("/clear", 43))
    assert api.conversation["custom_attributes"]["context_epoch"] == 1
    assert api.conversation["custom_attributes"]["handoff_requested"] is True
    assert await service.process(event("/system_info", 44))
    assert len(api.replies) == 4


@pytest.mark.asyncio
async def test_failed_notification_can_retry_without_advancing_workflow() -> None:
    class OnceFailingApi(FakeChatwoot):
        fail: bool = True

        async def add_private_note(self, conversation_id, content, *, event_key=None):
            if self.fail:
                self.fail = False
                raise ConnectionError("temporary")
            await super().add_private_note(conversation_id, content, event_key=event_key)

    api = OnceFailingApi()
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()), duty_team_id=9)
    with pytest.raises(ConnectionError):
        await service.process(event("human"))
    assert api.conversation["custom_attributes"]["workflow_state"] == "open_conversation"
    assert api.replies == []
    assert await service.process(event("human"))
    assert len(api.notes) == 1
    assert api.conversation["custom_attributes"]["handoff_requested"] is True


@pytest.mark.asyncio
async def test_takeover_during_notification_prevents_late_reply() -> None:
    class ClaimingApi(FakeChatwoot):
        async def add_private_note(self, conversation_id, content, *, event_key=None):
            await super().add_private_note(conversation_id, content, event_key=event_key)
            self.conversation["meta"] = {"assignee": {"id": 4}, "assignee_type": "User"}

    api = ClaimingApi()
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()), duty_team_id=9)
    assert await service.process(event("human")) is False
    assert len(api.notes) == 1
    assert api.replies == []
    assert await service.process(event("/system_info", 42))
    assert api.conversation["custom_attributes"]["reply_owner"] == "human"


@pytest.mark.asyncio
async def test_bot_owned_conversation_replies_through_chatwoot_with_human_button() -> None:
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(event())

    assert handled is True
    assert gateway.calls == 1
    assert api.replies[0]["turn_key"] == "message:41"
    assert [choice.id for choice in api.replies[0]["choices"]][-1] == "human"


@pytest.mark.asyncio
async def test_certificate_is_claimed_and_delivered_directly_through_chatwoot() -> None:
    api = FakeChatwoot()
    api.conversation["custom_attributes"].update(
        workflow_state="choosing_aid", workflow_need="food_money"
    )
    claimed: list[tuple[str, str]] = []

    async def claim(aid_id: str, issuance_key: str, recipient_id: int) -> CertificateClaimResult:
        claimed.append((aid_id, issuance_key))
        assert recipient_id == 7
        certificate = StoredCertificate(
            aid_id=aid_id,
            provider="Test provider",
            nominal_rubles=300,
            activation_code="TEST-CODE",
            expires_at=datetime.now(UTC) + timedelta(days=30),
            serial_number="TEST-SERIAL",
        )
        return CertificateClaimResult("issued", certificate)

    service = ChatwootAgentService(api, certificate_claim=claim)
    preview = await service.process(event("aid:food_card"))
    assert preview is True
    assert len(claimed) == 0
    handled = await service.process(
        event("certificate:confirm", 42)
    )

    assert handled is True
    assert len(claimed) == 1
    assert claimed[0][0] == "food_card"
    assert "TEST-CODE" in api.replies[-1]["text"]
    assert api.replies[-1]["sensitive_content"] == "certificate"
    assert api.conversation["custom_attributes"]["workflow_state"] == "aid_requested"
    assert "contact=not_provided:not_provided" in api.notes[0]


@pytest.mark.asyncio
async def test_pdf_certificate_is_sent_before_followup_and_marked_submitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = FakeChatwoot()
    api.conversation["custom_attributes"].update(
        workflow_state="choosing_aid", workflow_need="food_money"
    )
    payload = b"%PDF-1.7 test"

    class Store:
        async def download(self, ref: CertificateObjectRef) -> bytes:
            assert ref.key == "test/object.pdf"
            return payload

        async def upload(self, parsed):  # pragma: no cover - protocol completeness
            raise AssertionError

        async def delete(self, ref):  # pragma: no cover - protocol completeness
            raise AssertionError

    async def claim(aid_id: str, issuance_key: str, recipient_id: int):
        return CertificateClaimResult("issued", StoredCertificate(
            aid_id=aid_id, provider="Пятёрочка", nominal_rubles=250,
            activation_code="TEST-NUMBER", serial_number="TEST-NUMBER",
            expires_at=datetime.now(UTC) + timedelta(days=30), issuance_key=issuance_key,
            pdf_bucket="private", pdf_object_key="test/object.pdf", pdf_sha256="a" * 64,
            pdf_size=len(payload), pdf_filename="certificate-pyaterochka.pdf", is_test=True,
        ))

    submitted: list[tuple[str, int]] = []
    delay = AsyncMock()
    monkeypatch.setattr("app.chatwoot.service.asyncio.sleep", delay)
    service = ChatwootAgentService(
        api,
        certificate_claim=claim,
        certificate_store=Store(),
        certificate_mark_submitted=lambda key, message_id: _append(submitted, key, message_id),
    )
    await service.process(event("aid:food_card", 41))
    api.replies.clear()
    await service.process(event("certificate:confirm", 42))

    assert len(api.replies) == 2
    assert api.replies[0]["turn_key"] == "message:42:certificate"
    assert api.replies[0]["attachment"].data == payload
    assert api.replies[1]["turn_key"] == "message:42"
    assert api.replies[1]["text"] == "Что можно сделать дальше?"
    assert api.replies[1]["attachment"] is None
    delay.assert_awaited_once_with(2)
    assert len(submitted) == 1
    assert submitted[0][0]
    assert submitted[0][1] == 100


async def _append(target: list[tuple[str, int]], key: str, message_id: int) -> None:
    target.append((key, message_id))


@pytest.mark.asyncio
async def test_certificate_delivery_webhook_updates_only_by_message_id() -> None:
    delivered: list[int] = []

    async def mark(message_id: int) -> None:
        delivered.append(message_id)

    handled = await ChatwootAgentService(
        FakeChatwoot(), certificate_mark_delivered=mark
    ).process(MessageDeliveryChanged(91, 23, "delivered"))

    assert handled is False
    assert delivered == [91]


@pytest.mark.asyncio
async def test_chatwoot_certificate_limit_survives_clear_and_allows_legal_help() -> None:
    api = FakeChatwoot()
    api.conversation["custom_attributes"].update(
        workflow_state="choosing_aid", workflow_need="food_money"
    )
    recipients: set[int] = set()

    async def claim(aid_id: str, _key: str, recipient_id: int) -> CertificateClaimResult:
        if recipient_id in recipients:
            return CertificateClaimResult("already_issued")
        recipients.add(recipient_id)
        return CertificateClaimResult("issued", StoredCertificate(
            aid_id=aid_id, provider="Test", nominal_rubles=300,
            activation_code="TEST-FIRST", expires_at=datetime.now(UTC) + timedelta(days=30),
            serial_number="TEST-SERIAL",
        ))

    service = ChatwootAgentService(api, certificate_claim=claim)
    await service.process(event("aid:food_card", 50))
    await service.process(event("certificate:confirm", 51))
    await service.process(event("/clear", 52))
    await service.process(event("continue", 53))
    await service.process(event("need:food_money", 54))
    await service.process(event("aid:medicine_card", 55))
    await service.process(event("certificate:confirm", 56))

    assert len(recipients) == 1
    assert "уже получили сертификат" in api.replies[-1]["text"]
    assert "TEST-FIRST" not in api.replies[-1]["text"]
    await service.process(event("extra:legal", 57))
    assert api.conversation["custom_attributes"]["workflow_state"] == "collecting_contact_method"


@pytest.mark.asyncio
async def test_human_owned_conversation_is_silent_before_model_call() -> None:
    api = FakeChatwoot()
    api.conversation["custom_attributes"]["reply_owner"] = "human"
    api.conversation["meta"] = {"assignee": {"id": 4}, "assignee_type": "User"}
    gateway = StubGateway(ordinary_evaluation())

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(event())

    assert handled is False
    assert gateway.calls == 0
    assert api.replies == []


@pytest.mark.asyncio
async def test_safety_handoff_notifies_team_without_stopping_bot() -> None:
    api = FakeChatwoot()
    gateway = StubGateway(
        AgentEvaluation(
            safety=SafetyDiagnostic(level=RiskLevel.CRITICAL, escalation=SafetyEscalation.HANDOFF),
            support=SupportDiagnostic(
                intent=SupportIntent.OPEN_CONVERSATION, draft_text="Я рядом."
            ),
            safety_status=DiagnosticStatus.COMPLETED,
            support_status=DiagnosticStatus.COMPLETED,
        )
    )

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(event())

    assert handled is True
    assert api.conversation["custom_attributes"]["reply_owner"] == "bot"
    assert api.conversation["custom_attributes"]["handoff_requested"] is True
    assert api.teams == [9]
    assert api.statuses == []
    assert "mention://team/9/queue" in api.notes[-1]
    assert len(api.replies) == 1


@pytest.mark.asyncio
async def test_existing_turn_key_prevents_a_second_model_call() -> None:
    api = FakeChatwoot(reply_exists=True)
    gateway = StubGateway(ordinary_evaluation())

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(event())

    assert handled is False
    assert gateway.calls == 0
    assert api.replies == []


@pytest.mark.asyncio
async def test_human_takeover_during_model_call_blocks_late_reply() -> None:
    api = FakeChatwoot()

    class TakingOverGateway(StubGateway):
        async def evaluate(self, context: object) -> AgentEvaluation:
            self.calls += 1
            api.conversation["custom_attributes"]["reply_owner"] = "human"
            api.conversation["meta"] = {"assignee": {"id": 4}, "assignee_type": "User"}
            return self.result

    gateway = TakingOverGateway(ordinary_evaluation())

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(event())

    assert handled is False
    assert api.replies == []


@pytest.mark.asyncio
async def test_clear_creates_new_epoch_without_deleting_chatwoot_history() -> None:
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(
        event("/clear")
    )

    assert handled is True
    assert api.attributes[-1]["context_epoch"] == 1
    assert "context-epoch:1" in api.notes[-1]
    assert api.replies


@pytest.mark.parametrize("state", ["pending", "open"])
async def test_team_notification_and_legacy_owner_do_not_block_conversation(state):
    api = FakeChatwoot()
    api.conversation.update(status=state, assignee_team_id=9)
    api.conversation["custom_attributes"].update(reply_owner="human", handoff_requested=True)
    gateway = StubGateway(ordinary_evaluation())
    assert await ChatwootAgentService(api, gateway=gateway).process(event())
    assert gateway.calls == 1
    assert api.conversation["custom_attributes"]["reply_owner"] == "bot"
    assert api.conversation["custom_attributes"]["handoff_requested"]


async def test_continue_button_after_notification_works_without_new_notification():
    api = FakeChatwoot()
    api.conversation["custom_attributes"].update(
        workflow_state="safety_escalation", workflow_need="housing", handoff_requested=True
    )
    gateway = StubGateway(ordinary_evaluation())
    assert await ChatwootAgentService(api, gateway=gateway).process(event("continue_bot"))
    assert gateway.calls == 0
    assert api.notes == []
    assert api.conversation["custom_attributes"]["workflow_state"] == "choosing_aid"


async def test_clear_works_for_human_preserves_assignment_request_and_history():
    api = FakeChatwoot()
    api.conversation.update(status="open", assignee_id=4, assignee_team_id=9)
    api.conversation["custom_attributes"].update(reply_owner="human", handoff_requested=True)
    original_messages = api.messages
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway)
    assert await service.process(event("/clear"))
    assert gateway.calls == 0
    assert api.conversation["assignee_id"] == 4
    assert api.conversation["status"] == "open"
    assert api.conversation["custom_attributes"]["reply_owner"] == "human"
    assert api.conversation["custom_attributes"]["handoff_requested"]
    assert api.messages == original_messages
    assert "специалиста" in api.replies[-1]["text"]
    assert not await service.process(event("test input", 42))


async def test_clear_retry_after_reset_before_reply_does_not_advance_epoch_twice():
    api = FakeChatwoot()
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    await service.process(event("/clear"))
    # Simulate lost acknowledgement: reply key unavailable, persisted reset is present.
    await service.process(event("/clear"))
    assert api.conversation["custom_attributes"]["context_epoch"] == 1
    assert len(api.notes) == 1


async def test_takeover_and_return_sync_owner_without_model_or_reply():
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway)
    api.conversation["assignee_id"] = 4
    await service.process(ConversationChanged(23))
    assert api.conversation["custom_attributes"]["reply_owner"] == "human"
    api.conversation["assignee_id"] = None
    await service.process(ConversationChanged(23))
    assert api.conversation["custom_attributes"]["reply_owner"] == "human"
    await service.process(StaffMessage(50, 23, 4, return_to_bot=True))
    assert api.conversation["custom_attributes"]["reply_owner"] == "bot"
    count = len(api.attributes)
    await service.process(ConversationChanged(23))
    assert len(api.attributes) == count
    assert gateway.calls == 0 and api.replies == []
