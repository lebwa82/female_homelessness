import pytest

from scripts import chatwoot_acceptance
from scripts.chatwoot_acceptance import TEST_INBOX, validate_test_inbox
from scripts.chatwoot_acceptance import TestConversationTransport as Transport


@pytest.fixture
def transport():
    value = Transport(1)
    value.conversation_ids.add(123)
    return value


def test_inbox_is_api_and_not_connected_to_external_recipient():
    validate_test_inbox({"name": TEST_INBOX, "channel_type": "Channel::Api"})


@pytest.mark.parametrize("inbox", [
    {"name": TEST_INBOX, "channel_type": "Channel::Telegram"},
    {"name": "User inbox", "channel_type": "Channel::Api"},
    {"name": TEST_INBOX, "channel_type": "Channel::Api", "webhook_url": "https://example.org"},
    {"name": TEST_INBOX, "channel_type": "Channel::Api",
     "callback_webhook_url": "https://example.org"},
])
def test_real_or_external_inbox_is_rejected(inbox):
    with pytest.raises(ValueError):
        validate_test_inbox(inbox)


@pytest.mark.parametrize("suffix", ["messages", "assignments", "toggle_status", "custom_attributes"])
def test_write_to_test_conversation_allowed(transport, suffix):
    transport.check_write("POST", f"/api/v1/accounts/1/conversations/123/{suffix}", {})


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/api/v1/accounts/1/conversations/456/messages", {}),
    ("POST", "/api/v1/accounts/2/conversations/123/messages", {}),
    ("DELETE", "/api/v1/accounts/1/conversations/123", {}),
    ("POST", "/api/v1/accounts/1/macros/1/execute", {"conversation_ids": [123, 456]}),
    ("POST", "/api/v1/accounts/1/macros/1/execute", {"conversation_ids": []}),
    ("POST", "/api/v1/accounts/1/inboxes", {}),
])
def test_non_test_mutations_rejected(transport, method, path, payload):
    with pytest.raises(ValueError):
        transport.check_write(method, path, payload)


def test_test_macro_and_read_only_filter_allowed(transport):
    transport.check_write("POST", "/api/v1/accounts/1/macros/1/execute",
                          {"conversation_ids": [123]})
    transport.check_write("POST", "/api/v1/accounts/1/conversations/filter", {})


@pytest.mark.parametrize("public_ids,valid", [([2], True), ([1, 2], False), ([], False)])
async def test_client_view_requires_visible_staff_and_hidden_notes(monkeypatch, public_ids, valid):
    class Api:
        async def get_messages(self, _):
            return [{"id": 1, "private": True},
                    {"id": 2, "message_type": 1, "sender": {"type": "user"}}]

    async def request(*_):
        return [{"id": value} for value in public_ids]

    monkeypatch.setattr(chatwoot_acceptance, "request", request)
    if valid:
        await chatwoot_acceptance.verify_client_visibility(Api(), 123, "/public/test")
    else:
        with pytest.raises(AssertionError):
            await chatwoot_acceptance.verify_client_visibility(Api(), 123, "/public/test")
