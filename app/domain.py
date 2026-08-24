from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

DELIVERY_SEMANTICS = "bounded_at_least_once"
DELIVERY_AMBIGUOUS_CATEGORY = "delivery_ambiguous"


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


class DiagnosticStatus(str, Enum):
    """Transport/schema health of an agent diagnostic, not a safety meaning."""

    COMPLETED = "completed"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


class DeliveryAuthorization(str, Enum):
    """Durable delivery decision; absence and confirmed denial are distinct."""

    ALLOW = "allow"
    DENY_CONFIRMED = "deny_confirmed"
    UNAVAILABLE = "unavailable"


class InboundExecutionKind(str, Enum):
    """Namespace for durable outcomes sharing one Telegram message-id domain."""

    MESSAGE = "message"
    CALLBACK = "callback"


@dataclass(frozen=True, slots=True)
class InboundExecutionKey:
    """Stable storage identity for an inbound event and its durable outcome.

    Telegram callback queries identify the bot message whose keyboard was
    pressed, while ordinary inbound messages use a user-message id.  Both ids
    are numeric and can coincide in synthetic adapters or other channels, so
    callback outcomes require an explicit namespace.
    """

    kind: InboundExecutionKind
    message_id: int | None

    @classmethod
    def message(cls, message_id: int | None) -> InboundExecutionKey:
        return cls(InboundExecutionKind.MESSAGE, message_id)

    @classmethod
    def callback(cls, message_id: int | None) -> InboundExecutionKey:
        return cls(InboundExecutionKind.CALLBACK, message_id)

    @property
    def storage_key(self) -> str:
        value = str(self.message_id) if self.message_id is not None else "missing"
        if self.kind is InboundExecutionKind.CALLBACK:
            return f"callback:{value}"
        # Preserve deployed message keys so an upgrade can replay old rows.
        return value

    @classmethod
    def from_storage_key(cls, value: str) -> InboundExecutionKey:
        kind = InboundExecutionKind.MESSAGE
        raw_value = value
        if value.startswith("callback:"):
            kind = InboundExecutionKind.CALLBACK
            raw_value = value.removeprefix("callback:")
        message_id = None if raw_value == "missing" else int(raw_value)
        return cls(kind, message_id)


class HardSignalKind(str, Enum):
    EXPLICIT_HUMAN_REQUEST = "explicit_human_request"
    OPEN_CONVERSATION_REQUEST = "open_conversation_request"
    CONCRETE_AID = "concrete_aid"
    GENERIC_AID_INTEREST = "generic_aid_interest"
    PSYCHOLOGIST_CONSIDERING = "psychologist_considering"
    PSYCHOLOGIST_REQUEST = "psychologist_request"
    SUICIDE_OR_SELF_HARM = "suicide_or_self_harm"
    VIOLENCE_OR_THREAT_NOW = "violence_or_threat_now"
    URGENT_SHELTER = "urgent_shelter"
    SAFETY_CONCERN = "safety_concern"


class SignalMatch(BaseModel):
    """Audit-safe deterministic match without retaining any user text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: HardSignalKind
    rule_id: str = Field(min_length=1, max_length=96)
    token_start: int = Field(ge=0)
    token_end: int = Field(ge=0)
    need: NeedKind | None = None

    @model_validator(mode="after")
    def require_nonempty_token_span(self) -> SignalMatch:
        if self.token_end <= self.token_start:
            raise ValueError("token_end must be greater than token_start")
        return self


class DeterministicSignals(BaseModel):
    """Versioned, hash-addressed output of the backend signal extractor."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    matcher_version: str = Field(min_length=1, max_length=64)
    input_hash: str = Field(min_length=64, max_length=64)
    matches: tuple[SignalMatch, ...] = ()


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


class SafetyDiagnostic(BaseModel):
    """Model observation only; the policy never treats it as an authorization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    level: RiskLevel
    categories: tuple[str, ...] = Field(default=(), max_length=5)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = Field(default="not_provided", min_length=1, max_length=240)
    evidence_claims: tuple[str, ...] = Field(default=(), max_length=5)
    rationale_alias_used: bool = Field(default=False, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def accept_provider_rationale_alias(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        if "rationale_short" not in value:
            return value
        normalized = dict(value)
        alias_value = normalized.pop("rationale_short")
        if "rationale" not in normalized:
            normalized["rationale"] = alias_value
            normalized["rationale_alias_used"] = True
        return normalized


class SupportDiagnostic(BaseModel):
    """Conversational model observation without any product-control fields."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    intent: SupportIntent | None = None
    need_hint: NeedKind | None = None
    evidence_claims: tuple[str, ...] = Field(default=(), max_length=5)
    draft_text: str = Field(min_length=1, max_length=1200)
    suggested_support: SupportOffer | None = None


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
    START_NEED_DISCOVERY = "start_need_discovery"
    OFFER_AID = "offer_aid"
    START_PSYCHOLOGIST_REQUEST = "start_psychologist_request"
    HUMAN_HANDOFF = "human_handoff"
    CRITICAL_ESCALATION = "critical_escalation"
    CAPTURE_LOCATION = "capture_location"
    COMPLETE_CONTACT = "complete_contact"
    REPLAY_WORKFLOW = "replay_workflow"
    CANCEL_WORKFLOW = "cancel_workflow"
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


class PolicyContext(BaseModel):
    """Complete backend-owned input for resolving one text turn."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state: str
    signals: DeterministicSignals | None
    local_risk: RiskAssessment
    safety_status: DiagnosticStatus = DiagnosticStatus.UNAVAILABLE
    support_status: DiagnosticStatus = DiagnosticStatus.UNAVAILABLE
    safety: SafetyDiagnostic | None = None
    support: SupportDiagnostic | None = None
    pending_offer: SupportOffer | None = None
    workflow_value: str = ""
    need: str | None = None


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
