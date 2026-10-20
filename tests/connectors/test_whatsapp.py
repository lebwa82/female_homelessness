import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.connectors.app import create_application
from app.connectors.http import File, ProviderError
from app.connectors.whatsapp import WhatsApp


def payload(**changes):
    message = {"from": "79990000001", "id": "wamid.test", "type": "text",
               "timestamp": str(int(time.time())), "text": {"body": "Тест"}} | changes
    return {"object": "whatsapp_business_account", "entry": [{"id": "5678", "changes": [
        {"field": "messages", "value": {"metadata": {"phone_number_id": "1234"},
                                          "messages": [message]}}]}]}


def sign(raw):
    return "sha256=" + hmac.new(b"app-secret", raw, hashlib.sha256).hexdigest()


def test_whatsapp_raw_body_signature_and_scope(settings_factory, http, redis):
    adapter = WhatsApp(settings_factory("whatsapp"), http, redis)
    raw = json.dumps(payload()).encode()
    assert adapter.authenticate(raw, {"X-Hub-Signature-256": sign(raw)}, {})
    assert not adapter.authenticate(raw + b" ", {"X-Hub-Signature-256": sign(raw)}, {})
    assert not adapter.authenticate(raw, {}, {})
    foreign = payload()
    foreign["entry"][0]["changes"][0]["value"]["metadata"]["phone_number_id"] = "9999"
    assert not adapter.incoming(foreign)
    foreign = payload()
    foreign["entry"][0]["id"] = "9999"
    assert not adapter.incoming(foreign)


def test_whatsapp_text_buttons_lists_and_media(settings_factory, http, redis):
    adapter = WhatsApp(settings_factory("whatsapp"), http, redis)
    assert adapter.incoming(payload())[0]["text"] == "Тест"
    for action in ("button_reply", "list_reply"):
        event = adapter.incoming(payload(type="interactive", interactive={
            "type": action, action: {"id": "action:2", "title": "Вариант"}}))[0]
        assert event["text"] == "action:2"
    event = adapter.incoming(payload(type="document", document={
        "id": "media123", "mime_type": "application/pdf", "filename": "test.pdf"}))[0]
    assert event["files"][0]["id"] == "media123"
    assert not adapter.incoming(payload(timestamp="invalid"))


@pytest.mark.parametrize("count", [1, 3, 4, 10, 13])
def test_whatsapp_choice_limits_preserve_machine_values(settings_factory, http, redis, count):
    adapter = WhatsApp(settings_factory("whatsapp"), http, redis)
    parts = adapter.parts({"content": "Тест", "content_attributes": {"items": [
        {"title": "Очень длинное название варианта " + str(i), "value": f"action:{i}"}
        for i in range(count)]}})
    values = []
    for part in parts:
        interactive = part["interactive"]
        if interactive["type"] == "button":
            buttons = interactive["action"]["buttons"]
            assert len(buttons) <= 3
            assert len({b["reply"]["title"] for b in buttons}) == len(buttons)
            assert all(len(b["reply"]["title"]) <= 20 for b in buttons)
            values.extend(b["reply"]["id"] for b in buttons)
        else:
            rows = interactive["action"]["sections"][0]["rows"]
            assert len(rows) <= 10
            assert all(len(row["title"]) <= 24 for row in rows)
            values.extend(row["id"] for row in rows)
    assert values == [f"action:{i}" for i in range(count)]


async def test_whatsapp_service_window_not_extended_by_retry(settings_factory, http, redis):
    adapter = WhatsApp(settings_factory("whatsapp"), http, redis)
    event = adapter.incoming(payload())[0]
    await adapter.received(event)
    await adapter.received(event | {"timestamp": event["timestamp"] - 3600})
    assert int(await redis.get(adapter.window_key(event["peer"]))) == event["timestamp"]
    assert event["peer"] not in adapter.window_key(event["peer"])
    http.json.return_value = {"messages": [{"id": "wamid.out"}]}
    assert await adapter.send(event["peer"], {"type": "text", "text": {"body": "Тест"}},
                              key="test") == "wamid.out"
    await redis.set(adapter.window_key(event["peer"]), int(time.time()) - 86401)
    with pytest.raises(ProviderError) as caught:
        await adapter.send(event["peer"], {}, key="test")
    assert caught.value.status == 131047 and not caught.value.retryable
    assert http.json.await_count == 1


async def test_whatsapp_pdf_upload_and_media_download(settings_factory, http, redis):
    adapter = WhatsApp(settings_factory("whatsapp"), http, redis)
    await adapter.received(adapter.incoming(payload())[0])
    http.json.side_effect = [{"id": "media"}, {"messages": [{"id": "wamid.pdf"}]}]
    assert await adapter.send("79990000001", {}, key="pdf",
                              file=File("test.pdf", "application/pdf", b"%PDF-test")) == "wamid.pdf"
    body = http.json.call_args.kwargs["payload"]
    assert body["document"] == {"id": "media", "filename": "test.pdf"}
    http.json.side_effect = None
    http.json.return_value = {"url": "https://lookaside.fbsbx.com/media"}
    file = await adapter.inbound_file({"id": "media", "name": "test.pdf", "mime": "application/pdf"})
    assert file.data == b"%PDF-test"


async def test_whatsapp_verification_signed_delivery_and_queue_failure(
        settings_factory, http, redis, client_factory):
    settings = settings_factory("whatsapp")
    queue = SimpleNamespace(enqueue=AsyncMock(), healthy=AsyncMock(return_value=True))
    client = await client_factory(create_application(settings, WhatsApp(settings, http, redis), queue))
    path = "/webhooks/whatsapp"
    response = await client.get(path, params={"hub.mode": "subscribe", "hub.verify_token": "verify-secret",
                                             "hub.challenge": "123"})
    assert response.status == 200 and await response.text() == "123"
    assert (await client.get(path, params={"hub.mode": "subscribe"})).status == 403
    raw = json.dumps(payload()).encode()
    assert (await client.post(path, data=raw)).status == 401
    headers = {"X-Hub-Signature-256": sign(raw), "Content-Type": "application/json"}
    assert (await client.post(path, data=raw, headers=headers)).status == 200
    queue.enqueue.side_effect = ConnectionError("synthetic")
    assert (await client.post(path, data=raw, headers=headers)).status == 503


def test_whatsapp_status_events_are_separate_from_bot_input(settings_factory, http, redis):
    adapter = WhatsApp(settings_factory("whatsapp"), http, redis)
    event = payload()
    value = event["entry"][0]["changes"][0]["value"]
    value.pop("messages")
    value["statuses"] = [{"id": "wamid.out", "status": "read"}]
    assert not adapter.incoming(event)
    assert adapter.statuses(event)[0] == {
        "kind": "status", "id": "wamid.out", "status": "read", "peer": "status:wamid.out"}
    value["metadata"]["phone_number_id"] = "9999"
    assert not adapter.statuses(event)
