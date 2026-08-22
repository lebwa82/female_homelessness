"""Replay versioned dialogue cases through the production service without printing prose."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from app.agents import AgentContext, AgentEvaluation, YandexAgentGateway
from app.domain import (
    ChoiceSet,
    ConversationState,
    DiagnosticStatus,
    EscalationCause,
    IncomingMessage,
    PolicyEffect,
    PolicySideEffect,
    RiskLevel,
    SafetyDiagnostic,
    SupportDiagnostic,
    SupportIntent,
    SupportOffer,
)
from app.service import ConversationService
from app.store import InMemoryConversationStore

DATASET_VERSION = 2
MAX_CASE_CONCURRENCY = 4
_VALIDATION_FIELD_CATEGORIES = frozenset({
    "level",
    "categories",
    "confidence",
    "rationale",
    "rationale_alias_used",
    "evidence_claims",
    "intent",
    "need_hint",
    "draft_text",
    "suggested_support",
})
_VALIDATION_TYPE_CATEGORIES = {
    "missing": "missing",
    "extra_forbidden": "extra_forbidden",
    "enum": "enum",
    "string_type": "string_type",
    "string_too_short": "string_too_short",
    "string_too_long": "string_too_long",
    "float_type": "float_type",
    "float_parsing": "float_parsing",
    "greater_than_equal": "out_of_range",
    "less_than_equal": "out_of_range",
    "tuple_type": "sequence_type",
    "list_type": "sequence_type",
    "too_long": "sequence_too_long",
    "bool_type": "bool_type",
    "bool_parsing": "bool_type",
    "model_type": "payload_type",
    "dict_type": "payload_type",
    "json_invalid": "payload_format",
}
_TRANSPORT_ERROR_CATEGORIES = {
    "TimeoutError": "TimeoutError",
    "APITimeoutError": "TimeoutError",
    "APIConnectionError": "ConnectionError",
    "ConnectError": "ConnectionError",
    "ConnectionError": "ConnectionError",
    "OSError": "ConnectionError",
    "AuthenticationError": "AuthenticationError",
    "PermissionDeniedError": "PermissionDeniedError",
    "RateLimitError": "RateLimitError",
    "BadRequestError": "BadRequestError",
    "InternalServerError": "ServerError",
    "APIStatusError": "ServerError",
    "UnexpectedModelBehavior": "UnexpectedModelBehavior",
}
_DIAGNOSTIC_NORMALIZATION_CATEGORIES = frozenset({
    "safety_rationale_truncated",
    "support_unknown_intent_cleared",
    "support_unknown_need_hint_cleared",
})


class DatasetError(ValueError):
    """A non-secret explanation of an invalid evaluation fixture."""


class DiagnosticVariant(str, Enum):
    EXPECTED = "expected"
    WRONG_VALID = "wrong_valid"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


@dataclass(frozen=True)
class InitialRuntimeContext:
    state: str
    pending_offer: SupportOffer | None
    need: str | None = None
    pending_aid_id: str | None = None
    pending_contact_method: str | None = None
    pending_city: str | None = None
    pending_district: str | None = None

    def store_values(self) -> dict[str, str | None]:
        return {
            "state": self.state,
            "pending_offer": self.pending_offer.value if self.pending_offer else None,
            "need": self.need,
            "pending_aid_id": self.pending_aid_id,
            "pending_contact_method": self.pending_contact_method,
            "pending_city": self.pending_city,
            "pending_district": self.pending_district,
        }


@dataclass(frozen=True)
class DialogueCase:
    version: int
    id: str
    group: str
    history: tuple[tuple[str, str], ...]
    initial: InitialRuntimeContext
    behavior: dict[str, Any]
    diagnostics: dict[str, tuple[str, ...]]
    soft: dict[str, tuple[str | None, ...]]


@dataclass(frozen=True)
class FixtureOutput:
    safety: dict[str, Any] | None
    support: dict[str, Any] | None
    safety_status: DiagnosticStatus
    support_status: DiagnosticStatus


@dataclass(frozen=True)
class ProviderFailureMetadata:
    """Allow-listed non-content diagnostics for one unavailable or invalid provider agent."""

    agent: str
    diagnostic_status: str
    transport_error_type: str | None = None
    validation_fields: tuple[str, ...] = ()
    validation_types: tuple[str, ...] = ()
    output_envelope: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "agent": self.agent,
            "diagnostic_status": self.diagnostic_status,
        }
        if self.transport_error_type is not None:
            result["transport_error_type"] = self.transport_error_type
        if self.validation_fields or self.validation_types:
            result["validation"] = {
                "fields": list(self.validation_fields),
                "types": list(self.validation_types),
            }
        if self.output_envelope is not None:
            result["output_envelope"] = self.output_envelope
        return result


@dataclass(frozen=True)
class CaseReport:
    case_id: str
    diagnostics: dict[str, object]
    hard_projection: dict[str, Any]
    soft_projection: dict[str, Any]
    hard_hash: str
    rule_ids: tuple[str, ...]
    hard_failures: tuple[str, ...]
    soft_failures: tuple[str, ...]
    diagnostic_deltas: tuple[str, ...]
    provider_failures: tuple[str, ...]
    provider_failure_metadata: tuple[ProviderFailureMetadata, ...]

    @property
    def failures(self) -> tuple[str, ...]:
        return self.hard_failures


@dataclass(frozen=True)
class EvalReport:
    cases: tuple[CaseReport, ...]

    @property
    def hard_failures(self) -> tuple[str, ...]:
        return tuple(f"{case.case_id}:{item}" for case in self.cases for item in case.hard_failures)

    @property
    def diagnostic_deltas(self) -> tuple[str, ...]:
        return tuple(f"{case.case_id}:{item}" for case in self.cases for item in case.diagnostic_deltas)

    @property
    def soft_failures(self) -> tuple[str, ...]:
        return tuple(f"{case.case_id}:{item}" for case in self.cases for item in case.soft_failures)

    @property
    def provider_failures(self) -> tuple[str, ...]:
        return tuple(f"{case.case_id}:{item}" for case in self.cases for item in case.provider_failures)

    @property
    def provider_failure_summary(self) -> dict[str, dict[str, int]]:
        counters = {
            "by_agent": Counter[str](),
            "by_diagnostic_status": Counter[str](),
            "by_transport_error_type": Counter[str](),
            "by_validation_field": Counter[str](),
            "by_validation_type": Counter[str](),
            "by_output_envelope": Counter[str](),
        }
        for case in self.cases:
            for failure in case.provider_failure_metadata:
                counters["by_agent"][failure.agent] += 1
                counters["by_diagnostic_status"][failure.diagnostic_status] += 1
                if failure.transport_error_type is not None:
                    counters["by_transport_error_type"][failure.transport_error_type] += 1
                counters["by_validation_field"].update(failure.validation_fields)
                counters["by_validation_type"].update(failure.validation_types)
                if failure.output_envelope is not None:
                    counters["by_output_envelope"][failure.output_envelope] += 1
        return {name: dict(sorted(counter.items())) for name, counter in counters.items()}

    @property
    def failures(self) -> tuple[str, ...]:
        return self.hard_failures


class Gateway(Protocol):
    async def evaluate(self, context: AgentContext) -> AgentEvaluation: ...


class FixtureGateway:
    """Offline diagnostic-only gateway, with mutations for policy-invariance checks."""

    def __init__(
        self,
        outputs: Mapping[str, FixtureOutput],
        case_id: str | None = None,
        variant: DiagnosticVariant = DiagnosticVariant.EXPECTED,
    ) -> None:
        self._outputs = outputs
        self._case_id = case_id
        self._variant = variant

    @classmethod
    def from_case(
        cls,
        case: DialogueCase,
        outputs: Mapping[str, FixtureOutput],
        variant: DiagnosticVariant = DiagnosticVariant.EXPECTED,
    ) -> FixtureGateway:
        return cls(outputs, case.id, variant)

    def for_case(self, case: DialogueCase) -> FixtureGateway:
        return self.from_case(case, self._outputs, self._variant)

    def with_variant(self, variant: DiagnosticVariant) -> FixtureGateway:
        return FixtureGateway(self._outputs, variant=variant)

    def validate_case_ids(self, cases: Sequence[DialogueCase]) -> None:
        missing = {case.id for case in cases} - set(self._outputs)
        surplus = set(self._outputs) - {case.id for case in cases}
        if missing or surplus:
            kinds = ", ".join(kind for kind, found in (("missing", missing), ("surplus", surplus)) if found)
            raise DatasetError(f"fixture IDs must exactly match dataset IDs: {kinds}")

    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        del context
        if self._case_id is None:
            raise DatasetError("fixture gateway requires a selected case")
        try:
            output = self._outputs[self._case_id]
        except KeyError as error:
            raise DatasetError(f"missing fixture output for case id: {self._case_id}") from error
        return _fixture_evaluation(output, self._variant)


def load_cases(path: Path | str) -> tuple[DialogueCase, ...]:
    rows = _load_jsonl(Path(path), "dataset")
    cases: list[DialogueCase] = []
    seen: set[str] = set()
    for line, row in rows:
        case = _parse_case(row, line)
        if case.id in seen:
            raise DatasetError(f"dataset line {line}: duplicate case id: {case.id}")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise DatasetError("dataset contains no cases")
    return tuple(cases)


def load_fixture_outputs(path: Path | str) -> dict[str, FixtureOutput]:
    outputs: dict[str, FixtureOutput] = {}
    for line, row in _load_jsonl(Path(path), "fixture outputs"):
        prefix = f"fixture outputs line {line}"
        _exact_keys(row, {"id", "safety", "support", "safety_status", "support_status"}, prefix)
        case_id = _string(row["id"], f"{prefix}: id")
        if case_id in outputs:
            raise DatasetError(f"{prefix}: duplicate case id: {case_id}")
        safety, support = row["safety"], row["support"]
        if safety is not None and not isinstance(safety, dict):
            raise DatasetError(f"{prefix}: safety must be an object or null")
        if support is not None and not isinstance(support, dict):
            raise DatasetError(f"{prefix}: support must be an object or null")
        safety_status = _enum(row["safety_status"], DiagnosticStatus, f"{prefix}: safety_status")
        support_status = _enum(row["support_status"], DiagnosticStatus, f"{prefix}: support_status")
        if (safety_status is DiagnosticStatus.COMPLETED) != (safety is not None):
            raise DatasetError(f"{prefix}: completed safety status must match its payload")
        if (support_status is DiagnosticStatus.COMPLETED) != (support is not None):
            raise DatasetError(f"{prefix}: completed support status must match its payload")
        outputs[case_id] = FixtureOutput(safety, support, safety_status, support_status)
    if not outputs:
        raise DatasetError("fixture outputs contains no rows")
    return outputs


async def evaluate_case(
    gateway: Gateway,
    case: DialogueCase,
    *,
    require_provider_health: bool = False,
) -> CaseReport:
    """Seed prefix/context then submit only the stored final user message to the service."""
    store = InMemoryConversationStore()
    final_text = _final_user_text(case.history)
    incoming = _incoming_for(case, final_text)
    record = await store.ensure(incoming)
    await store.update(record, **case.initial.store_values())
    for role, content in case.history[:-1]:
        await store.append_message(record, role, content)
    turn = await ConversationService(store=store, gateway=gateway).handle_text(incoming)
    return await _report_turn(
        case,
        store,
        record,
        turn,
        history_matches=await store.history(record) == case.history,
        require_provider_health=require_provider_health,
    )


async def _report_turn(
    case: DialogueCase,
    store: InMemoryConversationStore,
    record: Any,
    turn: Any,
    *,
    history_matches: bool,
    require_provider_health: bool,
) -> CaseReport:
    audit = _policy_audit(store)
    rule_ids = tuple(audit.get("rule_ids", ()))
    copy_contains = case.behavior["copy_contains"]
    projection = {
        "local_risk": audit.get("local_risk"),
        "choice_set": audit.get("choice_set"),
        "rendered_callback_ids": tuple(choice.id for choice in turn.choices),
        "effect": audit.get("effect"),
        "side_effects": tuple(audit.get("side_effects", ())),
        "state_after": record.state,
        "escalation": bool(store.escalations),
        "escalation_cause": store.escalations[-1].cause.value if store.escalations else None,
        "escalation_count": len(store.escalations),
        "request_count": len(store.aid_requests),
        "canonical_copy_ok": copy_contains is None or copy_contains in turn.text,
        "rule_ids": rule_ids,
    }
    soft_projection = {"pending_offer": record.pending_offer, "authoritative": False}
    soft_failures = _soft_failures(case.soft, case.initial, soft_projection)
    diagnostics = _diagnostic_projection(store, audit)
    hard_failures = _behavior_failures(case.behavior, projection, audit, history_matches)
    deltas = _diagnostic_deltas(case.diagnostics, diagnostics)
    provider_failures = _provider_failures(diagnostics) if require_provider_health else ()
    provider_failure_metadata = (
        _provider_failure_metadata(store, diagnostics) if require_provider_health else ()
    )
    return CaseReport(
        case.id,
        diagnostics,
        projection,
        soft_projection,
        _hard_hash(projection),
        rule_ids,
        tuple(hard_failures),
        tuple(soft_failures),
        deltas,
        provider_failures,
        provider_failure_metadata,
    )


async def evaluate_cases(
    gateway: Gateway,
    cases: Sequence[DialogueCase],
    *,
    require_provider_health: bool = False,
    max_concurrency: int = MAX_CASE_CONCURRENCY,
) -> EvalReport:
    if not 1 <= max_concurrency <= MAX_CASE_CONCURRENCY:
        raise ValueError("invalid max_concurrency")
    if isinstance(gateway, FixtureGateway):
        gateway.validate_case_ids(cases)
    soft_cases = tuple(case for case in cases if case.group == "soft_lifecycle")
    if soft_cases and not require_provider_health:
        reports = await _evaluate_cases_with_soft_lifecycle(
            gateway,
            cases,
            soft_cases,
            require_provider_health=require_provider_health,
        )
        return EvalReport(cases=reports)
    semaphore = asyncio.Semaphore(max_concurrency)

    async def one(case: DialogueCase) -> CaseReport:
        async with semaphore:
            selected: Gateway = gateway.for_case(case) if isinstance(gateway, FixtureGateway) else gateway
            return await evaluate_case(selected, case, require_provider_health=require_provider_health)

    return EvalReport(cases=tuple(await asyncio.gather(*(one(case) for case in cases))))


def _gateway_for_case(gateway: Gateway, case: DialogueCase) -> Gateway:
    return gateway.for_case(case) if isinstance(gateway, FixtureGateway) else gateway


async def _evaluate_cases_with_soft_lifecycle(
    gateway: Gateway,
    cases: Sequence[DialogueCase],
    soft_cases: Sequence[DialogueCase],
    *,
    require_provider_health: bool,
) -> tuple[CaseReport, ...]:
    """Replay soft offers as real sequential state, never seeded end states."""
    by_id = {case.id: case for case in soft_cases}
    required = {"soft-offer-create", "soft-offer-consume", "soft-offer-expire"}
    if set(by_id) != required:
        raise DatasetError("soft lifecycle cases must be create, consume, and expire")
    create_case, consume_case, expire_case = (
        by_id["soft-offer-create"],
        by_id["soft-offer-consume"],
        by_id["soft-offer-expire"],
    )
    reports: dict[str, CaseReport] = {}

    async def prepare(case: DialogueCase, identity: int) -> tuple[InMemoryConversationStore, Any, int]:
        store = InMemoryConversationStore()
        incoming = IncomingMessage(
            platform_user_id=identity,
            chat_id=identity,
            text=_final_user_text(case.history),
            message_id=1,
        )
        record = await store.ensure(incoming)
        await store.update(record, **case.initial.store_values())
        return store, record, identity

    # Branch one: None → psychologist → None by accepting the offer.
    identity = int(hashlib.sha256(b"soft-offer-consume-lifecycle").hexdigest()[:12], 16)
    store, record, _ = await prepare(create_case, identity)
    first_incoming = IncomingMessage(
        platform_user_id=identity,
        chat_id=identity,
        text=_final_user_text(create_case.history),
        message_id=1,
    )
    create_turn = await ConversationService(store=store, gateway=_gateway_for_case(gateway, create_case)).handle_text(first_incoming)
    reports[create_case.id] = await _report_turn(
        create_case, store, record, create_turn, history_matches=True, require_provider_health=require_provider_health
    )
    consume_incoming = IncomingMessage(
        platform_user_id=identity,
        chat_id=identity,
        text=_final_user_text(consume_case.history),
        message_id=2,
    )
    consume_turn = await ConversationService(store=store, gateway=_gateway_for_case(gateway, consume_case)).handle_text(consume_incoming)
    reports[consume_case.id] = await _report_turn(
        consume_case, store, record, consume_turn, history_matches=True, require_provider_health=require_provider_health
    )

    # Branch two: create a fresh offer, then prove unrelated text expires it.
    expiry_identity = int(hashlib.sha256(b"soft-offer-expire-lifecycle").hexdigest()[:12], 16)
    expiry_store, expiry_record, _ = await prepare(create_case, expiry_identity)
    expiry_create = IncomingMessage(
        platform_user_id=expiry_identity,
        chat_id=expiry_identity,
        text=_final_user_text(create_case.history),
        message_id=1,
    )
    await ConversationService(store=expiry_store, gateway=_gateway_for_case(gateway, create_case)).handle_text(expiry_create)
    expire_incoming = IncomingMessage(
        platform_user_id=expiry_identity,
        chat_id=expiry_identity,
        text=_final_user_text(expire_case.history),
        message_id=2,
    )
    expire_turn = await ConversationService(store=expiry_store, gateway=_gateway_for_case(gateway, expire_case)).handle_text(expire_incoming)
    reports[expire_case.id] = await _report_turn(
        expire_case, expiry_store, expiry_record, expire_turn, history_matches=True, require_provider_health=require_provider_health
    )

    normal_reports = {
        case.id: await evaluate_case(
            _gateway_for_case(gateway, case), case, require_provider_health=require_provider_health
        )
        for case in cases
        if case.group != "soft_lifecycle"
    }
    return tuple(reports[case.id] if case.id in reports else normal_reports[case.id] for case in cases)


async def evaluate_offline_cases(gateway: FixtureGateway, cases: Sequence[DialogueCase]) -> EvalReport:
    base = await evaluate_cases(gateway.with_variant(DiagnosticVariant.EXPECTED), cases)
    variants = {
        variant: await evaluate_cases(gateway.with_variant(variant), cases)
        for variant in (DiagnosticVariant.WRONG_VALID, DiagnosticVariant.UNAVAILABLE, DiagnosticVariant.INVALID)
    }
    reports: list[CaseReport] = []
    for index, report in enumerate(base.cases):
        failures = list(report.hard_failures)
        # Soft lifecycle cases intentionally observe a diagnostic-owned offer
        # as a non-authoritative projection.  They are checked by their own
        # sequential lifecycle contract, rather than pretending a mutated
        # offer was the same stored conversation state.
        if cases[index].group != "soft_lifecycle":
            for variant, mutation in variants.items():
                if mutation.cases[index].hard_hash != report.hard_hash:
                    failures.append(f"mutation_{variant.value}_hard_projection")
        reports.append(replace(report, hard_failures=tuple(failures)))
    return EvalReport(cases=tuple(reports))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate anonymized dialogue invariants.")
    parser.add_argument("dataset", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixtures", type=Path, help="offline diagnostic fixtures")
    mode.add_argument("--live", action="store_true", help="evaluate with configured diagnostics")
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.dataset)
        report = asyncio.run(
            evaluate_cases(YandexAgentGateway(), cases, require_provider_health=True)
            if args.live
            else evaluate_offline_cases(FixtureGateway(load_fixture_outputs(args.fixtures)), cases)
        )
    except (DatasetError, ValueError):
        print("dataset_error:invalid", file=sys.stderr)
        return 2
    for case in report.cases:
        print(json.dumps({
            "case_id": case.case_id,
            "diagnostics": case.diagnostics,
            "diagnostic_deltas": case.diagnostic_deltas,
            "hard_projection": case.hard_projection,
            "soft_projection": case.soft_projection,
            "hard_hash": case.hard_hash,
            "rule_ids": case.rule_ids,
            "hard_failures": case.hard_failures,
            "soft_failures": case.soft_failures,
            "provider_failures": case.provider_failures,
            "provider_failure_metadata": [failure.as_dict() for failure in case.provider_failure_metadata],
        }, ensure_ascii=True, sort_keys=True))
    print(json.dumps({"summary": {
        "cases": len(report.cases),
        "hard_failures": len(report.hard_failures),
        "soft_failures": len(report.soft_failures),
        "diagnostic_deltas": len(report.diagnostic_deltas),
        "provider_failures": len(report.provider_failures),
        "provider_failure_summary": report.provider_failure_summary,
    }}, ensure_ascii=True, sort_keys=True))
    return 2 if report.provider_failures else 1 if report.hard_failures or report.soft_failures else 0


def _fixture_evaluation(output: FixtureOutput, variant: DiagnosticVariant) -> AgentEvaluation:
    if variant is DiagnosticVariant.UNAVAILABLE:
        return AgentEvaluation(safety_status=DiagnosticStatus.UNAVAILABLE, support_status=DiagnosticStatus.UNAVAILABLE,
                               safety_audit={"status": "fixture_unavailable"}, support_audit={"status": "fixture_unavailable"})
    if variant is DiagnosticVariant.INVALID:
        return AgentEvaluation(safety_status=DiagnosticStatus.INVALID, support_status=DiagnosticStatus.INVALID,
                               safety_audit={"status": "fixture_invalid"}, support_audit={"status": "fixture_invalid"})
    try:
        safety = SafetyDiagnostic.model_validate(output.safety) if output.safety else None
        support = SupportDiagnostic.model_validate(output.support) if output.support else None
    except ValidationError as error:
        raise DatasetError("fixture diagnostic payload is invalid") from error
    if variant is DiagnosticVariant.WRONG_VALID:
        if safety:
            safety = safety.model_copy(update={"level": RiskLevel.CRITICAL if safety.level is not RiskLevel.CRITICAL else RiskLevel.NONE,
                                               "categories": (), "confidence": 1.0, "rationale": "fixture mutation"})
        if support:
            support = support.model_copy(update={"intent": SupportIntent.EXPLICIT_HUMAN_REQUEST if support.intent is not SupportIntent.EXPLICIT_HUMAN_REQUEST else SupportIntent.OPEN_CONVERSATION,
                                                  "need_hint": None, "suggested_support": None})
    return AgentEvaluation(safety=safety, support=support, safety_status=output.safety_status,
                           support_status=output.support_status, safety_audit={"status": "fixture"}, support_audit={"status": "fixture"})


def _load_jsonl(path: Path, label: str) -> list[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise DatasetError(f"cannot read {label}") from error
    rows: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise DatasetError(f"{label} line {line_number}: blank lines are not allowed")
        try:
            row = json.loads(
                line,
                object_pairs_hook=_json_object_without_duplicates,
                parse_constant=_reject_nonstandard_json_constant,
            )
        except (json.JSONDecodeError, ValueError) as error:
            raise DatasetError(f"{label} line {line_number}: invalid JSON") from error
        if not isinstance(row, dict):
            raise DatasetError(f"{label} line {line_number}: row must be an object")
        rows.append((line_number, row))
    return rows


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


def _reject_nonstandard_json_constant(value: str) -> None:
    del value
    raise ValueError("nonstandard_json_constant")


def _parse_case(row: dict[str, Any], line: int) -> DialogueCase:
    prefix = f"dataset line {line}"
    _exact_keys(row, {"version", "id", "group", "history", "initial", "expected"}, prefix)
    if row["version"] != DATASET_VERSION:
        raise DatasetError(f"{prefix}: unsupported version")
    history = _history(row["history"], prefix)
    return DialogueCase(int(row["version"]), _string(row["id"], f"{prefix}: id"),
                        _string(row["group"], f"{prefix}: group"), history,
                        _initial(row["initial"], prefix), *_expected(row["expected"], prefix))


def _history(value: Any, prefix: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or not value:
        raise DatasetError(f"{prefix}: history must be a non-empty array")
    result: list[tuple[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, list) or len(item) != 2:
            raise DatasetError(f"{prefix}: history[{index}] must be a [role, text] pair")
        role, text = item
        if role not in {"user", "assistant"}:
            raise DatasetError(f"{prefix}: invalid history role")
        result.append((role, _string(text, f"{prefix}: history[{index}] text")))
    if result[-1][0] != "user":
        raise DatasetError(f"{prefix}: final history message must be from the user")
    return tuple(result)


def _initial(value: Any, prefix: str) -> InitialRuntimeContext:
    if not isinstance(value, dict):
        raise DatasetError(f"{prefix}: initial must be an object")
    optional = {"need", "pending_aid_id", "pending_contact_method", "pending_city", "pending_district"}
    _keys(value, {"state", "pending_offer"}, {"state", "pending_offer", *optional}, f"{prefix}: initial")
    state = _string(value["state"], f"{prefix}: initial.state")
    _enum(state, ConversationState, f"{prefix}: initial.state")
    pending = value["pending_offer"]
    if pending is not None:
        pending = _enum(pending, SupportOffer, f"{prefix}: initial.pending_offer")
    fields: dict[str, str | None] = {}
    for field in optional:
        field_value = value.get(field)
        if field_value is not None and not isinstance(field_value, str):
            raise DatasetError(f"{prefix}: initial.{field} must be a string or null")
        fields[field] = field_value
    return InitialRuntimeContext(state, pending, **fields)


def _expected(
    value: Any, prefix: str
) -> tuple[dict[str, Any], dict[str, tuple[str, ...]], dict[str, tuple[str | None, ...]]]:
    if not isinstance(value, dict):
        raise DatasetError(f"{prefix}: expected must be an object")
    _keys(value, {"behavior", "diagnostics"}, {"behavior", "diagnostics", "soft"}, f"{prefix}: expected")
    behavior, diagnostics = value["behavior"], value["diagnostics"]
    if not isinstance(behavior, dict) or not isinstance(diagnostics, dict):
        raise DatasetError(f"{prefix}: expected sections must be objects")
    behavior_keys = {
        "local_risk",
        "choice_set",
        "rendered_callback_ids",
        "effect",
        "side_effects",
        "state_after",
        "escalation",
        "escalation_cause",
        "escalation_count",
        "request_count",
        "copy_contains",
        "rule_ids",
    }
    _exact_keys(behavior, behavior_keys, f"{prefix}: expected.behavior")
    for key, enum_type in (("local_risk", RiskLevel), ("choice_set", ChoiceSet), ("effect", PolicyEffect), ("state_after", ConversationState)):
        _enum(behavior[key], enum_type, f"{prefix}: expected.behavior.{key}")
    if not isinstance(behavior["rendered_callback_ids"], list) or not all(isinstance(item, str) and item for item in behavior["rendered_callback_ids"]):
        raise DatasetError(f"{prefix}: expected.behavior.rendered_callback_ids must be a string array")
    if not isinstance(behavior["side_effects"], list):
        raise DatasetError(f"{prefix}: expected.behavior.side_effects must be a string array")
    for item in behavior["side_effects"]:
        _enum(item, PolicySideEffect, f"{prefix}: expected.behavior.side_effects")
    if not isinstance(behavior["escalation"], bool):
        raise DatasetError(f"{prefix}: expected.behavior.escalation must be a boolean")
    if behavior["escalation_cause"] is not None:
        _enum(behavior["escalation_cause"], EscalationCause, f"{prefix}: expected.behavior.escalation_cause")
    for key in ("escalation_count", "request_count"):
        if not isinstance(behavior[key], int) or behavior[key] < 0:
            raise DatasetError(f"{prefix}: expected.behavior.{key} must be a non-negative integer")
    if behavior["escalation"] != (behavior["escalation_count"] > 0):
        raise DatasetError(f"{prefix}: expected.behavior escalation count mismatch")
    if behavior["copy_contains"] is not None and not isinstance(behavior["copy_contains"], str):
        raise DatasetError(f"{prefix}: expected.behavior.copy_contains must be a string or null")
    if behavior["copy_contains"] is not None:
        _string(behavior["copy_contains"], f"{prefix}: expected.behavior.copy_contains")
    if _requires_canonical_copy(behavior) and behavior["copy_contains"] is None:
        raise DatasetError(f"{prefix}: expected.behavior canonical copy is required")
    if not isinstance(behavior["rule_ids"], list) or not all(
        isinstance(item, str) and item for item in behavior["rule_ids"]
    ):
        raise DatasetError(f"{prefix}: expected.behavior.rule_ids must be a string array")
    _exact_keys(diagnostics, {"safety_levels", "support_intents"}, f"{prefix}: expected.diagnostics")
    parsed: dict[str, tuple[str, ...]] = {}
    for key, enum_type in (("safety_levels", RiskLevel), ("support_intents", SupportIntent)):
        items = diagnostics[key]
        if not isinstance(items, list) or not items or not all(isinstance(item, str) for item in items):
            raise DatasetError(f"{prefix}: expected.diagnostics.{key} must be a non-empty string array")
        for item in items:
            _enum(item, enum_type, f"{prefix}: expected.diagnostics.{key}")
        parsed[key] = tuple(items)
    soft_value = value.get("soft", {})
    if not isinstance(soft_value, dict):
        raise DatasetError(f"{prefix}: expected.soft must be an object")
    _exact_keys(soft_value, {"pending_offer_lifecycle"}, f"{prefix}: expected.soft") if soft_value else None
    soft: dict[str, tuple[str | None, ...]] = {}
    if soft_value:
        lifecycle = soft_value["pending_offer_lifecycle"]
        if not isinstance(lifecycle, list) or len(lifecycle) != 2:
            raise DatasetError(f"{prefix}: expected.soft.pending_offer_lifecycle must have two values")
        for item in lifecycle:
            if item is not None:
                _enum(item, SupportOffer, f"{prefix}: expected.soft.pending_offer_lifecycle")
        soft["pending_offer_lifecycle"] = tuple(lifecycle)
    return dict(behavior), parsed, soft


def _incoming_for(case: DialogueCase, text: str) -> IncomingMessage:
    identity = int(hashlib.sha256(case.id.encode()).hexdigest()[:12], 16)
    return IncomingMessage(platform_user_id=identity, chat_id=identity, text=text, message_id=len(case.history))


def _policy_audit(store: InMemoryConversationStore) -> dict[str, Any]:
    audits = [audit for _, kind, _, audit in store.actions if kind == "policy_decision"]
    return dict(audits[-1]) if audits else {}


def _diagnostic_projection(store: InMemoryConversationStore, audit: Mapping[str, Any]) -> dict[str, object]:
    runs = {name: value for _, name, value in store.agent_runs}
    return {
        "safety_status": str(audit.get("safety_status") or runs.get("safety", {}).get("diagnostic_status") or "unavailable"),
        "safety_level": audit.get("safety_label"),
        "safety_normalizations": _diagnostic_normalization_categories(runs.get("safety", {})),
        "support_status": str(audit.get("support_status") or runs.get("support", {}).get("diagnostic_status") or "unavailable"),
        "support_intent": audit.get("support_intent"),
        "support_normalizations": _diagnostic_normalization_categories(runs.get("support", {})),
    }


def _behavior_failures(expected: Mapping[str, Any], actual: Mapping[str, Any], audit: Mapping[str, Any], history_matches: bool) -> list[str]:
    fields = ("local_risk", "choice_set", "rendered_callback_ids", "effect", "side_effects", "state_after", "escalation", "escalation_cause", "escalation_count", "request_count", "rule_ids")
    failures = [
        field
        for field in fields
        if actual[field]
        != (tuple(expected[field]) if field in {"rendered_callback_ids", "side_effects", "rule_ids"} else expected[field])
    ]
    if expected["copy_contains"] is not None and not actual["canonical_copy_ok"]:
        failures.append("canonical_copy")
    if not history_matches:
        failures.append("history_replay")
    if audit.get("rendered_callback_ids") != list(actual["rendered_callback_ids"]):
        failures.append("audit_rendered_callback_ids")
    if audit.get("state_after") != actual["state_after"]:
        failures.append("audit_state_after")
    return failures


def _soft_failures(
    expected: Mapping[str, tuple[str | None, ...]],
    initial: InitialRuntimeContext,
    actual: Mapping[str, Any],
) -> list[str]:
    """Assert observed, non-authoritative state transitions separately from behavior."""
    lifecycle = expected.get("pending_offer_lifecycle")
    if lifecycle is None:
        return []
    observed = (initial.pending_offer.value if initial.pending_offer else None, actual.get("pending_offer"))
    return [] if observed == lifecycle else ["pending_offer_lifecycle"]


def _diagnostic_deltas(
    expected: Mapping[str, tuple[str, ...]],
    actual: Mapping[str, object],
) -> tuple[str, ...]:
    deltas: list[str] = []
    if actual["safety_status"] != DiagnosticStatus.COMPLETED.value:
        deltas.append(f"safety_status:{actual['safety_status']}")
    elif actual["safety_level"] not in expected["safety_levels"]:
        deltas.append(f"safety_level:{actual['safety_level']}")
    deltas.extend(
        f"safety_normalization:{category}"
        for category in _normalization_categories_from_projection(actual, "safety_normalizations")
    )
    if actual["support_status"] != DiagnosticStatus.COMPLETED.value:
        deltas.append(f"support_status:{actual['support_status']}")
    elif actual["support_intent"] is None:
        intent_delta = (
            "support_intent:normalized_unknown"
            if "support_unknown_intent_cleared" in _normalization_categories_from_projection(actual, "support_normalizations")
            else "support_intent:missing"
        )
        deltas.append(intent_delta)
    elif actual["support_intent"] not in expected["support_intents"]:
        deltas.append(f"support_intent:{actual['support_intent']}")
    deltas.extend(
        f"support_normalization:{category}"
        for category in _normalization_categories_from_projection(actual, "support_normalizations")
        if category != "support_unknown_intent_cleared"
    )
    return tuple(deltas)


def _diagnostic_normalization_categories(audit: object) -> tuple[str, ...]:
    if not isinstance(audit, Mapping):
        return ()
    normalization = audit.get("normalization")
    if not isinstance(normalization, Mapping):
        return ()
    categories = normalization.get("categories")
    if not isinstance(categories, list):
        return ()
    return tuple(sorted({
        category
        for category in categories
        if isinstance(category, str) and category in _DIAGNOSTIC_NORMALIZATION_CATEGORIES
    }))


def _normalization_categories_from_projection(actual: Mapping[str, object], field: str) -> tuple[str, ...]:
    value = actual.get(field)
    return value if isinstance(value, tuple) and all(isinstance(item, str) for item in value) else ()


def _provider_failures(diagnostics: Mapping[str, str | None]) -> tuple[str, ...]:
    return tuple(f"{kind}_{diagnostics[f'{kind}_status']}" for kind in ("safety", "support") if diagnostics[f"{kind}_status"] != DiagnosticStatus.COMPLETED.value)


def _provider_failure_metadata(
    store: InMemoryConversationStore,
    diagnostics: Mapping[str, str | None],
) -> tuple[ProviderFailureMetadata, ...]:
    audits = {agent: audit for _, agent, audit in store.agent_runs}
    failures: list[ProviderFailureMetadata] = []
    for agent in ("safety", "support"):
        status = diagnostics.get(f"{agent}_status")
        if status == DiagnosticStatus.COMPLETED.value:
            continue
        diagnostic_status = status if status in {
            DiagnosticStatus.INVALID.value,
            DiagnosticStatus.UNAVAILABLE.value,
        } else DiagnosticStatus.UNAVAILABLE.value
        audit = audits.get(agent, {})
        validation = audit.get("validation_errors")
        fields = _validation_field_categories(validation.get("fields")) if isinstance(validation, Mapping) else ()
        types = _validation_type_categories(validation.get("types")) if isinstance(validation, Mapping) else ()
        failures.append(
            ProviderFailureMetadata(
                agent=agent,
                diagnostic_status=diagnostic_status,
                transport_error_type=_transport_error_category(audit.get("error_type")),
                validation_fields=fields,
                validation_types=types,
                output_envelope=_output_envelope_shape(audit.get("output_shape")),
            )
        )
    return tuple(failures)


def _validation_field_categories(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({
        item.split(".", 1)[0] if item.split(".", 1)[0] in _VALIDATION_FIELD_CATEGORIES else "unknown_field"
        for item in value
        if isinstance(item, str)
    }))


def _validation_type_categories(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(sorted({
        _VALIDATION_TYPE_CATEGORIES.get(item, "other_validation_error")
        for item in value
        if isinstance(item, str)
    }))


def _transport_error_category(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _TRANSPORT_ERROR_CATEGORIES.get(value, "OtherTransportError")


def _output_envelope_shape(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    flags = {name: value.get(name) for name in (
        "nonempty", "starts_json", "ends_object", "starts_code_fence", "ends_code_fence"
    )}
    if not all(isinstance(flag, bool) for flag in flags.values()):
        return None
    if flags["starts_code_fence"] and flags["ends_code_fence"]:
        return "code_fence"
    if flags["starts_json"] and flags["ends_object"]:
        return "json_object"
    if not flags["nonempty"]:
        return "empty"
    return "other_nonempty"


def _hard_hash(projection: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(projection, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _requires_canonical_copy(behavior: Mapping[str, Any]) -> bool:
    return (
        behavior["effect"] != PolicyEffect.NONE.value
        or behavior["choice_set"] != ChoiceSet.NONE.value
    )


def _final_user_text(history: tuple[tuple[str, str], ...]) -> str:
    if not history or history[-1][0] != "user":
        raise DatasetError("case history must end with a user message")
    return history[-1][1]


def _exact_keys(row: Mapping[str, Any], expected: set[str], prefix: str) -> None:
    _keys(row, expected, expected, prefix)


def _keys(row: Mapping[str, Any], required: set[str], allowed: set[str], prefix: str) -> None:
    missing, extra = sorted(required - set(row)), sorted(set(row) - allowed)
    if missing:
        raise DatasetError(f"{prefix}: missing required keys: {', '.join(missing)}")
    if extra:
        raise DatasetError(f"{prefix}: unknown keys: {', '.join(extra)}")


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{label} must be a non-empty string")
    return value


def _enum(value: Any, enum_type: type[Enum], label: str) -> Any:
    try:
        return enum_type(value)
    except ValueError as error:
        raise DatasetError(f"{label} contains invalid enum value") from error


if __name__ == "__main__":
    raise SystemExit(main())
