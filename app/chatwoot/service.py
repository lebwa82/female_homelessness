"""Channel-neutral policy orchestration backed entirely by Chatwoot."""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.agents import YandexAgentGateway
from app.certificate_documents import CertificateObjectRef, CertificateObjectStore
from app.chatwoot.client import BinaryAttachment
from app.chatwoot.contracts import (
    ConversationChanged,
    IncomingChatwootMessage,
    MessageDeliveryChanged,
    StaffMessage,
    parse_staff_message,
)
from app.chatwoot.queues import QueueCoordinator
from app.chatwoot.routing import QueueRouter
from app.chatwoot.scenario_effects import ScenarioEffects, returning_after_day
from app.config import settings
from app.domain import AgentTurn, ConversationState, IncomingMessage
from app.release_info import active_release_info
from app.service import ConversationService
from app.store import CertificateClaimResult, ConversationRecord, InMemoryConversationStore
from app.ui import HUMAN_CHOICE

_CONTEXT_MARKER_PREFIX = "[women-help/context-epoch:"
logger = logging.getLogger(__name__)
_WORKFLOW_ATTRS = (
    "workflow_state",
    "workflow_need",
    "pending_aid_id",
    "pending_contact_method",
    "pending_city",
    "pending_district",
    "pending_offer",
    "context_epoch",
    "workflow_navigation",
    "scenario",
)


