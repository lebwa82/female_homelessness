from dataclasses import dataclass

import pytest

from app.agents import AgentEvaluation
from app.domain import RiskAssessment, RiskLevel, SupportPlan
from scripts.llm_health_check import check_structured


@dataclass
class HealthyGateway:
    async def evaluate(self, context):  # type: ignore[no-untyped-def]
        return AgentEvaluation(
            risk=RiskAssessment(level=RiskLevel.NONE, detector="model"),
            plan=SupportPlan(
                intent="open_conversation",
                next_action="continue_conversation",
                text="Я рядом.",
            ),
            risk_audit={"status": "completed"},
            support_audit={"status": "completed"},
        )


@dataclass
class UnhealthyGateway:
    async def evaluate(self, context):  # type: ignore[no-untyped-def]
        return AgentEvaluation(
            risk=RiskAssessment(level=RiskLevel.UNKNOWN, detector="model"),
            plan=None,
            risk_audit={"status": "validation_error"},
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
