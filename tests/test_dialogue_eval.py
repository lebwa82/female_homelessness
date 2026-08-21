from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path

import pytest

from app.agents import AgentContext, AgentEvaluation
from scripts.dialogue_eval import (
    DatasetError,
    DiagnosticVariant,
    FixtureGateway,
    evaluate_case,
    evaluate_cases,
    evaluate_offline_cases,
    load_cases,
    load_fixture_outputs,
    main,
)

DATASET = Path(__file__).parent / "fixtures" / "dialogue_scenarios.jsonl"
FIXTURE_OUTPUTS = Path(__file__).parent / "fixtures" / "dialogue_agent_outputs.jsonl"


@pytest.mark.asyncio
async def test_fixture_replay_has_no_hard_failures_and_retains_all_cases() -> None:
    """Versioned diagnostic fixtures must exercise the service path without weakening cases."""
    cases = load_cases(DATASET)
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)

    report = await evaluate_offline_cases(FixtureGateway(payloads), cases)

    assert report.hard_failures == ()
    assert report.diagnostic_deltas == ()
    assert len(report.cases) == 53


@pytest.mark.asyncio
async def test_evaluator_replays_pending_offer_and_active_workflow_through_service() -> None:
    """Direct policy resolution misses persisted context that changes rendered service output."""
    cases = load_cases(DATASET)
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)

    report = await evaluate_cases(FixtureGateway(payloads), cases)
    by_id = {case.case_id: case for case in report.cases}

    assert {key: by_id["psychologist-considering-01"].hard_projection[key] for key in (
        "effect", "rendered_callback_ids", "state_after"
    )} == {
        "effect": "none",
        "rendered_callback_ids": ("support:psychologist", "human"),
        "state_after": "open_conversation",
    }
    assert {key: by_id["multi-aid-completion-open-01"].hard_projection[key] for key in (
        "effect", "rendered_callback_ids", "state_after"
    )} == {
        "effect": "none",
        "rendered_callback_ids": ("human",),
        "state_after": "open_conversation",
    }


@pytest.mark.asyncio
async def test_fixture_gateway_consumes_separate_agent_payload_not_expected_invariants() -> None:
    """Diagnostic labels remain observable but cannot become behavioural authority."""
    case = load_cases(DATASET)[0]
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)
    mutated = replace(
        case,
        diagnostics={**case.diagnostics, "support_intents": ("explicit_human_request",)},
    )

    report = await evaluate_case(FixtureGateway.from_case(mutated, payloads), mutated)

    assert report.hard_failures == ()
    assert report.diagnostic_deltas == ("support_intent:open_conversation",)


@pytest.mark.asyncio
async def test_rule_ids_are_a_deploy_blocking_hard_expectation() -> None:
    case = load_cases(DATASET)[0]
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)
    mismatched = replace(case, behavior={**case.behavior, "rule_ids": ("unexpected.rule",)})

    report = await evaluate_case(FixtureGateway.from_case(mismatched, payloads), mismatched)

    assert "rule_ids" in report.hard_failures


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["missing", "surplus"])
async def test_fixture_ids_must_exactly_match_dataset_before_replay(mode: str) -> None:
    """A stale fixture file must fail before any case can be evaluated."""
    cases = load_cases(DATASET)
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)
    if mode == "missing":
        payloads.pop("prod-listen-01")
    else:
        payloads["surplus-case"] = payloads["prod-listen-01"]

    with pytest.raises(DatasetError, match=f"fixture IDs must exactly match dataset IDs: {mode}"):
        await evaluate_cases(FixtureGateway(payloads), cases)


@pytest.mark.asyncio
async def test_production_regression_replays_through_conversation_service() -> None:
    """The stored production prefix reaches the gateway intact and remains a free conversation."""
    case = next(case for case in load_cases(DATASET) if case.id == "prod-listen-01")
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)
    gateway = RecordingGateway(FixtureGateway.from_case(case, payloads))

    report = await evaluate_case(gateway, case)

    assert report.hard_failures == ()
    assert report.hard_projection["rendered_callback_ids"] == ("human",)
    assert report.hard_projection["effect"] == "none"
    assert report.hard_projection["state_after"] == "open_conversation"
    assert report.hard_projection["escalation_count"] == 0
    assert len(gateway.contexts) == 1
    assert _history_digest(gateway.contexts[0].history) == _history_digest(case.history)


@pytest.mark.asyncio
async def test_provider_health_failure_is_separate_from_hard_behavior() -> None:
    cases = load_cases(DATASET)
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)

    report = await evaluate_cases(
        FixtureGateway(payloads).with_variant(DiagnosticVariant.UNAVAILABLE),
        cases,
        require_provider_health=True,
    )

    assert report.hard_failures == ()
    assert len(report.provider_failures) == len(cases) * 2


def test_cli_output_never_includes_history_or_reply_fields(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--fixtures", str(FIXTURE_OUTPUTS), str(DATASET)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "prod-listen-01" in captured.out
    assert "history" not in captured.out
    assert "draft_text" not in captured.out


def test_cli_returns_nonzero_for_hard_invariant_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A regression failure must make CI fail without disclosing the input text."""
    broken_dataset = tmp_path / "cases.jsonl"
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines()]
    rows[0]["expected"]["behavior"]["state_after"] = "closed"
    broken_dataset.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    exit_code = main(["--fixtures", str(FIXTURE_OUTPUTS), str(broken_dataset)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "prod-listen-01" in captured.out
    assert "state_after" in captured.out
    assert "history" not in captured.out


@dataclass
class ScriptedGateway:
    evaluations: list[AgentEvaluation]
    contexts: list[AgentContext] = field(default_factory=list)

    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        self.contexts.append(context)
        return self.evaluations.pop(0)


@dataclass
class RecordingGateway:
    delegate: FixtureGateway
    contexts: list[AgentContext] = field(default_factory=list)

    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        self.contexts.append(context)
        return await self.delegate.evaluate(context)


def _history_digest(history: tuple[tuple[str, str], ...]) -> str:
    return sha256(json.dumps(history, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
