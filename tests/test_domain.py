import pytest
from pydantic import ValidationError

from app.catalog import available_aid_for_need, get_aid_item
from app.domain import (
    ActionKind,
    AgentAction,
    AgentTurn,
    Choice,
    NeedKind,
    RiskAssessment,
    RiskLevel,
)


def test_housing_offer_is_bounded_to_catalog() -> None:
    assert [item.id for item in available_aid_for_need(NeedKind.HOUSING)] == [
        "hostel_3_nights",
        "peer_consultation",
        "legal_consultation",
    ]


def test_every_need_offer_contains_three_catalog_items() -> None:
    for need in NeedKind:
        if need is NeedKind.OTHER:
            continue
        offers = available_aid_for_need(need)
        assert len(offers) == 3
        assert all(get_aid_item(item.id) == item for item in offers)


def test_agent_action_rejects_more_than_four_choices() -> None:
    with pytest.raises(ValidationError):
        AgentAction(
            kind=ActionKind.SHOW_CHOICES,
            text="Выберите",
            choices=tuple(Choice(id=str(i), label=str(i)) for i in range(5)),
        )


def test_agent_action_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AgentAction.model_validate({"kind": "reply", "text": "Рядом", "invented": True})


def test_agent_turn_always_appends_human_choice_once() -> None:
    turn = AgentTurn(
        text="Что сейчас важнее?",
        choices=(Choice(id="need:food", label="Еда или деньги"),),
    ).with_human_choice()
    repeated = turn.with_human_choice()

    assert [choice.id for choice in repeated.choices] == ["need:food", "human"]


def test_risk_assessment_has_conservative_levels() -> None:
    assessment = RiskAssessment(
        level=RiskLevel.URGENT,
        categories=("acute_homelessness",),
        confidence=0.91,
        rationale="Сегодня негде ночевать",
    )

    assert assessment.level is RiskLevel.URGENT
    assert assessment.confidence == 0.91

