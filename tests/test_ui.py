import pytest

from app.bot import render_keyboard
from app.domain import AgentTurn, Choice, ChoiceSet
from app.ui import choices_for


def test_none_has_no_contextual_buttons_but_renders_the_global_human_affordance() -> None:
    from app.ui import contextual_choices_for

    assert contextual_choices_for(ChoiceSet.NONE) == ()
    assert [choice.id for choice in choices_for(ChoiceSet.NONE)] == ["human"]


def test_all_concrete_choices_render_as_stable_inline_callbacks() -> None:
    turn = AgentTurn(
        text="Что выбрать?",
        choices=(
            Choice(id="aid:food_card", label="Карточка на продукты"),
            Choice(id="human", label="Поговорить с живым человеком"),
        ),
    )

    markup = render_keyboard(turn)

    assert markup is not None
    assert [button.callback_data for row in markup.inline_keyboard for button in row] == [
        "aid:food_card",
        "human",
    ]


def test_no_choices_means_no_keyboard() -> None:
    assert render_keyboard(AgentTurn(text="Этот чат открыт.")) is None


@pytest.mark.parametrize(
    ("choice_set", "expected_ids"),
    [
        (ChoiceSet.NONE, ["human"]),
        (ChoiceSet.SAFE_CONTINUE, ["continue_bot", "human"]),
        (ChoiceSet.PSYCHOLOGIST_INTEREST, ["support:psychologist", "human"]),
        (
            ChoiceSet.CONTACT_METHODS,
            [
                "contact:current_telegram",
                "contact:other_telegram",
                "contact:phone",
                "contact:email",
                "contact:later",
                "human",
            ],
        ),
    ],
)
def test_choice_registry_renders_stable_callback_ids(
    choice_set: ChoiceSet, expected_ids: list[str]
) -> None:
    assert [choice.id for choice in choices_for(choice_set)] == expected_ids


def test_catalog_choice_set_keeps_only_known_catalog_items() -> None:
    choices = choices_for(
        ChoiceSet.AID_CATALOG,
        ("food_card", "not-a-catalog-item", "legal_consultation"),
    )

    assert [(choice.id, choice.label) for choice in choices] == [
        ("aid:food_card", "Карточка на продукты"),
        ("aid:legal_consultation", "Юрист или адвокат"),
        ("human", "Поговорить с живым человеком"),
    ]
