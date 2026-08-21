from dataclasses import dataclass

import pytest

from app.agents import AgentEvaluation
from app.domain import DiagnosticStatus, RiskLevel, SafetyDiagnostic, SupportDiagnostic
from scripts.llm_health_check import check_structured


@dataclass
class HealthyGateway:
    async def evaluate(self, context):  # type: ignore[no-untyped-def]
        return AgentEvaluation(
            safety=SafetyDiagnostic(level=RiskLevel.NONE, confidence=1.0, rationale="diagnostic"),
            support=SupportDiagnostic(
                intent="open_conversation",
                draft_text="Я рядом.",
            ),
            safety_status=DiagnosticStatus.COMPLETED,
            support_status=DiagnosticStatus.COMPLETED,
            safety_audit={"status": "completed"},
            support_audit={"status": "completed"},
        )


@dataclass
class UnhealthyGateway:
    async def evaluate(self, context):  # type: ignore[no-untyped-def]
        return AgentEvaluation(
            safety=None,
            support=None,
            safety_status=DiagnosticStatus.INVALID,
            support_status=DiagnosticStatus.INVALID,
            safety_audit={"status": "validation_error"},
            support_audit={"status": "validation_error"},
        )


@pytest.mark.asyncio
async def test_structured_health_check_accepts_two_valid_agent_results(capsys: pytest.CaptureFixture[str]) -> None:
    assert await check_structured(HealthyGateway()) == 0

    output = capsys.readouterr().out
    assert "structured agents ok" in output
    assert "Я рядом" not in output


@pytest.mark.asyncio
async def test_structured_health_check_fails_closed_when_result_is_invalid() -> None:
    assert await check_structured(UnhealthyGateway()) == 1
