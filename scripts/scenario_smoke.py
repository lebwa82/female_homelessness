"""Run deterministic product scenarios without Telegram or Yandex credentials."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.agents import AgentContext, AgentEvaluation
from app.domain import (
    DiagnosticStatus,
    IncomingMessage,
    RiskLevel,
    SafetyDiagnostic,
    SupportDiagnostic,
)
from app.service import ConversationService
from app.store import InMemoryConversationStore


@dataclass
class SmokeGateway:
    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        return AgentEvaluation(
            safety=SafetyDiagnostic(level=RiskLevel.NONE, confidence=1.0, rationale="smoke"),
            support=SupportDiagnostic(
                intent="open_conversation",
                draft_text="Что сейчас важнее всего?",
            ),
            safety_status=DiagnosticStatus.COMPLETED,
            support_status=DiagnosticStatus.COMPLETED,
            safety_audit={"status": "completed"},
            support_audit={"status": "completed"},
        )


@dataclass
class PsychologistSmokeGateway:
    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        return AgentEvaluation(
            safety=SafetyDiagnostic(level=RiskLevel.NONE, confidence=1.0, rationale="smoke"),
            support=SupportDiagnostic(
                intent="psychologist_request",
                draft_text="Начинаю запрос к психологу.",
            ),
            safety_status=DiagnosticStatus.COMPLETED,
            support_status=DiagnosticStatus.COMPLETED,
            safety_audit={"status": "completed"},
            support_audit={"status": "completed"},
        )


@dataclass
class CriticalSmokeGateway:
    async def evaluate(self, context: AgentContext) -> AgentEvaluation:
        return AgentEvaluation(
            safety=SafetyDiagnostic(
                level=RiskLevel.CRITICAL,
                categories=("suicide",),
                confidence=1.0,
                rationale="smoke",
            ),
            support=SupportDiagnostic(intent="open_conversation", draft_text="Я рядом."),
            safety_status=DiagnosticStatus.COMPLETED,
            support_status=DiagnosticStatus.COMPLETED,
            safety_audit={"status": "completed"},
            support_audit={"status": "completed"},
        )


def incoming(text: str = "", message_id: int = 1) -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=900_001,
        chat_id=900_002,
        username="scenario_smoke",
        text=text,
        message_id=message_id,
    )


async def run_scenarios() -> None:
    open_conversation_store = InMemoryConversationStore()
    open_conversation_service = ConversationService(
        store=open_conversation_store, gateway=SmokeGateway()
    )
    open_turn = await open_conversation_service.handle_text(incoming("мне нужно выговориться"))
    assert [choice.id for choice in open_turn.choices] == ["human"]
    assert not open_conversation_store.escalations

    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=SmokeGateway())

    await service.start(incoming(message_id=11))
    await service.handle_callback(incoming(message_id=12), "continue")
    await service.handle_callback(incoming(message_id=13), "need:food_money")
    await service.handle_callback(incoming(message_id=14), "aid:food_card")
    await service.handle_callback(incoming(message_id=15), "contact:current_telegram")
    assert len(store.aid_requests) == 1
    assert len(store.followup_jobs) == 1

    critical_store = InMemoryConversationStore()
    critical_service = ConversationService(store=critical_store, gateway=CriticalSmokeGateway())
    critical = await critical_service.handle_text(incoming("не хочу жить"))
    assert "8-800-2000-122" in critical.text
    assert critical_store.escalations[-1].level is RiskLevel.CRITICAL

    psychologist_store = InMemoryConversationStore()
    psychologist_service = ConversationService(store=psychologist_store, gateway=PsychologistSmokeGateway())
    contact = await psychologist_service.handle_text(incoming("хочу поговорить с психологом"))
    assert any(choice.id == "contact:current_telegram" for choice in contact.choices)
    await psychologist_service.handle_callback(incoming(), "contact:current_telegram")
    assert psychologist_store.aid_requests[-1].aid_id == "psychologist_3_sessions"


def main() -> None:
    asyncio.run(run_scenarios())
    print("Scenario smoke: aid, open conversation, psychologist request and crisis paths passed")


if __name__ == "__main__":
    main()
