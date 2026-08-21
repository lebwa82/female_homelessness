from __future__ import annotations

import hashlib
from typing import Any

from app.agents import AgentContext, YandexAgentGateway
from app.catalog import PSYCHOLOGIST_AID_ID, AidItem, available_aid_for_need, get_aid_item
from app.domain import (
    AgentTurn,
    Choice,
    ChoiceSet,
    ContactMethod,
    ConversationState,
    EscalationCause,
    EscalationRequest,
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
from app.policy import HUMAN_HANDOFF_PROMPT, POLICY_VERSION, resolve_turn
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


class ConversationService:
    def __init__(self, store: Any | None = None, gateway: YandexAgentGateway | None = None) -> None:
        self.store = store or PostgresConversationStore()
        self.gateway = gateway or YandexAgentGateway()

    async def start(self, incoming: IncomingMessage) -> AgentTurn:
        record = await self.store.ensure(incoming)
        await self.store.append_message(record, "user", "/start", {"telegram_message_id": incoming.message_id})
        await self.store.record_action(record, "started", "completed")
        return self._turn(WELCOME, CONTINUE_CHOICES)

    async def record_outbound(self, incoming: IncomingMessage, turn: AgentTurn) -> None:
        record = await self.store.ensure(incoming)
        await self.store.append_message(record, "assistant", turn.text, {"ui": {"choices": [choice.id for choice in turn.choices]}})

    async def delete(self, incoming: IncomingMessage) -> AgentTurn:
        record = await self.store.ensure(incoming)
        await self.store.delete_data(record)
        return self._turn(
            "Запрос на удаление данных принят. Если сейчас нужна помощь, можно продолжить писать здесь."
        )

    async def handle_callback(self, incoming: IncomingMessage, callback_id: str) -> AgentTurn:
        record = await self.store.ensure(incoming)
        lease_token = await self.store.claim_callback(record, callback_id, incoming.message_id)
        if lease_token is None:
            return await self._replay_callback(record, callback_id)
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
            await self.store.update(record, state=ConversationState.CLOSED.value)
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
            await self.store.update(record, state=ConversationState.CLOSED.value)
            return self._turn("Хорошо. Этот чат всегда открыт — пишите, когда захотите.")
        if callback_id in {"followup:same", "followup:worse"}:
            if record.state != ConversationState.FOLLOWUP_ANSWERED.value:
                return await self._state_turn(record)
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
            return self._turn("Хорошо. К этой возможности можно вернуться в любое время.")
        return await self._state_turn(record)

    async def handle_text(self, incoming: IncomingMessage) -> AgentTurn:
        record = await self.store.ensure(incoming)
        await self.store.append_message(record, "user", incoming.text, {"telegram_message_id": incoming.message_id})
        history = await self.store.history(record)
        knowledge_query = " ".join(content for role, content in history if role == "user")
        verified_articles = find_verified_articles(knowledge_query)
        evaluation = await self.gateway.evaluate(
            AgentContext(
                history=history,
                state=record.state,
                catalog=tuple(item.model_dump(mode="json") for item in available_catalog()),
                knowledge=(format_verified_context(verified_articles),) if verified_articles else (),
            )
        )
        await self.store.record_agent_run(record, "safety", evaluation.safety_audit)
        await self.store.record_agent_run(record, "support", evaluation.support_audit)
        try:
            pending_offer = SupportOffer(record.pending_offer) if record.pending_offer else None
            signals = extract_signals(incoming.text, pending_offer=pending_offer)
            local_risk = assess_local_risk_from_signals(signals)
        except Exception:  # noqa: BLE001 - a local inspection failure has its own deterministic route
            signals = None
            local_risk = RiskAssessment(
                level=RiskLevel.UNKNOWN,
                rationale="local inspection unavailable",
                detector="local-signals",
            )
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
        turn = await self._execute_resolved_turn(record, decision, local_risk)
        await self._record_policy_decision(
            record,
            state_before,
            local_risk,
            signals,
            evaluation,
            decision,
            turn,
        )
        return turn

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
        await self.store.record_action(record, "create_aid_request", "completed")
        if decision is not None:
            return self._render_resolved_turn(decision)
        return self._turn("Хорошо, запрос сохранён. Нужно что-то ещё?", MORE_HELP_CHOICES)

    async def _execute_resolved_turn(
        self,
        record: ConversationRecord,
        decision: ResolvedTurn,
        assessment: RiskAssessment | None = None,
        handoff_request: EscalationRequest | None = None,
    ) -> AgentTurn:
        """Perform the side effects permitted by a policy-resolved turn."""
        for side_effect in decision.side_effects:
            if side_effect is PolicySideEffect.RECORD_SAFETY and assessment is not None:
                await self.store.create_escalation(record, self._safety_escalation(assessment))
            if side_effect is PolicySideEffect.COMPLETE_FOLLOWUP:
                await self.store.cancel_pending_reminder(record)
                await self.store.update(record, state=ConversationState.FOLLOWUP_ANSWERED.value)

        if decision.effect is PolicyEffect.CRITICAL_ESCALATION:
            await self.store.update(
                record,
                pending_offer=None,
                state=ConversationState.OPEN_CONVERSATION.value,
            )
            await self.store.record_action(record, "critical_escalation", "completed")
            return self._render_resolved_turn(decision)
        if decision.effect is PolicyEffect.HUMAN_HANDOFF:
            await self.store.create_escalation(
                record,
                handoff_request
                or EscalationRequest(cause=EscalationCause.HUMAN_REQUEST, reason="verified_signal"),
            )
            await self.store.update(
                record,
                pending_offer=None,
                state=ConversationState.OPEN_CONVERSATION.value,
            )
            await self.store.record_action(record, "human_handoff", "simulated")
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
            return await self._complete_pending_request(record, decision.workflow_value, decision=decision)
        if decision.effect is PolicyEffect.REPLAY_WORKFLOW:
            return self._render_resolved_turn(decision)
        if decision.effect is PolicyEffect.CLOSE:
            await self.store.update(record, pending_offer=None, state=ConversationState.CLOSED.value)
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
        return ConversationService._turn(decision.text, choices)

    async def _record_policy_decision(
        self,
        record: ConversationRecord,
        state_before: str,
        assessment: RiskAssessment,
        signals: Any,
        evaluation: Any,
        decision: ResolvedTurn,
        turn: AgentTurn,
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
                "support_intent": evaluation.support.intent.value if evaluation.support else None,
                "support_status": evaluation.support_status.value,
                "rule_ids": [match.rule_id for match in signals.matches] if signals is not None else [],
                "choice_set": decision.choice_set.value,
                "rendered_callback_ids": [choice.id for choice in turn.choices],
                "effect": decision.effect.value,
                "side_effects": [side_effect.value for side_effect in decision.side_effects],
                "fallback_reason": decision.fallback_reason,
            },
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
    def _safety_escalation(assessment: RiskAssessment) -> EscalationRequest:
        return EscalationRequest(
            cause=EscalationCause.SAFETY,
            level=assessment.level,
            categories=assessment.categories,
            reason=assessment.rationale,
        )

    @staticmethod
    def _callback_request_key(record: ConversationRecord, callback_id: str, message_id: int | None) -> str:
        source_message_id = str(message_id) if message_id is not None else "missing"
        origin = f"{record.id}:{callback_id}:{source_message_id}".encode()
        return f"callback:{hashlib.sha256(origin).hexdigest()}"

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
