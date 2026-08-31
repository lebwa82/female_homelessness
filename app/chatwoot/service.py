"""Channel-neutral policy orchestration backed entirely by Chatwoot."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol

from app.agents import YandexAgentGateway
from app.chatwoot.contracts import IncomingChatwootMessage
from app.domain import ConversationState, IncomingMessage
from app.service import ConversationService
from app.store import ConversationRecord, InMemoryConversationStore

_CONTEXT_MARKER_PREFIX = "[women-help/context-epoch:"
_WORKFLOW_ATTRS = (
    "workflow_state",
    "workflow_need",
    "pending_aid_id",
    "pending_contact_method",
    "pending_city",
    "pending_district",
    "pending_offer",
    "context_epoch",
)


class ChatwootConversationApi(Protocol):
    async def get_conversation(self, conversation_id: int) -> dict[str, Any]: ...

    async def get_messages(self, conversation_id: int) -> tuple[dict[str, Any], ...]: ...

    async def has_reply_for_turn(self, conversation_id: int, turn_key: str) -> bool: ...

    async def set_custom_attributes(self, conversation_id: int, attributes: dict[str, Any]) -> None: ...

    async def set_status(self, conversation_id: int, status: str) -> None: ...

    async def assign_team(self, conversation_id: int, team_id: int) -> None: ...

    async def add_private_note(self, conversation_id: int, content: str) -> None: ...

    async def send_reply(
        self,
        conversation_id: int,
        *,
        text: str,
        choices: tuple[Any, ...],
        turn_key: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _SeededConversation:
    incoming: IncomingMessage
    store: InMemoryConversationStore
    record: ConversationRecord


class ChatwootAgentService:
    """Apply one turn while Chatwoot remains the sole durable source of truth."""

    def __init__(
        self,
        api: ChatwootConversationApi,
        *,
        gateway: YandexAgentGateway | None = None,
        duty_team_id: int | None = None,
    ) -> None:
        self._api = api
        self._gateway = gateway or YandexAgentGateway()
        self._duty_team_id = duty_team_id
        self._locks: dict[int, asyncio.Lock] = {}

    async def process(self, event: IncomingChatwootMessage) -> bool:
        """Process one trusted inbound event; ``False`` means intentionally silent."""
        lock = self._locks.setdefault(event.conversation_id, asyncio.Lock())
        async with lock:
            return await self._process_locked(event)

    async def _process_locked(self, event: IncomingChatwootMessage) -> bool:
        conversation = await self._api.get_conversation(event.conversation_id)
        if not _bot_owns(conversation):
            return False

        turn_key = f"message:{event.message_id}"
        if await self._api.has_reply_for_turn(event.conversation_id, turn_key):
            return False

        messages = await self._api.get_messages(event.conversation_id)
        seeded = _seed_conversation(event, conversation, messages)
        legacy_service = ConversationService(store=seeded.store, gateway=self._gateway)
        if event.content == "/start":
            turn = await legacy_service.start(seeded.incoming)
        elif event.content == "/clear":
            turn = await legacy_service.clear(seeded.incoming)
        elif _is_callback(event.content):
            turn = await legacy_service.handle_callback(seeded.incoming, event.content)
        else:
            turn = await legacy_service.handle_text(seeded.incoming)

        # A staff member may have claimed the conversation while Qwen was
        # evaluating. Never overwrite that ownership with a stale workflow
        # projection.
        before_side_effects = await self._api.get_conversation(event.conversation_id)
        if not _bot_owns(before_side_effects):
            return False

        handoff = _requires_human_handoff(seeded)
        if handoff:
            if not await self._handoff(event.conversation_id, seeded):
                return False
        else:
            await self._persist_workflow(event.conversation_id, seeded.record, reply_owner="bot")
            await self._persist_aid_requests(event.conversation_id, seeded)

        if event.content == "/clear":
            await self._api.add_private_note(
                event.conversation_id, _context_marker(seeded.record.context_epoch)
            )

        # For a normal turn a human intervention wins even if it happens after
        # model evaluation. A handoff is different: its one safe transition
        # message is intentionally sent immediately after ownership changes.
        if not handoff:
            current = await self._api.get_conversation(event.conversation_id)
            if not _bot_owns(current):
                return False
        if await self._api.has_reply_for_turn(event.conversation_id, turn_key):
            return False
        await self._api.send_reply(
            event.conversation_id,
            text=turn.text,
            choices=turn.choices,
            turn_key=turn_key,
        )
        return True

    async def _persist_workflow(
        self,
        conversation_id: int,
        record: ConversationRecord,
        *,
        reply_owner: str,
    ) -> None:
        await self._api.set_custom_attributes(
            conversation_id,
            {
                "reply_owner": reply_owner,
                "workflow_state": record.state,
                "workflow_need": record.need,
                "pending_aid_id": record.pending_aid_id,
                "pending_contact_method": record.pending_contact_method,
                "pending_city": record.pending_city,
                "pending_district": record.pending_district,
                "pending_offer": record.pending_offer,
                "context_epoch": record.context_epoch,
            },
        )

    async def _handoff(self, conversation_id: int, seeded: _SeededConversation) -> bool:
        """Switch ownership first; any failure suppresses the model response."""
        if self._duty_team_id is None:
            return False
        try:
            await self._persist_workflow(conversation_id, seeded.record, reply_owner="human")
            await self._api.assign_team(conversation_id, self._duty_team_id)
            await self._api.set_status(conversation_id, "open")
            await self._api.add_private_note(
                conversation_id,
                "Women-help: conversation routed to the duty team; automated replies are disabled.",
            )
        except Exception:  # noqa: BLE001 - an incomplete transfer must fail closed
            return False
        return True

    async def _persist_aid_requests(self, conversation_id: int, seeded: _SeededConversation) -> None:
        """Keep operator-needed request details in a Chatwoot-private note only."""
        for request in seeded.store.aid_requests:
            contact = request.contact_value or "not_provided"
            method = request.contact_method or "not_provided"
            location = request.city or request.district or "not_provided"
            await self._api.add_private_note(
                conversation_id,
                f"Women-help aid request: {request.aid_id}; contact={method}:{contact}; location={location}.",
            )


def _seed_conversation(
    event: IncomingChatwootMessage,
    conversation: dict[str, Any],
    messages: tuple[dict[str, Any], ...],
) -> _SeededConversation:
    attributes = _custom_attributes(conversation)
    incoming = IncomingMessage(
        channel="chatwoot",
        platform_user_id=event.contact_id,
        chat_id=event.conversation_id,
        text=event.content,
        message_id=event.message_id,
    )
    record = ConversationRecord(
        id=event.conversation_id,
        channel=incoming.channel,
        platform_user_id=incoming.platform_user_id,
        chat_id=incoming.chat_id,
        username=None,
        state=_string_attribute(attributes, "workflow_state", ConversationState.GREETING.value),
        need=_optional_string_attribute(attributes, "workflow_need"),
        pending_aid_id=_optional_string_attribute(attributes, "pending_aid_id"),
        pending_contact_method=_optional_string_attribute(attributes, "pending_contact_method"),
        pending_city=_optional_string_attribute(attributes, "pending_city"),
        pending_district=_optional_string_attribute(attributes, "pending_district"),
        pending_offer=_optional_string_attribute(attributes, "pending_offer"),
        context_epoch=_epoch(attributes.get("context_epoch")),
    )
    store = InMemoryConversationStore(conversations={event.contact_id: record})
    for role, content in _history_after_epoch(messages, record.context_epoch, event.message_id):
        store.messages.append((record.id, role, content, {"context_epoch": record.context_epoch}))
    return _SeededConversation(incoming=incoming, store=store, record=record)


def _custom_attributes(conversation: dict[str, Any]) -> dict[str, Any]:
    attributes = conversation.get("custom_attributes")
    return dict(attributes) if isinstance(attributes, dict) else {}


def _bot_owns(conversation: dict[str, Any]) -> bool:
    attributes = _custom_attributes(conversation)
    if attributes.get("reply_owner") == "human":
        return False
    return conversation.get("assignee_id") is None and conversation.get("assignee_team_id") is None


def _history_after_epoch(
    messages: tuple[dict[str, Any], ...], epoch: int, current_message_id: int
) -> tuple[tuple[str, str], ...]:
    ordered = sorted(messages, key=lambda message: message.get("created_at", 0))
    marker = _context_marker(epoch)
    start_index = 0
    for index, message in enumerate(ordered):
        if message.get("private") is True and message.get("content") == marker:
            start_index = index + 1

    history: list[tuple[str, str]] = []
    for message in ordered[start_index:]:
        if message.get("private") is True or message.get("id") == current_message_id:
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        role = "user" if message.get("message_type") in {"incoming", 0} else "assistant"
        history.append((role, content.strip()))
    return tuple(history)


def _requires_human_handoff(seeded: _SeededConversation) -> bool:
    if seeded.record.state == ConversationState.SAFETY_ESCALATION.value:
        return True
    return any(action[1] == "human_handoff" for action in seeded.store.actions)


def _is_callback(content: str) -> bool:
    return content in {
        "continue",
        "pause",
        "continue_bot",
        "human",
        "location:skip",
        "more_help",
        "finish",
        "followup:better",
        "followup:same",
        "followup:worse",
        "level2:yes",
        "level2:details",
        "level2:later",
        "support:psychologist",
    } or content.startswith(("need:", "aid:", "contact:"))


def _context_marker(epoch: int) -> str:
    return f"{_CONTEXT_MARKER_PREFIX}{epoch}]"


def _epoch(value: object) -> int:
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return 0
    return max(epoch, 0)


def _string_attribute(attributes: dict[str, Any], name: str, default: str) -> str:
    value = attributes.get(name)
    return value if isinstance(value, str) and value else default


def _optional_string_attribute(attributes: dict[str, Any], name: str) -> str | None:
    value = attributes.get(name)
    return value if isinstance(value, str) and value else None
