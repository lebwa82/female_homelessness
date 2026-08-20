from __future__ import annotations

from typing import Any

from app.agents import AgentContext, YandexAgentGateway
from app.catalog import AidItem, available_aid_for_need, get_aid_item
from app.domain import (
    ActionKind,
    AgentAction,
    AgentTurn,
    Choice,
    ContactMethod,
    ConversationState,
    IncomingMessage,
    NeedKind,
    RiskAssessment,
    RiskLevel,
)
from app.knowledge import find_verified_articles, format_verified_context
from app.safety import assess_local_risk, merge_risk
from app.store import ConversationRecord, PostgresConversationStore
from app.ui import (
    CONTACT_CHOICES,
    CONTINUE_CHOICES,
    FOLLOWUP_CHOICES,
    LEVEL_TWO_CHOICES,
    MORE_HELP_CHOICES,
    NEED_CHOICES,
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
            return await self._enter_need_discovery(record)
        if callback_id == "human":
            return await self._human_turn(record, "button")
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
            return await self._human_turn(record, "level_two_support")
        if callback_id == "level2:later":
            return self._turn("Хорошо. К этой возможности можно вернуться в любое время.")
        return self._turn("Можно выбрать следующий шаг или написать своими словами.", NEED_CHOICES)

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
        await self.store.record_agent_run(record, "support", evaluation.action_audit)
        merged = merge_risk(assess_local_risk(incoming.text), evaluation.risk)
        await self.store.record_risk(record, merged)

        if merged.level is RiskLevel.CRITICAL:
            await self.store.create_escalation(record, merged)
            await self.store.record_action(record, "critical_escalation", "completed")
            return self._critical_turn(merged)
        if merged.level is RiskLevel.UNKNOWN:
            await self.store.record_action(record, "model_failure", "safe_fallback")
            return AgentTurn(
                text=UNKNOWN_PROMPT,
                choices=(
                    Choice(id="continue_bot", label="Продолжить здесь"),
                    Choice(id="human", label="Поговорить с живым человеком"),
                ),
            )
        if merged.level is RiskLevel.HUMAN_REQUESTED:
            return await self._human_turn(record, "message")
        if merged.level in {RiskLevel.URGENT, RiskLevel.CONCERN}:
            await self.store.create_escalation(record, merged)

        if record.state == ConversationState.COLLECTING_LOCATION.value:
            await self.store.update(
                record,
                pending_city=incoming.text[:120],
                state=ConversationState.COLLECTING_CONTACT_METHOD.value,
            )
            return self._contact_turn()
        if record.state == ConversationState.COLLECTING_CONTACT_VALUE.value:
            return await self._complete_pending_request(record, incoming.text.strip()[:320])

        action = evaluation.action
        if action is None:
            return await self._enter_need_discovery(record)
        return await self._apply_model_action(record, action, incoming.text)

    async def _handle_need_choice(self, record: ConversationRecord, raw_need: str) -> AgentTurn:
        try:
            need = NeedKind(raw_need)
        except ValueError:
            return await self._enter_need_discovery(record)
        await self.store.update(record, need=need.value, state=ConversationState.CHOOSING_AID.value)
        if need is NeedKind.OTHER:
            return self._turn(OTHER_PROMPT)
        return self._offer_turn(need)

    async def _handle_aid_choice(self, record: ConversationRecord, aid_id: str) -> AgentTurn:
        item = get_aid_item(aid_id)
        if item is None:
            return await self._enter_need_discovery(record)
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
            return await self._enter_need_discovery(record)
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

    async def _apply_model_action(
        self, record: ConversationRecord, action: AgentAction, user_text: str
    ) -> AgentTurn:
        if action.kind is ActionKind.RECORD_ESCALATION:
            assessment = RiskAssessment(
                level=RiskLevel.CONCERN,
                detector="support_agent",
                rationale="support action escalation",
            )
            await self.store.create_escalation(record, assessment)
            return self._turn(action.text, (Choice(id="continue_bot", label="Продолжить здесь"),))
        if action.kind is ActionKind.OFFER_AID and action.need and action.need is not NeedKind.OTHER:
            await self.store.update(record, need=action.need.value, state=ConversationState.CHOOSING_AID.value)
            return self._offer_turn(action.need, prefix=action.text)
        if action.kind is ActionKind.REQUEST_CONTACT and record.pending_aid_id:
            return self._contact_turn(prefix=action.text)
        if action.kind is ActionKind.CLOSE_CONVERSATION:
            await self.store.update(record, state=ConversationState.CLOSED.value)
            return self._turn(action.text)
        if action.kind is ActionKind.SHOW_CHOICES:
            choices = safe_choices(action.choices)
            if choices:
                if any(choice.id.startswith("need:") for choice in choices):
                    return await self._enter_need_discovery(record, action.text, choices)
                return self._turn(action.text, choices)
        detected = detect_need(user_text)
        if detected is not None:
            await self.store.update(record, need=detected.value, state=ConversationState.CHOOSING_AID.value)
            return self._offer_turn(detected, prefix=action.text)
        return await self._enter_need_discovery(record, action.text, NEED_CHOICES)

    async def _human_turn(self, record: ConversationRecord, reason: str) -> AgentTurn:
        assessment = RiskAssessment(
            level=RiskLevel.HUMAN_REQUESTED,
            categories=("human_requested",),
            detector="backend",
            rationale=reason,
        )
        await self.store.create_escalation(record, assessment)
        await self.store.record_action(record, "human_handoff", "simulated")
        return AgentTurn(
            text=HUMAN_PROMPT,
            choices=(
                Choice(id="continue_bot", label="Продолжить здесь"),
                Choice(id="human", label="Поговорить с живым человеком"),
            ),
        )

    @staticmethod
    def _critical_turn(assessment: RiskAssessment) -> AgentTurn:
        if "suicide" in assessment.categories:
            return AgentTurn(
                text=(
                    "Слышу вас. Это важно.\n\n"
                    "Телефон доверия — бесплатно, круглосуточно: 8-800-2000-122\n\n"
                    "Я здесь параллельно. Можно написать, что происходит."
                ),
                choices=(
                    Choice(id="human", label="Поговорить с живым человеком"),
                    Choice(id="continue_bot", label="Продолжить здесь"),
                ),
            )
        return AgentTurn(
            text=(
                "Слышу вас. Хочу убедиться, что вы сейчас в безопасности. "
                "Если есть непосредственная опасность и это безопасно, можно позвонить 112. "
                "Зову человека, а здесь можно продолжать писать."
            ),
            choices=(
                Choice(id="human", label="Поговорить с живым человеком"),
                Choice(id="continue_bot", label="Продолжить здесь"),
            ),
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

    @staticmethod
    def _offer_turn(need: NeedKind, prefix: str = "") -> AgentTurn:
        items = available_aid_for_need(need)
        if not items:
            return ConversationService._turn(OTHER_PROMPT)
        descriptions = "\n".join(f"— {item.label}" for item in items)
        text = f"{prefix.strip()}\n\n" if prefix.strip() else ""
        text += f"Вот что можем предложить сейчас:\n\n{descriptions}\n\nЧто сейчас ближе?"
        choices = tuple(Choice(id=f"aid:{item.id}", label=item.label) for item in items)
        return ConversationService._turn(text, (*choices, Choice(id="need:other", label="Что-то другое")))

    @staticmethod
    def _contact_turn(prefix: str = "") -> AgentTurn:
        text = prefix.strip() or "Чтобы это передать, нужен удобный способ связи. Что вам подходит?"
        return ConversationService._turn(text, CONTACT_CHOICES)

    async def _state_turn(self, record: ConversationRecord) -> AgentTurn:
        if record.state == ConversationState.AID_REQUESTED.value:
            return ConversationService._turn("Запрос уже сохранён. Нужно что-то ещё?", MORE_HELP_CHOICES)
        if record.state == ConversationState.CHOOSING_AID.value and record.need:
            try:
                return ConversationService._offer_turn(NeedKind(record.need))
            except ValueError:
                pass
        if record.state == ConversationState.COLLECTING_CONTACT_METHOD.value:
            return self._contact_turn()
        return await self._enter_need_discovery(record)


def available_catalog() -> tuple[AidItem, ...]:
    return tuple({item.id: item for need in NeedKind for item in available_aid_for_need(need)}.values())


def safe_choices(choices: tuple[Choice, ...]) -> tuple[Choice, ...]:
    allowed = {
        choice.id: choice
        for choice in (*NEED_CHOICES, *CONTACT_CHOICES, *MORE_HELP_CHOICES, *FOLLOWUP_CHOICES, *LEVEL_TWO_CHOICES)
    }
    return tuple(allowed[choice.id] for choice in choices if choice.id in allowed)[:4]


def detect_need(text: str) -> NeedKind | None:
    normalized = text.lower()
    if any(term in normalized for term in ("жиль", "некуда", "ночев", "хостел")):
        return NeedKind.HOUSING
    if any(term in normalized for term in ("ед", "продукт", "деньг", "карт")):
        return NeedKind.FOOD_MONEY
    if any(term in normalized for term in ("документ", "юрист", "прав")):
        return NeedKind.LEGAL
    if any(term in normalized for term in ("ребен", "ребён", "дет")):
        return NeedKind.CHILDREN
    if any(term in normalized for term in ("поговор", "поддерж", "психолог", "зависим")):
        return NeedKind.SUPPORT
    return None
