"""Synthetic historical surveys preserve controls without asserting certificate validity."""

from types import SimpleNamespace

import pytest

from app.scenario import CONSULTATION_IDS, SCREENS, ScenarioFlow, render
from app.store import ConversationRecord, InMemoryConversationStore

CERTIFICATE_SUBJECTS = (
    ("food_card", "продукты"),
    ("medicine_card", "лекарства"),
    ("hostel_3_nights", "проживание"),
    ("children_card", "детские товары"),
)
NEUTRAL_ANSWER = (
    "Спасибо за ответ. Если с сертификатом возникли сложности, "
    "можно написать человеку из команды."
)
GARBAGE = {
    "expires_at": "SYNTHETIC-EXPIRY", "expiry_date": "SYNTHETIC-DATE",
    "code": "SYNTHETIC-CODE",
}


@pytest.mark.parametrize(("aid_id", "subject"), CERTIFICATE_SUBJECTS)
@pytest.mark.parametrize("screen", ("s5", "s56", "s59"))
def test_historical_copy_keeps_controls_and_does_not_expose_certificate_fields(aid_id, subject,
                                                                            screen):
    context = {"aid_id": aid_id, **GARBAGE}
    ordinary = render(screen, context=context, revision=7)
    historical = render(screen, context={**context, "historical_survey": True}, revision=7)

    if screen == "s5":
        assert historical.text == f"Удалось ли вам воспользоваться сертификатом на {subject}?"
    elif screen == "s56":
        assert historical.text == NEUTRAL_ANSWER
    else:
        assert historical.text == ordinary.text
    assert historical.choices == ordinary.choices
    assert all(value not in historical.text for value in GARBAGE.values())


@pytest.mark.parametrize("flag", (False, None, "true", 1))
@pytest.mark.parametrize("screen", ("s5", "s56"))
def test_historical_copy_requires_boolean_true(flag, screen):
    context = {"aid_id": "food_card", "expiry_date": "SYNTHETIC-DATE"}
    assert render(screen, context={**context, "historical_survey": flag}) == render(
        screen, context=context,
    )


@pytest.mark.parametrize("aid_id", CONSULTATION_IDS)
@pytest.mark.parametrize("screen", ("s5", "s56", "s59"))
def test_historical_flag_does_not_change_consultation_copy(aid_id, screen):
    context = {"aid_id": aid_id}
    assert render(screen, context={**context, "historical_survey": True}) == render(
        screen, context=context,
    )


def test_ordinary_certificate_copy_keeps_original_timing_and_expiry():
    context = {"aid_id": "food_card", "expiry_date": "SYNTHETIC-DATE"}
    assert render("s5", context=context).text == SCREENS["s5"]["texts"][0]
    assert render("s56", context=context).text == SCREENS["s56"]["texts"][0].replace(
        "[ДАТА]", "SYNTHETIC-DATE",
    )


@pytest.fixture
def flow():
    store = InMemoryConversationStore()
    record = ConversationRecord(1, "chatwoot", 1, 1, None)
    return ScenarioFlow(SimpleNamespace(store=store)), record, store


@pytest.mark.parametrize(("aid_id", "subject"), CERTIFICATE_SUBJECTS)
@pytest.mark.parametrize("screen", ("s5", "s59"))
async def test_historical_flag_survives_not_yet_answer(flow, aid_id, subject, screen):
    scenario, record, store = flow
    survey = await scenario.show(record, screen, aid_id=aid_id, job_key="synthetic-survey",
                                 historical_survey=True, **GARBAGE)
    answer = next(choice for choice in survey.choices if choice.label == "Пока нет")

    result = await scenario.callback(record, answer.id, "synthetic-answer")

    assert result.text == NEUTRAL_ANSWER
    assert record.scenario["screen"] == "s56"
    assert record.scenario["historical_survey"] is True
    assert record.scenario["job_key"] == "synthetic-survey"
    assert [action[3]["kind"] for action in store.actions] == ["survey_answer"]


@pytest.mark.parametrize("screen", ("s5", "s59"))
async def test_new_followup_without_flag_does_not_inherit_historical_copy(flow, screen):
    scenario, record, _ = flow
    await scenario.show(record, "s5", aid_id="food_card", job_key="synthetic-old",
                        historical_survey=True)

    result = await scenario.show(record, screen, aid_id="medicine_card", job_key="synthetic-new")

    assert "historical_survey" not in record.scenario
    assert result == render(screen, context={"aid_id": "medicine_card"},
                            revision=record.navigation.get("revision", 0))


@pytest.mark.parametrize("screen", ("s5", "s59"))
@pytest.mark.parametrize("flag", (True, False))
async def test_new_followup_keeps_its_explicit_historical_flag(flow, screen, flag):
    scenario, record, _ = flow
    await scenario.show(record, "s5", aid_id="food_card", job_key="synthetic-old",
                        historical_survey=True)
    await scenario.show(record, screen, aid_id="food_card", job_key="synthetic-new",
                        historical_survey=flag)

    assert record.scenario["historical_survey"] is flag


@pytest.mark.parametrize("screen", ("s5", "s59"))
async def test_same_followup_keeps_historical_flag(flow, screen):
    scenario, record, _ = flow
    await scenario.show(record, "s5", aid_id="food_card", job_key="synthetic-same",
                        historical_survey=True)
    await scenario.show(record, screen, job_key="synthetic-same")

    assert record.scenario["historical_survey"] is True
