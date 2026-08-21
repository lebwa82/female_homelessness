from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.agents import AgentEvaluation
from app.domain import (
    DiagnosticStatus,
    EscalationCause,
    IncomingMessage,
    SafetyDiagnostic,
    SupportDiagnostic,
    SupportOffer,
)
from app.service import ConversationService
from app.store import InMemoryConversationStore, StoredFollowupJob


@dataclass
class FixedGateway:
    evaluation: AgentEvaluation

    async def evaluate(self, context) -> AgentEvaluation:  # type: ignore[no-untyped-def]
        return self.evaluation


@dataclass
class ScriptedGateway:
    evaluations: list[AgentEvaluation]

    async def evaluate(self, context) -> AgentEvaluation:  # type: ignore[no-untyped-def]
        return self.evaluations.pop(0)


class FailOnceHumanEscalationStore(InMemoryConversationStore):
    def __init__(self) -> None:
        super().__init__()
        self._fail_human_escalation = True

    async def create_escalation(self, record, request) -> None:  # type: ignore[no-untyped-def]
        if request.cause is EscalationCause.HUMAN_REQUEST and self._fail_human_escalation:
            self._fail_human_escalation = False
            raise RuntimeError("simulated handoff failure")
        await super().create_escalation(record, request)


class FailAfterHumanEscalationStore(InMemoryConversationStore):
    def __init__(self) -> None:
        super().__init__()
        self._fail_handoff_audit = True

    async def record_action(self, record, kind, status, audit=None) -> None:  # type: ignore[no-untyped-def]
        if kind == "human_handoff" and self._fail_handoff_audit:
            self._fail_handoff_audit = False
            raise RuntimeError("simulated post-escalation failure")
        await super().record_action(record, kind, status, audit)


def identity(text: str = "", message_id: int | None = 303) -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=101,
        chat_id=202,
        username="helper_test",
        text=text,
        message_id=message_id,
    )


def diagnostic_evaluation(
    *,
    draft_text: str = "Я рядом.",
    intent: str = "open_conversation",
    suggested_support: SupportOffer | None = None,
    status: DiagnosticStatus = DiagnosticStatus.COMPLETED,
) -> AgentEvaluation:
    support = (
        SupportDiagnostic(
            intent=intent,
            draft_text=draft_text,
            suggested_support=suggested_support,
        )
        if status is DiagnosticStatus.COMPLETED
        else None
    )
    return AgentEvaluation(
        safety=SafetyDiagnostic(level="none", confidence=1.0, rationale="diagnostic"),
        support=support,
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=status,
        safety_audit={"diagnostic_status": "completed"},
        support_audit={"diagnostic_status": status.value},
    )


@pytest.mark.asyncio
async def test_exact_listen_regression_uses_conversational_draft_and_only_global_human_button() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store,
        gateway=FixedGateway(diagnostic_evaluation(draft_text="Да, я могу вас выслушать.")),
    )

    turn = await service.handle_text(identity("мне просто хочется выговориться — ты можешь меня выслушать?"))

    assert turn.text == "Да, я могу вас выслушать."
    assert [choice.id for choice in turn.choices] == ["human"]
    assert store.escalations == []
    assert store.aid_requests == []
    assert store.conversations[101].state == "open_conversation"


@pytest.mark.asyncio
async def test_model_action_claim_cannot_create_aid_or_menu_without_a_local_signal() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store,
        gateway=FixedGateway(
            diagnostic_evaluation(
                intent="concrete_need",
                draft_text="Я оформила жильё и передала контакт.",
            )
        ),
    )

    turn = await service.handle_text(identity("мне хочется выговориться"))

    assert [choice.id for choice in turn.choices] == ["human"]
    assert turn.text != "Я оформила жильё и передала контакт."
    assert store.aid_requests == []
    assert store.escalations == []


@pytest.mark.asyncio
async def test_explicit_human_request_wins_during_a_workflow_and_invalid_diagnostics() -> None:
    store = InMemoryConversationStore()
    record = await store.ensure(identity())
    record.state = "collecting_contact_value"
    service = ConversationService(
        store=store,
        gateway=FixedGateway(diagnostic_evaluation(status=DiagnosticStatus.INVALID)),
    )

    turn = await service.handle_text(identity("Позовите человека", message_id=304))

    assert [choice.id for choice in turn.choices] == ["continue_bot", "human"]
    assert [item.cause for item in store.escalations] == [EscalationCause.HUMAN_REQUEST]
    assert record.state == "open_conversation"


@pytest.mark.asyncio
async def test_generic_and_concrete_aid_routes_are_backend_owned() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))

    generic = await service.handle_text(identity("какую помощь можно получить", message_id=305))
    concrete = await service.handle_text(identity("мне нужны продукты", message_id=306))

    assert any(choice.id == "need:housing" for choice in generic.choices)
    assert store.conversations[101].state == "choosing_aid"
    assert any(choice.id == "aid:food_card" for choice in concrete.choices)
    assert not any(
        choice.id.startswith("need:") and choice.id != "need:other" for choice in concrete.choices
    )


