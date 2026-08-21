"""Run deterministic product scenarios without Telegram or Yandex credentials."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.agents import AgentContext, AgentEvaluation
from app.domain import IncomingMessage, RiskAssessment, RiskLevel, SupportPlan
from app.service import ConversationService
from app.store import InMemoryConversationStore


@dataclass
class SmokeGateway:
    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        return AgentEvaluation(
            risk=RiskAssessment(level=RiskLevel.NONE, detector="smoke"),
            plan=SupportPlan(
                intent="open_conversation",
                next_action="continue_conversation",
                text="Что сейчас важнее всего?",
            ),
            risk_audit={"status": "completed"},
            support_audit={"status": "completed"},
        )


@dataclass
class PsychologistSmokeGateway:
    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        return AgentEvaluation(
            risk=RiskAssessment(level=RiskLevel.NONE, detector="smoke"),
            plan=SupportPlan(
                intent="psychologist_request",
                next_action="start_psychologist_request",
                text="Начинаю запрос к психологу.",
            ),
            risk_audit={"status": "completed"},
            support_audit={"status": "completed"},
        )


def incoming(text: str = "") -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=900_001,
        chat_id=900_002,
        username="scenario_smoke",
        text=text,
        message_id=1,
    )


async def run_scenarios() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=SmokeGateway())

    await service.start(incoming())
    await service.handle_callback(incoming(), "continue")
    await service.handle_callback(incoming(), "need:food_money")
    await service.handle_callback(incoming(), "aid:food_card")
    await service.handle_callback(incoming(), "contact:current_telegram")
    assert len(store.aid_requests) == 1
    assert len(store.followup_jobs) == 1

    critical = await service.handle_text(incoming("не хочу жить"))
    assert "8-800-2000-122" in critical.text
    assert store.escalations[-1].level is RiskLevel.CRITICAL

    psychologist_store = InMemoryConversationStore()
    psychologist_service = ConversationService(store=psychologist_store, gateway=PsychologistSmokeGateway())
    contact = await psychologist_service.handle_text(incoming("хочу поговорить с психологом"))
    assert any(choice.id == "contact:current_telegram" for choice in contact.choices)
    await psychologist_service.handle_callback(incoming(), "contact:current_telegram")
    assert psychologist_store.aid_requests[-1].aid_id == "psychologist_3_sessions"


def main() -> None:
    asyncio.run(run_scenarios())
    print("Scenario smoke: aid flow and crisis escalation passed")


if __name__ == "__main__":
    main()
