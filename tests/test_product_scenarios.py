from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from app.agents import AgentEvaluation
from app.domain import (
    EscalationCause,
    IncomingMessage,
    RiskAssessment,
    RiskLevel,
    SupportPlan,
)
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


@dataclass
class ScriptedGateway:
    evaluations: list[AgentEvaluation]

    async def evaluate(self, context) -> AgentEvaluation:  # type: ignore[no-untyped-def]
        return self.evaluations.pop(0)


def identity(text: str = "") -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=101,
        chat_id=202,
        username="helper_test",
        text=text,
        message_id=303,
    )


def safe_evaluation(plan: SupportPlan | None = None) -> AgentEvaluation:
    return AgentEvaluation(
        risk=RiskAssessment(level=RiskLevel.NONE, detector="model"),
        plan=plan
        or SupportPlan(
            intent="open_conversation",
            next_action="continue_conversation",
            text="Что сейчас важнее всего?",
        ),
        risk_audit={"status": "completed"},
        support_audit={"status": "completed"},
    )


def scripted_service(*plans: SupportPlan) -> tuple[ConversationService, InMemoryConversationStore, ScriptedGateway]:
    store = InMemoryConversationStore()
    gateway = ScriptedGateway([safe_evaluation(plan) for plan in plans])
    return ConversationService(store=store, gateway=gateway), store, gateway


def psychologist_offer_plan() -> SupportPlan:
    return SupportPlan(
        intent="open_conversation",
        next_action="continue_conversation",
        text="Я рядом. Если вам это откликается, могу рассказать о поддержке психолога.",
        offered_support="psychologist",
    )


def considering_psychologist_plan() -> SupportPlan:
    return SupportPlan(
        intent="psychologist_considering",
        next_action="clarify",
        text="Могу помочь начать запрос к психологу, если вы этого хотите.",
    )


def psychologist_request_plan() -> SupportPlan:
    return SupportPlan(
        intent="psychologist_request",
        next_action="start_psychologist_request",
        text="Хорошо, начнём запрос к психологу.",
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
async def test_offer_aid_plan_opens_aid_options() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store,
        gateway=FixedGateway(
            safe_evaluation(
                SupportPlan(
                    intent="concrete_need",
                    next_action="offer_aid",
                    text="Можно посмотреть варианты жилья.",
                    need="housing",
                )
            )
        ),
    )

    offer = await service.handle_text(identity("мне нужна помощь"))

    assert any(choice.id == "aid:hostel_3_nights" for choice in offer.choices)


@pytest.mark.asyncio
async def test_request_to_be_heard_continues_bot_without_menu_or_handoff() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store,
        gateway=FixedGateway(
            safe_evaluation(
                SupportPlan(
                    intent="open_conversation",
                    next_action="continue_conversation",
                    text="Да. Я здесь и могу вас выслушать.",
                    choice_set="none",
                )
            )
        ),
    )

    turn = await service.handle_text(identity("мне просто хочется выговориться"))

    assert [choice.id for choice in turn.choices] == ["human"]
    assert store.escalations == []
    assert store.conversations[101].state == "open_conversation"


@pytest.mark.asyncio
async def test_continue_after_handoff_returns_to_open_conversation() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))

    await service.handle_callback(identity(), "human")
    turn = await service.handle_callback(identity(), "continue_bot")

    assert [choice.id for choice in turn.choices] == ["human"]
    assert not any(choice.id.startswith("need:") for choice in turn.choices)
    assert store.conversations[101].state == "open_conversation"


@pytest.mark.asyncio
async def test_support_need_callback_returns_to_open_conversation_without_a_catalog() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))

    turn = await service.handle_callback(identity(), "need:support")

    assert turn.text == "Я здесь и могу вас выслушать. Можно написать, что сейчас особенно важно."
    assert [choice.id for choice in turn.choices] == ["human"]
    assert store.conversations[101].state == "open_conversation"


@pytest.mark.asyncio
async def test_psychologist_interest_after_offer_collects_contact() -> None:
    service, store, _ = scripted_service(psychologist_offer_plan(), considering_psychologist_plan())

    await service.handle_text(identity("мне очень тяжело"))
    offer = await service.handle_text(identity("расскажите о психологе"))
    contact = await service.handle_callback(identity(), "support:psychologist")
    await service.handle_callback(identity(), "contact:current_telegram")

    assert [choice.id for choice in offer.choices] == ["support:psychologist", "human"]
    assert any(choice.id == "contact:current_telegram" for choice in contact.choices)
    assert store.aid_requests[-1].aid_id == "psychologist_3_sessions"


@pytest.mark.asyncio
async def test_direct_psychologist_request_collects_contact_without_a_prior_offer() -> None:
    service, store, _ = scripted_service(psychologist_request_plan())

    contact = await service.handle_text(identity("хочу поговорить с психологом"))

    assert [choice.id for choice in contact.choices] == [
        "contact:current_telegram",
        "contact:other_telegram",
        "contact:phone",
        "contact:email",
        "contact:later",
        "human",
    ]
    assert store.conversations[101].pending_aid_id == "psychologist_3_sessions"
    assert store.conversations[101].state == "collecting_contact_method"


