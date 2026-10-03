"""Run existing integration probes through the official CLI, in fresh API chats only."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import time
import traceback
import uuid
from pathlib import Path

from app.chatwoot.client import ChatwootClient
from scripts import chatwoot_live_smoke, chatwoot_queue_smoke
from scripts.resolve_prod_host import resolve_prod_host, verify_ssh_host
from scripts.setup_chatwoot_cli import cli_binary

TEST_INBOX = "Техническая проверка интеграции"
TEST_CONTACT = "Техническая проверка — не обращение"


async def cli_json(*args: str):
    process = await asyncio.create_subprocess_exec(
        cli_binary(), *args, "-o", "json", "--no-color",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("cli_timeout") from None
    if process.returncode:
        raise RuntimeError("cli_request_failed")
    return json.loads(stdout) if stdout.strip() else {}


async def request(method: str, path: str, payload=None):
    args = ["api", "--exact", path, "-X", method]
    if payload is not None:
        args += ["--data", json.dumps(payload, ensure_ascii=False)]
    return await cli_json(*args)


class TestConversationTransport:
    """Reuse our API client's reads, but constrain test mutations to created chats."""

    def __init__(self, account_id: int):
        self.base = f"/api/v1/accounts/{account_id}"
        self.conversation_ids: set[int] = set()

    def check_write(self, method: str, path: str, payload) -> None:
        if method == "GET":
            return
        if method == "POST":
            for conversation_id in self.conversation_ids:
                prefix = f"{self.base}/conversations/{conversation_id}"
                if path in {prefix + suffix for suffix in (
                    "/messages", "/assignments", "/custom_attributes", "/toggle_status",
                )}:
                    return
            if path == self.base + "/conversations/filter":
                return  # Read-only filter endpoint uses POST.
            if path.startswith(self.base + "/macros/") and path.endswith("/execute"):
                ids = (payload or {}).get("conversation_ids")
                if isinstance(ids, list) and ids and set(ids) <= self.conversation_ids:
                    return
        raise ValueError("Refusing to modify anything outside this run's test conversations")

    async def request(self, method, path, token, payload=None):
        self.check_write(method, path, payload)
        return await request(method, path, payload)


def validate_test_inbox(inbox: dict) -> None:
    if (inbox.get("channel_type") != "Channel::Api" or inbox.get("name") != TEST_INBOX
            or inbox.get("webhook_url") or inbox.get("callback_webhook_url")):
        raise ValueError("Expected the isolated API inbox with no external delivery webhook")


async def fresh_conversation(base: str, inbox_id: int, run_id: str, suite: str) -> tuple[int, str]:
    contact = await request("POST", base + "/contacts", {
        "inbox_id": inbox_id, "name": TEST_CONTACT,
        "identifier": f"chatwoot-acceptance:{run_id}:{suite}",
    })
    contact = contact["payload"]["contact"]
    source = next(item["source_id"] for item in contact["contact_inboxes"]
                  if item["inbox"]["id"] == inbox_id)
    conversation = await request("POST", base + "/conversations", {
        "inbox_id": inbox_id, "contact_id": contact["id"], "source_id": source,
        "status": "pending", "custom_attributes": {"acceptance_run_id": run_id},
    })
    return int(conversation["id"]), source


async def verify_client_visibility(api, conversation_id: int, public_path: str) -> None:
    internal = await api.get_messages(conversation_id)
    private_ids = {m["id"] for m in internal if m.get("private")}
    staff_ids = {m["id"] for m in internal if not m.get("private")
                 and m.get("message_type") == 1 and (m.get("sender") or {}).get("type") == "user"}
    assert private_ids and staff_ids, "Test must contain both private and public staff messages"
    public = await request("GET", public_path)
    assert isinstance(public, list)
    public_ids = {m["id"] for m in public}
    assert not private_ids & public_ids
    assert staff_ids & public_ids
    assert not any(m.get("private") for m in public)
    print(json.dumps({"check": "client_view_hides_notes_and_shows_staff_reply", "passed": True}))


