"""Exercise the deployed service through Chatwoot's HTTP API, in a test inbox only.

Run inside the configured agent container. No Telegram recipients, credentials,
or message text are printed. The synthetic transcript is retained in Chatwoot.
"""

import argparse
import asyncio
import json
from time import monotonic

from app.chatwoot.client import ChatwootClient
from app.config import settings


async def main(conversation_id: int) -> None:
    api = ChatwootClient(
        base_url=settings.chatwoot_base_url,
        account_id=settings.chatwoot_account_id,
        read_token=settings.chatwoot_read_token,
        bot_token=settings.chatwoot_bot_token,
    )
    conversation = await api.get_conversation(conversation_id)
    meta = conversation.get("meta", {})
    if (
        meta.get("channel") != "Channel::Api"
        or meta.get("sender", {}).get("name") != "Техническая проверка — не обращение"
    ):
        raise RuntimeError("Refusing to modify a non-test conversation")
    prefix = f"/api/v1/accounts/{settings.chatwoot_account_id}/conversations/{conversation_id}"

    async def post(suffix, payload):
        return await api._transport.request(
            "POST", prefix + suffix, settings.chatwoot_read_token, payload
        )

    async def send(content):
        incoming = await post("/messages", {"content": content, "message_type": "incoming"})
        key = f"message:{incoming['id']}"
        deadline = monotonic() + 90
        while monotonic() < deadline:
            messages = await api.get_messages(conversation_id)
            for message in messages:
                if message.get("content_attributes", {}).get("bot_turn_key") == key:
                    return message
            await asyncio.sleep(0.5)
        raise RuntimeError("No response within deadline")

    def choices(message):
        return [item["value"] for item in message.get("content_attributes", {}).get("items", [])]

    def passed(name, **metadata):
        print(json.dumps({"check": name, "passed": True, **metadata}), flush=True)

    async def return_to_bot():
        await post("/assignments", {"team_id": None})
        await post("/assignments", {"assignee_id": None})
        current = await api.get_conversation(conversation_id)
        await post(
            "/custom_attributes",
            {
                "custom_attributes": {
                    **current.get("custom_attributes", {}),
                    "reply_owner": "bot",
                }
            },
        )
        await post("/toggle_status", {"status": "pending"})

    await return_to_bot()
    old_count = len(await api.get_messages(conversation_id))
    await send("/clear")
    assert len(await api.get_messages(conversation_id)) > old_count
    passed("clear_preserves_history")
    assert "continue" in choices(await send("/start"))
    passed("start")
    assert "need:other" in choices(await send("continue"))
    passed("need_menu")
    response = await send("need:other")
    back = next(value for value in choices(response) if value.startswith("back:"))
    assert "need:other" in choices(await send(back))
    passed("back_restores_state")
    response = await send("Мне нужна еда.")
    buttons = choices(response)
    assert any("food" in value for value in buttons)
    assert "human" in buttons
    passed("live_qwen_and_contextual_buttons", buttons=buttons)
    response = await send("/system_info")
    assert "Chatwoot → Telegram" in response["content"]
    passed("system_info")
    await send("human")
    current = await api.get_conversation(conversation_id)
    assert current["custom_attributes"]["reply_owner"] == "human"
    assert current["status"] == "open" and current["meta"].get("team")
    passed("handoff_to_duty_team")
    await post(
        "/messages",
        {"message_type": "outgoing", "content": "Техническая проверка ответа дежурного."},
    )
    before = len(
        [
            m
            for m in await api.get_messages(conversation_id)
            if m.get("content_attributes", {}).get("bot_turn_key")
        ]
    )
    await post("/messages", {"message_type": "incoming", "content": "Проверка режима дежурного."})
    await asyncio.sleep(4)
    after = len(
        [
            m
            for m in await api.get_messages(conversation_id)
            if m.get("content_attributes", {}).get("bot_turn_key")
        ]
    )
    assert before == after
    passed("human_reply_without_bot_interruption")
    await return_to_bot()
    assert "continue" in choices(await send("/start"))
    passed("return_to_bot")
    await post("/toggle_status", {"status": "resolved"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--conversation", type=int, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.conversation))
    except Exception as error:  # noqa: BLE001 - safe diagnostics only
        print(json.dumps({"error_type": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
