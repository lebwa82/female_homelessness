from dataclasses import dataclass

import pytest

from app.agents import AgentEvaluation
from app.domain import AgentAction, RiskAssessment, RiskLevel
from scripts.llm_health_check import check_structured


@dataclass
class HealthyGateway:
    async def evaluate(self, context):  # type: ignore[no-untyped-def]
        return AgentEvaluation(
            risk=RiskAssessment(level=RiskLevel.NONE, detector="model"),
            action=AgentAction(kind="reply", text="Я рядом."),
            risk_audit={"status": "completed"},
            action_audit={"status": "completed"},
        )


@dataclass
class UnhealthyGateway:
    async def evaluate(self, context):  # type: ignore[no-untyped-def]
        return AgentEvaluation(
            risk=RiskAssessment(level=RiskLevel.UNKNOWN, detector="model"),
            action=None,
            risk_audit={"status": "validation_error"},
            action_audit={"status": "validation_error"},
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
