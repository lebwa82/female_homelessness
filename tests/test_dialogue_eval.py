from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from app.agents import AgentContext, AgentEvaluation
from app.domain import IncomingMessage, RiskAssessment, RiskLevel, SupportPlan
from app.service import ConversationService
from app.store import InMemoryConversationStore
from scripts.dialogue_eval import (
    FixtureGateway,
    evaluate_case,
    evaluate_cases,
    load_cases,
    load_fixture_outputs,
    main,
)

DATASET = Path(__file__).parent / "fixtures" / "dialogue_scenarios.jsonl"
FIXTURE_OUTPUTS = Path(__file__).parent / "fixtures" / "dialogue_agent_outputs.jsonl"


@pytest.mark.asyncio
async def test_fixture_replay_enforces_hand_derived_behavioral_invariants() -> None:
    """Changing resolved behaviour must fail literal, independently stored invariants."""
    cases = load_cases(DATASET)
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)

    reports = await evaluate_cases(FixtureGateway(payloads), cases)

    assert reports.failures == ()
    assert len(reports.cases) >= 48


@pytest.mark.asyncio
async def test_fixture_gateway_consumes_separate_agent_payload_not_expected_invariants() -> None:
    """Fixture output is sourced from the payload fixture, never synthesized from expected."""
    case = load_cases(DATASET)[0]
    payloads = load_fixture_outputs(FIXTURE_OUTPUTS)
    mutated_expected = case.__class__(
        id=case.id,
        group=case.group,
        history=case.history,
        expected={**case.expected, "intent": ["explicit_human_request"]},
    )

    report = await evaluate_case(FixtureGateway.from_case(mutated_expected, payloads), mutated_expected)

    assert "intent" in report.failures


@pytest.mark.asyncio
async def test_production_regression_replays_through_conversation_service() -> None:
    """A request to be heard after an assistant turn must remain an open conversation."""
    store = InMemoryConversationStore()
    service = ConversationService(
        store=store,
        gateway=ScriptedGateway(
            [
                open_conversation_plan("Я рядом. Что сейчас особенно тяжело?"),
                open_conversation_plan("Да, я могу вас выслушать."),
            ]
        ),
    )

    await service.handle_text(identity("мне плохо", 1))
    turn = await service.handle_text(
        identity("мне просто хочется выговориться — ты можешь меня выслушать?", 2)
    )

    assert [choice.id for choice in turn.choices] == ["human"]
    assert store.escalations == []


def test_cli_output_does_not_echo_dialogue_history(capsys: pytest.CaptureFixture[str]) -> None:
    """The evaluator must keep anonymized input text out of CLI output as well."""
    exit_code = main(["--fixtures", str(FIXTURE_OUTPUTS), str(DATASET)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "prod-listen-01" in captured.out
    assert "мне просто хочется выговориться" not in captured.out
    assert "История" not in captured.out


def test_cli_returns_nonzero_for_hard_invariant_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A regression failure must make CI fail without disclosing the input text."""
    broken_payloads = tmp_path / "payloads.jsonl"
    first = json.loads(FIXTURE_OUTPUTS.read_text(encoding="utf-8").splitlines()[0])
    first["plan"]["intent"] = "explicit_human_request"
    lines = [json.dumps(first), *FIXTURE_OUTPUTS.read_text(encoding="utf-8").splitlines()[1:]]
    broken_payloads.write_text("\n".join(lines), encoding="utf-8")

    exit_code = main(["--fixtures", str(broken_payloads), str(DATASET)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "prod-listen-01" in captured.out
    assert "intent" in captured.out
    assert "мне просто хочется выговориться" not in captured.out


@dataclass
class ScriptedGateway:
    evaluations: list[AgentEvaluation]

    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        return self.evaluations.pop(0)


def open_conversation_plan(text: str) -> AgentEvaluation:
    return AgentEvaluation(
        risk=RiskAssessment(level=RiskLevel.NONE, detector="fixture"),
        plan=SupportPlan(
            intent="open_conversation",
            next_action="continue_conversation",
            text=text,
            choice_set="none",
        ),
        risk_audit={"status": "fixture"},
        support_audit={"status": "fixture"},
    )


def identity(text: str, message_id: int) -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=711,
        chat_id=812,
        username="test_identity",
        text=text,
        message_id=message_id,
    )
