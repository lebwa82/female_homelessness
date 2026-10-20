import hashlib
import hmac
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.connectors.chatwoot import Chatwoot
from app.connectors.config import ConnectorSettings


@pytest.fixture
def bridge():
    settings = ConnectorSettings(
        provider="vk", chatwoot_url="https://cw.example.org", account_id=1, inbox_id=2,
        api_token="secret", inbox_identifier="identifier", inbox_hmac_token="hmac-secret",
        callback_secret="x" * 32, vk_group_id=3, vk_token="vk-token", vk_secret="vk-secret",
        vk_confirmation="confirmation",
    )
    http = SimpleNamespace(json=AsyncMock(), download=AsyncMock(return_value=b"%PDF-test"))
    return Chatwoot(settings, http)


async def test_bridge_reuses_contact_and_only_its_own_inbox(bridge):
    bridge.http.json.side_effect = [{"id": 9, "source_id": "vk:3:42"}, {"payload": [
        {"id": 88, "inbox_id": 99}, {"id": 7, "inbox_id": 2, "status": "resolved"}]}]
    assert await bridge.conversation("42") == 7
    calls = bridge.http.json.call_args_list
    contact = calls[0].kwargs["payload"]
    assert contact["identifier"] == contact["source_id"] == "vk:3:42"
    assert contact["identifier_hash"] == hmac.new(b"hmac-secret", b"vk:3:42",
                                                hashlib.sha256).hexdigest()
    assert calls[1].args[1].endswith("/api/v1/accounts/1/contacts/9/conversations")
    assert calls[1].kwargs["headers"]["Api-Access-Token"] == "secret"


async def test_bridge_creates_only_its_own_api_conversation(bridge):
    bridge.http.json.side_effect = [{"id": 9}, {"payload": []}, {"id": 7}]
    assert await bridge.conversation("42") == 7
    assert bridge.http.json.call_args.kwargs["payload"] == {
        "source_id": "vk:3:42", "inbox_id": 2, "contact_id": 9}


async def test_bridge_reconciles_lost_incoming_response_using_external_source(bridge):
    bridge.http.json.return_value = {"payload": [{"id": 10, "source_id": "vk:3:42:11"}]}
    await bridge.incoming(7, {"id": "11", "peer": "42", "text": "Тест"}, [])
    assert bridge.http.json.await_count == 1


async def test_bridge_posts_incoming_with_contact_sender_and_source(bridge):
    bridge.http.json.side_effect = [{"payload": []}, {"id": 10}]
    await bridge.incoming(7, {"id": "11", "peer": "42", "text": "Тест"}, [])
    assert bridge.http.json.call_args.kwargs["payload"] == {
        "content": "Тест", "message_type": "incoming", "source_id": "vk:3:42:11"}


@pytest.mark.parametrize("private,message_type", [(True, 1), (False, 0), (None, 1)])
async def test_bridge_never_sends_private_or_incoming_messages(bridge, private, message_type):
    bridge.http.json.side_effect = [
        {"inbox_id": 2, "contact_inbox": {"source_id": "vk:3:42"}},
        {"payload": [{"id": 11, "private": private, "message_type": message_type}]},
    ]
    assert await bridge.outgoing({"conversation_id": 7, "message_id": 11}) is None


async def test_bridge_does_not_trust_callback_content_or_recipient(bridge):
    bridge.http.json.side_effect = [
        {"inbox_id": 2, "contact_inbox": {"source_id": "vk:3:42"}},
        {"payload": [{"id": 11, "private": False, "message_type": 1, "content": "canonical"}]},
    ]
    peer, message = await bridge.outgoing({"conversation_id": 7, "message_id": 11,
                                           "peer": "999", "content": "forged"})
    assert peer == "42" and message["content"] == "canonical"
    assert bridge.http.json.call_args.args[1].endswith("/messages?before=12")


@pytest.mark.parametrize("inbox", [
    {"channel_type": "Channel::Telegram", "inbox_identifier": "identifier", "hmac_mandatory": True},
    {"channel_type": "Channel::Api", "inbox_identifier": "other", "hmac_mandatory": True},
    {"channel_type": "Channel::Api", "inbox_identifier": "identifier", "hmac_mandatory": False},
])
async def test_bridge_refuses_telegram_or_misconfigured_inbox(bridge, inbox):
    bridge.http.json.return_value = inbox
    with pytest.raises(ValueError):
        await bridge.check_inbox()


async def test_provider_acceptance_is_a_private_receipt_not_fake_delivery(bridge):
    bridge.http.json.side_effect = [{"payload": []}, {"id": 12}]
    await bridge.receipt(7, 11, ["external-91"])
    body = bridge.http.json.call_args.kwargs["payload"]
    assert body["private"] is True
    assert body["content_attributes"]["connector_delivery_receipt"] == {
        "message_id": 11, "provider": "vk", "external_ids": ["external-91"]}
    assert "status" not in body


async def test_permanent_provider_failure_uses_supported_api_status_patch(bridge):
    await bridge.failed(7, 11, 901)
    assert bridge.http.json.call_args.args[0] == "PATCH"
    assert bridge.http.json.call_args.args[1].endswith("/conversations/7/messages/11")
    assert bridge.http.json.call_args.kwargs["payload"] == {
        "status": "failed", "external_error": "connector_vk:901"}