@pytest.mark.asyncio
async def test_pending_psychologist_offer_needs_a_later_verified_signal_before_button_or_workflow() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store,
        gateway=ScriptedGateway(
            [
                diagnostic_evaluation(
                    draft_text="Я рядом. Могу рассказать о психологе.",
                    suggested_support=SupportOffer.PSYCHOLOGIST,
                ),
                diagnostic_evaluation(draft_text="Расскажу, как устроена поддержка."),
            ]
        ),
    )

    offer = await service.handle_text(identity("мне очень тяжело", message_id=307))
    assert store.conversations[101].pending_offer == "psychologist"
    interest = await service.handle_text(identity("расскажите, пожалуйста", message_id=308))
    contact = await service.handle_callback(identity(message_id=309), "support:psychologist")

    assert [choice.id for choice in offer.choices] == ["human"]
    assert [choice.id for choice in interest.choices] == ["support:psychologist", "human"]
    assert any(choice.id == "contact:current_telegram" for choice in contact.choices)
    assert store.conversations[101].state == "collecting_contact_method"
    assert store.conversations[101].pending_offer is None


@pytest.mark.asyncio
async def test_pending_psychologist_offer_expires_after_an_unrelated_reply() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store,
        gateway=ScriptedGateway(
            [
                diagnostic_evaluation(
                    draft_text="Я рядом. Могу рассказать о психологе.",
                    suggested_support=SupportOffer.PSYCHOLOGIST,
                ),
                diagnostic_evaluation(draft_text="Хорошо, давайте разберёмся."),
            ]
        ),
    )

    await service.handle_text(identity("мне трудно", message_id=313))
    turn = await service.handle_text(identity("да, хочу продукты", message_id=314))

    assert [choice.id for choice in turn.choices] == ["human"]
    assert store.conversations[101].state == "open_conversation"
    assert store.conversations[101].pending_offer is None
    assert store.aid_requests == []


@pytest.mark.asyncio
async def test_concern_and_critical_local_signals_record_only_their_deterministic_escalations() -> None:
    concern_store = InMemoryConversationStore()
    concern = ConversationService(store=concern_store, gateway=FixedGateway(diagnostic_evaluation()))
    concern_turn = await concern.handle_text(identity("я боюсь возвращаться домой", message_id=310))

    critical_store = InMemoryConversationStore()
    critical = ConversationService(
        store=critical_store,
        gateway=FixedGateway(diagnostic_evaluation(draft_text="Я оформила заявку.")),
    )
    critical_turn = await critical.handle_text(identity("не хочу жить", message_id=311))

    assert [choice.id for choice in concern_turn.choices] == ["human"]
    assert concern_store.escalations[-1].cause is EscalationCause.SAFETY
    assert "8-800-2000-122" in critical_turn.text
    assert critical_store.escalations[-1].cause is EscalationCause.SAFETY


@pytest.mark.asyncio
async def test_policy_audit_has_v2_allow_list_and_never_raw_turn_data() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))

    turn = await service.handle_text(identity("мне хочется выговориться private-marker-7F3D", message_id=312))

    _, kind, _, audit = store.actions[-1]
    assert kind == "policy_decision"
    assert set(audit) == {
        "policy_version",
        "matcher_version",
        "state_before",
        "state_after",
        "local_risk",
        "safety_label",
        "safety_status",
        "support_intent",
        "support_status",
        "rule_ids",
        "choice_set",
        "rendered_callback_ids",
        "effect",
        "side_effects",
        "fallback_reason",
    }
    assert audit["rendered_callback_ids"] == [choice.id for choice in turn.choices]
    assert "private-marker-7F3D" not in repr(audit)
    assert "next_action" not in audit


@pytest.mark.asyncio
async def test_human_callback_remains_idempotent_per_originating_message() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))

    await service.handle_callback(identity(message_id=410), "human")
    await service.handle_callback(identity(message_id=410), "human")
    await service.handle_callback(identity(message_id=411), "human")

    assert [item.cause for item in store.escalations] == [
        EscalationCause.HUMAN_REQUEST,
        EscalationCause.HUMAN_REQUEST,
    ]


@pytest.mark.asyncio
async def test_callback_food_workflow_creates_one_request_and_followup() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))

    await service.start(identity(message_id=501))
    await service.handle_callback(identity(message_id=502), "continue")
    await service.handle_callback(identity(message_id=503), "need:food_money")
    contact = await service.handle_callback(identity(message_id=504), "aid:food_card")
    done = await service.handle_callback(identity(message_id=505), "contact:current_telegram")

    assert any(choice.id == "contact:current_telegram" for choice in contact.choices)
    assert any(choice.id == "more_help" for choice in done.choices)
    assert [(item.aid_id, item.contact_method, item.contact_value) for item in store.aid_requests] == [
        ("food_card", "current_telegram", "@helper_test")
    ]
    assert len(store.followup_jobs) == 1


