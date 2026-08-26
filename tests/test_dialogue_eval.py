from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path

import pytest

import scripts.dialogue_eval as dialogue_eval_module
from app.agents import AgentContext, AgentEvaluation
from app.domain import (
    DiagnosticStatus,
    RiskLevel,
    SafetyDiagnostic,
    SupportDiagnostic,
    SupportOffer,
)
from scripts.dialogue_eval import (
    DatasetError,
    DiagnosticVariant,
    FixtureGateway,
    ProviderFailureMetadata,
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
    assert len(report.cases) == 66
    assert "s11-child-custody" in {case.case_id for case in report.cases}
    assert {
        "suicide-direct-want-die",
        "suicide-direct-kill-self",
        "suicide-direct-self-harm-now",
        "suicide-direct-not-want-live-help",
    } <= {case.case_id for case in report.cases}
    assert {
        "suicide-clause-comma",
        "suicide-clause-dash",
        "suicide-clause-period",
        "suicide-clause-city-distress",
        "suicide-clause-relationship-distress",
    } <= {case.case_id for case in report.cases}
    assert not report.soft_failures


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
        "effect": "replay_workflow",
        "rendered_callback_ids": ("more_help", "finish", "human"),
        "state_after": "aid_requested",
    }


@pytest.mark.asyncio
async def test_evaluator_reports_pending_offer_as_soft_state_without_authorizing_effects() -> None:
    case = next(case for case in load_cases(DATASET) if case.id == "psychologist-considering-01")
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)

    report = await evaluate_case(FixtureGateway.from_case(case, payloads), case)

    assert report.soft_projection == {"pending_offer": "psychologist", "authoritative": False}


@pytest.mark.asyncio
async def test_soft_offer_cases_are_replayed_as_two_sequential_lifecycles() -> None:
    cases = tuple(case for case in load_cases(DATASET) if case.group == "soft_lifecycle")
    offer = AgentEvaluation(
        safety=SafetyDiagnostic(level="none"),
        support=SupportDiagnostic(intent="open_conversation", draft_text="safe", suggested_support=SupportOffer.PSYCHOLOGIST),
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=DiagnosticStatus.COMPLETED,
        safety_audit={"status": "fixture"},
        support_audit={"status": "fixture"},
    )
    ordinary = AgentEvaluation(
        safety=SafetyDiagnostic(level="none"),
        support=SupportDiagnostic(intent="open_conversation", draft_text="safe"),
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=DiagnosticStatus.COMPLETED,
        safety_audit={"status": "fixture"},
        support_audit={"status": "fixture"},
    )
    psychologist_request = AgentEvaluation(
        safety=SafetyDiagnostic(level="none"),
        support=SupportDiagnostic(intent="psychologist_request", draft_text="safe"),
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=DiagnosticStatus.COMPLETED,
        safety_audit={"status": "fixture"},
        support_audit={"status": "fixture"},
    )
    gateway = ScriptedGateway([offer, psychologist_request, offer, ordinary])

    report = await evaluate_cases(gateway, cases)

    assert report.soft_failures == ()
    assert all(case.soft_projection["authoritative"] is False for case in report.cases)
    assert len(gateway.contexts) == 4
    assert any(content == "мне тяжело" for _, content in gateway.contexts[1].history)
    assert any(content == "мне тяжело" for _, content in gateway.contexts[3].history)


@pytest.mark.asyncio
async def test_missing_model_offer_is_a_hard_behavior_failure() -> None:
    """A model-owned offer must be present for its dependent workflow to be available."""
    cases = tuple(case for case in load_cases(DATASET) if case.group == "soft_lifecycle")
    ordinary = AgentEvaluation(
        safety=SafetyDiagnostic(level="none"),
        support=SupportDiagnostic(intent="open_conversation", draft_text="safe"),
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=DiagnosticStatus.COMPLETED,
        safety_audit={"status": "fixture"},
        support_audit={"status": "fixture"},
    )

    report = await evaluate_cases(
        ScriptedGateway([ordinary, ordinary, ordinary, ordinary]),
        cases,
        require_provider_health=True,
    )

    assert report.hard_failures
    assert dialogue_eval_module._release_exit_code(report, live=True) == 1
    assert dialogue_eval_module._release_exit_code(report, live=False) == 1


