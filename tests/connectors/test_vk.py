import copy
import json

import pytest

from app.connectors.app import create_application
from app.connectors.http import File, ProviderError
from app.connectors.vk import VK


def payload(**changes):
    message = {"id": 10, "peer_id": 42, "from_id": 42, "text": "Тест", "out": 0} | changes
    return {"type": "message_new", "group_id": 3, "secret": "vk-secret",
            "object": {"message": message}}


def test_vk_authentication_and_direct_chat_scope(settings_factory, http):
    adapter = VK(settings_factory(), http)
    assert adapter.authenticate(b"", {}, payload())
    for change in ({"group_id": 4}, {"secret": "wrong"}, {"group_id": True}):
        assert not adapter.authenticate(b"", {}, payload() | change)
    for change in ({"out": 1}, {"peer_id": 2_000_000_001}, {"from_id": -3}, {"id": 0}):
        assert not adapter.incoming(payload(**change))


def test_vk_text_button_and_attachment_normalization(settings_factory, http):
    adapter = VK(settings_factory(), http)
    assert adapter.incoming(payload())[0]["text"] == "Тест"
    event = adapter.incoming(payload(payload=json.dumps({"action": "action:2"})))[0]
    assert event["text"] == "action:2"
    event = adapter.incoming(payload(text="", attachments=[{
        "type": "doc", "doc": {"url": "https://x.userapi.com/doc", "title": "test.pdf"},
    }]))[0]
    assert event["files"][0]["name"] == "test.pdf"
    assert event["text"]


async def test_vk_send_uses_stable_random_id_and_keeps_all_choices(settings_factory, http):
    adapter = VK(settings_factory(), http)
    message = {"content": "Тест", "content_attributes": {"items": [
        {"title": f"Вариант {i}", "value": f"action:{i}"} for i in range(13)]}}
    parts = adapter.parts(message)
    assert len(parts) == 3
    assert all(len(p["keyboard"]["buttons"]) <= 6 for p in parts)
    buttons = [row[0] for p in parts for row in p["keyboard"]["buttons"]]
    assert [json.loads(b["action"]["payload"])["action"] for b in buttons] == [
        f"action:{i}" for i in range(13)]
    http.json.return_value = {"response": 91}
    await adapter.send("42", parts[0], key="cw:1:10")
    first = copy.deepcopy(http.json.call_args.kwargs["data"])
    await adapter.send("42", parts[0], key="cw:1:10")
    assert http.json.call_args.kwargs["data"]["random_id"] == first["random_id"]
    await adapter.send("42", parts[0], key="cw:1:11")
    assert http.json.call_args.kwargs["data"]["random_id"] != first["random_id"]
    assert first["peer_id"] == "42"


async def test_vk_pdf_upload_then_send(settings_factory, http):
    adapter = VK(settings_factory(), http)
    http.json.side_effect = [
        {"response": {"upload_url": "https://pu.vk.com/upload"}},
        {"file": "upload-handle"},
        {"response": {"doc": {"id": 8, "owner_id": 9, "access_key": "key"}}},
        {"response": 91},
    ]
    assert await adapter.send("42", {}, key="pdf", file=File("test.pdf", "application/pdf",
                                                            b"%PDF-test")) == "91"
    assert http.json.call_args.kwargs["data"]["attachment"] == "doc9_8_key"


@pytest.mark.parametrize("code,retryable", [(6, True), (901, False), (5, False)])
async def test_vk_provider_error_classification(settings_factory, http, code, retryable):
    adapter = VK(settings_factory(), http)
    http.json.return_value = {"error": {"error_code": code, "error_msg": "must-not-leak"}}
    with pytest.raises(ProviderError) as caught:
        await adapter.send("42", {"text": "Тест"}, key="test")
    assert caught.value.retryable is retryable
    assert "must-not-leak" not in str(caught.value)


@pytest.mark.parametrize("url", ["http://pu.vk.com/a", "https://vk.com.evil.org/a",
                                 "https://127.0.0.1/a", "https://a.vk.com:444/a"])
def test_vk_rejects_untrusted_media_hosts(settings_factory, http, url):
    with pytest.raises(ProviderError):
        VK(settings_factory(), http).media_host(url)


async def test_vk_webhook_confirmation_and_enqueue_failure(settings_factory, http, client_factory):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    settings = settings_factory()
    queue = SimpleNamespace(enqueue=AsyncMock(), healthy=AsyncMock(return_value=True))
    client = await client_factory(create_application(settings, VK(settings, http), queue))
    response = await client.post("/webhooks/vk", json=payload() | {"type": "confirmation"})
    assert response.status == 200 and await response.text() == "confirm"
    assert queue.enqueue.await_count == 0
    assert (await client.post("/webhooks/vk", json=payload() | {"secret": "bad"})).status == 401
    assert (await client.post("/webhooks/vk", json=payload())).status == 200
    assert queue.enqueue.call_args.args[0]["kind"] == "incoming"
    queue.enqueue.side_effect = ConnectionError("synthetic")
    assert (await client.post("/webhooks/vk", json=payload())).status == 503
