from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from app.agents import AgentContext, AgentEvaluation, YandexAgentGateway
from app.catalog import PSYCHOLOGIST_AID_ID, AidItem, available_aid_for_need, get_aid_item
from app.domain import (
    AgentTurn,
    Choice,
    ChoiceSet,
    ContactMethod,
    ConversationState,
    DeliveryAuthorization,
    DiagnosticStatus,
    EscalationCause,
    EscalationRequest,
    InboundExecutionKey,
    InboundExecutionKind,
    IncomingMessage,
    NeedKind,
    PolicyContext,
    PolicyEffect,
    PolicySideEffect,
    ResolvedTurn,
    RiskAssessment,
    RiskLevel,
    SupportOffer,
)
from app.knowledge import find_verified_articles, format_verified_context
from app.policy import HUMAN_HANDOFF_PROMPT, POLICY_VERSION, critical_resolved_turn, resolve_turn
from app.safety import assess_local_risk_from_signals
from app.signals import MATCHER_VERSION, extract_signals
from app.store import ConversationRecord, PostgresConversationStore
from app.ui import (
    CONTACT_CHOICES,
    CONTINUE_CHOICES,
    LEVEL_TWO_CHOICES,
    MORE_HELP_CHOICES,
    NEED_CHOICES,
    choices_for,
)

WELCOME = (
    "Привет. Здесь можно получить поддержку без необходимости называть себя или объяснять всё сразу.\n\n"
    "Хотите продолжить?"
)
PAUSE = "Хорошо. Если захотите вернуться — напишите в любое время. Этот чат никуда не денется."
NEED_PROMPT = "Что сейчас важнее всего? Можно выбрать или написать своими словами."
OTHER_PROMPT = "Расскажите немного — что сейчас происходит? Не обязательно в деталях, только если хотите."
UNKNOWN_PROMPT = "Я здесь. Можно продолжить разговор или позвать человека."
PERSISTENCE_UNAVAILABLE_PROMPT = (
    "Не получилось безопасно сохранить сообщение. Можно повторить позже или позвать человека."
)


def _diagnostics_unavailable() -> AgentEvaluation:
    return AgentEvaluation(
        safety_status=DiagnosticStatus.UNAVAILABLE,
        support_status=DiagnosticStatus.UNAVAILABLE,
        safety_audit={"status": "unavailable"},
        support_audit={"status": "unavailable"},
    )


