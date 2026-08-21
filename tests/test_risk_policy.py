import pytest

from app.domain import RiskAssessment, RiskLevel
from app.safety import assess_local_risk, merge_risk


@pytest.mark.parametrize(
    ("text", "level"),
    [
        ("я хочу покончить с собой", RiskLevel.CRITICAL),
        ("он сейчас меня бьёт", RiskLevel.CRITICAL),
        ("сегодня мне негде ночевать", RiskLevel.URGENT),
        ("боюсь возвращаться", RiskLevel.CONCERN),
        ("хочу поговорить с человеком", RiskLevel.NONE),
        ("мне нужны продукты", RiskLevel.NONE),
    ],
)
def test_local_red_flags(text: str, level: RiskLevel) -> None:
    assert assess_local_risk(text).level is level


def test_merge_keeps_highest_safety_level_and_categories() -> None:
    local = RiskAssessment(
        level=RiskLevel.CONCERN,
        categories=("fear",),
        confidence=1.0,
        rationale="local",
        detector="local",
    )
    model = RiskAssessment(
        level=RiskLevel.URGENT,
        categories=("acute_homelessness",),
        confidence=0.89,
        rationale="model",
        detector="model",
    )

    merged = merge_risk(local, model)

    assert merged.level is RiskLevel.URGENT
    assert merged.categories == ("fear", "acute_homelessness")
    assert merged.detector == "merged"


def test_unknown_model_result_blocks_side_effects_even_when_local_is_safe() -> None:
    local = assess_local_risk("нужна еда")
    unknown = RiskAssessment(level=RiskLevel.UNKNOWN, detector="model", rationale="timeout")

    assert merge_risk(local, unknown).level is RiskLevel.UNKNOWN


def test_request_to_be_heard_is_not_a_safety_risk() -> None:
    assert assess_local_risk("мне просто хочется выговориться").level is RiskLevel.NONE
    assert assess_local_risk("хочу поговорить с человеком").level is RiskLevel.NONE
