from __future__ import annotations

import re

from app.domain import CrisisAssessment, Risk

# This is a deliberately conservative fallback, not the only safety layer.
# Production must add a tested, Russian-language LLM classifier and 24/7 response SLA.
ACUTE_PATTERNS = [
    r"\b(убью|убить|меня убивают|хочет убить)\b",
    r"\b(избивает|бьет|бьёт|насилие прямо сейчас)\b",
    r"\b(суицид|покончить с собой|не хочу жить)\b",
    r"\b(сейчас рожаю|сильн(ое|ая) кровотечени[ея]|не могу дышать)\b",
]
CONCERN_PATTERNS = [
    r"\b(насилие|угрожает|страшно|преследует|нет где ночевать)\b",
    r"\b(беременна|с ребенком|с ребёнком|документы забрали)\b",
]


def assess_crisis(text: str) -> CrisisAssessment:
    normalized = text.lower().strip()
    if any(re.search(pattern, normalized) for pattern in ACUTE_PATTERNS):
        return CrisisAssessment(Risk.ACUTE, "rule-based acute signal")
    if any(re.search(pattern, normalized) for pattern in CONCERN_PATTERNS):
        return CrisisAssessment(Risk.CONCERN, "rule-based concern signal")
    return CrisisAssessment(Risk.NONE)


