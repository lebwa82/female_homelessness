import json

import pytest
from test_chatwoot_service import FakeChatwoot

from app.scenario import render
from scripts.chatwoot_full_acceptance import Acceptance, ScopedTransport


@pytest.mark.asyncio
async def test_seeded_screen_callback_matches_persisted_navigation(tmp_path):
    class Api(FakeChatwoot):
        async def get_messages(self, cid):
            return tuple({"id": r["message_id"], "content_attributes": {
                "items": [{"value": c.id} for c in r["choices"]],
            }} for r in self.replies)

    runner = Acceptance("candidate", tmp_path / "report.json", [])
    runner.api = Api()
    runner.contacts[23] = 7
    reply = await runner.seed_screen(23, "s35")
    attrs = await runner.attrs(23)
    expected = render("s35", revision=attrs["workflow_navigation"]["revision"],
                      context=attrs["scenario"])
    assert runner.choices(reply) == [c.id for c in expected.choices]


@pytest.mark.asyncio
async def test_full_acceptance_cannot_mutate_other_chat_or_upload_to_it():
    transport = ScopedTransport("http://unused.test")
    with pytest.raises(ValueError, match="outside"):
        await transport.request("POST", "/api/v1/accounts/1/conversations/3/messages", "", {})
    with pytest.raises(ValueError, match="outside"):
        await transport.request_multipart("POST", "/api/v1/accounts/1/conversations/3/messages", "", {}, None)


def test_metrics_count_all_cases_not_only_passes(tmp_path):
    runner = Acceptance("candidate", tmp_path / "report.json", [])
    runner.report["cases"] = [
        {"passed": passed, "classification": {"safety": safety, "intent": intent,
          "safety_status": "completed", "support_status": status},
         "expected": {"safety_levels": ["none"], "support_intents": ["open_conversation"]}}
        for passed, safety, intent, status in (
            (True, "none", "open_conversation", "completed"),
            (False, "concern", None, "invalid"),
        )
    ]
    runner.save()
    report = json.loads(runner.output.read_text())
    assert report["summary"] == {"dialogs": 2, "passed": 1, "failed": 1}
    assert report["classification_metrics"] == {
        "samples": 2, "safety_correct": 1, "intent_correct": 1, "provider_healthy": 1,
    }
    assert runner.output.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_golden_context_is_never_seeded_into_live_bound_inbox(tmp_path):
    runner = Acceptance("webhook", tmp_path / "report.json", [])
    with pytest.raises(RuntimeError, match="requires_unbound"):
        await runner.classifications()
