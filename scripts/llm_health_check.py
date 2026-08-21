"""Make a safe structured connectivity check to Yandex AI Studio.

It deliberately exercises the same two PydanticAI agents used by the bot. It
never prints the API key, request body, response text, or provider error body.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from app.agents import AgentContext, AgentEvaluation, YandexAgentGateway
from app.domain import DiagnosticStatus


class AgentGateway(Protocol):
    async def evaluate(self, context: AgentContext) -> AgentEvaluation: ...


async def check_structured(gateway: AgentGateway | None = None) -> int:
    evaluation = await (gateway or YandexAgentGateway()).evaluate(
        AgentContext(
            history=(("user", "Проверка доступности. Мне нужна еда."),),
            state="health_check",
        )
    )
    healthy = (
        evaluation.safety_status is DiagnosticStatus.COMPLETED
        and evaluation.support_status is DiagnosticStatus.COMPLETED
        and evaluation.safety is not None
        and evaluation.support is not None
    )
    if healthy:
        print("LLM health-check: structured agents ok")
        return 0
    print("LLM health-check: structured agent validation failed")
    print(
        "LLM health-check diagnostics: "
        f"risk={evaluation.risk_audit.get('status', 'unknown')}/"
        f"{evaluation.risk_audit.get('error_type', 'none')}@"
        f"{evaluation.risk_audit.get('error_origin', 'none')}, "
        f"support={evaluation.support_audit.get('status', 'unknown')}/"
        f"{evaluation.support_audit.get('error_type', 'none')}@"
        f"{evaluation.support_audit.get('error_origin', 'none')}"
    )
    return 1


async def main() -> int:
    return await check_structured()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
