from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ConversationState(str, Enum):
    GREETING = "greeting"
    DISCOVERING_NEED = "discovering_need"
    CHOOSING_AID = "choosing_aid"
    COLLECTING_LOCATION = "collecting_location"
    COLLECTING_CONTACT_METHOD = "collecting_contact_method"
    COLLECTING_CONTACT_VALUE = "collecting_contact_value"
    AID_REQUESTED = "aid_requested"
    FOLLOWUP_WAITING = "followup_waiting"
    FOLLOWUP_SENT = "followup_sent"
    FOLLOWUP_ANSWERED = "followup_answered"
    CLOSED = "closed"


class NeedKind(str, Enum):
    HOUSING = "housing"
    FOOD_MONEY = "food_money"
    LEGAL = "legal"
    SUPPORT = "support"
    CHILDREN = "children"
    OTHER = "other"


class ContactMethod(str, Enum):
    CURRENT_TELEGRAM = "current_telegram"
    OTHER_TELEGRAM = "other_telegram"
    PHONE = "phone"
    EMAIL = "email"
    LATER = "later"


class RiskLevel(str, Enum):
    NONE = "none"
    CONCERN = "concern"
    HUMAN_REQUESTED = "human_requested"
    URGENT = "urgent"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


class ActionKind(str, Enum):
    REPLY = "reply"
    SHOW_CHOICES = "show_choices"
    OFFER_AID = "offer_aid"
    REQUEST_LOCATION = "request_location"
    REQUEST_CONTACT = "request_contact"
    CREATE_AID_REQUEST = "create_aid_request"
    RECORD_ESCALATION = "record_escalation"
    OFFER_MORE_HELP = "offer_more_help"
    CLOSE_CONVERSATION = "close_conversation"


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=64)


class RiskAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    level: RiskLevel
    categories: tuple[str, ...] = Field(default=(), max_length=5)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    rationale: str = Field(default="", max_length=240)
    detector: str = Field(default="model", max_length=64)


class AgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ActionKind
    text: str = Field(min_length=1, max_length=1200)
    choices: tuple[Choice, ...] = Field(default=(), max_length=4)
    need: NeedKind | None = None
    aid_id: str | None = Field(default=None, max_length=64)
    contact_method: ContactMethod | None = None
    contact_value: str | None = Field(default=None, max_length=320)
    city: str | None = Field(default=None, max_length=120)
    district: str | None = Field(default=None, max_length=120)


class AgentTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=4096)
    choices: tuple[Choice, ...] = ()
    audit: dict = Field(default_factory=dict)

    def with_human_choice(self) -> AgentTurn:
        if any(choice.id == "human" for choice in self.choices):
            return self
        return self.model_copy(
            update={
                "choices": (*self.choices, Choice(id="human", label="Поговорить с живым человеком"))
            }
        )


class IncomingMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: str = "telegram"
    platform_user_id: int
    chat_id: int
    username: str | None = None
    text: str
    message_id: int | None = None
    received_at: str | None = None


# Compatibility aliases for the first prototype. They are removed when the
# legacy scripted service is replaced by ConversationService.
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
    "Слышу вас. Сейчас важнее всего безопасность. "
    "Если опасность непосредственная, позвоните 112, если можете сделать это безопасно. "
    "Зову живого человека, а здесь можно продолжать писать."
)

