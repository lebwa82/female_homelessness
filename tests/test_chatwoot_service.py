from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.agents import AgentEvaluation
from app.chatwoot.contracts import IncomingChatwootMessage
from app.chatwoot.service import ChatwootAgentService
from app.domain import (
    DiagnosticStatus,
    RiskLevel,
    SafetyDiagnostic,
    SafetyEscalation,
    SupportDiagnostic,
    SupportIntent,
)


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

    async def get_conversation(self, conversation_id: int) -> dict[str, Any]:
        self.conversation_reads += 1
        return self.conversation

    async def get_messages(self, conversation_id: int) -> tuple[dict[str, Any], ...]:
        return self.messages

    async def has_reply_for_turn(self, conversation_id: int, turn_key: str) -> bool:
        return self.reply_exists

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

    async def add_private_note(self, conversation_id: int, content: str) -> None:
        self.notes.append(content)

    async def send_reply(
        self,
        conversation_id: int,
        *,
        text: str,
        choices: tuple[object, ...],
        turn_key: str,
    ) -> None:
        self.replies.append({"text": text, "choices": choices, "turn_key": turn_key})


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
async def test_bot_owned_conversation_replies_through_chatwoot_with_human_button() -> None:
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(event())

    assert handled is True
    assert gateway.calls == 1
    assert api.replies[0]["turn_key"] == "message:41"
    assert [choice.id for choice in api.replies[0]["choices"]][-1] == "human"


@pytest.mark.asyncio
async def test_human_owned_conversation_is_silent_before_model_call() -> None:
    api = FakeChatwoot()
    api.conversation["custom_attributes"]["reply_owner"] = "human"
    gateway = StubGateway(ordinary_evaluation())

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(event())

    assert handled is False
    assert gateway.calls == 0
    assert api.replies == []


@pytest.mark.asyncio
async def test_safety_handoff_transfers_to_team_before_visible_reply() -> None:
    api = FakeChatwoot()
    gateway = StubGateway(
        AgentEvaluation(
            safety=SafetyDiagnostic(level=RiskLevel.CRITICAL, escalation=SafetyEscalation.HANDOFF),
            support=SupportDiagnostic(intent=SupportIntent.OPEN_CONVERSATION, draft_text="Я рядом."),
            safety_status=DiagnosticStatus.COMPLETED,
            support_status=DiagnosticStatus.COMPLETED,
        )
    )

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(event())

    assert handled is True
    assert api.attributes[-1]["reply_owner"] == "human"
    assert api.teams == [9]
    assert api.statuses == ["open"]
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
            return self.result

    gateway = TakingOverGateway(ordinary_evaluation())

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(event())

    assert handled is False
    assert api.replies == []


@pytest.mark.asyncio
async def test_clear_creates_new_epoch_without_deleting_chatwoot_history() -> None:
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())

    handled = await ChatwootAgentService(api, gateway=gateway, duty_team_id=9).process(event("/clear"))

    assert handled is True
    assert api.attributes[-1]["context_epoch"] == 1
    assert "context-epoch:1" in api.notes[-1]
    assert api.replies
