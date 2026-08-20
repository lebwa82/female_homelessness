from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.agents import AgentEvaluation
from app.domain import ActionKind, AgentAction, Choice, IncomingMessage, RiskAssessment, RiskLevel
from app.service import ConversationService
from app.store import InMemoryConversationStore, StoredFollowupJob


@dataclass
class FixedGateway:
    evaluation: AgentEvaluation

    async def evaluate(self, context) -> AgentEvaluation:  # type: ignore[no-untyped-def]
        return self.evaluation


@dataclass
class CapturingGateway:
    evaluation: AgentEvaluation
    contexts: list = field(default_factory=list)

    async def evaluate(self, context) -> AgentEvaluation:  # type: ignore[no-untyped-def]
        self.contexts.append(context)
        return self.evaluation


def identity(text: str = "") -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=101,
        chat_id=202,
        username="helper_test",
        text=text,
        message_id=303,
    )


def safe_evaluation(action: AgentAction | None = None) -> AgentEvaluation:
    return AgentEvaluation(
        risk=RiskAssessment(level=RiskLevel.NONE, detector="model"),
        action=action or AgentAction(kind="reply", text="Что сейчас важнее всего?"),
        risk_audit={"status": "completed"},
        action_audit={"status": "completed"},
    )


@pytest.mark.asyncio
async def test_start_then_food_card_current_telegram_creates_one_aid_request() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))

    start = await service.start(identity())
    need = await service.handle_callback(identity(), "continue")
    offer = await service.handle_callback(identity(), "need:food_money")
    contact = await service.handle_callback(identity(), "aid:food_card")
    done = await service.handle_callback(identity(), "contact:current_telegram")

    assert "тест" not in start.text.lower()
    assert start.choices[-1].id == "human"
    assert {choice.id for choice in need.choices} >= {"need:food_money", "human"}
    assert {choice.id for choice in offer.choices} >= {"aid:food_card", "human"}
    assert {choice.id for choice in contact.choices} >= {"contact:current_telegram", "human"}
    assert "нужно что-то ещё" in done.text.lower()
    assert [(request.aid_id, request.contact_method, request.contact_value) for request in store.aid_requests] == [
        ("food_card", "current_telegram", "@helper_test")
    ]
    assert len(store.followup_jobs) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    (
        AgentAction(
            kind=ActionKind.SHOW_CHOICES,
            text="Что сейчас важнее всего?",
            choices=(Choice(id="need:housing", label="Жильё / некуда идти"),),
        ),
        AgentAction(kind=ActionKind.REPLY, text="Что сейчас важнее всего?"),
    ),
)
async def test_need_button_after_free_text_opens_aid_options_instead_of_repeating_needs(
    action: AgentAction,
) -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation(action)))

    need_turn = await service.handle_text(identity("мне нужна помощь"))
    offer = await service.handle_callback(identity(), "need:housing")

    assert any(choice.id == "need:housing" for choice in need_turn.choices)
    assert any(choice.id == "aid:hostel_3_nights" for choice in offer.choices)


@pytest.mark.asyncio
async def test_other_telegram_contact_is_collected_as_free_text_after_button_choice() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))
    await service.start(identity())
    await service.handle_callback(identity(), "continue")
    await service.handle_callback(identity(), "need:legal")
    await service.handle_callback(identity(), "aid:legal_consultation")
    ask = await service.handle_callback(identity(), "contact:other_telegram")
    done = await service.handle_text(identity("@another_contact"))

    assert "ник" in ask.text.lower()
    assert store.aid_requests[0].contact_method == "other_telegram"
    assert store.aid_requests[0].contact_value == "@another_contact"
    assert done.choices[-1].id == "human"


@pytest.mark.asyncio
async def test_physical_aid_asks_city_before_contact_and_never_exact_address() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))
    await service.start(identity())
    await service.handle_callback(identity(), "continue")
    await service.handle_callback(identity(), "need:housing")
    location = await service.handle_callback(identity(), "aid:hostel_3_nights")

    assert "город" in location.text.lower()
    assert "адрес куда" not in location.text.lower()
    assert "не нужен" in location.text.lower()


@pytest.mark.asyncio
async def test_city_is_optional_and_is_saved_with_physical_aid_request() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))
    await service.start(identity())
    await service.handle_callback(identity(), "continue")
    await service.handle_callback(identity(), "need:housing")
    await service.handle_callback(identity(), "aid:hostel_3_nights")
    contact = await service.handle_text(identity("Москва"))
    done = await service.handle_callback(identity(), "contact:later")

    assert any(choice.id == "contact:later" for choice in contact.choices)
    assert "запрос сохранён" in done.text.lower()
    assert store.aid_requests[0].city == "Москва"