def save_report(path: Path, report: dict) -> None:
    with open(path, "w", opener=lambda name, flags: os.open(name, flags, 0o600)) as out:
        json.dump(report, out, ensure_ascii=False, indent=2)


async def main(suite: str) -> bool:
    # Identity is checked afresh before any API mutation, not trusted from CLI config.
    host = await asyncio.to_thread(resolve_prod_host)
    await asyncio.to_thread(verify_ssh_host, host)
    address = host.rsplit("@", 1)[-1]
    url = f"https://chatwoot.{address.replace('.', '-')}.sslip.io"
    profile = await cli_json("auth", "status")
    if profile.get("Instance", "").rstrip("/") != url or str(profile.get("Account")) != "1":
        raise ValueError("CLI points at another instance/account; run just chatwoot-login")
    base = "/api/v1/accounts/1"
    inboxes = (await request("GET", base + "/inboxes"))["payload"]
    inbox = next(i for i in inboxes if i.get("name") == TEST_INBOX)
    inbox = await request("GET", f"{base}/inboxes/{inbox['id']}")
    validate_test_inbox(inbox)
    binding = await request("GET", f"{base}/inboxes/{inbox['id']}/agent_bot")
    if not (binding.get("agent_bot") or {}).get("id"):
        raise ValueError("Test inbox has no Agent Bot attached")
    teams = await request("GET", base + "/teams")
    duty_id = next(t["id"] for t in teams if t["name"].casefold() == "дежурные")
    transport = TestConversationTransport(1)
    api = ChatwootClient(
        base_url=url, account_id=1, read_token="", bot_token="", transport=transport,
    )
    run_id = str(uuid.uuid4())
    report = {"run_id": run_id, "url": url, "scope": "chatwoot_api_only", "suites": []}
    output = Path(".runtime/chatwoot-tests")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    report_path = output / f"{run_id}.json"
    suites = {"dialogue": chatwoot_live_smoke.main, "queues": chatwoot_queue_smoke.main}
    for name, test in suites.items():
        if suite != "all" and suite != name:
            continue
        conversation_id, source = await fresh_conversation(base, inbox["id"], run_id, name)
        transport.conversation_ids.add(conversation_id)
        entry = {"suite": name, "conversation_id": conversation_id, "passed": False}
        capture, started = io.StringIO(), time.monotonic()
        try:
            with contextlib.redirect_stdout(capture):
                await test(conversation_id, api=api, account_id=1, duty_team_id=duty_id)
                if name == "dialogue":
                    public_path = (f"/public/api/v1/inboxes/{inbox['inbox_identifier']}/contacts/"
                                   f"{source}/conversations/{conversation_id}/messages")
                    await verify_client_visibility(api, conversation_id, public_path)
            entry["passed"] = True
        except Exception as error:  # noqa: BLE001 - report location, never response text
            location = traceback.extract_tb(error.__traceback__)[-1]
            entry.update(error_type=type(error).__name__, function=location.name,
                         file=Path(location.filename).name, line=location.lineno)
        finally:
            try:
                await api.set_status(conversation_id, "resolved")
            except Exception as error:  # noqa: BLE001
                entry.update(cleanup_error=type(error).__name__, passed=False)
            entry["elapsed_seconds"] = round(time.monotonic() - started, 2)
            entry["checks"] = [json.loads(line) for line in capture.getvalue().splitlines()]
            report["suites"].append(entry)
            await asyncio.to_thread(save_report, report_path, report)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    print(json.dumps({"report": str(report_path)}), flush=True)
    return all(s["passed"] for s in report["suites"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", choices=["all", "dialogue", "queues"], default="all")
    args = parser.parse_args()
    try:
        success = asyncio.run(main(args.suite))
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"passed": False, "error_type": type(error).__name__}))
        raise SystemExit(1) from None
    raise SystemExit(0 if success else 1)
