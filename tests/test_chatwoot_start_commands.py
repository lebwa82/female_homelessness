"""Start command payloads use the canonical entry without bypassing ownership."""

from copy import deepcopy

import pytest

from app.chatwoot.service import ChatwootAgentService
from tests.test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation


@pytest.mark.parametrize("command", ["/start", "/start campaign_test", "/start  campaign_test"])
async def test_start_command_payload_uses_canonical_entry_and_deduplicates(command):
    expected_api = FakeChatwoot()
    expected_service = ChatwootAgentService(
        expected_api, gateway=StubGateway(ordinary_evaluation())
    )
    assert await expected_service.process(event("/start"))

    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway)

    assert await service.process(event(command))
    assert gateway.calls == 0
    assert api.replies == expected_api.replies
    assert api.conversation["custom_attributes"] == expected_api.conversation["custom_attributes"]
    state = deepcopy(api.conversation["custom_attributes"])
    assert not await service.process(event(command))
    assert gateway.calls == 0 and len(api.replies) == 1
    assert api.conversation["custom_attributes"] == state


@pytest.mark.parametrize("status", ["open", "pending", "snoozed"])
async def test_start_payload_does_not_take_ownership_from_human(status):
    api = FakeChatwoot()
    api.conversation.update(status=status, assignee_id=4)
    api.conversation["custom_attributes"].update(
        scenario={"screen": "test-screen"},
        scenario_requests={"keep": {"state": "requested"}},
    )
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway)

    assert await service.process(event("/start campaign_test"))
    assert not await service.process(event("/start campaign_test"))
    assert api.conversation["assignee_id"] == 4
    attrs = api.conversation["custom_attributes"]
    assert attrs["reply_owner"] == "human"
    assert attrs["scenario"] == {"screen": "test-screen"}
    assert attrs["scenario_requests"] == {"keep": {"state": "requested"}}
    assert gateway.calls == 0 and len(api.replies) == 1
    assert "специалист" in api.replies[0]["text"]


@pytest.mark.parametrize(
    "text", ["please use /start campaign_test", "/starter campaign_test", "/start_campaign_test"]
)
async def test_ordinary_text_containing_start_is_not_a_command(text):
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway)

    assert await service.process(event(text))
    assert gateway.calls == 1
    assert len(api.replies) == 1


async def test_start_payload_keeps_scenario_buttons_and_clear_available():
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway)

    assert await service.process(event("/start campaign_test"))
    assert await service.process(event("continue", 42))
    assert any(choice.id.startswith("need:") for choice in api.replies[-1]["choices"])
    assert await service.process(event("/clear", 43))
    assert api.conversation["custom_attributes"]["context_epoch"] == 1
    assert gateway.calls == 0 and len(api.replies) == 3
