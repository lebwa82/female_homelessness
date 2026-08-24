"""Round-4 regressions for systemic parser and workflow semantics.

The inputs are synthetic and every dependency remains local.  Provider calls
use the existing deterministic gateway double; no external service is touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agents import AgentCallResult, YandexAgentGateway
from app.domain import (
    ConversationState,
    IncomingMessage,
)
from app.service import ConversationService
from app.store import InMemoryConversationStore
from scripts import dialogue_eval

DATASET = Path("tests/fixtures/dialogue_scenarios.jsonl")


def _incoming(text: str, message_id: int) -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=901,
        chat_id=902,
        text=text,
        message_id=message_id,
    )


def _gateway() -> YandexAgentGateway:
    async def call(name: str, _: str, __: str) -> AgentCallResult:
        if name == "risk":
            return AgentCallResult(
                payload={"level": "none", "rationale": "synthetic"},
                audit={"status": "completed"},
            )
        return AgentCallResult(
            payload={"intent": "open_conversation", "draft_text": "synthetic"},
            audit={"status": "completed"},
        )

    return YandexAgentGateway(call=call)


def test_dataset_v4_is_the_only_supported_model_routing_schema(tmp_path: Path) -> None:
    row = json.loads(DATASET.read_text(encoding="utf-8").splitlines()[0])
    fixture = tmp_path / "case.jsonl"
    row["version"] = 4
    fixture.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")

    assert dialogue_eval.load_cases(fixture)[0].version == 4

    row["version"] = 3
    fixture.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    with pytest.raises(dialogue_eval.DatasetError, match="unsupported version"):
        dialogue_eval.load_cases(fixture)


@pytest.mark.asyncio
async def test_followup_same_renders_only_callbacks_valid_for_the_resulting_state() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(_incoming("", 910))
    await store.update(record, state=ConversationState.FOLLOWUP_SENT.value)

    turn = await service.handle_callback(_incoming("followup:same", 911), "followup:same")

    assert record.state == ConversationState.AID_REQUESTED.value
    assert {choice.id for choice in turn.choices} >= {"more_help", "finish"}
    next_turn = await service.handle_callback(_incoming("more_help", 912), "more_help")
    assert record.state == ConversationState.DISCOVERING_NEED.value
    assert any(choice.id.startswith("need:") for choice in next_turn.choices)


@pytest.mark.asyncio
async def test_level_two_later_uses_common_cleanup_and_cancels_reminders() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(_incoming("", 920))
    await store.update(
        record,
        state=ConversationState.FOLLOWUP_ANSWERED.value,
        need="legal",
        pending_aid_id="legal_consultation",
        pending_contact_method="phone",
        pending_city="synthetic-city",
    )
    request = await store.create_aid_request(
        record,
        "legal_consultation",
        "later",
        None,
        request_key="synthetic-level-two",
    )
    assert request.id

    await service.handle_callback(_incoming("level2:later", 921), "level2:later")

    assert record.state == ConversationState.OPEN_CONVERSATION.value
    assert record.need is None
    assert record.pending_aid_id is None
    assert record.pending_contact_method is None
    assert record.pending_city is None
    assert store.followup_jobs == []
