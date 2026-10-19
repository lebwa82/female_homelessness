"""Focused real API/LLM acceptance, isolated from all channel connectors."""
from __future__ import annotations

import argparse
import asyncio
import os
import re
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.scenario import NEEDS, SCREENS
from scripts.chatwoot_full_acceptance import Acceptance


def check_reply_copy(text: str) -> None:
    assert len(re.findall(r"\b(?:здравствуйте|привет)\b", text, re.IGNORECASE)) <= 1, "duplicate_greeting"
    assert not re.search(r"\bя\b[^.!?\n]{0,35}\bготовы\b", text, re.IGNORECASE), "first_person_agreement"
    assert not re.search(
        r"где\s+вы\s+(?:сейчас\s+)?(?:находитесь|жив[её]те|проживаете)|"
        r"вы\s+(?:сейчас\s+)?находитесь|ваш(?:е|его)?\s+(?:местоположение|адрес)|"
        r"в\s+каком\s+городе|где\s+(?:сейчас\s+)?находитесь", text, re.IGNORECASE
    ), "location_question"


class RegressionAcceptance(Acceptance):
    async def regressions(self):
        for screen, need in NEEDS.items():
            async def category(cid, screen=screen, need=need):
                await self.send(cid, "/start")
                await self.send(cid, "continue")
                reply = await self.send(cid, f"need:{need.value}")
                await self.check_screen(cid, screen)
                assert reply["content"] == SCREENS[screen]["texts"][0], "canonical_scenario_copy"
                check_reply_copy(reply["content"])
            await self.case("category_" + need.value, category)

        async def housing(cid):
            reply = await self.send(cid, "мне некуда пойти помогите")
            attrs = await self.attrs(cid)
            await self.check_screen(cid, "s21")
            assert "aid:hostel_3_nights" in self.choices(reply), "housing_offer"
            assert not attrs.get("handoff_requested"), "unwanted_handoff"
            assert reply["content"] == SCREENS["s21"]["texts"][0], "canonical_housing_copy"
            check_reply_copy(reply["content"])
            diagnostics = attrs.get("bot_last_diagnostics") or []
            assert {d["agent"] for d in diagnostics if d["status"] == "completed"} == {
                "safety", "support"}, "model_diagnostics_not_completed"
        await self.case("housing_text", housing)

        async def conversation(cid):
            if self.mode == "candidate":
                # Use a future clock only for menu restoration, not model context.
                service = self.services[cid]
                service._scenario_effects.clock = lambda: datetime.now(UTC) + timedelta(days=2)
            reply = await self.send(cid, "Здравствуйте. Сейчас всё спокойно, просто хочу немного поговорить.")
            check_reply_copy(reply["content"])
            attrs = await self.attrs(cid)
            assert not attrs.get("handoff_requested"), "unwanted_chat_handoff"
            diagnostics = attrs.get("bot_last_diagnostics") or []
            assert {d["agent"] for d in diagnostics if d["status"] == "completed"} == {
                "safety", "support"}, "model_diagnostics_not_completed"
        await self.case("support_language", conversation)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["candidate", "webhook"], required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--inbox-name", help="Exact name of the approved isolated API inbox")
    args = parser.parse_args()
    os.umask(0o077)
    runner = RegressionAcceptance(args.mode, args.output, ["regressions", "ownership"],
                                  inbox_name=args.inbox_name)
    runner.report["provider"] = settings.llm_provider
    runner.report["model"] = settings.timeweb_ai_model if settings.llm_provider == "timeweb" else settings.yandex_ai_model
    raise SystemExit(0 if asyncio.run(runner.run()) else 1)


if __name__ == "__main__":
    main()