class ConversationService:
    def __init__(self, store: Any | None = None, gateway: YandexAgentGateway | None = None) -> None:
        self.store = store or PostgresConversationStore()
        self.gateway = gateway or YandexAgentGateway()
        self._conversation_locks: dict[tuple[str, int], asyncio.Lock] = {}

    async def start(self, incoming: IncomingMessage) -> AgentTurn:
        async with self._lock_for(incoming):
            return await self._bind_turn_to_current_record(incoming, await self._start(incoming))

    async def _start(self, incoming: IncomingMessage) -> AgentTurn:
        try:
            async with self.store.unit_of_work(incoming) as record:
                if record is None:
                    raise LookupError("conversation disappeared during start")
                return await self._start_serialized(record, incoming)
        except Exception:  # noqa: BLE001 - a failed inbound write must not pretend to have succeeded
            return self._persistence_unavailable_turn()

    async def _start_serialized(self, record: ConversationRecord, incoming: IncomingMessage) -> AgentTurn:
        lease_token = await self.store.claim_text(record, incoming.message_id)
        if lease_token is None:
            outcome = await self._replay_text_outcome(record, incoming.message_id)
            if outcome is not None:
                return outcome
            return self._turn(WELCOME, CONTINUE_CHOICES).model_copy(
                update={"audit": {"skip_outbound_persistence": True, "suppress_delivery": True}}
            )
        outcome = await self._replay_text_outcome(record, incoming.message_id, lease_token)
        if outcome is not None:
            return outcome
        await self.store.append_message(record, "user", "/start", {"telegram_message_id": incoming.message_id})
        start_key = self._text_request_key(record, incoming.message_id, PolicyEffect.NONE)
        await self.store.record_action(
            record,
            "started",
            "completed",
            effect_key=self._effect_key(start_key, "started"),
        )
        turn = self._turn(WELCOME, CONTINUE_CHOICES)
        await self.store.save_text_outcome(record, incoming.message_id, lease_token, turn)
        return turn

    async def record_outbound(self, incoming: IncomingMessage, turn: AgentTurn) -> None:
        execution_key = self._execution_key_for_turn(incoming, turn)
        async with self._lock_for(incoming):
            # An outbound audit is never an identity-creating operation.  In
            # particular, a delayed pre-delete turn must not recreate a record.
            async with self.store.unit_of_work(incoming, create=False) as record:
                if record is None or not self._turn_matches_record(record, turn):
                    return
                # Idempotent acknowledgement is its own atomic boundary.  It
                # must survive an optional transcript-audit failure below.
                await self.store.acknowledge_text_outcome(record, execution_key)
            async with self.store.unit_of_work(incoming, create=False) as record:
                if record is None or not self._turn_matches_record(record, turn):
                    return
                await self.store.append_message(
                    record,
                    "assistant",
                    turn.text,
                    {"ui": {"choices": [choice.id for choice in turn.choices]}},
                )

    async def record_delivery_ambiguity(self, incoming: IncomingMessage, turn: AgentTurn) -> None:
        """Persist the finite post-send/pre-ack transport observation."""
        execution_key = self._execution_key_for_turn(incoming, turn)
        async with self._lock_for(incoming), self.store.unit_of_work(
            incoming,
            create=False,
        ) as record:
            if record is None or not self._turn_matches_record(record, turn):
                return
            await self.store.mark_delivery_ambiguous(record, execution_key)

    @staticmethod
    def _turn_matches_record(record: ConversationRecord, turn: AgentTurn) -> bool:
        expected_id = turn.audit.get("conversation_id")
        expected_generation = turn.audit.get("conversation_generation")
        return not (
            (isinstance(expected_id, int) and record.id != expected_id)
            or (isinstance(expected_generation, int) and record.generation != expected_generation)
        )

    async def authorize_delivery(self, incoming: IncomingMessage, turn: AgentTurn) -> bool:
        """Authorize the visible reply against its durable conversation identity."""
        return await self.delivery_status(incoming, turn) is DeliveryAuthorization.ALLOW

    async def delivery_status(
        self,
        incoming: IncomingMessage,
        turn: AgentTurn,
    ) -> DeliveryAuthorization:
        try:
            async with self.store.unit_of_work(incoming, create=False) as record:
                if record is None:
                    tombstone_generation = await self.store.tombstone_generation(incoming)
                    expected_generation = turn.audit.get("conversation_generation")
                    if tombstone_generation is not None and (
                        not isinstance(expected_generation, int)
                        or tombstone_generation > expected_generation
                    ):
                        return DeliveryAuthorization.DENY_CONFIRMED
                    return DeliveryAuthorization.UNAVAILABLE
                expected_id = turn.audit.get("conversation_id")
                expected_generation = turn.audit.get("conversation_generation")
                if (
                    not isinstance(expected_id, int)
                    or not isinstance(expected_generation, int)
                    or record.id != expected_id
                    or record.generation != expected_generation
                ):
                    return DeliveryAuthorization.DENY_CONFIRMED
                return DeliveryAuthorization.ALLOW
        except Exception:  # noqa: BLE001 - unavailable is not a confirmed tombstone
            return DeliveryAuthorization.UNAVAILABLE

    @asynccontextmanager
    async def delivery_authorization(
        self,
        incoming: IncomingMessage,
        turn: AgentTurn,
    ) -> AsyncIterator[DeliveryAuthorization]:
        """Hold deletion serialization from durable authorization through send.

        The yielded token belongs to the replayable outbox record.  A missing
        record is confirmed denial only when a newer tombstone exists;
        otherwise storage absence is unavailable, allowing only canonical
        critical delivery to fail open at the adapter boundary.
        """
        yielded = False
        execution_key = self._execution_key_for_turn(incoming, turn)
        try:
            async with self._lock_for(incoming), self.store.unit_of_work(
                incoming,
                create=False,
            ) as record:
                if record is None:
                    tombstone_generation = await self.store.tombstone_generation(incoming)
                    expected_generation = turn.audit.get("conversation_generation")
                    yielded = True
                    if tombstone_generation is not None and (
                        not isinstance(expected_generation, int)
                        or tombstone_generation > expected_generation
                    ):
                        yield DeliveryAuthorization.DENY_CONFIRMED
                    else:
                        yield DeliveryAuthorization.UNAVAILABLE
                    return
                expected_id = turn.audit.get("conversation_id")
                expected_generation = turn.audit.get("conversation_generation")
                if (
                    not isinstance(expected_id, int)
                    or not isinstance(expected_generation, int)
                    or record.id != expected_id
                    or record.generation != expected_generation
                ):
                    yielded = True
                    yield DeliveryAuthorization.DENY_CONFIRMED
                    return
                if turn.audit.get("skip_outbound_persistence"):
                    yielded = True
                    yield DeliveryAuthorization.ALLOW
                    return
                token = await self.store.claim_outbound_delivery(record, execution_key)
                if token is None:
                    yielded = True
                    yield DeliveryAuthorization.DENY_CONFIRMED
                    return
                try:
                    yielded = True
                    yield DeliveryAuthorization.ALLOW
                except BaseException:
                    await self.store.release_outbound_delivery(record, execution_key, token)
                    raise
                else:
                    await self.store.acknowledge_text_outcome(record, execution_key)
        except Exception:
            if yielded:
                raise
            yield DeliveryAuthorization.UNAVAILABLE

    async def claim_inbound(self, incoming: IncomingMessage) -> bool:
        """Claim a stateless command/media update so a Telegram retry has no side effect."""
        async with self._lock_for(incoming), self.store.unit_of_work(incoming) as record:
            if record is None:
                raise LookupError("conversation disappeared during stateless update")
            lease_token = await self.store.claim_text(record, incoming.message_id)
            if lease_token is None:
                return False
            await self.store.complete_text(record, incoming.message_id, lease_token)
            return True

    async def delete(self, incoming: IncomingMessage) -> AgentTurn:
        async with self._lock_for(incoming):
            try:
                return await self._delete(incoming)
            except Exception:  # noqa: BLE001 - deletion must never falsely claim completion
                return self._persistence_unavailable_turn()

    async def _delete(self, incoming: IncomingMessage) -> AgentTurn:
        async with self.store.unit_of_work(incoming, create=False) as record:
            if record is not None:
                await self.store.delete_data(record)
        return self._turn(
            "Запрос на удаление данных принят. Если сейчас нужна помощь, можно продолжить писать здесь.",
        ).model_copy(update={"audit": {"skip_outbound_persistence": True}})

    async def handle_callback(self, incoming: IncomingMessage, callback_id: str) -> AgentTurn:
        async with self._lock_for(incoming):
            return await self._bind_turn_to_current_record(incoming, await self._handle_callback(incoming, callback_id))

    async def _handle_callback(self, incoming: IncomingMessage, callback_id: str) -> AgentTurn:
        async with self.store.unit_of_work(incoming) as record:
            if record is None:
                raise LookupError("conversation disappeared during callback")
            return await self._handle_callback_serialized(record, incoming, callback_id)

    async def _handle_callback_serialized(
        self,
        record: ConversationRecord,
        incoming: IncomingMessage,
        callback_id: str,
    ) -> AgentTurn:
        outcome_key = InboundExecutionKey.callback(incoming.message_id)
        lease_token = await self.store.claim_callback(record, callback_id, incoming.message_id)
        if lease_token is None:
            outcome = await self._replay_text_outcome(record, outcome_key)
            if outcome is not None:
                return outcome
            return self._bind_execution_key(await self._replay_callback(record, callback_id), outcome_key)
        text_lease_token = await self.store.claim_text(record, outcome_key)
        if text_lease_token is None:
            outcome = await self._replay_text_outcome(record, outcome_key)
            if outcome is not None:
                return outcome
            raise RuntimeError("callback_outcome_claim_lost")
        try:
            await self.store.append_message(record, "user", callback_id, {"callback": True})
            turn = await self._dispatch_callback(
                record,
                callback_id,
                self._callback_request_key(record, callback_id, incoming.message_id),
            )
        except Exception:
            await self.store.fail_callback(record, callback_id, incoming.message_id, lease_token)
            raise
        turn = self._bind_execution_key(turn, outcome_key)
        await self.store.save_text_outcome(record, outcome_key, text_lease_token, turn)
        await self.store.complete_callback(record, callback_id, incoming.message_id, lease_token)
        return turn

    async def _dispatch_callback(
        self,
        record: ConversationRecord,
        callback_id: str,
        request_key: str,
    ) -> AgentTurn:
        if callback_id.startswith("followup:") and record.state == ConversationState.FOLLOWUP_SENT.value:
            await self.store.cancel_pending_reminder(record)
            record = await self.store.update(record, state=ConversationState.FOLLOWUP_ANSWERED.value)
        if callback_id == "continue":
            if record.state != ConversationState.GREETING.value:
                return await self._state_turn(record)
            return await self._enter_need_discovery(record)
        if callback_id == "pause":
            if record.state != ConversationState.GREETING.value:
                return await self._state_turn(record)
            await self._clear_abandoned_workflow(record, state=ConversationState.CLOSED)
            return self._turn(PAUSE)
        if callback_id == "continue_bot":
            if record.state != ConversationState.OPEN_CONVERSATION.value:
                return await self._state_turn(record)
            return await self._open_conversation_turn(
                record,
                "Я здесь. Можно продолжить с того места, где остановились.",
            )
        if callback_id == "human":
            return await self._human_turn(record, "button", request_key=request_key)
        if callback_id == "support:psychologist":
            if (
                record.state != ConversationState.OPEN_CONVERSATION.value
                or record.pending_offer != SupportOffer.PSYCHOLOGIST.value
            ):
                return await self._state_turn(record)
            return await self._execute_resolved_turn(
                record,
                ResolvedTurn(
                    text="Хорошо, начнём запрос к психологу.",
                    choice_set=ChoiceSet.CONTACT_METHODS,
                    effect=PolicyEffect.START_PSYCHOLOGIST_REQUEST,
                ),
                request_key=request_key,
            )
        if callback_id == "location:skip":
            if record.state != ConversationState.COLLECTING_LOCATION.value:
                return await self._state_turn(record)
            await self.store.update(record, state=ConversationState.COLLECTING_CONTACT_METHOD.value)
            return self._contact_turn()
        if callback_id.startswith("need:"):
            if record.state not in {
                ConversationState.GREETING.value,
                ConversationState.DISCOVERING_NEED.value,
                ConversationState.CHOOSING_AID.value,
            }:
                return await self._state_turn(record)
            if callback_id == f"need:{NeedKind.SUPPORT.value}":
                return await self._open_conversation_turn(
                    record,
                    "Я здесь и могу вас выслушать. Можно написать, что сейчас особенно важно.",
                    clear_pending_offer=True,
                )
            return await self._handle_need_choice(record, callback_id.removeprefix("need:"))
        if callback_id.startswith("aid:"):
            if record.state != ConversationState.CHOOSING_AID.value:
                return await self._state_turn(record)
            return await self._handle_aid_choice(record, callback_id.removeprefix("aid:"))
        if callback_id.startswith("contact:"):
            if record.state != ConversationState.COLLECTING_CONTACT_METHOD.value:
                return await self._state_turn(record)
            return await self._handle_contact_choice(
                record,
                callback_id.removeprefix("contact:"),
                request_key=request_key,
            )
        if callback_id == "more_help":
            if record.state != ConversationState.AID_REQUESTED.value:
                return await self._state_turn(record)
            return await self._enter_need_discovery(record)
        if callback_id == "finish":
            if record.state not in {
                ConversationState.AID_REQUESTED.value,
                ConversationState.FOLLOWUP_ANSWERED.value,
            }:
                return await self._state_turn(record)
            await self._clear_abandoned_workflow(record, state=ConversationState.CLOSED)
            return self._turn("Хорошо. Этот чат всегда открыт — пишите, когда захотите.")
        if callback_id in {"followup:same", "followup:worse"}:
            if record.state != ConversationState.FOLLOWUP_ANSWERED.value:
                return await self._state_turn(record)
            await self.store.update(record, state=ConversationState.AID_REQUESTED.value)
            return self._turn("Понятно. Хотите попробовать что-то ещё из того, что можем предложить?", MORE_HELP_CHOICES)
        if callback_id == "followup:better":
            if record.state != ConversationState.FOLLOWUP_ANSWERED.value:
                return await self._state_turn(record)
            return self._turn(
                "Рада слышать. Если захотите, можно рассказать о более глубокой поддержке.",
                (
                    Choice(id="level2:yes", label="Да, интересно"),
                    Choice(id="finish", label="Нет, спасибо"),
                ),
            )
        if callback_id == "level2:yes":
            if record.state != ConversationState.FOLLOWUP_ANSWERED.value:
                return await self._state_turn(record)
            return self._turn(
                "Есть более глубокая поддержка — временное жильё, помощь специалистов с документами и работой, финансовая поддержка.\n\n"
                "Это уже с живым человеком, не через бот. Хотите узнать подробнее?",
                LEVEL_TWO_CHOICES,
            )
        if callback_id == "level2:details":
            if record.state != ConversationState.FOLLOWUP_ANSWERED.value:
                return await self._state_turn(record)
            return await self._human_turn(
                record,
                "level_two_support",
                cause=EscalationCause.LEVEL_TWO_SUPPORT,
                request_key=request_key,
            )
        if callback_id == "level2:later":
            if record.state != ConversationState.FOLLOWUP_ANSWERED.value:
                return await self._state_turn(record)
            await self._clear_abandoned_workflow(record, state=ConversationState.OPEN_CONVERSATION)
            return self._turn("Хорошо. К этой возможности можно вернуться в любое время.")
        return await self._state_turn(record)

    async def handle_text(self, incoming: IncomingMessage) -> AgentTurn:
        async with self._lock_for(incoming):
            return await self._bind_turn_to_current_record(incoming, await self._handle_text(incoming))

    async def _handle_text(self, incoming: IncomingMessage) -> AgentTurn:
        try:
            preliminary_signals = extract_signals(incoming.text)
            preliminary_risk = assess_local_risk_from_signals(preliminary_signals)
        except Exception:  # noqa: BLE001 - the persisted route below has an unavailable projection
            preliminary_risk = RiskAssessment(
                level=RiskLevel.UNKNOWN,
                rationale="local inspection unavailable",
                detector="local-signals",
            )
        critical_identity: dict[str, int] = {}
        try:
            async with self.store.unit_of_work(incoming) as record:
                if record is None:
                    raise LookupError("conversation disappeared during text update")
                if preliminary_risk.level is RiskLevel.CRITICAL:
                    critical_identity = {
                        "conversation_id": record.id,
                        "conversation_generation": record.generation,
                    }
                    return await self._handle_persisted_critical(
                        record,
                        incoming,
                        preliminary_risk,
                        preliminary_signals,
                    )
                return await self._handle_noncritical_text_serialized(record, incoming)
        except Exception:  # noqa: BLE001 - a critical local route alone may fail open
            if preliminary_risk.level is RiskLevel.CRITICAL:
                return self._render_resolved_turn(critical_resolved_turn(preliminary_risk)).model_copy(
                    update={
                        "audit": {
                            "skip_outbound_persistence": True,
                            "critical_delivery": True,
                            **critical_identity,
                        }
                    }
                )
            return self._persistence_unavailable_turn()

    async def _handle_noncritical_text_serialized(
        self,
        record: ConversationRecord,
        incoming: IncomingMessage,
    ) -> AgentTurn:
        lease_token = await self.store.claim_text(record, incoming.message_id)
        if lease_token is None:
            outcome = await self._replay_text_outcome(record, incoming.message_id)
            if outcome is not None:
                return outcome
            return (await self._state_turn(record)).model_copy(
                update={"audit": {"skip_outbound_persistence": True, "suppress_delivery": True}}
            )
        try:
            outcome = await self._replay_text_outcome(record, incoming.message_id, lease_token)
            if outcome is not None:
                return outcome
            try:
                pending_offer = SupportOffer(record.pending_offer) if record.pending_offer else None
                signals = extract_signals(
                    incoming.text,
                    pending_offer=pending_offer,
                    state=record.state,
                )
                local_risk = assess_local_risk_from_signals(signals)
            except Exception:  # noqa: BLE001 - local safety has an explicit unavailable route
                pending_offer = None
                signals = None
                local_risk = RiskAssessment(
                    level=RiskLevel.UNKNOWN,
                    rationale="local inspection unavailable",
                    detector="local-signals",
                )

            # A workflow contact is retained locally only for the requested aid path.  Its
            # every model view is an exact generic marker, including this current message.
            audit: dict[str, Any] = {"telegram_message_id": incoming.message_id}
            if record.state == ConversationState.COLLECTING_CONTACT_VALUE.value:
                audit["content_type"] = "contact_value"
            await self.store.append_message(record, "user", incoming.text, audit)

            evaluation = await self._evaluate_diagnostics(record)
            await self.store.record_agent_run(record, "safety", evaluation.safety_audit)
            await self.store.record_agent_run(record, "support", evaluation.support_audit)
            await self.store.record_risk(record, local_risk)
            state_before = record.state
            decision = resolve_turn(
                PolicyContext(
                    state=state_before,
                    signals=signals,
                    local_risk=local_risk,
                    safety_status=evaluation.safety_status,
                    support_status=evaluation.support_status,
                    safety=evaluation.safety,
                    support=evaluation.support,
                    pending_offer=pending_offer,
                    workflow_value=incoming.text,
                    need=record.need,
                )
            )
            request_key = self._text_request_key(record, incoming.message_id, decision.effect)
            turn = await self._execute_resolved_turn(
                record,
                decision,
                local_risk,
                request_key=request_key,
            )
            await self._record_policy_decision(
                record,
                state_before,
                local_risk,
                signals,
                evaluation,
                decision,
                turn,
                effect_key=self._effect_key(request_key, "policy_decision"),
            )
            await self.store.save_text_outcome(record, incoming.message_id, lease_token, turn)
            return turn
        except Exception:  # noqa: TRY203 - UoW exit must observe and roll back the original failure
            raise

    async def _handle_persisted_critical(
        self,
        record: ConversationRecord,
        incoming: IncomingMessage,
        assessment: RiskAssessment,
        signals: Any,
    ) -> AgentTurn:
        """Persist a critical turn opportunistically without delaying its canonical response."""
        decision = critical_resolved_turn(assessment)
        try:
            lease_token = await self.store.claim_text(record, incoming.message_id)
            if lease_token is None:
                return await self._replay_text_outcome(record, incoming.message_id) or self._render_resolved_turn(decision)
            outcome = await self._replay_text_outcome(record, incoming.message_id, lease_token)
            if outcome is not None:
                return outcome
            audit: dict[str, Any] = {"telegram_message_id": incoming.message_id}
            if record.state == ConversationState.COLLECTING_CONTACT_VALUE.value:
                audit["content_type"] = "contact_value"
            await self.store.append_message(record, "user", incoming.text, audit)
            # A successfully prepared critical turn still runs exactly the two
            # diagnostic calls.  Their outputs are observed only; the canonical
            # local crisis decision below remains authoritative.
            evaluation = await self._evaluate_diagnostics(record)
            await self.store.record_agent_run(record, "safety", evaluation.safety_audit)
            await self.store.record_agent_run(record, "support", evaluation.support_audit)
            await self.store.record_risk(record, assessment)
            state_before = record.state
            request_key = self._text_request_key(record, incoming.message_id, decision.effect)
            turn = await self._execute_resolved_turn(
                record,
                decision,
                assessment,
                request_key=request_key,
            )
            await self._record_policy_decision(
                record,
                state_before,
                assessment,
                signals,
                evaluation,
                decision,
                turn,
                effect_key=self._effect_key(request_key, "policy_decision"),
            )
            await self.store.save_text_outcome(record, incoming.message_id, lease_token, turn)
            return turn
        except Exception:  # noqa: TRY203 - outer critical boundary supplies the fail-open copy
            raise

    async def _evaluate_diagnostics(self, record: ConversationRecord) -> AgentEvaluation:
        """Return exactly one diagnostic pair, or an explicit unavailable pair.

        Store/PII/knowledge preparation is deliberately complete before the
        gateway is invoked, so a preparation failure creates zero provider work.
        """
        try:
            history = await self.store.model_history(record)
            knowledge_query = " ".join(content for role, content in history if role == "user")
            verified_articles = find_verified_articles(knowledge_query)
            return await self.gateway.evaluate(
                AgentContext(
                    history=history,
                    state=record.state,
                    catalog=tuple(item.model_dump(mode="json") for item in available_catalog()),
                    knowledge=(format_verified_context(verified_articles),) if verified_articles else (),
                )
            )
        except Exception:  # noqa: BLE001 - diagnostics never alter the deterministic route
            return _diagnostics_unavailable()

    async def _replay_text_outcome(
        self,
        record: ConversationRecord,
        message_id: InboundExecutionKey | int | None,
        lease_token: str | None = None,
    ) -> AgentTurn | None:
        """Reuse a committed turn; acknowledge its claim if a failed lease was reclaimed."""
        outcome = await self.store.load_text_outcome(record, message_id)
        if outcome is None:
            return None
        turn, delivered = outcome
        if lease_token is not None:
            await self.store.complete_text(record, message_id, lease_token)
        if delivered:
            return turn.model_copy(
                update={"audit": {**turn.audit, "skip_outbound_persistence": True, "suppress_delivery": True}}
            )
        return turn

    @staticmethod
    def _persistence_unavailable_turn() -> AgentTurn:
        return ConversationService._turn(PERSISTENCE_UNAVAILABLE_PROMPT).model_copy(
            update={"audit": {"skip_outbound_persistence": True}}
        )

    async def _handle_need_choice(self, record: ConversationRecord, raw_need: str) -> AgentTurn:
        try:
            need = NeedKind(raw_need)
        except ValueError:
            return await self._state_turn(record)
        await self.store.update(record, need=need.value, state=ConversationState.CHOOSING_AID.value)
        if need is NeedKind.OTHER:
            return self._turn(OTHER_PROMPT)
        return self._offer_turn(need)

    async def _handle_aid_choice(self, record: ConversationRecord, aid_id: str) -> AgentTurn:
        item = get_aid_item(aid_id)
        if item is None:
            return await self._state_turn(record)
        if item.needs_location:
            await self.store.update(
                record,
                pending_aid_id=aid_id,
                state=ConversationState.COLLECTING_LOCATION.value,
            )
            return self._turn(
                "Чтобы понять, где это может быть удобно, можно написать город. Точный адрес не нужен.",
                (
                    Choice(id="location:skip", label="Не хочу указывать место"),
                    Choice(id="human", label="Поговорить с живым человеком"),
                ),
            )
        await self.store.update(
            record,
            pending_aid_id=aid_id,
            state=ConversationState.COLLECTING_CONTACT_METHOD.value,
        )
        return self._contact_turn()

    async def _handle_contact_choice(
        self,
        record: ConversationRecord,
        raw_method: str,
        request_key: str | None = None,
    ) -> AgentTurn:
        try:
            method = ContactMethod(raw_method)
        except ValueError:
            return self._contact_turn()
        if method is ContactMethod.LATER:
            return await self._complete_pending_request(record, None, method, request_key=request_key)
        if method is ContactMethod.CURRENT_TELEGRAM:
            value = f"@{record.username}" if record.username else None
            return await self._complete_pending_request(record, value, method, request_key=request_key)
        await self.store.update(
            record,
            pending_contact_method=method.value,
            state=ConversationState.COLLECTING_CONTACT_VALUE.value,
        )
        label = {ContactMethod.OTHER_TELEGRAM: "ник в Telegram", ContactMethod.PHONE: "номер телефона", ContactMethod.EMAIL: "email"}[method]
        return self._turn(f"Можно написать {label}. Он нужен только для организации выбранной помощи.")

    async def _complete_pending_request(
        self,
        record: ConversationRecord,
        contact_value: str | None,
        method: ContactMethod | None = None,
        decision: ResolvedTurn | None = None,
        request_key: str | None = None,
    ) -> AgentTurn:
        aid_id = record.pending_aid_id
        if aid_id is None or get_aid_item(aid_id) is None:
            return await self._state_turn(record)
        contact_method = method.value if method else record.pending_contact_method
        await self.store.create_aid_request(
            record,
            aid_id,
            contact_method,
            contact_value,
            city=record.pending_city,
            district=record.pending_district,
            request_key=request_key,
        )
        await self.store.update(
            record,
            state=ConversationState.AID_REQUESTED.value,
            pending_aid_id=None,
            pending_contact_method=None,
            pending_city=None,
            pending_district=None,
        )
        await self.store.record_action(
            record,
            "create_aid_request",
            "completed",
            effect_key=self._effect_key(request_key, "create_aid_request"),
        )
        if decision is not None:
            return self._render_resolved_turn(decision)
        return self._turn("Хорошо, запрос сохранён. Нужно что-то ещё?", MORE_HELP_CHOICES)

    async def _execute_resolved_turn(
        self,
        record: ConversationRecord,
        decision: ResolvedTurn,
        assessment: RiskAssessment | None = None,
        handoff_request: EscalationRequest | None = None,
        request_key: str | None = None,
    ) -> AgentTurn:
        """Perform the side effects permitted by a policy-resolved turn."""
        for side_effect in decision.side_effects:
            if side_effect is PolicySideEffect.RECORD_SAFETY and assessment is not None:
                await self.store.create_escalation(record, self._safety_escalation(assessment, request_key))
            if side_effect is PolicySideEffect.COMPLETE_FOLLOWUP:
                await self.store.cancel_pending_reminder(record)
                await self.store.update(record, state=ConversationState.FOLLOWUP_ANSWERED.value)

        if decision.effect is PolicyEffect.CRITICAL_ESCALATION:
            await self._clear_abandoned_workflow(record)
            await self.store.record_action(
                record,
                "critical_escalation",
                "completed",
                effect_key=self._effect_key(request_key, "critical_escalation"),
            )
            return self._render_resolved_turn(decision)
        if decision.effect is PolicyEffect.HUMAN_HANDOFF:
            escalation = handoff_request or EscalationRequest(
                cause=EscalationCause.HUMAN_REQUEST,
                reason="verified_signal",
                request_key=request_key,
            )
            if escalation.request_key is None and request_key is not None:
                escalation = escalation.model_copy(update={"request_key": request_key})
            await self.store.create_escalation(
                record,
                escalation,
            )
            await self._clear_abandoned_workflow(record)
            await self.store.record_action(
                record,
                "human_handoff",
                "simulated",
                effect_key=self._effect_key(escalation.request_key or request_key, "human_handoff"),
            )
            return self._render_resolved_turn(decision)
        if decision.effect is PolicyEffect.CANCEL_WORKFLOW:
            await self._clear_abandoned_workflow(record)
            await self.store.record_action(
                record,
                "workflow_cancelled",
                "completed",
                effect_key=self._effect_key(request_key, "workflow_cancelled"),
            )
            return self._render_resolved_turn(decision)
        if decision.effect is PolicyEffect.OFFER_AID and decision.need is not None:
            await self.store.update(
                record,
                need=decision.need.value,
                pending_offer=None,
                state=ConversationState.CHOOSING_AID.value,
            )
            return self._render_resolved_turn(decision)
        if decision.effect is PolicyEffect.START_NEED_DISCOVERY:
            await self.store.update(
                record,
                pending_offer=None,
                state=ConversationState.DISCOVERING_NEED.value,
            )
            return self._render_resolved_turn(decision)
        if decision.effect is PolicyEffect.START_PSYCHOLOGIST_REQUEST:
            await self.store.update(
                record,
                pending_aid_id=PSYCHOLOGIST_AID_ID,
                pending_offer=None,
                state=ConversationState.COLLECTING_CONTACT_METHOD.value,
            )
            return self._render_resolved_turn(decision)
        if decision.effect is PolicyEffect.CAPTURE_LOCATION:
            await self.store.update(
                record,
                pending_city=(decision.workflow_value or "")[:120],
                state=ConversationState.COLLECTING_CONTACT_METHOD.value,
            )
            return self._render_resolved_turn(decision)
        if decision.effect is PolicyEffect.COMPLETE_CONTACT:
            return await self._complete_pending_request(
                record,
                decision.workflow_value,
                decision=decision,
                request_key=request_key,
            )
        if decision.effect is PolicyEffect.REPLAY_WORKFLOW:
            return self._render_resolved_turn(decision)
        if decision.effect is PolicyEffect.CLOSE:
            await self._clear_abandoned_workflow(record, state=ConversationState.CLOSED.value)
            return self._render_resolved_turn(decision)

        update_values: dict[str, str | None] = {
            "state": ConversationState.OPEN_CONVERSATION.value,
            # A diagnostic offer only authorizes interpretation of the immediately following
            # acknowledgement.  Keep it for the rendered callback, but never let it drift
            # through an unrelated later text turn.
            "pending_offer": (
                record.pending_offer
                if decision.choice_set is ChoiceSet.PSYCHOLOGIST_INTEREST
                else None
            ),
        }
        if decision.offered_support is not None:
            update_values["pending_offer"] = decision.offered_support.value
        await self.store.update(record, **update_values)
        return self._render_resolved_turn(decision)

    @staticmethod
    def _render_resolved_turn(decision: ResolvedTurn) -> AgentTurn:
        choices = choices_for(decision.choice_set, decision.catalog_item_ids)
        if decision.choice_set is ChoiceSet.AID_CATALOG:
            choices = (*choices[:-1], Choice(id="need:other", label="Что-то другое"), choices[-1])
        turn = ConversationService._turn(decision.text, choices)
        if decision.effect is PolicyEffect.CRITICAL_ESCALATION:
            return turn.model_copy(update={"audit": {**turn.audit, "critical_delivery": True}})
        return turn

    async def _record_policy_decision(
        self,
        record: ConversationRecord,
        state_before: str,
        assessment: RiskAssessment,
        signals: Any,
        evaluation: Any,
        decision: ResolvedTurn,
        turn: AgentTurn,
        effect_key: str | None = None,
    ) -> None:
        await self.store.record_action(
            record,
            "policy_decision",
            "completed",
            {
                "policy_version": POLICY_VERSION,
                "matcher_version": signals.matcher_version if signals is not None else MATCHER_VERSION,
                "state_before": state_before,
                "state_after": record.state,
                "local_risk": assessment.level.value,
                "safety_label": evaluation.safety.level.value if evaluation.safety else None,
                "safety_status": evaluation.safety_status.value,
                "support_intent": (
                    evaluation.support.intent.value
                    if evaluation.support is not None and evaluation.support.intent is not None
                    else None
                ),
                "support_status": evaluation.support_status.value,
                "rule_ids": [match.rule_id for match in signals.matches] if signals is not None else [],
                "choice_set": decision.choice_set.value,
                "rendered_callback_ids": [choice.id for choice in turn.choices],
                "effect": decision.effect.value,
                "side_effects": [side_effect.value for side_effect in decision.side_effects],
                "fallback_reason": decision.fallback_reason,
            },
            effect_key=effect_key,
        )

    async def _human_turn(
        self,
        record: ConversationRecord,
        reason: str,
        cause: EscalationCause = EscalationCause.HUMAN_REQUEST,
        request_key: str | None = None,
    ) -> AgentTurn:
        decision = ResolvedTurn(
            text=HUMAN_HANDOFF_PROMPT,
            choice_set=ChoiceSet.SAFE_CONTINUE,
            effect=PolicyEffect.HUMAN_HANDOFF,
        )
        return await self._execute_resolved_turn(
            record,
            decision,
            handoff_request=EscalationRequest(cause=cause, reason=reason, request_key=request_key),
        )

    async def _clear_abandoned_workflow(
        self,
        record: ConversationRecord,
        *,
        state: ConversationState | str = ConversationState.OPEN_CONVERSATION,
    ) -> None:
        """Clear every finite-flow value before returning to conversation or a handoff."""
        await self.store.cancel_pending_reminder(record)
        await self.store.update(
            record,
            state=state.value if isinstance(state, ConversationState) else state,
            need=None,
            pending_aid_id=None,
            pending_contact_method=None,
            pending_city=None,
            pending_district=None,
            pending_offer=None,
        )

    async def _replay_callback(self, record: ConversationRecord, callback_id: str) -> AgentTurn:
        if callback_id == "human":
            return self._render_resolved_turn(
                ResolvedTurn(
                    text=HUMAN_HANDOFF_PROMPT,
                    choice_set=ChoiceSet.SAFE_CONTINUE,
                    effect=PolicyEffect.HUMAN_HANDOFF,
                )
            )
        return await self._state_turn(record)

    @staticmethod
    def _safety_escalation(assessment: RiskAssessment, request_key: str | None = None) -> EscalationRequest:
        return EscalationRequest(
            cause=EscalationCause.SAFETY,
            level=assessment.level,
            categories=assessment.categories,
            reason=assessment.rationale,
            request_key=request_key,
        )

    @staticmethod
    def _text_request_key(record: ConversationRecord, message_id: int | None, _effect: PolicyEffect) -> str:
        source_message_id = str(message_id) if message_id is not None else "missing"
        # The update identity is fixed before policy evaluation.  A changed
        # risk result on replay must never create a second request/escalation.
        origin = f"{record.id}:{source_message_id}".encode()
        return f"text:{hashlib.sha256(origin).hexdigest()}"

    @staticmethod
    def _callback_request_key(record: ConversationRecord, callback_id: str, message_id: int | None) -> str:
        source_message_id = str(message_id) if message_id is not None else "missing"
        origin = f"{record.id}:{callback_id}:{source_message_id}".encode()
        return f"callback:{hashlib.sha256(origin).hexdigest()}"

    @staticmethod
    def _bind_execution_key(turn: AgentTurn, key: InboundExecutionKey) -> AgentTurn:
        return turn.model_copy(
            update={
                "audit": {
                    **turn.audit,
                    "inbound_execution_kind": key.kind.value,
                }
            }
        )

    @staticmethod
    def _execution_key_for_turn(
        incoming: IncomingMessage,
        turn: AgentTurn,
    ) -> InboundExecutionKey:
        try:
            kind = InboundExecutionKind(turn.audit.get("inbound_execution_kind", "message"))
        except ValueError:
            kind = InboundExecutionKind.MESSAGE
        return InboundExecutionKey(kind, incoming.message_id)

    @staticmethod
    def _effect_key(request_key: str | None, kind: str) -> str | None:
        """Bind an audit effect to the same immutable update key as its work."""
        return f"{request_key}:{kind}" if request_key is not None else None

    def _lock_for(self, incoming: IncomingMessage) -> asyncio.Lock:
        return self._conversation_locks.setdefault(
            (incoming.channel, incoming.platform_user_id),
            asyncio.Lock(),
        )

    async def _bind_turn_to_current_record(self, incoming: IncomingMessage, turn: AgentTurn) -> AgentTurn:
        """Bind a delivery audit to the original durable conversation identity.

        A delete followed by a new inbound update can create a new identity for
        the same platform user.  Old turns may never attach their audit to it.
        """
        if turn.audit.get("skip_outbound_persistence") and not turn.audit.get("critical_delivery"):
            return turn
        try:
            record = await self.store.get(incoming)
        except Exception:  # noqa: BLE001 - delivery remains independently attempted
            return turn
        if record is None:
            # The held pre-send authorization distinguishes a tombstone from a
            # first-turn write failure.  A row lookup alone cannot make that
            # distinction, so retain the original identity evidence here.
            if turn.audit.get("critical_delivery"):
                return turn
            return turn.model_copy(
                update={
                    "audit": {
                        **turn.audit,
                        "skip_outbound_persistence": True,
                        "suppress_delivery": True,
                    }
                }
            )
        expected_id = turn.audit.get("conversation_id")
        expected_generation = turn.audit.get("conversation_generation")
        if (
            isinstance(expected_id, int)
            and isinstance(expected_generation, int)
            and (record.id != expected_id or record.generation != expected_generation)
        ):
            return turn
        return turn.model_copy(
            update={
                "audit": {
                    **turn.audit,
                    "conversation_id": record.id,
                    "conversation_generation": record.generation,
                }
            }
        )

    @staticmethod
    def _turn(text: str, choices: tuple[Choice, ...] = ()) -> AgentTurn:
        return AgentTurn(text=text, choices=choices).with_human_choice()

    async def _enter_need_discovery(
        self,
        record: ConversationRecord,
        text: str = NEED_PROMPT,
        choices: tuple[Choice, ...] = NEED_CHOICES,
    ) -> AgentTurn:
        await self.store.update(record, state=ConversationState.DISCOVERING_NEED.value)
        return self._turn(text, choices)

    async def _open_conversation_turn(
        self,
        record: ConversationRecord,
        text: str,
        *,
        clear_pending_offer: bool = False,
    ) -> AgentTurn:
        values: dict[str, str | None] = {"state": ConversationState.OPEN_CONVERSATION.value}
        if clear_pending_offer:
            values["pending_offer"] = None
        await self.store.update(record, **values)
        return self._turn(text)

    @staticmethod
    def _offer_turn(need: NeedKind, prefix: str = "", item_ids: tuple[str, ...] = ()) -> AgentTurn:
        items = (
            tuple(item for item_id in item_ids if (item := get_aid_item(item_id)) is not None)
            if item_ids
            else available_aid_for_need(need)
        )
        if not items:
            return ConversationService._turn(OTHER_PROMPT)
        descriptions = "\n".join(f"— {item.label}" for item in items)
        text = f"{prefix.strip()}\n\n" if prefix.strip() else ""
        text += f"Вот что можем предложить сейчас:\n\n{descriptions}\n\nЧто сейчас ближе?"
        choices = choices_for(ChoiceSet.AID_CATALOG, tuple(item.id for item in items))
        return ConversationService._turn(
            text,
            (*choices[:-1], Choice(id="need:other", label="Что-то другое"), choices[-1]),
        )

    @staticmethod
    def _contact_turn(prefix: str = "") -> AgentTurn:
        text = prefix.strip() or "Чтобы это передать, нужен удобный способ связи. Что вам подходит?"
        return ConversationService._turn(text, CONTACT_CHOICES)

    async def _state_turn(self, record: ConversationRecord) -> AgentTurn:
        if record.state == ConversationState.AID_REQUESTED.value:
            return ConversationService._turn("Запрос уже сохранён. Нужно что-то ещё?", MORE_HELP_CHOICES)
        if record.state == ConversationState.DISCOVERING_NEED.value:
            return ConversationService._turn(NEED_PROMPT, NEED_CHOICES)
        if record.state == ConversationState.CHOOSING_AID.value and record.need:
            try:
                return ConversationService._offer_turn(NeedKind(record.need))
            except ValueError:
                pass
        if record.state == ConversationState.COLLECTING_LOCATION.value:
            return ConversationService._turn(
                "Чтобы понять, где это может быть удобно, можно написать город. Точный адрес не нужен.",
                (
                    Choice(id="location:skip", label="Не хочу указывать место"),
                    Choice(id="human", label="Поговорить с живым человеком"),
                ),
            )
        if record.state == ConversationState.COLLECTING_CONTACT_METHOD.value:
            return self._contact_turn()
        if record.state == ConversationState.COLLECTING_CONTACT_VALUE.value:
            try:
                method = ContactMethod(record.pending_contact_method or "")
            except ValueError:
                return self._contact_turn()
            label = {
                ContactMethod.OTHER_TELEGRAM: "ник в Telegram",
                ContactMethod.PHONE: "номер телефона",
                ContactMethod.EMAIL: "email",
            }.get(method)
            if label:
                return self._turn(f"Можно написать {label}. Он нужен только для организации выбранной помощи.")
            return self._contact_turn()
        return self._turn(UNKNOWN_PROMPT)


def available_catalog() -> tuple[AidItem, ...]:
    return tuple({item.id: item for need in NeedKind for item in available_aid_for_need(need)}.values())
