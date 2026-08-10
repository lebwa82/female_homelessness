from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Stage(str, Enum):
    WELCOME = "welcome"
    NEED = "need"
    DELIVERY = "delivery"
    FOLLOW_UP = "follow_up"
    CASE_MANAGEMENT = "case_management"
    HUMAN_HANDOFF = "human_handoff"


class Risk(str, Enum):
    NONE = "none"
    CONCERN = "concern"
    ACUTE = "acute"


@dataclass(frozen=True)
class CrisisAssessment:
    risk: Risk
    reason: str | None = None


CRISIS_REPLY = (
    "Мне очень жаль, что вы сейчас через это проходите. Ваша безопасность важнее всего. "
    "Если есть непосредственная опасность, позвоните 112 или, если можете, перейдите в безопасное место. "
    "Я уже передала запрос специалистке — она подключится, как только сможет."
)

