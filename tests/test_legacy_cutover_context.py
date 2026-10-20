"""A migrated conversation continues in place with its public history."""

from datetime import UTC, datetime

from app.chatwoot.service import ChatwootAgentService
from tests.test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation


async def test_imported_assistant_history_continues_without_greeting_or_human_takeover():
    api = FakeChatwoot()
    api.conversation["custom_attributes"].update(
        reply_owner="bot", ownership_version=3, ownership_last_staff_message_id=32,
        workflow_state="open_conversation", context_epoch=0, scenario={},
        legacy_telegram_cutover={"source_sha256": "a" * 64},
    )
    now = datetime.now(UTC).timestamp()
    api.messages = (
        {"id": 30, "created_at": now - 3, "message_type": 0, "private": False,
         "content": "раньше мы обсуждали планы", "sender_type": "Contact"},
        {"id": 31, "created_at": now - 2, "message_type": 1, "private": False,
         "content": "можем продолжить разговор", "sender_type": None},
        {"id": 32, "created_at": now - 1, "message_type": 1, "private": True,
         "content": "Internal migration note", "sender_type": None},
        {"id": 41, "created_at": now, "message_type": 0, "private": False,
         "content": "хочу продолжить наш разговор", "sender_type": "Contact"},
    )
    captured = []

    class CapturingGateway(StubGateway):
        async def evaluate(self, context):
            captured.append(context.history)
            return await super().evaluate(context)

    gateway = CapturingGateway(ordinary_evaluation())
    assert await ChatwootAgentService(api, gateway=gateway).process(
        event("хочу продолжить наш разговор")
    )
    assert captured == [(
        ("user", "раньше мы обсуждали планы"), ("assistant", "можем продолжить разговор"),
        ("user", "хочу продолжить наш разговор"),
    )]
    assert gateway.calls == 1 and len(api.replies) == 1
    assert api.conversation["assignee_id"] is None
    assert api.conversation["custom_attributes"]["reply_owner"] == "bot"
    assert api.conversation["custom_attributes"]["workflow_state"] == "open_conversation"
