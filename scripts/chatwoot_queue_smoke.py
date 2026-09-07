"""Exercise native macros, routing and sticky ownership in an isolated API conversation."""

import argparse
import asyncio
import json
from time import monotonic

from app.chatwoot.client import ChatwootClient
from app.chatwoot.queues import team_id
from app.config import settings


async def main(conversation_id: int):
    api = ChatwootClient(
        base_url=settings.chatwoot_base_url, account_id=settings.chatwoot_account_id,
        read_token=settings.chatwoot_read_token, bot_token=settings.chatwoot_bot_token,
    )
    conversation = await api.get_conversation(conversation_id)
    meta = conversation.get("meta", {})
    if (
        meta.get("channel") != "Channel::Api"
        or meta.get("sender", {}).get("name") != "Техническая проверка — не обращение"
    ):
        raise RuntimeError("Refusing to modify a non-test conversation")
    base = f"/api/v1/accounts/{settings.chatwoot_account_id}"
    prefix = f"{base}/conversations/{conversation_id}"

    async def request(method, path, payload=None):
        return await api._transport.request(method, path, settings.chatwoot_read_token, payload)

    async def post(path, payload):
        return await request("POST", prefix + path, payload)

    def passed(name, **details):
        print(json.dumps({"check": name, "passed": True, **details}), flush=True)

    async def wait_for(check, label):
        deadline = monotonic() + 90
        while monotonic() < deadline:
            if value := await check():
                return value
            await asyncio.sleep(0.5)
        raise RuntimeError(f"timeout:{label}")

    async def send(content):
        message = await post("/messages", {"content": content, "message_type": "incoming"})

        async def reply():
            return next((m for m in await api.get_messages(conversation_id)
                         if m.get("content_attributes", {}).get("bot_turn_key")
                         == f"message:{message['id']}"), None)

        return await wait_for(reply, "reply")

    macro_payload = await request("GET", base + "/macros")
    macros = macro_payload if isinstance(macro_payload, list) else macro_payload["payload"]
    macro_ids = {m["name"]: m["id"] for m in macros}
    teams = await api.get_teams()
    legal_id = next(t["id"] for t in teams if t["name"].casefold() == "юристы")
    default_id = settings.chatwoot_duty_team_id
    assert legal_id != default_id
    assert await api.get_team_members(legal_id)
    passed("native_queue_catalog", team_count=len(teams))

    async def macro(name):
        await request("POST", f"{base}/macros/{macro_ids[name]}/execute", {
            "conversation_ids": [conversation_id],
        })

    async def return_to_bot():
        before = (await api.get_conversation(conversation_id))["custom_attributes"].get(
            "ownership_last_staff_message_id", 0
        )
        await macro("Вернуть боту")

        async def returned():
            attrs = (await api.get_conversation(conversation_id))["custom_attributes"]
            return attrs.get("reply_owner") == "bot" and attrs.get(
                "ownership_last_staff_message_id", 0
            ) > before

        await wait_for(returned, "return_macro")

    try:
        await return_to_bot()
        # Explicitly remove routing from this synthetic conversation only.
        await post("/assignments", {"team_id": None})
        await asyncio.sleep(2)
        await api.set_custom_attributes(conversation_id, {
            "routing_team_id": None, "routing_origin": "auto", "routing_notice": None,
        })
        await send("/clear")
        await send("Мне нужна консультация юриста по трудовому договору.")
        await send("human")
        current = await api.get_conversation(conversation_id)
        assert team_id(current) == legal_id
        assert current["custom_attributes"]["routing_last_decision"]["reason"] == "model_selected"
        assert current["custom_attributes"]["reply_owner"] == "bot"
        notice = current["custom_attributes"]["routing_notice"]
        assert notice["sent"] and notice["team_id"] == legal_id
        passed("live_qwen_routes_to_legal_and_notifies_team")

        profile = await request("GET", "/api/v1/profile")
        await post("/assignments", {"assignee_id": profile["id"]})

        async def human():
            return (await api.get_conversation(conversation_id))["custom_attributes"].get(
                "reply_owner"
            ) == "human"

        await wait_for(human, "human_takeover")
        await macro("Передать дежурным")

        async def moved():
            c = await api.get_conversation(conversation_id)
            attrs = c["custom_attributes"]
            return (
                team_id(c) == default_id and attrs.get("routing_origin") == "manual"
                and attrs.get("routing_notice", {}).get("sent")
                and attrs.get("routing_notice", {}).get("team_id") == default_id
            )

        await wait_for(moved, "transfer_macro")
        await post("/assignments", {"assignee_id": None})
        await send("/system_info")
        assert await human()
        incoming = await post("/messages", {
            "content": "Техническая проверка ожидания специалистки.", "message_type": "incoming",
        })
        await asyncio.sleep(4)
        assert not await api.has_reply_for_turn(conversation_id, f"message:{incoming['id']}")
        await send("/clear")
        assert await human()
        passed("native_transfer_and_unassignment_keep_human_mode")
        await return_to_bot()
        await send("/start")
        passed("native_return_macro_reenables_bot")
        folders = await request("GET", base + "/custom_filters?filter_type=conversation")
        folders = folders if isinstance(folders, list) else folders["payload"]
        folder = next(f for f in folders if f["name"] == "Юридическая помощь")
        assert str(legal_id) in folder["query"]["payload"][0]["values"]
        filtered = await request("POST", base + "/conversations/filter", folder["query"])
        assert isinstance(filtered, dict)
        passed("native_navigation_folder_filter")
    finally:
        await post("/toggle_status", {"status": "resolved"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--conversation", type=int, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.conversation))
    except Exception as error:  # noqa: BLE001 - no conversation or credential-bearing errors
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        raise SystemExit(1) from None
