from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ConversationState(str, Enum):
    OPEN_CONVERSATION = "open_conversation"
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


class SupportIntent(str, Enum):
    OPEN_CONVERSATION = "open_conversation"
    CONCRETE_NEED = "concrete_need"
    AID_INTEREST = "aid_interest"
    PSYCHOLOGIST_CONSIDERING = "psychologist_considering"
    PSYCHOLOGIST_REQUEST = "psychologist_request"
    VERIFIED_INFORMATION = "verified_information"
    EXPLICIT_HUMAN_REQUEST = "explicit_human_request"
    CLOSE = "close"


class SupportAction(str, Enum):
    CONTINUE_CONVERSATION = "continue_conversation"
    CLARIFY = "clarify"
    OFFER_AID = "offer_aid"
    PROVIDE_VERIFIED_INFO = "provide_verified_info"
    REQUEST_HUMAN = "request_human"
    START_PSYCHOLOGIST_REQUEST = "start_psychologist_request"
    CLOSE = "close"


class ChoiceSet(str, Enum):
    NONE = "none"
    SAFE_CONTINUE = "safe_continue"
    NEED_CATEGORIES = "need_categories"
    AID_CATALOG = "aid_catalog"
    PSYCHOLOGIST_INTEREST = "psychologist_interest"
    CONTACT_METHODS = "contact_methods"
    MORE_HELP = "more_help"


class SupportOffer(str, Enum):
    PSYCHOLOGIST = "psychologist"


class SupportPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: SupportIntent
    next_action: SupportAction
    text: str = Field(min_length=1, max_length=1200)
    choice_set: ChoiceSet = ChoiceSet.NONE
    need: NeedKind | None = None
    catalog_item_ids: tuple[str, ...] = Field(default=(), max_length=4)
    offered_support: SupportOffer | None = None


class EscalationCause(str, Enum):
    SAFETY = "safety"
    HUMAN_REQUEST = "human_request"
    LEVEL_TWO_SUPPORT = "level_two_support"


class EscalationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cause: EscalationCause
    level: RiskLevel | None = None
    categories: tuple[str, ...] = ()
    reason: str = Field(default="", max_length=240)
    request_key: str | None = Field(default=None, max_length=128)


class PolicyEffect(str, Enum):
    NONE = "none"
    OFFER_AID = "offer_aid"
    START_PSYCHOLOGIST_REQUEST = "start_psychologist_request"
    HUMAN_HANDOFF = "human_handoff"
    CRITICAL_ESCALATION = "critical_escalation"
    CAPTURE_LOCATION = "capture_location"
    COMPLETE_CONTACT = "complete_contact"
    REPLAY_WORKFLOW = "replay_workflow"
    CLOSE = "close"


class PolicySideEffect(str, Enum):
    RECORD_SAFETY = "record_safety"
    COMPLETE_FOLLOWUP = "complete_followup"


class ResolvedTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1, max_length=4096)
    choice_set: ChoiceSet = ChoiceSet.NONE
    effect: PolicyEffect = PolicyEffect.NONE
    need: NeedKind | None = None
    catalog_item_ids: tuple[str, ...] = ()
    offered_support: SupportOffer | None = None
    workflow_value: str | None = Field(default=None, max_length=320)
    side_effects: tuple[PolicySideEffect, ...] = ()
    fallback_reason: str | None = Field(default=None, max_length=120)


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