class ChatwootConversationApi(Protocol):
    async def get_conversation(self, conversation_id: int) -> dict[str, Any]: ...

    async def get_messages(self, conversation_id: int) -> tuple[dict[str, Any], ...]: ...

    async def has_reply_for_turn(self, conversation_id: int, turn_key: str) -> bool: ...

    async def reply_id_for_turn(self, conversation_id: int, turn_key: str) -> int | None: ...

    async def wait_for_external_delivery(
        self, conversation_id: int, message_id: int
    ) -> None: ...

    async def set_custom_attributes(
        self, conversation_id: int, attributes: dict[str, Any]
    ) -> None: ...

    async def set_status(self, conversation_id: int, status: str) -> None: ...

    async def assign_team(self, conversation_id: int, team_id: int) -> None: ...

    async def unassign_human(self, conversation_id: int) -> None: ...

    async def assign_human(self, conversation_id: int, user_id: int) -> None: ...

    async def get_teams(self) -> tuple[dict[str, Any], ...]: ...

    async def get_team_members(self, team_id: int) -> tuple[int, ...]: ...

    async def add_private_note(
        self, conversation_id: int, content: str, *, event_key: str | None = None
    ) -> None: ...

    async def send_reply(
        self,
        conversation_id: int,
        *,
        text: str,
        choices: tuple[Any, ...],
        turn_key: str,
        sensitive_content: str | None = None,
        attachment: BinaryAttachment | None = None,
    ) -> int | None: ...


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
        queue_router: QueueRouter | None = None,
        certificate_claim: Callable[[str, str, int], Awaitable[CertificateClaimResult]] | None = None,
        certificate_store: CertificateObjectStore | None = None,
        certificate_mark_submitted: Callable[[str, int], Awaitable[None]] | None = None,
        certificate_mark_failed: Callable[[str], Awaitable[None]] | None = None,
        certificate_mark_delivered: Callable[[int], Awaitable[None]] | None = None,
        certificate_mark_delivery_failed: Callable[[int], Awaitable[None]] | None = None,
    ) -> None:
        self._api = api
        self._gateway = gateway or YandexAgentGateway()
        self._duty_team_id = duty_team_id
        self._queues = QueueCoordinator(api, duty_team_id, queue_router)
        self._scenario_effects = ScenarioEffects(api, self._queues, can_route=_bot_owns)
        self._locks: dict[int, asyncio.Lock] = {}
        self._certificate_claim = certificate_claim
        self._certificate_store = certificate_store
        self._certificate_mark_submitted = certificate_mark_submitted
        self._certificate_mark_failed = certificate_mark_failed
        self._certificate_mark_delivered = certificate_mark_delivered
        self._certificate_mark_delivery_failed = certificate_mark_delivery_failed

    async def process(
        self,
        event: IncomingChatwootMessage | ConversationChanged | StaffMessage | MessageDeliveryChanged,
    ) -> bool:
        """Process one trusted inbound event; ``False`` means intentionally silent."""
        lock = self._locks.setdefault(event.conversation_id, asyncio.Lock())
        async with lock:
            if isinstance(event, MessageDeliveryChanged):
                if event.status in {"delivered", "read"}:
                    if self._certificate_mark_delivered is not None:
                        await self._certificate_mark_delivered(event.message_id)
                elif event.status == "failed" and self._certificate_mark_delivery_failed is not None:
                    await self._certificate_mark_delivery_failed(event.message_id)
                return False
            if isinstance(event, StaffMessage):
                await self._staff_message(event)
                return False
            if isinstance(event, ConversationChanged):
                conversation = await self._sync_owner(event.conversation_id)
                await self._queues.sync(conversation)
                return False
            return await self._process_locked(event)

    async def _process_locked(self, event: IncomingChatwootMessage) -> bool:
        conversation = await self._sync_owner(event.conversation_id)
        attributes = _custom_attributes(conversation)
        if event.message_id < max(_epoch(attributes.get("scenario_last_message_id")),
                                  _epoch(attributes.get("last_clear_message_id")),
                                  _epoch(attributes.get("ownership_last_staff_message_id"))):
            return False
        conversation = await self._scenario_effects.cancel_idle(conversation, before_message=event.message_id)
        # A new client message wakes a closed/snoozed conversation. Timers do not.
        if conversation.get("status") in {"resolved", "snoozed"}:
            await self._api.set_status(
                event.conversation_id, "open" if _human_assigned(conversation) else "pending",
            )
            conversation = await self._sync_owner(event.conversation_id)
        if event.content == "/clear":
            return await self._clear_context(event, conversation)
        if event.content == "/system_info":
            release = active_release_info()
            return await self._control_reply(event, (
                f"🛠 Служебная информация\nENV: {settings.app_env}\n"
                f"Сборка: {release.revision or 'неизвестно'}\n"
                f"Релиз: {release.released_at}\n"
                f"LLM: {'включена' if settings.llm_enabled else 'выключена'}\n"
                "Канал: Chatwoot → Telegram\n"
                f"Отвечает: {'специалист' if _human_assigned(conversation) else 'бот'}\n"
                f"Статус: {conversation.get('status', 'неизвестно')}"
            ))
        if not _bot_owns(conversation):
            if event.content == "/start" or _is_callback(event.content):
                return await self._control_reply(
                    event, "Сейчас разговор ведёт специалист. Можно написать ему здесь. "
                    "Кнопки бота снова станут доступны после завершения разговора со специалистом.",
                )
            return False

        await self._queues.sync(conversation)

        turn_key = f"message:{event.message_id}"
        if await self._api.has_reply_for_turn(event.conversation_id, turn_key):
            await self._scenario_effects.menu_delivered(event.conversation_id, event.message_id)
            pending = _custom_attributes(conversation).get("scenario_pending_input") or {}
            if pending.get("message_id") == event.message_id:
                await self._api.set_custom_attributes(event.conversation_id, {
                    "scenario_pending_input": None, "scenario_last_message_id": event.message_id,
                })
            return False

        await self._scenario_effects.answer_current(conversation)
        # Retrying a partially applied callback must start at its original screen,
        # while durable requests remain outside the navigable workflow snapshot.
        attributes = _custom_attributes(conversation)
        pending = attributes.get("scenario_pending_input") or {}
        if pending.get("message_id") == event.message_id:
            conversation = {**conversation, "custom_attributes": {
                **attributes, **pending["workflow"],
            }}
        else:
            await self._api.set_custom_attributes(event.conversation_id, {
                "scenario_pending_input": {
                    "message_id": event.message_id,
                    "workflow": {k: copy.deepcopy(attributes.get(k)) for k in _WORKFLOW_ATTRS},
                },
            })
        messages = await self._api.get_messages(event.conversation_id)
        seeded = _seed_conversation(event, conversation, messages, self._certificate_claim)
        legacy_service = ConversationService(store=seeded.store, gateway=self._gateway,
                                             html_scenario=True)
        if event.content == "/start":
            turn = await legacy_service.start(seeded.incoming)
            if returning_after_day(messages, event.message_id, self._scenario_effects.clock()):
                turn = await legacy_service.scenario_flow.show(seeded.record, "i9", variant=0)
                turn = legacy_service._with_back_choice(seeded.record, turn)
        elif _is_callback(event.content):
            turn = await legacy_service.handle_callback(seeded.incoming, event.content)
        else:
            turn = await legacy_service.handle_text(seeded.incoming)
            if (seeded.record.state == ConversationState.OPEN_CONVERSATION.value
                    and not turn.audit.get("critical_delivery")
                    and returning_after_day(messages, event.message_id, self._scenario_effects.clock())):
                welcome = await legacy_service.scenario_flow.show(seeded.record, "i9", variant=0)
                choices = {c.id: c for c in (*welcome.choices, *turn.choices)
                           if not c.id.startswith("back:")}
                turn = turn.model_copy(update={"text": welcome.text + "\n\n" + turn.text,
                                                "choices": tuple(choices.values())})
                turn = legacy_service._with_back_choice(seeded.record, turn)

        # A staff member may have claimed the conversation while Qwen was
        # evaluating. Never overwrite that ownership with a stale workflow
        # projection.
        before_side_effects = await self._sync_owner(event.conversation_id)
        if not _bot_owns(before_side_effects):
            return False
        # A public staff reply can arrive while the LLM holds our local lock,
        # before that reply's webhook has projected the visible assignment.
        staff = _latest_staff_message(
            await self._api.get_messages(event.conversation_id), event.conversation_id,
            after=max(event.message_id, _epoch(_custom_attributes(before_side_effects).get(
                "ownership_last_staff_message_id"))),
        )
        if staff is not None:
            await self._staff_message(staff)
            return False

        if seeded.store.agent_runs:
            fields = ("status", "reason", "error_type", "error_origin", "latency_ms")
            diagnostics = [
                {"agent": name, **{key: audit.get(key) for key in fields}}
                for _, name, audit in seeded.store.agent_runs
            ]
            await self._api.set_custom_attributes(
                event.conversation_id, {"bot_last_diagnostics": diagnostics}
            )
            for item in diagnostics:
                if item["status"] != "completed":
                    logger.warning("chatwoot diagnostic unavailable: %s", item)

        decisions = [payload for _, kind, _, payload in seeded.store.actions if kind == "policy_decision"]
        if decisions:
            # Only enum labels and routing metadata, never raw prompts/reasoning.
            await self._api.set_custom_attributes(event.conversation_id, {
                "bot_last_decision": {"message_id": event.message_id, **decisions[-1]},
            })

        handoff = _requires_human_handoff(seeded)
        await self._scenario_effects.apply(event.conversation_id, event.message_id, seeded.store.actions)
        if handoff:
            if not await self._notify_duty(event, seeded):
                return False
        else:
            await self._persist_workflow(event.conversation_id, seeded.record)
            await self._persist_aid_requests(event.conversation_id, seeded)

        # A notification is not a takeover. A real human assignment wins even
        # while we are notifying the duty team; do not publish a late model reply.
        current = await self._sync_owner(event.conversation_id)
        if not _bot_owns(current):
            return False
        if await self._api.has_reply_for_turn(event.conversation_id, turn_key):
            return False
        if turn.attachment is not None:
            if not await self._send_certificate(event.conversation_id, turn_key, turn):
                return False
        else:
            await self._api.send_reply(
                event.conversation_id,
                text=turn.text,
                choices=turn.choices,
                turn_key=turn_key,
                sensitive_content=turn.audit.get("sensitive_content"),
            )
        await self._scenario_effects.menu_delivered(event.conversation_id, event.message_id)
        await self._api.set_custom_attributes(event.conversation_id, {
            "scenario_pending_input": None, "scenario_last_message_id": event.message_id,
        })
        return True

    async def _send_certificate(
        self, conversation_id: int, turn_key: str, turn: AgentTurn
    ) -> bool:
        attachment = turn.attachment
        if attachment is None or self._certificate_store is None:
            raise RuntimeError("certificate attachment store is not configured")
        attachment_turn_key = f"{turn_key}:certificate"
        message_id = await self._api.reply_id_for_turn(conversation_id, attachment_turn_key)
        try:
            if message_id is None:
                payload = await self._certificate_store.download(CertificateObjectRef(
                    bucket=attachment.bucket,
                    key=attachment.key,
                    version_id=attachment.version_id,
                    sha256=attachment.sha256,
                    size=attachment.size,
                ))
                if not _bot_owns(await self._sync_owner(conversation_id)):
                    return False
                message_id = await self._api.send_reply(
                    conversation_id,
                    text=turn.text,
                    choices=(),
                    turn_key=attachment_turn_key,
                    sensitive_content="certificate",
                    attachment=BinaryAttachment(
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                        data=payload,
                    ),
                )
                if message_id is None:
                    message_id = await self._api.reply_id_for_turn(
                        conversation_id, attachment_turn_key
                    )
            if message_id is None:
                raise RuntimeError("chatwoot did not return a certificate message id")
            if self._certificate_mark_submitted is not None:
                await self._certificate_mark_submitted(attachment.issuance_key, message_id)
        except Exception:
            if self._certificate_mark_failed is not None:
                await self._certificate_mark_failed(attachment.issuance_key)
            raise
        # Chatwoot stores Telegram's external message ID only after sendDocument
        # returns, so this is an exact delivery-order barrier rather than a delay.
        await self._api.wait_for_external_delivery(conversation_id, message_id)
        if turn.audit.get("certificate_followup"):
            await self._scenario_effects.certificate_delivered(
                conversation_id, attachment.issuance_key, turn.audit["certificate_followup"],
            )
        if not _bot_owns(await self._sync_owner(conversation_id)):
            return False
        await self._api.send_reply(
            conversation_id,
            text=turn.audit.get("after_certificate_text", "Что можно сделать дальше?"),
            choices=turn.choices,
            turn_key=turn_key,
        )
        return True

    async def _persist_workflow(
        self,
        conversation_id: int,
        record: ConversationRecord,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        await self._api.set_custom_attributes(
            conversation_id,
            {
                "workflow_state": record.state,
                "workflow_need": record.need,
                "pending_aid_id": record.pending_aid_id,
                "pending_contact_method": record.pending_contact_method,
                "pending_city": record.pending_city,
                "pending_district": record.pending_district,
                "pending_offer": record.pending_offer,
                "context_epoch": record.context_epoch,
                "workflow_navigation": record.navigation,
                "scenario": record.scenario,
                **(extra or {}),
            },
        )

    async def _sync_owner(self, conversation_id: int) -> dict[str, Any]:
        conversation = await self._api.get_conversation(conversation_id)
        attrs = _custom_attributes(conversation)
        # Native assignment is authoritative, not a second, hidden state machine.
        # Closing a conversation releases the old specialist, without declaring
        # any consultation completed or changing durable requests.
        if conversation.get("status") == "resolved" and _human_assigned(conversation):
            await self._api.unassign_human(conversation_id)
            conversation = await self._api.get_conversation(conversation_id)
        owner = "human" if _human_assigned(conversation) else "bot"
        if owner == "human" or conversation.get("status") in {"resolved", "snoozed"}:
            conversation = await self._scenario_effects.cancel_idle(conversation)
            attrs = _custom_attributes(conversation)
        if attrs.get("reply_owner") != owner or attrs.get("ownership_version") != 3:
            update: dict[str, Any] = {"reply_owner": owner, "ownership_version": 3}
            if attrs.get("reply_owner") != owner:
                update["scenario_pending_input"] = None
            if attrs.get("reply_owner") == "human" and owner == "bot":
                # Do not let a delayed old staff webhook undo an explicit UI
                # unassignment (including migration of a v2 orphaned latch).
                staff = _latest_staff_message(await self._api.get_messages(conversation_id),
                                              conversation_id)
                if staff is not None:
                    update["ownership_last_staff_message_id"] = max(
                        staff.message_id, _epoch(attrs.get("ownership_last_staff_message_id")),
                    )
            await self._api.set_custom_attributes(conversation_id, update)
            conversation = {**conversation, "custom_attributes": {**attrs, **update}}
        return conversation

    async def _staff_message(self, event: StaffMessage) -> None:
        if event.consultation_schedule:
            await self._scenario_effects.plan_consultation(event)
            return  # Scheduling alone never changes ownership or the current menu.
        conversation = await self._api.get_conversation(event.conversation_id)
        attrs = _custom_attributes(conversation)
        if event.message_id <= max(_epoch(attrs.get("ownership_last_staff_message_id")),
                                  _epoch(attrs.get("scenario_last_message_id"))):
            return
        if event.consultation_completed and not await self._scenario_effects.complete(event):
            return
        release = event.return_to_bot or event.consultation_completed
        if not release:
            await self._scenario_effects.cancel_idle(conversation)
            # A public answer is a takeover, but it must be visible in the UI.
            # Never steal another specialist's existing assignment.
            if not _human_assigned(conversation):
                await self._api.assign_human(event.conversation_id, event.sender_id)
            if conversation.get("status") != "open":
                await self._api.set_status(event.conversation_id, "open")
            requests = copy.deepcopy(attrs.get("scenario_requests") or {})
            active = [r for r in requests.values() if r["state"] == "requested"]
            if len(active) == 1:
                active[0]["specialist_id"] = event.sender_id
                await self._api.set_custom_attributes(event.conversation_id,
                                                       {"scenario_requests": requests})
        if release:
            # The public Telegram input parser can never create this event.
            # Keep history, queue, requests and workflow; change only ownership.
            await self._api.unassign_human(event.conversation_id)
            await self._api.set_status(
                event.conversation_id, "resolved" if event.consultation_completed else "pending",
            )
            await self._api.set_custom_attributes(event.conversation_id, {"scenario_pending_input": None})
        await self._api.set_custom_attributes(
            event.conversation_id,
            {
                "reply_owner": "bot" if release else "human",
                "ownership_version": 3,
                "scenario_pending_input": None,
                "ownership_last_staff_message_id": event.message_id,
            },
        )
        if release:
            await self._api.add_private_note(
                event.conversation_id,
                "Консультация завершена. Новые сообщения обработает бот."
                if event.consultation_completed else "Бот снова включён явным действием сотрудницы.",
                event_key=f"return-to-bot:{event.message_id}",
            )

    async def _control_reply(self, event: IncomingChatwootMessage, text: str) -> bool:
        """Service commands explain ownership without advancing forms or surveys."""
        turn_key = f"message:{event.message_id}"
        if await self._api.has_reply_for_turn(event.conversation_id, turn_key):
            return False
        await self._api.send_reply(event.conversation_id, text=text,
                                   choices=(HUMAN_CHOICE,), turn_key=turn_key)
        return True

    async def _clear_context(
        self, event: IncomingChatwootMessage, conversation: dict[str, Any]
    ) -> bool:
        turn_key = f"message:{event.message_id}"
        if await self._api.has_reply_for_turn(event.conversation_id, turn_key):
            return False
        attrs = _custom_attributes(conversation)
        messages = await self._api.get_messages(event.conversation_id)
        seeded = _seed_conversation(event, conversation, messages)
        # A retry after writing the reset but before sending its acknowledgement
        # must not increment the epoch a second time.
        if attrs.get("last_clear_message_id") != event.message_id:
            await ConversationService(store=seeded.store, gateway=self._gateway,
                                      html_scenario=True).clear(
                seeded.incoming
            )
            await self._persist_workflow(
                event.conversation_id,
                seeded.record,
                extra={"last_clear_message_id": event.message_id,
                       "scenario_pending_input": None, "scenario_last_message_id": event.message_id},
            )
        await self._api.add_private_note(
            event.conversation_id,
            _context_marker(seeded.record.context_epoch),
            event_key=f"clear:{event.message_id}",
        )
        current = await self._sync_owner(event.conversation_id)
        text = "Контекст бота очищен. Переписка и запрос дежурному, если он был, сохранены."
        if not _bot_owns(current):
            text += " Разговор остаётся у специалиста."
        else:
            text += " Можно продолжить здесь."
        await self._api.send_reply(
            event.conversation_id, text=text, choices=(HUMAN_CHOICE,), turn_key=turn_key
        )
        return True

    async def _notify_duty(
        self, event: IncomingChatwootMessage, seeded: _SeededConversation
    ) -> bool:
        """Native team mention creates Chatwoot bell notifications, not a takeover."""
        if self._duty_team_id is None:
            raise RuntimeError("default_queue_missing")
        conversation = await self._sync_owner(event.conversation_id)
        if not _bot_owns(conversation):
            return False
        messages = await self._api.get_messages(event.conversation_id)
        history = _history_after_epoch(messages, seeded.record.context_epoch, event.message_id)
        selected = await self._queues.route(
            conversation,
            (*history, ("user", event.content)),
            event.message_id,
            urgent=any(a[1] == "safety_escalation" for a in seeded.store.actions),
            can_route=_bot_owns,
        )
        if selected is None:
            await self._sync_owner(event.conversation_id)
            return False
        await self._persist_workflow(
            event.conversation_id,
            seeded.record,
            extra={
                "handoff_requested": True,
                "handoff_last_message_id": event.message_id,
            },
        )
        return True

    async def _persist_aid_requests(
        self, conversation_id: int, seeded: _SeededConversation
    ) -> None:
        """Keep operator-needed request details in a Chatwoot-private note only."""
        for request in seeded.store.aid_requests:
            contact = request.contact_value or "not_provided"
            method = request.contact_method or "not_provided"
            location = request.city or request.district or "not_provided"
            await self._api.add_private_note(
                conversation_id,
                f"Women-help aid request: {request.aid_id}; contact={method}:{contact}; location={location}.",
                event_key=f"aid-request:{request.request_key}" if request.request_key else None,
            )

    async def send_due_followup(self, conversation_id: int) -> bool:
        """Called by the single-process worker; shares the inbound conversation lock."""
        async with self._locks.setdefault(conversation_id, asyncio.Lock()):
            conversation = await self._sync_owner(conversation_id)
            attributes = _custom_attributes(conversation)
            if (attributes.get("reply_owner") != "bot" or _human_assigned(conversation)
                    or conversation.get("status") == "snoozed"
                    or attributes.get("scenario_pending_input")
                    or attributes.get("workflow_state") == "safety_escalation"
                    or (attributes.get("scenario") or {}).get("awaiting_text")):
                return False
            due = await self._scenario_effects.due(conversation)
            if due is None:
                return False
            key, job = due
            turn_key = f"scenario-followup:{key}"
            if await self._api.has_reply_for_turn(conversation_id, turn_key):
                await self._scenario_effects.sent(conversation_id, key)
                return False
            if conversation.get("status") == "resolved":
                await self._api.set_status(conversation_id, "pending")
            # Timers do not invoke LLMs and never copy customer history.
            incoming = IncomingChatwootMessage(0, conversation_id, 0, 0, "")
            seeded = _seed_conversation(incoming, conversation, ())
            service = ConversationService(store=seeded.store, gateway=self._gateway,
                                          html_scenario=True)
            turn = await service.scenario_flow.show(
                seeded.record, job["screen"],
                **self._scenario_effects.context(conversation, key, job),
            )
            turn = service._with_back_choice(seeded.record, turn)
            await self._persist_workflow(conversation_id, seeded.record)
            if not _bot_owns(await self._sync_owner(conversation_id)):
                return False
            await self._api.send_reply(conversation_id, text=turn.text, choices=turn.choices,
                                       turn_key=turn_key)
            await self._scenario_effects.sent(conversation_id, key)
            return True


def _seed_conversation(
    event: IncomingChatwootMessage,
    conversation: dict[str, Any],
    messages: tuple[dict[str, Any], ...],
    certificate_claim: Callable[[str, str, int], Awaitable[CertificateClaimResult]] | None = None,
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
        navigation=attributes.get("workflow_navigation") or {},
        scenario=attributes.get("scenario") or {},
    )
    store = InMemoryConversationStore(
        conversations={event.contact_id: record}, certificate_claim=certificate_claim
    )
    for role, content in _history_after_epoch(messages, record.context_epoch, event.message_id):
        store.messages.append((record.id, role, content, {"context_epoch": record.context_epoch}))
    return _SeededConversation(incoming=incoming, store=store, record=record)


def _custom_attributes(conversation: dict[str, Any]) -> dict[str, Any]:
    attributes = conversation.get("custom_attributes")
    return dict(attributes) if isinstance(attributes, dict) else {}


def _human_assigned(conversation: dict[str, Any]) -> bool:
    meta = conversation.get("meta") or {}
    kind = meta.get("assignee_type") or conversation.get("assignee_type")
    return kind != "AgentBot" and bool(meta.get("assignee") or conversation.get("assignee_id"))


def _bot_owns(conversation: dict[str, Any]) -> bool:
    return not (
        _human_assigned(conversation)
        or conversation.get("status") in {"resolved", "snoozed"}
    )


def _latest_staff_message(
    messages: tuple[dict[str, Any], ...], conversation_id: int, *, after: int = 0,
) -> StaffMessage | None:
    candidates = []
    for message in messages:
        if _epoch(message.get("id")) <= after:
            continue
        staff = parse_staff_message({
            **message, "event": "message_created", "conversation": {"id": conversation_id},
            "message_type": "outgoing" if message.get("message_type") == 1
            else message.get("message_type"),
        })
        if staff is not None and not staff.consultation_schedule:
            candidates.append(staff)
    return max(candidates, key=lambda e: e.message_id, default=None)


def _history_after_epoch(
    messages: tuple[dict[str, Any], ...], epoch: int, current_message_id: int
) -> tuple[tuple[str, str], ...]:
    # A queued newer input must not appear as past context for an older turn.
    # Synthetic followups use id=0 and intentionally consume the full history.
    ordered = sorted(
        (m for m in messages if not current_message_id or _epoch(m.get("id")) < current_message_id),
        key=lambda m: (m.get("created_at") or 0, _epoch(m.get("id"))),
    )
    marker = _context_marker(epoch)
    start_index = 0
    for index, message in enumerate(ordered):
        if message.get("private") is True and message.get("content") == marker:
            start_index = index + 1

    history: list[tuple[str, str]] = []
    for message in ordered[start_index:]:
        if message.get("private") is True or message.get("id") == current_message_id:
            continue
        if message.get("message_type") not in {"incoming", "outgoing", 0, 1}:
            continue
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        role = "user" if message.get("message_type") in {"incoming", 0} else "assistant"
        attributes = message.get("content_attributes")
        if (
            isinstance(attributes, dict)
            and attributes.get("bot_sensitive_content") == "certificate"
        ):
            content = "[SENSITIVE_DELIVERY]"
        history.append((role, content.strip()))
    return tuple(history)


def _requires_human_handoff(seeded: _SeededConversation) -> bool:
    return any(
        action[1] in {"human_handoff", "safety_escalation"} for action in seeded.store.actions
    )


def _is_callback(content: str) -> bool:
    if content.startswith("sc:"):
        return True
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
        "restart",
    } or content.startswith(("need:", "aid:", "contact:", "back:", "certificate:", "extra:"))


def _context_marker(epoch: int) -> str:
    return f"{_CONTEXT_MARKER_PREFIX}{epoch}]"


def _epoch(value: object) -> int:
    try:
        epoch = int(value)
    except TypeError, ValueError:
        return 0
    return max(epoch, 0)


def _string_attribute(attributes: dict[str, Any], name: str, default: str) -> str:
    value = attributes.get(name)
    return value if isinstance(value, str) and value else default


def _optional_string_attribute(attributes: dict[str, Any], name: str) -> str | None:
    value = attributes.get(name)
    return value if isinstance(value, str) and value else None
