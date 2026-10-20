import copy

import pytest

from app.chatwoot.client import ChatwootApiError, ChatwootClient


class Transport:
    def __init__(self, channel="Channel::Api", *, receipt=None, status="sent"):
        self.channel, self.receipt, self.status = channel, receipt, status

    async def request(self, _method, path, _token, _payload=None):
        if path.endswith("/conversations/7"):
            return {"meta": {"channel": self.channel}, "inbox_id": 2}
        if path.endswith("/inboxes/2"):
            return {"webhook_url": "https://connector.example/webhook"}
        messages = [{"id": 11, "status": self.status, "source_id": None,
                     "attachments": [{"id": 1}]}]
        if self.receipt:
            messages.append(self.receipt)
        return {"payload": messages}


def client(transport):
    return ChatwootClient(base_url="https://cw.example", account_id=1, read_token="read",
                          bot_token="bot", transport=transport)


RECEIPT = {"id": 12, "message_type": 1, "private": True, "content_attributes": {
    "connector_delivery_receipt": {"message_id": 11, "provider": "vk", "external_ids": ["91"]}}}


async def test_api_pdf_acceptance_waits_for_receipt():
    with pytest.raises(ChatwootApiError, match="504"):
        await client(Transport()).wait_for_external_delivery(7, 11, timeout_seconds=0)
    await client(Transport(receipt=RECEIPT)).wait_for_external_delivery(7, 11, timeout_seconds=0)


async def test_connector_receipt_cannot_confirm_telegram_pdf():
    with pytest.raises(ChatwootApiError, match="504"):
        await client(Transport("Channel::Telegram", receipt=RECEIPT)).wait_for_external_delivery(
            7, 11, timeout_seconds=0)


async def test_provider_failure_wins_over_an_earlier_receipt():
    with pytest.raises(ChatwootApiError, match="502"):
        await client(Transport(receipt=RECEIPT, status="failed")).wait_for_external_delivery(
            7, 11, timeout_seconds=0)


@pytest.mark.parametrize("change", ["private", "wrong_message", "empty_ids", "incoming", "unknown"])
async def test_invalid_receipts_do_not_confirm_delivery(change):
    receipt = copy.deepcopy(RECEIPT)
    value = receipt["content_attributes"]["connector_delivery_receipt"]
    if change == "private":
        receipt["private"] = False
    elif change == "wrong_message":
        value["message_id"] = 99
    elif change == "empty_ids":
        value["external_ids"] = []
    elif change == "incoming":
        receipt["message_type"] = 0
    else:
        value["provider"] = "unknown"
    with pytest.raises(ChatwootApiError, match="504"):
        await client(Transport(receipt=receipt)).wait_for_external_delivery(7, 11, timeout_seconds=0)