@pytest.mark.asyncio
async def test_psychologist_callback_without_a_recorded_offer_returns_open_conversation() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))

    turn = await service.handle_callback(identity(), "support:psychologist")

    assert [choice.id for choice in turn.choices] == ["human"]
    assert store.aid_requests == []
    assert store.conversations[101].state == "open_conversation"


@pytest.mark.asyncio
async def test_only_open_conversation_can_record_a_psychologist_offer() -> None:
    service, store, _ = scripted_service(
        SupportPlan(
            intent="verified_information",
            next_action="provide_verified_info",
            text="Вот проверенная информация.",
            offered_support="psychologist",
        ),
        considering_psychologist_plan(),
    )

    await service.handle_text(identity("какая помощь бывает"))
    turn = await service.handle_text(identity("расскажите о психологе"))

    assert store.conversations[101].pending_offer is None
    assert [choice.id for choice in turn.choices] == ["human"]


@pytest.mark.asyncio
async def test_unknown_callback_returns_open_conversation_instead_of_need_menu() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))

    turn = await service.handle_callback(identity(), "unknown:old-button")

    assert [choice.id for choice in turn.choices] == ["human"]
    assert store.conversations[101].state == "open_conversation"


@pytest.mark.asyncio
async def test_policy_audit_contains_only_resolved_literals_after_execution() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))

    turn = await service.handle_text(identity("мне просто хочется выговориться"))

    _, kind, _, audit = store.actions[-1]
    assert kind == "policy_decision"
    assert audit == {
        "state_before": "greeting",
        "state_after": "open_conversation",
        "risk": "none",
        "intent": "open_conversation",
        "next_action": "continue_conversation",
        "choice_set": "none",
        "rendered_callback_ids": [choice.id for choice in turn.choices],
        "effect": "none",
        "fallback_reason": None,
    }
    assert "text" not in audit
    assert "prompt" not in audit
    assert "history" not in audit


@pytest.mark.asyncio
async def test_explicit_human_request_plan_starts_handoff() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store,
        gateway=FixedGateway(
            safe_evaluation(
                SupportPlan(
                    intent="explicit_human_request",
                    next_action="request_human",
                    text="Позову человека.",
                )
            )
        ),
    )

    turn = await service.handle_text(identity("Позовите человека"))

    assert "зову человека" in turn.text.lower()
    assert store.escalations[-1].cause is EscalationCause.HUMAN_REQUEST
    assert store.escalations[-1].level is None


@pytest.mark.asyncio
@pytest.mark.parametrize("level", [RiskLevel.CONCERN, RiskLevel.URGENT])
async def test_noncritical_safety_is_recorded_while_a_valid_plan_continues(level: RiskLevel) -> None:
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store,
        gateway=FixedGateway(
            AgentEvaluation(
                risk=RiskAssessment(level=level, detector="model"),
                plan=SupportPlan(
                    intent="open_conversation",
                    next_action="continue_conversation",
                    text="Я рядом и готова продолжить.",
                ),
                risk_audit={"status": "completed"},
                support_audit={"status": "completed"},
            )
        ),
    )

    turn = await service.handle_text(identity("мне непросто"))

    assert turn.text == "Я рядом и готова продолжить."
    assert [choice.id for choice in turn.choices] == ["human"]
    assert store.escalations[-1].cause is EscalationCause.SAFETY
    assert store.escalations[-1].level is level


@pytest.mark.asyncio
async def test_close_plan_closes_conversation() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store,
        gateway=FixedGateway(
            safe_evaluation(SupportPlan(intent="close", next_action="close", text="До свидания."))
        ),
    )

    turn = await service.handle_text(identity("Спасибо"))
    record = await store.ensure(identity())

    assert turn.text == "До свидания."
    assert record.state == "closed"


@pytest.mark.asyncio
async def test_existing_need_button_from_legacy_greeting_state_opens_aid_options() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(safe_evaluation()))
    record = await store.ensure(identity())
    record.state = "greeting"

    offer = await service.handle_callback(identity(), "need:housing")

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
        plan=SupportPlan(
            intent="aid_interest",
            next_action="offer_aid",
            text="Оформляю карточку на продукты",
            need="food_money",
        ),
        risk_audit={"status": "completed"},
        support_audit={"status": "completed"},
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
    assert store.escalations[-1].cause is EscalationCause.HUMAN_REQUEST
    assert store.escalations[-1].level is None


@pytest.mark.asyncio
async def test_unknown_model_result_returns_safe_buttons_without_side_effect() -> None:
    store = InMemoryConversationStore()
    evaluation = AgentEvaluation(
        risk=RiskAssessment(level=RiskLevel.UNKNOWN, detector="model", rationale="timeout"),
        plan=SupportPlan(
            intent="aid_interest",
            next_action="offer_aid",
            text="Оформляю",
            need="food_money",
        ),
        risk_audit={"status": "error"},
        support_audit={"status": "completed"},
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
    assert store.escalations[-1].cause is EscalationCause.LEVEL_TWO_SUPPORT
    assert store.escalations[-1].level is None


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