@pytest.mark.asyncio
async def test_physical_aid_location_and_contact_workflow_preserves_city_without_address() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))

    await service.start(identity(message_id=510))
    await service.handle_callback(identity(message_id=511), "continue")
    await service.handle_callback(identity(message_id=512), "need:housing")
    location = await service.handle_callback(identity(message_id=513), "aid:hostel_3_nights")
    contact = await service.handle_text(identity("Москва", message_id=514))
    await service.handle_callback(identity(message_id=515), "contact:later")

    assert "город" in location.text.lower()
    assert "адрес куда" not in location.text.lower()
    assert any(choice.id == "contact:later" for choice in contact.choices)
    assert store.aid_requests[0].city == "Москва"


@pytest.mark.asyncio
async def test_other_telegram_contact_value_completes_existing_workflow_once() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))

    await service.start(identity(message_id=520))
    await service.handle_callback(identity(message_id=521), "continue")
    await service.handle_callback(identity(message_id=522), "need:legal")
    await service.handle_callback(identity(message_id=523), "aid:legal_consultation")
    ask = await service.handle_callback(identity(message_id=524), "contact:other_telegram")
    await service.handle_text(identity("@another_contact", message_id=525))

    assert "ник" in ask.text.lower()
    assert [(item.aid_id, item.contact_method, item.contact_value) for item in store.aid_requests] == [
        ("legal_consultation", "other_telegram", "@another_contact")
    ]


@pytest.mark.asyncio
async def test_stale_callbacks_replay_active_contact_workflow_without_need_menu() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))

    await service.start(identity(message_id=530))
    await service.handle_callback(identity(message_id=531), "continue")
    await service.handle_callback(identity(message_id=532), "need:legal")
    await service.handle_callback(identity(message_id=533), "aid:legal_consultation")
    turn = await service.handle_callback(identity(message_id=534), "continue")

    assert store.conversations[101].state == "collecting_contact_method"
    assert store.conversations[101].pending_aid_id == "legal_consultation"
    assert any(choice.id == "contact:current_telegram" for choice in turn.choices)
    assert not any(choice.id.startswith("need:") for choice in turn.choices)


@pytest.mark.asyncio
async def test_replayed_contact_callback_cannot_create_a_second_aid_request() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))

    await service.start(identity(message_id=540))
    await service.handle_callback(identity(message_id=541), "continue")
    await service.handle_callback(identity(message_id=542), "need:food_money")
    await service.handle_callback(identity(message_id=543), "aid:food_card")
    incoming = identity(message_id=544)
    await service.handle_callback(incoming, "contact:current_telegram")
    replay = await service.handle_callback(incoming, "contact:current_telegram")

    assert len(store.aid_requests) == 1
    assert any(choice.id == "more_help" for choice in replay.choices)


@pytest.mark.asyncio
async def test_followup_text_completes_reminder_before_returning_to_open_conversation() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))
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

    await service.handle_text(identity("Спасибо", message_id=550))

    assert store.followup_jobs == []
    assert record.state == "open_conversation"
    assert store.actions[-1][3]["side_effects"] == ["complete_followup"]


@pytest.mark.asyncio
async def test_human_callback_retries_after_pre_effect_failure_then_replays_idempotently() -> None:
    store = FailOnceHumanEscalationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))
    incoming = identity(message_id=560)

    with pytest.raises(RuntimeError, match="simulated handoff failure"):
        await service.handle_callback(incoming, "human")

    successful = await service.handle_callback(incoming, "human")
    replay = await service.handle_callback(incoming, "human")

    assert len(store.escalations) == 1
    assert [choice.id for choice in successful.choices] == ["continue_bot", "human"]
    assert [choice.id for choice in replay.choices] == ["continue_bot", "human"]


@pytest.mark.asyncio
async def test_human_callback_retries_after_post_effect_failure_without_duplicate_escalation() -> None:
    store = FailAfterHumanEscalationStore()
    service = ConversationService(store=store, gateway=FixedGateway(diagnostic_evaluation()))
    incoming = identity(message_id=570)

    with pytest.raises(RuntimeError, match="simulated post-escalation failure"):
        await service.handle_callback(incoming, "human")

    successful = await service.handle_callback(incoming, "human")
    replay = await service.handle_callback(incoming, "human")

    assert len(store.escalations) == 1
    assert store.escalations[0].request.request_key is not None
    assert [choice.id for choice in successful.choices] == ["continue_bot", "human"]
    assert [choice.id for choice in replay.choices] == ["continue_bot", "human"]