@pytest.mark.asyncio
async def test_location_opt_out_moves_to_contact_without_address() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))
    await service.start(identity())
    await service.handle_callback(identity(), "continue")
    await service.handle_callback(identity(), "need:housing")
    await service.handle_callback(identity(), "aid:hostel_3_nights")

    contact = await service.handle_callback(identity(), "location:skip")

    assert any(choice.id == "contact:current_telegram" for choice in contact.choices)


@pytest.mark.asyncio
async def test_critical_risk_discards_model_create_aid_action_and_returns_hotline() -> None:
    store = InMemoryConversationStore()
    evaluation = AgentEvaluation(
        risk=RiskAssessment(level=RiskLevel.NONE, detector="model"),
        action=AgentAction(
            kind="create_aid_request", aid_id="food_card", text="Оформляю карточку на продукты"
        ),
        risk_audit={"status": "completed"},
        action_audit={"status": "completed"},
    )
    service = ConversationService(store=store, gateway=FixedGateway(evaluation))

    turn = await service.handle_text(identity("не хочу жить"))

    assert "8-800-2000-122" in turn.text
    assert store.aid_requests == []
    assert store.escalations[-1].level is RiskLevel.CRITICAL


@pytest.mark.asyncio
async def test_direct_human_button_records_escalation_without_stopping_bot() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))

    turn = await service.handle_callback(identity(), "human")

    assert "зову человека" in turn.text.lower()
    assert any(choice.id == "continue_bot" for choice in turn.choices)
    assert store.escalations[-1].level is RiskLevel.HUMAN_REQUESTED


@pytest.mark.asyncio
async def test_unknown_model_result_returns_safe_buttons_without_side_effect() -> None:
    store = InMemoryConversationStore()
    evaluation = AgentEvaluation(
        risk=RiskAssessment(level=RiskLevel.UNKNOWN, detector="model", rationale="timeout"),
        action=AgentAction(kind="create_aid_request", aid_id="food_card", text="Оформляю"),
        risk_audit={"status": "error"},
        action_audit={"status": "completed"},
    )
    service = ConversationService(store=store, gateway=FixedGateway(evaluation))

    turn = await service.handle_text(identity("мне нужна еда"))

    assert {choice.id for choice in turn.choices} == {"human", "continue_bot"}
    assert store.aid_requests == []


@pytest.mark.asyncio
async def test_followup_answer_cancels_the_one_pending_reminder() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))
    record = await store.ensure(identity())
    record.state = "followup_sent"
    store.followup_jobs.append(
        StoredFollowupJob(
            conversation_id=record.id,
            aid_request_id=1,
            due_at=datetime.now(UTC),
            kind="followup_reminder",
        )
    )

    await service.handle_callback(identity(), "followup:better")

    assert store.followup_jobs == []
    assert record.state == "followup_answered"


@pytest.mark.asyncio
async def test_followup_better_opens_level_two_explanation_before_human_handoff() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))
    record = await store.ensure(identity())
    record.state = "followup_sent"

    interest = await service.handle_callback(identity(), "followup:better")
    introduction = await service.handle_callback(identity(), "level2:yes")
    handoff = await service.handle_callback(identity(), "level2:details")

    assert "рада слышать" in interest.text.lower()
    assert "временное жильё" in introduction.text.lower()
    assert {choice.id for choice in introduction.choices} >= {"level2:details", "level2:later", "human"}
    assert "зову человека" in handoff.text.lower()
    assert store.escalations[-1].level is RiskLevel.HUMAN_REQUESTED


@pytest.mark.asyncio
async def test_replayed_contact_callback_cannot_create_a_second_aid_request() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))
    await service.start(identity())
    await service.handle_callback(identity(), "continue")
    await service.handle_callback(identity(), "need:food_money")
    await service.handle_callback(identity(), "aid:food_card")
    await service.handle_callback(identity(), "contact:current_telegram")
    replay = await service.handle_callback(identity(), "contact:current_telegram")

    assert len(store.aid_requests) == 1
    assert any(choice.id == "more_help" for choice in replay.choices)


@pytest.mark.asyncio
async def test_verified_articles_are_passed_to_the_support_agent_context() -> None:
    store = InMemoryConversationStore()
    gateway = CapturingGateway(safe_evaluation())
    service = ConversationService(store=store, gateway=gateway)

    await service.handle_text(identity("У меня забрали документы"))

    assert gateway.contexts
    assert any("Источник:" in item for item in gateway.contexts[0].knowledge)
    assert any("документ" in item.lower() for item in gateway.contexts[0].knowledge)


@pytest.mark.asyncio
async def test_verified_context_uses_the_conversation_not_only_the_last_short_reply() -> None:
    store = InMemoryConversationStore()
    gateway = CapturingGateway(safe_evaluation())
    service = ConversationService(store=store, gateway=gateway)

    await service.handle_text(identity("У меня забрали документы"))
    await service.handle_text(identity("А что дальше?"))

    assert any("документ" in item.lower() for item in gateway.contexts[-1].knowledge)
