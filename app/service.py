from __future__ import annotations

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
    PolicyEffect,
    ResolvedTurn,
    RiskAssessment,
    RiskLevel,
    SupportIntent,
    SupportOffer,
)
from app.knowledge import find_verified_articles, format_verified_context
from app.policy import resolve_turn
from app.safety import assess_local_risk, merge_risk
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
HUMAN_PROMPT = "Слышу вас. Зову человека, который работает с этим ресурсом. Здесь можно продолжать писать."
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
        await self.store.append_message(record, "user", callback_id, {"callback": True})
        if record.state == ConversationState.FOLLOWUP_SENT.value:
            await self.store.cancel_pending_reminder(record)
            record = await self.store.update(record, state=ConversationState.FOLLOWUP_ANSWERED.value)
        if callback_id == "continue":
            return await self._enter_need_discovery(record)
        if callback_id == "pause":
            await self.store.update(record, state=ConversationState.CLOSED.value)
            return self._turn(PAUSE)
        if callback_id == "continue_bot":
            return await self._open_conversation_turn(
                record,
                "Я здесь. Можно продолжить с того места, где остановились.",
            )
        if callback_id == "human":
            return await self._human_turn(record, "button")
        if callback_id == "support:psychologist":
            if record.state != ConversationState.OPEN_CONVERSATION.value or record.pending_offer != SupportOffer.PSYCHOLOGIST.value:
                return await self._state_turn(record)
            return await self._execute_resolved_turn(
                record,
                ResolvedTurn(
                    text="Хорошо, начнём запрос к психологу.",
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
            return await self._handle_contact_choice(record, callback_id.removeprefix("contact:"))
        if callback_id == "more_help":
            return await self._enter_need_discovery(record)
        if callback_id == "finish":
            await self.store.update(record, state=ConversationState.CLOSED.value)
            return self._turn("Хорошо. Этот чат всегда открыт — пишите, когда захотите.")
        if callback_id in {"followup:same", "followup:worse"}:
            return self._turn("Понятно. Хотите попробовать что-то ещё из того, что можем предложить?", MORE_HELP_CHOICES)
        if callback_id == "followup:better":
            return self._turn(
                "Рада слышать. Если захотите, можно рассказать о более глубокой поддержке.",
                (
                    Choice(id="level2:yes", label="Да, интересно"),
                    Choice(id="finish", label="Нет, спасибо"),
                ),
            )
        if callback_id == "level2:yes":
            return self._turn(
                "Есть более глубокая поддержка — временное жильё, помощь специалистов с документами и работой, финансовая поддержка.\n\n"
                "Это уже с живым человеком, не через бот. Хотите узнать подробнее?",
                LEVEL_TWO_CHOICES,
            )
        if callback_id == "level2:details":
            return await self._human_turn(
                record,
                "level_two_support",
                cause=EscalationCause.LEVEL_TWO_SUPPORT,
            )
        if callback_id == "level2:later":
            return self._turn("Хорошо. К этой возможности можно вернуться в любое время.")
        return await self._state_turn(record)

    async def handle_text(self, incoming: IncomingMessage) -> AgentTurn:
        record = await self.store.ensure(incoming)
        await self.store.append_message(record, "user", incoming.text, {"telegram_message_id": incoming.message_id})
        if record.state == ConversationState.FOLLOWUP_SENT.value:
            await self.store.cancel_pending_reminder(record)
            record = await self.store.update(record, state=ConversationState.FOLLOWUP_ANSWERED.value)
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
        await self.store.record_agent_run(record, "risk", evaluation.risk_audit)
        await self.store.record_agent_run(record, "support", evaluation.support_audit)
        merged = merge_risk(assess_local_risk(incoming.text), evaluation.risk)
        await self.store.record_risk(record, merged)
        state_before = record.state
        decision = resolve_turn(merged, evaluation.plan, state_before)

        if (
            evaluation.plan is None
            or evaluation.plan.intent is not SupportIntent.OPEN_CONVERSATION
        ):
            decision = decision.model_copy(update={"offered_support": None})

        if (
            decision.effect is PolicyEffect.NONE
            and merged.level is not RiskLevel.UNKNOWN
            and record.state
            in {
                ConversationState.CHOOSING_AID.value,
                ConversationState.COLLECTING_LOCATION.value,
                ConversationState.COLLECTING_CONTACT_METHOD.value,
                ConversationState.COLLECTING_CONTACT_VALUE.value,
                ConversationState.AID_REQUESTED.value,
            }
        ):
            if merged.level in {RiskLevel.CONCERN, RiskLevel.URGENT}:
                await self.store.create_escalation(record, self._safety_escalation(merged))
            return await self._execute_workflow_text(record, incoming.text, merged, state_before)

        if (
            decision.choice_set is ChoiceSet.PSYCHOLOGIST_INTEREST
            and record.pending_offer != SupportOffer.PSYCHOLOGIST.value
        ):
            decision = decision.model_copy(
                update={"choice_set": ChoiceSet.NONE, "fallback_reason": "pending_offer_required"}
            )

        turn = await self._execute_resolved_turn(record, decision, merged)
        await self._record_policy_decision(record, state_before, merged, evaluation.plan, decision, turn)
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

    async def _handle_contact_choice(self, record: ConversationRecord, raw_method: str) -> AgentTurn:
        try:
            method = ContactMethod(raw_method)
        except ValueError:
            return self._contact_turn()
        if method is ContactMethod.LATER:
            return await self._complete_pending_request(record, None, method)
        if method is ContactMethod.CURRENT_TELEGRAM:
            value = f"@{record.username}" if record.username else None
            return await self._complete_pending_request(record, value, method)
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
        return self._turn("Хорошо, запрос сохранён. Нужно что-то ещё?", MORE_HELP_CHOICES)

    async def _execute_resolved_turn(
        self,
        record: ConversationRecord,
        decision: ResolvedTurn,
        assessment: RiskAssessment | None = None,
    ) -> AgentTurn:
        """Perform the side effects permitted by a policy-resolved turn."""
        if assessment and assessment.level in {RiskLevel.CONCERN, RiskLevel.URGENT}:
            await self.store.create_escalation(record, self._safety_escalation(assessment))

        if decision.effect is PolicyEffect.CRITICAL_ESCALATION:
            if assessment is not None:
                await self.store.create_escalation(record, self._safety_escalation(assessment))
            await self.store.record_action(record, "critical_escalation", "completed")
            return self._turn(
                decision.text,
                choices_for(decision.choice_set, decision.catalog_item_ids),
            )
        if decision.effect is PolicyEffect.HUMAN_HANDOFF:
            return await self._human_turn(record, "support_plan")
        if decision.effect is PolicyEffect.OFFER_AID and decision.need is not None:
            item_ids = decision.catalog_item_ids or tuple(
                item.id for item in available_aid_for_need(decision.need)
            )
            await self.store.update(
                record,
                need=decision.need.value,
                pending_offer=None,
                state=ConversationState.CHOOSING_AID.value,
            )
            return self._offer_turn(decision.need, decision.text, item_ids)
        if decision.effect is PolicyEffect.START_PSYCHOLOGIST_REQUEST:
            await self.store.update(
                record,
                pending_aid_id=PSYCHOLOGIST_AID_ID,
                pending_offer=None,
                state=ConversationState.COLLECTING_CONTACT_METHOD.value,
            )
            return self._contact_turn(prefix=decision.text)
        if decision.effect is PolicyEffect.CLOSE:
            await self.store.update(record, pending_offer=None, state=ConversationState.CLOSED.value)
            return self._turn(decision.text, choices_for(decision.choice_set, decision.catalog_item_ids))

        update_values: dict[str, str | None] = {"state": ConversationState.OPEN_CONVERSATION.value}
        if decision.offered_support is not None:
            update_values["pending_offer"] = decision.offered_support.value
        await self.store.update(record, **update_values)
        return self._turn(decision.text, choices_for(decision.choice_set, decision.catalog_item_ids))

    async def _execute_workflow_text(
        self,
        record: ConversationRecord,
        text: str,
        assessment: RiskAssessment,
        state_before: str,
    ) -> AgentTurn:
        if record.state == ConversationState.COLLECTING_LOCATION.value:
            await self.store.update(
                record,
                pending_city=text[:120],
                state=ConversationState.COLLECTING_CONTACT_METHOD.value,
            )
            turn = self._contact_turn()
            transition = "collecting_location_to_collecting_contact_method"
        elif record.state == ConversationState.COLLECTING_CONTACT_VALUE.value:
            turn = await self._complete_pending_request(record, text.strip()[:320])
            transition = "collecting_contact_value_to_aid_requested"
        else:
            turn = await self._state_turn(record)
            transition = "replay_current_workflow"
        await self._record_workflow_decision(record, state_before, assessment, transition, turn)
        return turn

    async def _record_policy_decision(
        self,
        record: ConversationRecord,
        state_before: str,
        assessment: RiskAssessment,
        plan: Any,
        decision: ResolvedTurn,
        turn: AgentTurn,
    ) -> None:
        await self.store.record_action(
            record,
            "policy_decision",
            "completed",
            {
                "state_before": state_before,
                "state_after": record.state,
                "risk": assessment.level.value,
                "intent": plan.intent.value if plan else None,
                "next_action": plan.next_action.value if plan else None,
                "choice_set": decision.choice_set.value,
                "rendered_callback_ids": [choice.id for choice in turn.choices],
                "effect": decision.effect.value,
                "fallback_reason": decision.fallback_reason,
            },
        )

    async def _record_workflow_decision(
        self,
        record: ConversationRecord,
        state_before: str,
        assessment: RiskAssessment,
        transition: str,
        turn: AgentTurn,
    ) -> None:
        await self.store.record_action(
            record,
            "policy_decision",
            "completed",
            {
                "state_before": state_before,
                "state_after": record.state,
                "risk": assessment.level.value,
                "intent": None,
                "next_action": None,
                "choice_set": "workflow",
                "rendered_callback_ids": [choice.id for choice in turn.choices],
                "effect": "none",
                "fallback_reason": None,
                "decision_source": "workflow",
                "workflow_transition": transition,
            },
        )

    async def _human_turn(
        self,
        record: ConversationRecord,
        reason: str,
        cause: EscalationCause = EscalationCause.HUMAN_REQUEST,
    ) -> AgentTurn:
        await self.store.create_escalation(
            record,
            EscalationRequest(cause=cause, reason=reason),
        )
        await self.store.record_action(record, "human_handoff", "simulated")
        return AgentTurn(
            text=HUMAN_PROMPT,
            choices=(
                Choice(id="continue_bot", label="Продолжить здесь"),
                Choice(id="human", label="Поговорить с живым человеком"),
            ),
        )

    @staticmethod
    def _safety_escalation(assessment: RiskAssessment) -> EscalationRequest:
        return EscalationRequest(
            cause=EscalationCause.SAFETY,
            level=assessment.level,
            categories=assessment.categories,
            reason=assessment.rationale,
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
        return await self._open_conversation_turn(record, UNKNOWN_PROMPT)


def available_catalog() -> tuple[AidItem, ...]:
    return tuple({item.id: item for need in NeedKind for item in available_aid_for_need(need)}.values())