@pytest.mark.asyncio
async def test_live_health_mode_uses_the_same_accumulated_soft_lifecycle() -> None:
    cases = tuple(case for case in load_cases(DATASET) if case.group == "soft_lifecycle")
    offer = AgentEvaluation(
        safety=SafetyDiagnostic(level="none"),
        support=SupportDiagnostic(
            intent="open_conversation",
            draft_text="safe",
            suggested_support=SupportOffer.PSYCHOLOGIST,
        ),
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=DiagnosticStatus.COMPLETED,
        safety_audit={"status": "fixture"},
        support_audit={"status": "fixture"},
    )
    ordinary = AgentEvaluation(
        safety=SafetyDiagnostic(level="none"),
        support=SupportDiagnostic(intent="open_conversation", draft_text="safe"),
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=DiagnosticStatus.COMPLETED,
        safety_audit={"status": "fixture"},
        support_audit={"status": "fixture"},
    )
    psychologist_request = AgentEvaluation(
        safety=SafetyDiagnostic(level="none"),
        support=SupportDiagnostic(intent="psychologist_request", draft_text="safe"),
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=DiagnosticStatus.COMPLETED,
        safety_audit={"status": "fixture"},
        support_audit={"status": "fixture"},
    )
    gateway = ScriptedGateway([offer, psychologist_request, offer, ordinary])

    report = await evaluate_cases(gateway, cases, require_provider_health=True)

    assert report.hard_failures == ()
    assert len(gateway.contexts) == 4
    assert gateway.contexts[1].history == (
        ("user", "мне тяжело"),
        ("assistant", "safe"),
        ("user", "да хочу"),
    )
    assert gateway.contexts[3].history == (
        ("user", "мне тяжело"),
        ("assistant", "safe"),
        ("user", "о погоде"),
    )


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
async def test_model_risk_is_a_deploy_blocking_hard_expectation() -> None:
    case = load_cases(DATASET)[0]
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)
    mismatched = replace(case, behavior={**case.behavior, "model_risk": "critical"})

    report = await evaluate_case(FixtureGateway.from_case(mismatched, payloads), mismatched)

    assert "model_risk" in report.hard_failures


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

    assert report.hard_failures
    assert len(report.provider_failures) == len(cases) * 2


@pytest.mark.asyncio
async def test_normalized_support_fields_remain_observable() -> None:
    case = load_cases(DATASET)[0]
    evaluation = AgentEvaluation(
        safety=SafetyDiagnostic(level=RiskLevel.NONE),
        support=SupportDiagnostic(intent=None, need_hints=(), draft_text="safe draft"),
        safety_status=DiagnosticStatus.COMPLETED,
        support_status=DiagnosticStatus.COMPLETED,
        safety_audit={"diagnostic_status": "completed", "normalization": {"categories": []}},
        support_audit={
            "diagnostic_status": "completed",
            "normalization": {
                "categories": [
                    "support_unknown_intent_cleared",
                    "support_unknown_need_hints_cleared",
                ]
            },
        },
    )

    report = await evaluate_cases(
        ScriptedGateway([evaluation]), (case,), require_provider_health=True
    )

    assert report.hard_failures == ()
    assert report.provider_failures == ()
    assert report.cases[0].diagnostics["support_normalizations"] == (
        "support_unknown_intent_cleared",
        "support_unknown_need_hints_cleared",
    )
    assert report.cases[0].diagnostic_deltas == (
        "support_intent:normalized_unknown",
        "support_normalization:support_unknown_need_hints_cleared",
    )


@pytest.mark.asyncio
async def test_provider_failure_summary_retains_only_safe_agent_and_cause_metadata() -> None:
    case = load_cases(DATASET)[0]
    evaluation = AgentEvaluation(
        safety_status=DiagnosticStatus.INVALID,
        support_status=DiagnosticStatus.UNAVAILABLE,
        safety_audit={
            "diagnostic_status": "invalid",
            "validation_errors": {
                "fields": ["choice_set", "provider_controlled_extra_key"],
                "types": ["extra_forbidden", "provider_controlled_error_type"],
            },
            "output_shape": {
                "characters": 19,
                "nonempty": True,
                "starts_json": False,
                "ends_object": False,
                "starts_code_fence": True,
                "ends_code_fence": True,
            },
            "input_hash": "hidden-input-hash",
            "response_id": "hidden-response-id",
        },
        support_audit={
            "diagnostic_status": "unavailable",
            "error_type": "ProviderControlledErrorName",
            "error_origin": "hidden-origin",
            "model": "hidden-model",
        },
    )

    report = await evaluate_cases(
        ScriptedGateway([evaluation]), (case,), require_provider_health=True
    )

    assert report.cases[0].provider_failure_metadata == (
        ProviderFailureMetadata(
            agent="safety",
            diagnostic_status="invalid",
            validation_fields=("unknown_field",),
            validation_types=("extra_forbidden", "other_validation_error"),
            output_envelope="code_fence",
        ),
        ProviderFailureMetadata(
            agent="support",
            diagnostic_status="unavailable",
            transport_error_type="OtherTransportError",
        ),
    )
    assert report.provider_failure_summary == {
        "by_agent": {"safety": 1, "support": 1},
        "by_diagnostic_status": {"invalid": 1, "unavailable": 1},
        "by_transport_error_type": {"OtherTransportError": 1},
        "by_validation_field": {"unknown_field": 1},
        "by_validation_type": {"extra_forbidden": 1, "other_validation_error": 1},
        "by_output_envelope": {"code_fence": 1},
    }
    metadata_repr = repr(report.cases[0].provider_failure_metadata)
    assert "hidden-" not in metadata_repr
    assert "input_hash" not in metadata_repr
    assert "response_id" not in metadata_repr
    assert "provider_controlled" not in metadata_repr
    assert "ProviderControlled" not in metadata_repr


def test_cli_output_never_includes_history_or_reply_fields(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--fixtures", str(FIXTURE_OUTPUTS), str(DATASET)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "prod-listen-01" in captured.out
    assert "history" not in captured.out
    assert "draft_text" not in captured.out
    summary = json.loads(captured.out.splitlines()[-1])["summary"]
    assert summary["provider_failure_summary"] == {
        "by_agent": {},
        "by_diagnostic_status": {},
        "by_transport_error_type": {},
        "by_validation_field": {},
        "by_validation_type": {},
        "by_output_envelope": {},
    }


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
