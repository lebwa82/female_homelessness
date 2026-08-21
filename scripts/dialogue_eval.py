"""Replay versioned dialogue cases without emitting their conversation content."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from app.agents import AgentContext, AgentEvaluation, YandexAgentGateway
from app.domain import PolicyEffect, PolicySideEffect, RiskAssessment, SupportPlan
from app.policy import resolve_turn


class DatasetError(ValueError):
    """A non-secret explanation of an invalid evaluation fixture."""


@dataclass(frozen=True)
class DialogueCase:
    id: str
    group: str
    history: tuple[tuple[str, str], ...]
    expected: dict[str, Any]


@dataclass(frozen=True)
class FixtureOutput:
    risk: dict[str, Any]
    plan: dict[str, Any] | None


@dataclass(frozen=True)
class CaseReport:
    case_id: str
    classification: dict[str, Any]
    failures: tuple[str, ...]


@dataclass(frozen=True)
class EvalReport:
    cases: tuple[CaseReport, ...]

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(f"{case.case_id}:{failure}" for case in self.cases for failure in case.failures)


class Gateway(Protocol):
    async def evaluate(self, context: AgentContext) -> AgentEvaluation: ...


class FixtureGateway:
    """Offline gateway that uses recorded model payloads keyed only by case ID."""

    def __init__(self, outputs: Mapping[str, FixtureOutput], case_id: str | None = None) -> None:
        self._outputs = outputs
        self._case_id = case_id

    @classmethod
    def from_case(
        cls, case: DialogueCase, outputs: Mapping[str, FixtureOutput]
    ) -> FixtureGateway:
        return cls(outputs, case.id)

    def for_case(self, case: DialogueCase) -> FixtureGateway:
        return self.from_case(case, self._outputs)

    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        del context
        if self._case_id is None:
            raise DatasetError("fixture gateway requires a selected case")
        try:
            output = self._outputs[self._case_id]
        except KeyError as error:
            raise DatasetError(f"missing fixture output for case id: {self._case_id}") from error
        try:
            return AgentEvaluation(
                risk=RiskAssessment.model_validate(output.risk),
                plan=SupportPlan.model_validate(output.plan) if output.plan is not None else None,
                risk_audit={"status": "fixture"},
                support_audit={"status": "fixture"},
            )
        except ValueError as error:
            raise DatasetError(f"invalid fixture output for case id: {self._case_id}") from error


def load_cases(path: Path | str) -> tuple[DialogueCase, ...]:
    """Load a strict, version-controlled JSONL case file."""
    rows = _load_jsonl(Path(path), "dataset")
    cases: list[DialogueCase] = []
    seen_ids: set[str] = set()
    for line_number, row in rows:
        case = _parse_case(row, line_number)
        if case.id in seen_ids:
            raise DatasetError(f"dataset line {line_number}: duplicate case id: {case.id}")
        seen_ids.add(case.id)
        cases.append(case)
    if not cases:
        raise DatasetError("dataset contains no cases")
    return tuple(cases)


def load_fixture_outputs(path: Path | str) -> dict[str, FixtureOutput]:
    """Load agent payload fixtures kept separate from expected behavioural invariants."""
    rows = _load_jsonl(Path(path), "fixture outputs")
    outputs: dict[str, FixtureOutput] = {}
    for line_number, row in rows:
        _require_exact_keys(row, {"id", "risk", "plan"}, f"fixture outputs line {line_number}")
        case_id = _required_nonempty_string(row.get("id"), f"fixture outputs line {line_number}: id")
        if case_id in outputs:
            raise DatasetError(f"fixture outputs line {line_number}: duplicate case id: {case_id}")
        risk = row.get("risk")
        plan = row.get("plan")
        if not isinstance(risk, dict):
            raise DatasetError(f"fixture outputs line {line_number}: risk must be an object")
        if plan is not None and not isinstance(plan, dict):
            raise DatasetError(f"fixture outputs line {line_number}: plan must be an object or null")
        outputs[case_id] = FixtureOutput(risk=risk, plan=plan)
    if not outputs:
        raise DatasetError("fixture outputs contains no rows")
    return outputs


async def evaluate_case(gateway: Gateway, case: DialogueCase) -> CaseReport:
    """Evaluate the final turn from a case history against literal invariants."""
    evaluation = await gateway.evaluate(AgentContext(history=case.history, state="open_conversation"))
    decision = resolve_turn(evaluation.risk, evaluation.plan, "open_conversation")
    classification = {
        "risk": evaluation.risk.level.value,
        "intent": evaluation.plan.intent.value if evaluation.plan is not None else None,
        "choice_set": decision.choice_set.value,
        "effect": decision.effect.value,
        "escalation": _has_escalation(decision.effect, decision.side_effects),
    }
    return CaseReport(
        case_id=case.id,
        classification=classification,
        failures=tuple(_check_expectations(case.expected, evaluation, decision, classification)),
    )


async def evaluate_cases(gateway: Gateway, cases: Sequence[DialogueCase]) -> EvalReport:
    reports: list[CaseReport] = []
    for case in cases:
        case_gateway: Gateway = gateway.for_case(case) if isinstance(gateway, FixtureGateway) else gateway
        reports.append(await evaluate_case(case_gateway, case))
    return EvalReport(cases=tuple(reports))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate anonymized dialogue invariants.")
    parser.add_argument("dataset", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixtures", type=Path, help="offline agent payload fixtures")
    mode.add_argument("--live", action="store_true", help="evaluate with the configured Yandex gateway")
    args = parser.parse_args(argv)
    try:
        cases = load_cases(args.dataset)
        gateway: Gateway
        if args.live:
            gateway = YandexAgentGateway()
        else:
            gateway = FixtureGateway(load_fixture_outputs(args.fixtures))
        report = asyncio.run(evaluate_cases(gateway, cases))
    except DatasetError as error:
        print(f"dataset_error:{error}", file=sys.stderr)
        return 2

    for case in report.cases:
        print(
            json.dumps(
                {
                    "case_id": case.case_id,
                    "classification": case.classification,
                    "failures": case.failures,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    return 1 if report.failures else 0


def _load_jsonl(path: Path, label: str) -> list[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise DatasetError(f"cannot read {label}: {path}") from error
    rows: list[tuple[int, dict[str, Any]]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise DatasetError(f"{label} line {line_number}: blank lines are not allowed")
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise DatasetError(f"{label} line {line_number}: invalid JSON") from error
        if not isinstance(row, dict):
            raise DatasetError(f"{label} line {line_number}: row must be an object")
        rows.append((line_number, row))
    return rows


def _parse_case(row: dict[str, Any], line_number: int) -> DialogueCase:
    prefix = f"dataset line {line_number}"
    _require_exact_keys(row, {"id", "group", "history", "expected"}, prefix)
    case_id = _required_nonempty_string(row.get("id"), f"{prefix}: id")
    group = _required_nonempty_string(row.get("group"), f"{prefix}: group")
    history_value = row.get("history")
    if not isinstance(history_value, list) or not history_value:
        raise DatasetError(f"{prefix}: history must be a non-empty array")
    history: list[tuple[str, str]] = []
    for index, message in enumerate(history_value):
        if not isinstance(message, list) or len(message) != 2:
            raise DatasetError(f"{prefix}: history[{index}] must be a [role, text] pair")
        role, text = message
        if role not in {"user", "assistant"}:
            raise DatasetError(f"{prefix}: invalid history role")
        history.append((role, _required_nonempty_string(text, f"{prefix}: history[{index}] text")))
    expected = row.get("expected")
    if not isinstance(expected, dict) or not expected:
        raise DatasetError(f"{prefix}: expected must be a non-empty object")
    allowed_expected = {"risk", "intent", "choice_set", "effect", "contains", "escalation"}
    unknown = sorted(set(expected) - allowed_expected)
    if unknown:
        raise DatasetError(f"{prefix}: unknown expected keys: {', '.join(unknown)}")
    _validate_expected(expected, prefix)
    return DialogueCase(id=case_id, group=group, history=tuple(history), expected=expected)


def _validate_expected(expected: dict[str, Any], prefix: str) -> None:
    for key in ("risk", "intent"):
        if key in expected and (
            not isinstance(expected[key], list)
            or not expected[key]
            or not all(isinstance(value, str) and value for value in expected[key])
        ):
            raise DatasetError(f"{prefix}: expected.{key} must be a non-empty string array")
    for key in ("choice_set", "effect", "contains"):
        if key in expected and not isinstance(expected[key], str):
            raise DatasetError(f"{prefix}: expected.{key} must be a string")
    if "escalation" in expected and not isinstance(expected["escalation"], bool):
        raise DatasetError(f"{prefix}: expected.escalation must be a boolean")


def _check_expectations(
    expected: Mapping[str, Any],
    evaluation: AgentEvaluation,
    decision: Any,
    classification: Mapping[str, Any],
) -> list[str]:
    failures: list[str] = []
    if "risk" in expected and evaluation.risk.level.value not in expected["risk"]:
        failures.append("risk")
    intent = evaluation.plan.intent.value if evaluation.plan is not None else None
    if "intent" in expected and intent not in expected["intent"]:
        failures.append("intent")
    for key in ("choice_set", "effect", "escalation"):
        if key in expected and classification[key] != expected[key]:
            failures.append(key)
    if "contains" in expected and expected["contains"] not in decision.text:
        failures.append("contains")
    return failures


def _has_escalation(effect: PolicyEffect, side_effects: tuple[PolicySideEffect, ...]) -> bool:
    return effect in {PolicyEffect.HUMAN_HANDOFF, PolicyEffect.CRITICAL_ESCALATION} or (
        PolicySideEffect.RECORD_SAFETY in side_effects
    )


def _require_exact_keys(row: Mapping[str, Any], expected: set[str], prefix: str) -> None:
    missing = sorted(expected - set(row))
    extra = sorted(set(row) - expected)
    if missing:
        raise DatasetError(f"{prefix}: missing required keys: {', '.join(missing)}")
    if extra:
        raise DatasetError(f"{prefix}: unknown keys: {', '.join(extra)}")


def _required_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{label} must be a non-empty string")
    return value


if __name__ == "__main__":
    raise SystemExit(main())
