"""Real Chatwoot/Qwen acceptance in isolated API conversations, never Telegram.

Candidate mode explicitly dispatches recorded events to the candidate service;
webhook mode waits for the deployed service. Reports distinguish these modes.
Synthetic certificates exercise delivery/copy without consuming the real stock.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import json
import os
import time
import traceback
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.agents import YandexAgentGateway
from app.certificate_documents import ParsedCertificatePdf, S3CertificateObjectStore
from app.chatwoot.client import AiohttpChatwootTransport, ChatwootClient
from app.chatwoot.contracts import ConversationChanged, IncomingChatwootMessage, StaffMessage
from app.chatwoot.service import ChatwootAgentService
from app.config import settings
from app.domain import IncomingMessage
from app.scenario import AIDS, CERTIFICATES, CONSULTATION_IDS, NEEDS, SCREENS, button_indices
from app.service import ConversationService
from app.store import CertificateClaimResult, InMemoryConversationStore, StoredCertificate
from scripts.chatwoot_acceptance import TEST_CONTACT, TestConversationTransport, save_report
from scripts.dialogue_eval import load_cases

INBOX = "Приёмка нового сценария — без Telegram"


class ScopedTransport(AiohttpChatwootTransport):
    def __init__(self, base_url):
        super().__init__(base_url)
        self.scope = TestConversationTransport(settings.chatwoot_account_id)

    async def request(self, method, path, token, payload=None):
        self.scope.check_write(method, path, payload)
        return await super().request(method, path, token, payload)

    async def request_multipart(self, method, path, token, fields, attachment):
        self.scope.check_write(method, path, fields)
        return await super().request_multipart(method, path, token, fields, attachment)


class RecordedGateway(YandexAgentGateway):
    def __init__(self):
        super().__init__()
        self.last = None

    async def evaluate(self, context):
        self.last = await super().evaluate(context)
        return self.last


class Acceptance:
    def __init__(self, mode, output, suites, limit=0):
        self.mode, self.output, self.suites, self.limit = mode, Path(output), suites, limit
        self.run_id = str(uuid.uuid4())
        self.raw = AiohttpChatwootTransport(settings.chatwoot_base_url)
        self.transport = ScopedTransport(settings.chatwoot_base_url)
        self.api = ChatwootClient(base_url=settings.chatwoot_base_url,
                                  account_id=settings.chatwoot_account_id,
                                  read_token=settings.chatwoot_read_token,
                                  bot_token=settings.chatwoot_bot_token, transport=self.transport)
        self.base = f"/api/v1/accounts/{settings.chatwoot_account_id}"
        self.report = {"run_id": self.run_id, "mode": mode, "cases": [], "limitations": [
            "No Telegram transport test", "Synthetic certificate, not production inventory",
            "Timer checks use controlled clock; no real multi-day waiting",
        ]}
        self.report["model"] = settings.yandex_ai_model
        self.report["client_transport"] = "Chatwoot public contact API"
        source = hashlib.sha256()
        for path in sorted(Path("app").rglob("*")):
            if path.suffix in {".py", ".json"}:
                source.update(str(path).encode())
                source.update(path.read_bytes())
        self.report["source_sha256"] = source.hexdigest()
        self.services = {}
        self.contacts = {}
        self.source_ids = {}
        self.turns = 0

    async def raw_request(self, method, suffix, payload=None):
        return await self.raw.request(method, self.base + suffix, settings.chatwoot_read_token, payload)

    async def setup(self):
        if not settings.llm_enabled:
            raise RuntimeError("acceptance_requires_live_llm")
        inboxes = (await self.raw_request("GET", "/inboxes"))["payload"]
        name = INBOX if self.mode == "candidate" else "Техническая проверка интеграции"
        inbox = next((i for i in inboxes if i.get("name") == name), None)
        if inbox is None and self.mode == "candidate":
            inbox = await self.raw_request("POST", "/inboxes", {
                "name": name, "channel": {"type": "api"}, "enable_auto_assignment": False,
            })
        if inbox is None:
            raise RuntimeError("isolated_inbox_missing")
        self.inbox = await self.raw_request("GET", f"/inboxes/{inbox['id']}")
        if (self.inbox.get("channel_type") != "Channel::Api" or self.inbox.get("webhook_url")
                or self.inbox.get("callback_webhook_url")):
            raise RuntimeError("unsafe_acceptance_inbox")
        binding = await self.raw_request("GET", f"/inboxes/{self.inbox['id']}/agent_bot")
        has_bot = bool((binding.get("agent_bot") or {}).get("id"))
        if has_bot != (self.mode == "webhook"):
            raise RuntimeError("wrong_acceptance_binding")
        profile = await self.raw.request("GET", "/api/v1/profile", settings.chatwoot_read_token)
        self.staff_id = profile["id"]

    async def fresh(self, label):
        contact = await self.raw_request("POST", "/contacts", {
            "inbox_id": self.inbox["id"], "name": TEST_CONTACT,
            "identifier": f"acceptance:{self.run_id}:{label}",
        })
        contact = contact["payload"]["contact"]
        source = next(i["source_id"] for i in contact["contact_inboxes"]
                      if i["inbox"]["id"] == self.inbox["id"])
        conversation = await self.raw_request("POST", "/conversations", {
            "inbox_id": self.inbox["id"], "contact_id": contact["id"], "source_id": source,
            "status": "pending", "custom_attributes": {"acceptance_run_id": self.run_id},
        })
        cid = int(conversation["id"])
        self.transport.scope.conversation_ids.add(cid)
        self.contacts[cid], self.source_ids[cid] = contact["id"], source
        if self.mode == "candidate":
            self.services[cid] = ChatwootAgentService(
                self.api, gateway=RecordedGateway(), duty_team_id=settings.chatwoot_duty_team_id,
                certificate_claim=self.synthetic_claim,
            )
        return cid

    async def synthetic_claim(self, aid, key, recipient):
        return CertificateClaimResult("issued", StoredCertificate(
            aid_id=aid, provider="Acceptance synthetic — not valid", nominal_rubles=3000,
            activation_code="TEST-NOT-REDEEMABLE", serial_number="TEST-NOT-REDEEMABLE",
            expires_at=datetime.now(UTC) + timedelta(days=7), issuance_key=key, is_test=True,
        ))

    async def post(self, cid, suffix, payload):
        return await self.transport.request("POST", f"{self.base}/conversations/{cid}{suffix}",
                                            settings.chatwoot_read_token, payload)

    async def current(self, cid):
        return await self.api.get_conversation(cid)

    async def attrs(self, cid):
        return (await self.current(cid)).get("custom_attributes") or {}

    async def send(self, cid, text, *, expect_reply=True):
        started = time.monotonic()
        # Send as the synthetic contact through the same public API used by
        # clients, not as an administrator fabricating an incoming message.
        path = (f"/public/api/v1/inboxes/{self.inbox['inbox_identifier']}/contacts/"
                f"{self.source_ids[cid]}/conversations/{cid}/messages")
        message = await self.raw.request("POST", path, "", {"content": text})
        self.turns += 1
        if self.mode == "candidate":
            await self.services[cid].process(IncomingChatwootMessage(
                message["id"], cid, self.contacts[cid], self.inbox["id"], text,
            ))
        key = f"message:{message['id']}"
        deadline = time.monotonic() + (45 if expect_reply else 3)
        reply = None
        while time.monotonic() < deadline:
            messages = await self.api.get_messages(cid)
            reply = next((m for m in messages
                          if (m.get("content_attributes") or {}).get("bot_turn_key") == key), None)
            if reply:
                break
            if self.mode == "candidate":
                break
            await asyncio.sleep(.3)
        assert bool(reply) == expect_reply, "reply_presence"
        if reply:
            assert reply.get("content") and not reply.get("private"), "public_nonempty_reply"
            assert (reply.get("sender") or {}).get("type") == "agent_bot", "bot_identity"
            choices = self.choices(reply)
            assert "human" in choices, "human_button_missing"
            assert len(choices) == len(set(choices)), "duplicate_buttons"
            self.active_case.setdefault("turns", []).append({
                "input": text, "response": reply["content"], "buttons": choices,
                "message_id": message["id"], "reply_id": reply["id"],
                "seconds": round(time.monotonic() - started, 3),
            })
        return reply

    @staticmethod
    def choices(reply):
        return [i["value"] for i in (reply.get("content_attributes") or {}).get("items", [])]

    async def staff(self, cid, text, *, private=False, completed=False, returning=False, scheduling=None):
        m = await self.post(cid, "/messages", {"content": text, "message_type": "outgoing",
                                               "private": private})
        if self.mode == "candidate":
            await self.services[cid].process(StaffMessage(m["id"], cid, self.staff_id,
                                                        return_to_bot=returning,
                                                        consultation_completed=completed,
                                                        consultation_schedule=scheduling))
        else:
            for _ in range(60):
                watermark = "consultation_schedule_last_message_id" if scheduling else "ownership_last_staff_message_id"
                if (await self.attrs(cid)).get(watermark, 0) >= m["id"]:
                    break
                await asyncio.sleep(.3)
            else:
                raise AssertionError("staff_event_not_processed")
        return m

    async def native_change(self, cid, suffix, payload):
        await self.post(cid, suffix, payload)
        if self.mode == "candidate":
            await self.services[cid].process(ConversationChanged(cid))
        else:
            await asyncio.sleep(1)

    async def check_screen(self, cid, screen):
        assert (await self.attrs(cid)).get("scenario", {}).get("screen") == screen, f"screen:{screen}"

    async def choose(self, cid, reply, target):
        attrs = await self.attrs(cid)
        screen = attrs.get("scenario", {}).get("screen")
        for value in self.choices(reply):
            if value.startswith("sc:") and SCREENS[screen]["buttons"][int(value.split(":")[2])]["target"] == target:
                return await self.send(cid, value)
            if value.startswith("need:") and NEEDS.get(target) and value == f"need:{NEEDS[target].value}":
                return await self.send(cid, value)
            if value.startswith("aid:") and AIDS.get(target) and value == f"aid:{AIDS[target]}":
                return await self.send(cid, value)
        raise AssertionError(f"missing_button_to:{target}")

    async def case(self, name, callback):
        cid = await self.fresh(name)
        self.active_case = entry = {"name": name, "conversation_id": cid, "passed": False}
        started = time.monotonic()
        try:
            await callback(cid)
            entry["passed"] = True
        except Exception as error:  # noqa: BLE001 - retain metadata, never dump API errors
            frame = traceback.extract_tb(error.__traceback__)[-1]
            entry.update(error_type=type(error).__name__, function=frame.name, line=frame.lineno)
            if isinstance(error, AssertionError):
                entry["check"] = str(error)[:180]
        finally:
            # Only this run's synthetic conversations; retain all messages as evidence.
            await self.api.set_custom_attributes(cid, {"scenario_followups": {}, "scenario_pending_input": None})
            await self.api.set_status(cid, "resolved")
            entry["seconds"] = round(time.monotonic() - started, 2)
            self.report["cases"].append(entry)
            self.save()
            print(json.dumps({k: v for k, v in entry.items() if k in {
                "name", "conversation_id", "passed", "seconds", "check", "error_type", "button_edges",
            }}, ensure_ascii=False), flush=True)

    def save(self):
        self.report["client_turns"] = self.turns
        self.report["summary"] = {
            "dialogs": len(self.report["cases"]),
            "passed": sum(c["passed"] for c in self.report["cases"]),
            "failed": sum(not c["passed"] for c in self.report["cases"]),
        }
        classified = [c for c in self.report["cases"] if "classification" in c]
        if classified:
            self.report["classification_metrics"] = {
                "samples": len(classified),
                "safety_correct": sum(c["classification"]["safety"] in c["expected"]["safety_levels"]
                                      for c in classified),
                "intent_correct": sum(c["classification"]["intent"] in c["expected"]["support_intents"]
                                      for c in classified),
                "provider_healthy": sum(c["classification"]["safety_status"] == "completed"
                                        and c["classification"]["support_status"] == "completed"
                                        for c in classified),
            }
        self.output.parent.mkdir(parents=True, exist_ok=True)
        save_report(self.output, self.report)

    async def journeys(self):
        for need_screen in NEEDS:
            targets = [b["target"] for b in SCREENS[need_screen]["buttons"] if b["target"] in AIDS]
            if not targets:
                targets = [None]
            for target in targets:
                async def journey(cid, need_screen=need_screen, target=target):
                    await self.send(cid, "/start")
                    menu = await self.send(cid, "continue")
                    assert len([c for c in self.choices(menu) if c.startswith("need:")]) == 6, "six_needs"
                    category = await self.choose(cid, menu, need_screen)
                    await self.check_screen(cid, need_screen)
                    if target is None:
                        await self.send(cid, "Хочу уточнить, как работает фонд.")
                        await self.check_screen(cid, "s36b")
                        return
                    if AIDS[target] in CERTIFICATES and self.mode != "candidate":
                        preview = await self.choose(cid, category, target)
                        assert "certificate:confirm" in self.choices(preview), "certificate_confirmation"
                        self.active_case["certificate_scope"] = "preview_only_no_stock_consumed"
                        return
                    preview = await self.choose(cid, category, target)
                    assert not (await self.attrs(cid)).get("scenario_requests"), "premature_request"
                    if AIDS[target] in CERTIFICATES:
                        reply = await self.send(cid, "certificate:confirm")
                        assert "TEST-NOT-REDEEMABLE" in reply["content"], "synthetic_certificate_delivered"
                    else:
                        confirmation_target = {"s34": "s34b", "s35": "s35b", "s38": "s38b"}[target]
                        reply = await self.choose(cid, preview, confirmation_target)
                        assert len((await self.attrs(cid))["scenario_requests"]) == 1, "one_request"
                        # Same callback redelivery is stale, never creates another request.
                        await self.send(cid, self.choices(preview)[0])
                        assert len((await self.attrs(cid))["scenario_requests"]) == 1, "duplicate_request"
                        if target == "s35":
                            reply = await self.choose(cid, reply, "s35c")
                            await self.check_screen(cid, "s35c")
                    before = copy.deepcopy((await self.attrs(cid)).get("scenario_requests"))
                    await self.send(cid, "/clear")
                    assert (await self.attrs(cid)).get("scenario_requests") == before, "clear_loses_requests"
                await self.case(f"journey-{need_screen}-{target or 'text'}", journey)

    async def ownership(self):
        async def run(cid):
            await self.send(cid, "/start")
            await self.send(cid, "human")
            assert (await self.attrs(cid))["reply_owner"] == "bot", "notification_not_takeover"
            await self.send(cid, "Спасибо, пока продолжим здесь.")
            m = await self.staff(cid, "Техническая проверка. Специалист подключился.")
            c = await self.current(cid)
            assert (c.get("meta", {}).get("assignee") or {}).get("id") == self.staff_id, "visible_takeover"
            await self.send(cid, "Сообщение для специалиста.", expect_reply=False)
            for command in ("/start", "/clear", "/system_info"):
                await self.send(cid, command)
                assert (await self.attrs(cid))["reply_owner"] == "human", "command_steals_conversation"
            await self.native_change(cid, "/assignments", {"assignee_id": None})
            await self.send(cid, "Можно снова поговорить с ботом?")
            assert (await self.attrs(cid))["reply_owner"] == "bot", "unassignment_not_recovered"
            await self.staff(cid, "Техническая проверка повторного подключения.")
            await self.staff(cid, "[women-help:return-to-bot]", private=True, returning=True)
            await self.send(cid, "/start")
            await self.native_change(cid, "/assignments", {"assignee_id": self.staff_id})
            await self.native_change(cid, "/toggle_status", {"status": "resolved"})
            await self.send(cid, "/start")
            assert (await self.attrs(cid))["reply_owner"] == "bot", "closed_not_recovered"
            path = (f"/public/api/v1/inboxes/{self.inbox['inbox_identifier']}/contacts/"
                    f"{self.source_ids[cid]}/conversations/{cid}/messages")
            public = await self.raw.request("GET", path, settings.chatwoot_read_token)
            assert any(i["id"] == m["id"] for i in public), "staff_reply_not_visible"
            assert not any(i.get("private") for i in public), "private_note_exposed"
        await self.case("ownership-cycle", run)

    async def classifications(self):
        if self.mode != "candidate":
            raise RuntimeError("golden_context_seeding_requires_unbound_candidate_inbox")
        self.report["classification_scope"] = "Golden context seeded verbatim; final client turn uses live service and model"
        cases = load_cases(Path("tests/fixtures/dialogue_scenarios.jsonl"))
        # Old workflow lifecycle cases require synthetic initial state; assessed
        # separately by workflow tests, not mixed into classifier accuracy.
        cases = [c for c in cases if c.group != "soft_lifecycle"]
        if self.limit:
            cases = cases[:self.limit]
        for case in cases:
            async def run(cid, case=case):
                initial = case.initial.store_values()
                initial["workflow_state"] = initial.pop("state")
                initial["workflow_need"] = initial.pop("need")
                await self.api.set_custom_attributes(cid, initial)
                for role, text in case.history[:-1]:
                    if role == "user":
                        await self.post(cid, "/messages", {"content": text, "message_type": "incoming"})
                    else:
                        # Seed historical context only; do not impersonate staff.
                        await self.api.send_reply(cid, text=text, choices=(),
                                                  turn_key=f"seed:{uuid.uuid4().hex}")
                reply = await self.send(cid, case.history[-1][1])
                assert reply is not None
                if self.mode == "candidate":
                    result = self.services[cid]._gateway.last
                    assert result is not None, "missing_live_classification"
                    actual = {"safety": result.safety.level.value if result.safety else None,
                              "intent": result.support.intent.value if result.support and result.support.intent else None,
                              "escalation": result.safety.escalation.value if result.safety else None,
                              "needs": [n.value for n in result.support.need_hints] if result.support else [],
                              "safety_status": result.safety_status.value,
                              "support_status": result.support_status.value}
                    self.active_case["diagnostic_audit"] = {
                        name: {k: audit.get(k) for k in ("validation_errors", "normalization", "format_retry_count", "error_type")}
                        for name, audit in (("safety", result.safety_audit), ("support", result.support_audit))
                    }
                    self.active_case["classification"] = actual
                    self.active_case["expected"] = case.diagnostics
                    assert actual["safety_status"] == "completed" and actual["support_status"] == "completed", "provider_unavailable"
                    assert actual["safety"] in case.diagnostics["safety_levels"], "safety_classification"
                    intent_matches = actual["intent"] in case.diagnostics["support_intents"]
                    self.active_case["intent_label_matches"] = intent_matches
                    # During a safety handoff the risk policy overrides support
                    # intent. Preserve label drift in metrics, but gate on the
                    # actual escalation and continuation route, not an unused enum.
                    if not case.behavior["escalation"] or case.group != "crisis":
                        assert intent_matches, "intent_classification"
                attrs = await self.attrs(cid)
                if case.behavior["escalation"]:
                    assert attrs.get("handoff_requested"), "expected_handoff_missing"
                elif case.group in {"open_conversation", "human_near_miss"}:
                    assert not attrs.get("handoff_requested"), "unexpected_handoff"
                assert len(reply["content"]) < 4096, "overlong_reply"
                if case.id == "s11-child-custody":
                    assert "continue_bot" in self.choices(reply), "safety_continuation_missing"
                    await self.send(cid, "continue_bot")
                    await self.check_screen(cid, "s25")
            await self.case(f"classification-{case.id}", run)

    async def seed_screen(self, cid, screen, variant=0, **context):
        """Seed a documented entry point; count separately from complete journeys."""
        store = InMemoryConversationStore()
        incoming = IncomingMessage(channel="chatwoot", platform_user_id=self.contacts[cid],
                                   chat_id=cid, text="", message_id=0)
        record = await store.ensure(incoming)
        flow = ConversationService(store=store, html_scenario=True)
        turn = await flow.scenario_flow.show(record, screen, variant=variant,
                                             draft_key=uuid.uuid4().hex, **context)
        fields = {"workflow_state": record.state, "workflow_need": record.need,
                  "pending_aid_id": record.pending_aid_id, "scenario": record.scenario,
                  "workflow_navigation": record.navigation, "scenario_requests": {}, "scenario_followups": {},
                  "scenario_pending_input": None, "consultation_reviewed": False,
                  "pending_offer": None}
        await self.api.set_custom_attributes(cid, fields)
        mid = await self.api.send_reply(cid, text=turn.text, choices=turn.choices,
                                        turn_key=f"seed:{uuid.uuid4().hex}")
        return next(m for m in await self.api.get_messages(cid) if m["id"] == mid)

    async def screens(self):
        self.report["screen_scope"] = "Seeded entry points; actual button clicks and API replies"
        for screen, spec in SCREENS.items():
            if screen == "i3":
                continue  # A navigation rule, not a displayed screen.
            count = len(spec["texts"]) - int(screen == "s51")
            for variant in range(count):
                async def run(cid, screen=screen, variant=variant, spec=spec):
                    aid = "food_card"
                    if screen == "s5":
                        aid = (*CERTIFICATES, *CONSULTATION_IDS, "legal_consultation")[variant]
                    elif screen == "s7":
                        aid = CONSULTATION_IDS[variant]
                    elif screen in {"s56", "s59"} and variant == 1:
                        aid = "legal_consultation"
                    elif screen == "s51":
                        aid = CERTIFICATES[variant]
                    context = {"aid_id": aid, "completed": screen == "s5" and variant == 7}
                    reply = await self.seed_screen(cid, screen, variant, **context)
                    original = self.choices(reply)
                    assert "human" in original, "always_human"
                    self.active_case["screen"], self.active_case["variant"] = screen, variant
                    checked = []
                    for value in original:
                        if value == "human":
                            continue  # Tested once end to end by the ownership suite.
                        if value == "certificate:confirm" and self.mode != "candidate":
                            continue  # Never consume real inventory in acceptance.
                        await self.seed_screen(cid, screen, variant, **context)
                        if value.startswith("sc:"):
                            index = int(value.split(":")[2])
                            assert index in button_indices(screen, variant)
                            button = spec["buttons"][index]
                            target = button["target"]
                            writing = button["label"].startswith("Написать")
                        else:
                            writing = False
                            target = {"continue": "s2", "pause": "s1a"}.get(value)
                            if value.startswith("need:"):
                                target = next(k for k, v in NEEDS.items() if v.value == value[5:])
                            if value.startswith("aid:"):
                                target = next(k for k, v in AIDS.items() if v == value[4:])
                            if value == "certificate:confirm":
                                target = screen + "b"
                        await self.send(cid, value)
                        attrs = await self.attrs(cid)
                        if writing:
                            assert attrs["scenario"].get("awaiting_text"), "write_button_sent_empty_request"
                            await self.send(cid, "Техническая проверка обратной связи, спасибо.")
                        expected = "s33c" if target == "s33b" else target
                        await self.check_screen(cid, expected)
                        checked.append(value)
                    self.active_case["button_edges"] = len(checked)
                await self.case(f"screen-{screen}-v{variant}", run)

    async def timers(self):
        if self.mode != "candidate":
            return  # Server clock must not be changed for testing.
        for aid in CONSULTATION_IDS:
            async def run(cid, aid=aid):
                service = self.services[cid]
                now = (datetime.now(UTC) + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
                service._scenario_effects.clock = lambda: now
                source = next(k for k, v in AIDS.items() if v == aid)
                reply = await self.seed_screen(cid, source)
                await self.send(cid, self.choices(reply)[0])
                key = next(iter((await self.attrs(cid))["scenario_requests"]))
                await self.staff(cid, "Техническая консультация завершена.")
                await self.staff(cid, "[women-help:consultation-completed]", private=True, completed=True)
                current = await self.current(cid)
                assert current["status"] == "resolved" and current["custom_attributes"]["reply_owner"] == "bot"
                assert not await service.send_due_followup(cid), "survey_early"
                now += timedelta(hours=2)
                assert await service.send_due_followup(cid), "survey_missing"
                assert not await service.send_due_followup(cid), "duplicate_survey"
                await self.check_screen(cid, "s7")
                review = next(m for m in await self.api.get_messages(cid)
                              if (m.get("content_attributes") or {}).get("bot_turn_key") == f"scenario-followup:review:{key}")
                reply = await self.choose(cid, review, "s71")
                reply = await self.choose(cid, reply, "s72")
                reply = await self.choose(cid, reply, "s721")
                reply = await self.choose(cid, reply, "s73")
                reply = await self.choose(cid, reply, "s74")
                reply = await self.choose(cid, reply, "s75")
                await self.send(cid, "Спасибо за консультацию.")
                await self.check_screen(cid, "s75")
                attrs = await self.attrs(cid)
                assert attrs["consultation_reviewed"], "review_flag_missing"
                assert not any(k.startswith("reminder:review:") for k in attrs["scenario_followups"])
            await self.case(f"timer-review-{aid}", run)

        async def certificate_timer(cid):
            service = self.services[cid]
            now = (datetime.now(UTC) + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
            service._scenario_effects.clock = lambda: now
            await service._scenario_effects.certificate_delivered(cid, "synthetic", {
                "aid_id": "food_card", "expires_at": (now + timedelta(days=7)).isoformat(),
            })
            assert not await service.send_due_followup(cid)
            now += timedelta(days=4)
            await self.native_change(cid, "/assignments", {"assignee_id": self.staff_id})
            assert not await service.send_due_followup(cid), "survey_interrupts_human"
            await self.native_change(cid, "/assignments", {"assignee_id": None})
            assert await service.send_due_followup(cid), "four_day_check_missing"
            await self.check_screen(cid, "s5")
            now += timedelta(days=2)
            assert await service.send_due_followup(cid), "reminder_missing"
            await self.check_screen(cid, "s59")
            assert not await service.send_due_followup(cid), "repeat_reminder"
            await self.send(cid, "Спасибо, больше напоминать не нужно.")
            assert not await service.send_due_followup(cid)
        await self.case("timer-certificate-and-reminder", certificate_timer)

    async def scheduling(self):
        async def appointment(cid):
            # Real client confirmation, staff replies and native status changes.
            await self.send(cid, "/start")
            reply = await self.send(cid, "continue")
            for screen in ("s23", "s35", "s35b"):
                reply = await self.choose(cid, reply, screen)
            key = next(iter((await self.attrs(cid))["scenario_requests"]))
            await self.staff(cid, "Технический тест: встреча согласована.")
            end = (datetime.now(UTC) + timedelta(days=3)).replace(hour=12, minute=0, second=0, microsecond=0)
            from app.chatwoot.scenario_effects import MSK, daytime

            for shift in (0, 1):
                await self.api.set_custom_attributes(cid, {
                    "consultation_ends_at": (end + timedelta(days=shift)).astimezone(MSK).strftime("%d.%m.%Y %H:%M"),
                })
                await self.staff(cid, "[women-help:consultation-schedule]", private=True, scheduling="schedule")
                attrs = await self.attrs(cid)
                job = attrs["scenario_followups"][f"review:{key}"]
                assert job["due_at"] == daytime(end + timedelta(days=shift, hours=2)).isoformat()
                assert attrs["scenario_requests"][key]["state"] == "requested"
                assert attrs["reply_owner"] == "human", "schedule_changed_owner"
            await self.staff(cid, "[women-help:consultation-cancel]", private=True, scheduling="cancel")
            assert (await self.attrs(cid))["scenario_followups"][f"review:{key}"]["state"] == "cancelled"
            await self.staff(cid, "[women-help:consultation-schedule]", private=True, scheduling="schedule")
            await self.native_change(cid, "/toggle_status", {"status": "resolved"})
            assert (await self.attrs(cid))["scenario_requests"][key]["state"] == "requested"
            if self.mode == "candidate":
                service = self.services[cid]
                service._scenario_effects.clock = lambda: end + timedelta(days=1, hours=2)
                assert await service.send_due_followup(cid), "scheduled_review_missing"
                assert not await service.send_due_followup(cid), "scheduled_review_duplicated"
                await self.check_screen(cid, "s7")
            else:
                await self.send(cid, "/system_info")
        await self.case("scheduled-consultation-end-transfer-cancel", appointment)

        async def idle(cid):
            await self.send(cid, "/start")
            await self.send(cid, "continue")
            attrs = await self.attrs(cid)
            key = next(k for k in attrs["scenario_followups"] if k.startswith("idle:"))
            assert attrs["scenario_followups"][key]["state"] == "pending"
            if self.mode == "candidate":
                service = self.services[cid]
                due = datetime.fromisoformat(attrs["scenario_followups"][key]["due_at"])
                service._scenario_effects.clock = lambda: due - timedelta(seconds=1)
                assert not await service.send_due_followup(cid)
                service._scenario_effects.clock = lambda: due
                assert await service.send_due_followup(cid)
                assert not await service.send_due_followup(cid)
                await self.check_screen(cid, "i4")
            else:
                await self.send(cid, "/system_info")
                assert (await self.attrs(cid))["scenario_followups"][key]["state"] == "cancelled"
        await self.case("initial-menu-one-hour-reminder", idle)

    async def contextual(self):
        samples = (
            ("food", "Мне сейчас не хватает денег на продукты.", {"food_money"}),
            ("documents", "Хочу восстановить потерянный паспорт, нужна помощь с документами.", {"legal"}),
            ("children", "Мне нужна одежда для ребёнка.", {"children"}),
            ("multiple", "Нужны продукты, одежда детям и помощь в восстановлении паспорта.",
             {"food_money", "children", "legal"}),
            ("listen", "Можно просто выговориться, без предложений услуг?", set()),
        )
        for name, text, needs in samples:
            async def run(cid, text=text, needs=needs):
                await self.send(cid, "/start")
                await self.send(cid, "continue")
                reply = await self.send(cid, text)
                actual = {v[5:] for v in self.choices(reply) if v.startswith("need:")}
                assert actual == needs, "contextual_need_buttons"
                attrs = await self.attrs(cid)
                assert not attrs.get("scenario_requests"), "text_created_request_without_confirmation"
                assert not attrs.get("handoff_requested"), "ordinary_need_caused_handoff"
                if not needs:
                    second = await self.send(cid, "Сегодня был очень непростой день на работе.")
                    assert second["content"] != reply["content"], "repeated_stub"
                    assert not (await self.attrs(cid)).get("handoff_requested")
            await self.case(f"contextual-{name}", run)

        async def navigation(cid):
            await self.send(cid, "/start")
            await self.send(cid, "continue")
            menu = await self.send(cid, "need:legal")
            preview = await self.choose(cid, menu, "s35")
            confirmed = await self.choose(cid, preview, "s35b")
            before = copy.deepcopy((await self.attrs(cid))["scenario_requests"])
            back = next(v for v in self.choices(confirmed) if v.startswith("back:"))
            await self.send(cid, back)
            await self.check_screen(cid, "s35")
            await self.send(cid, back)  # Old Back cannot rewind twice.
            await self.check_screen(cid, "s35")
            assert (await self.attrs(cid))["scenario_requests"] == before
            await self.send(cid, "/clear")
            await self.send(cid, self.choices(preview)[0])  # Old confirmation is inert.
            assert (await self.attrs(cid))["scenario_requests"] == before
        await self.case("navigation-durable-request-and-stale-buttons", navigation)

    async def attachments(self):
        if self.mode != "candidate":
            return
        async def run(cid):
            # Neutral binary test data in memory, not a redeemable certificate.
            import pymupdf
            document = pymupdf.open()
            document.new_page().insert_text((72, 72), "ACCEPTANCE TEST - NOT VALID - NO MONETARY VALUE")
            payload = document.tobytes()
            document.close()

            object_store = S3CertificateObjectStore(
                bucket=settings.certificate_s3_bucket,
                endpoint_url=settings.certificate_s3_endpoint, region=settings.certificate_s3_region,
                access_key_id=settings.certificate_s3_access_key_id,
                secret_access_key=settings.certificate_s3_secret_access_key,
            )
            ref = await object_store.upload(ParsedCertificatePdf(
                provider_slug="pyaterochka", provider="Synthetic", nominal_rubles=3000,
                activation_code="NOT-VALID", serial_number=f"acceptance-{uuid.uuid4().hex}",
                valid_from=None, expires_at=datetime.now(UTC) + timedelta(days=7),
                is_test=True, filename="ACCEPTANCE-NOT-VALID.pdf",
                pdf_sha256=hashlib.sha256(payload).hexdigest(), pdf_bytes=payload,
            ))
            assert ref.key.startswith("test/"), "synthetic_s3_prefix"
            assert await object_store.download(ref) == payload, "s3_roundtrip_integrity"

            issued = None
            async def claim(aid, key, recipient):
                nonlocal issued
                if issued is not None:
                    return CertificateClaimResult("already_issued", issued)
                issued = (await self.synthetic_claim(aid, key, recipient)).certificate
                issued.pdf_bucket, issued.pdf_object_key = ref.bucket, ref.key
                issued.pdf_version_id = ref.version_id
                issued.pdf_sha256, issued.pdf_size = ref.sha256, ref.size
                issued.pdf_filename = "ACCEPTANCE-NOT-VALID.pdf"
                return CertificateClaimResult("issued", issued)

            self.services[cid]._certificate_claim = claim
            self.services[cid]._certificate_store = object_store
            await self.send(cid, "/start")
            await self.send(cid, "continue")
            await self.send(cid, "need:food_money")
            await self.send(cid, "aid:food_card")
            reply = await self.send(cid, "certificate:confirm")
            messages = await self.api.get_messages(cid)
            documents = [m for m in messages if m.get("attachments")]
            assert len(documents) == 1, "pdf_missing_or_duplicated"
            assert documents[0]["id"] < reply["id"], "choices_before_pdf"
            path = (f"/public/api/v1/inboxes/{self.inbox['inbox_identifier']}/contacts/"
                    f"{self.source_ids[cid]}/conversations/{cid}/messages")
            public = await self.raw.request("GET", path, settings.chatwoot_read_token)
            assert any(m["id"] == documents[0]["id"] and m.get("attachments") for m in public), "client_pdf_not_visible"
            await self.send(cid, "/clear")
            await self.send(cid, "/start")
            await self.send(cid, "continue")
            await self.send(cid, "need:food_money")
            await self.send(cid, "aid:food_card")
            await self.send(cid, "certificate:confirm")
            assert len([m for m in await self.api.get_messages(cid) if m.get("attachments")]) == 1
            self.active_case["pdf_bytes"] = len(payload)
            self.active_case["s3_roundtrip"] = True
        await self.case("certificate-pdf-client-visible-and-limit", run)

    async def run(self):
        await self.setup()
        for suite in self.suites:
            await getattr(self, suite)()
        self.save()
        print(json.dumps({"summary": self.report["summary"], "report": str(self.output)}), flush=True)
        return not self.report["summary"]["failed"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["candidate", "webhook"], default="candidate")
    parser.add_argument("--output", required=True)
    parser.add_argument("--suites", nargs="+", choices=["journeys", "ownership", "classifications", "screens", "timers", "scheduling", "contextual", "attachments"],
                        default=None)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    os.umask(0o077)
    suites = args.suites or (["journeys", "ownership", "scheduling", "contextual"] if args.mode == "webhook" else
                            ["journeys", "ownership", "classifications", "screens", "timers", "scheduling", "contextual", "attachments"])
    raise SystemExit(0 if asyncio.run(Acceptance(args.mode, args.output, suites, args.limit).run()) else 1)


if __name__ == "__main__":
    main()
