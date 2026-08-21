from __future__ import annotations

from app.domain import (
    CrisisAssessment,
    DeterministicSignals,
    HardSignalKind,
    Risk,
    RiskAssessment,
    RiskLevel,
)
from app.signals import extract_signals

_SIGNAL_RISK: dict[HardSignalKind, tuple[RiskLevel, str]] = {
    HardSignalKind.SUICIDE_OR_SELF_HARM: (RiskLevel.CRITICAL, "suicide"),
    HardSignalKind.VIOLENCE_OR_THREAT_NOW: (RiskLevel.CRITICAL, "violence_now"),
    HardSignalKind.URGENT_SHELTER: (RiskLevel.URGENT, "acute_homelessness"),
    HardSignalKind.SAFETY_CONCERN: (RiskLevel.CONCERN, "fear_or_threat"),
}

_PRECEDENCE = {
    RiskLevel.NONE: 0,
    RiskLevel.CONCERN: 1,
    RiskLevel.URGENT: 2,
    RiskLevel.UNKNOWN: 3,
    RiskLevel.CRITICAL: 4,
}


def assess_local_risk(text: str) -> RiskAssessment:
    return assess_local_risk_from_signals(extract_signals(text))


def assess_local_risk_from_signals(signals: DeterministicSignals) -> RiskAssessment:
    """Assess only reviewed local matches; provider health is a separate concern."""
    matches = [
        _SIGNAL_RISK[match.kind]
        for match in signals.matches
        if match.kind in _SIGNAL_RISK
    ]
    if not matches:
        return RiskAssessment(level=RiskLevel.NONE, detector="local-signals")
    level = max((item[0] for item in matches), key=_PRECEDENCE.__getitem__)
    return RiskAssessment(
        level=level,
        categories=tuple(dict.fromkeys(category for _, category in matches)),
        confidence=1.0,
        rationale="deterministic local signal",
        detector="local-signals",
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
    if assessment.level in {RiskLevel.CONCERN, RiskLevel.URGENT}:
        return CrisisAssessment(Risk.CONCERN, assessment.rationale)
    return CrisisAssessment(Risk.NONE)
