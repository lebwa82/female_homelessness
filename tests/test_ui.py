from app.bot import render_keyboard
from app.domain import AgentTurn, Choice


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
