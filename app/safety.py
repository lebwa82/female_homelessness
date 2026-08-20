from __future__ import annotations

import re

from app.domain import CrisisAssessment, Risk, RiskAssessment, RiskLevel

_LOCAL_PATTERNS: tuple[tuple[RiskLevel, str, tuple[str, ...]], ...] = (
    (
        RiskLevel.CRITICAL,
        "suicide",
        (
            r"\b(суицид|покончить с собой|не хочу жить|хочу исчезнуть|нет сил жить)\b",
            r"\b(не вижу смысла|не могу больше|всё бесполезно)\b",
        ),
    ),
    (
        RiskLevel.CRITICAL,
        "violence_now",
        (
            r"\b(меня убивают|хочет убить|убью|убить)\b",
            r"\b(сейчас.*(бь[её]т|избивает)|насилие прямо сейчас)\b",
            r"\b(реб[её]нок в опасности|дети на улице)\b",
        ),
    ),
    (
        RiskLevel.URGENT,
        "acute_homelessness",
        (
            r"\b(сегодня.*(негде ночевать|некуда идти)|ночую на улице|улица этой ночью)\b",
            r"\b(выгнали|выселили|нет жилья)\b",
        ),
    ),
    (
        RiskLevel.HUMAN_REQUESTED,
        "human_requested",
        (
            r"\b(хочу поговорить с человеком|нужен живой человек|позвоните мне)\b",
            r"\b(можно поговорить голосом|нужен оператор|позовите специалист)\b",
        ),
    ),
    (
        RiskLevel.CONCERN,
        "fear_or_threat",
        (
            r"\b(боюсь|страшно|угрожает|преследует|заперта|не могу уйти|держит)\b",
            r"\b(насилие|документы забрали|нет где ночевать)\b",
        ),
    ),
)

_PRECEDENCE = {
    RiskLevel.NONE: 0,
    RiskLevel.CONCERN: 1,
    RiskLevel.HUMAN_REQUESTED: 2,
    RiskLevel.URGENT: 3,
    RiskLevel.UNKNOWN: 4,
    RiskLevel.CRITICAL: 5,
}


def assess_local_risk(text: str) -> RiskAssessment:
    normalized = text.lower().strip()
    matches: list[tuple[RiskLevel, str]] = []
    for level, category, patterns in _LOCAL_PATTERNS:
        if any(re.search(pattern, normalized) for pattern in patterns):
            matches.append((level, category))
    if not matches:
        return RiskAssessment(level=RiskLevel.NONE, detector="local")
    level = max((item[0] for item in matches), key=_PRECEDENCE.__getitem__)
    return RiskAssessment(
        level=level,
        categories=tuple(category for _, category in matches),
        confidence=1.0,
        rationale="local high-precision signal",
        detector="local",
    )


def merge_risk(*assessments: RiskAssessment) -> RiskAssessment:
    if not assessments:
        return RiskAssessment(level=RiskLevel.UNKNOWN, detector="merged", rationale="no assessment")
    level = max((assessment.level for assessment in assessments), key=_PRECEDENCE.__getitem__)
    categories = tuple(
        dict.fromkeys(category for assessment in assessments for category in assessment.categories)
    )
    confidence = max(assessment.confidence for assessment in assessments if assessment.level is level)
    return RiskAssessment(
        level=level,
        categories=categories,
        confidence=confidence,
        rationale="; ".join(assessment.rationale for assessment in assessments if assessment.rationale)[:240],
        detector="merged",
    )


def assess_crisis(text: str) -> CrisisAssessment:
    """Compatibility adapter for the first prototype's callers."""
    assessment = assess_local_risk(text)
    if assessment.level is RiskLevel.CRITICAL:
        return CrisisAssessment(Risk.ACUTE, assessment.rationale)
    if assessment.level in {RiskLevel.CONCERN, RiskLevel.URGENT, RiskLevel.HUMAN_REQUESTED}:
        return CrisisAssessment(Risk.CONCERN, assessment.rationale)
    return CrisisAssessment(Risk.NONE)

