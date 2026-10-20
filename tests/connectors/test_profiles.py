import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.connectors.__main__ import load_provider
from app.connectors.app import create_application
from app.connectors.profiles import (
    PROFILES,
    initialize,
    inspect_profiles,
    read_values,
    settings_from_values,
)


def save(directory, profile, values):
    path = directory / f"{profile.name}.env"
    path.write_text("".join(f"{key}={json.dumps(value)}\n" for key, value in values.items()))
    path.chmod(0o600)


def fill(directory):
    initialize(directory, webhook_domain="bots.example.org", chatwoot_url="https://cw.example.org")
    for index, profile in enumerate(PROFILES, start=1):
        values = read_values(directory, profile)
        values.update({
            "CONNECTOR_ACCOUNT_ID": "1", "CONNECTOR_INBOX_ID": str(index),
            "CONNECTOR_API_TOKEN": f"service-token-{index}",
            "CONNECTOR_INBOX_IDENTIFIER": f"inbox-{index}",
            "CONNECTOR_INBOX_HMAC_TOKEN": f"hmac-{index}",
        })
        if profile.provider == "vk":
            values.update({"CONNECTOR_VK_GROUP_ID": str(index), "CONNECTOR_VK_TOKEN": "vk-test",
                           "CONNECTOR_VK_CONFIRMATION": "confirmation"})
        else:
            values.update({
                "CONNECTOR_WHATSAPP_PHONE_ID": str(index), "CONNECTOR_WHATSAPP_BUSINESS_ID": "90",
                "CONNECTOR_WHATSAPP_TOKEN": "wa-test", "CONNECTOR_WHATSAPP_APP_SECRET": "app-test",
                "CONNECTOR_WHATSAPP_API_VERSION": "v25.0",
            })
        save(directory, profile, values)


def test_four_private_profiles_have_independent_projects_ports_and_secrets(tmp_path):
    paths = initialize(tmp_path)
    assert len(paths) == 4
    assert {p.bot for p in PROFILES} == {"nevidima_bot", "nevidima_mvp_bot"}
    assert len({p.project for p in PROFILES}) == len({p.port for p in PROFILES}) == 4
    secrets = set()
    for profile, path in zip(PROFILES, paths, strict=True):
        assert path.stat().st_mode & 0o077 == 0
        values = read_values(tmp_path, profile)
        assert values["CONNECTOR_PROFILE"] == profile.bot
        assert values["CONNECTOR_ENV_FILE"] == str(path)
        secrets.add(values["CONNECTOR_CALLBACK_SECRET"])
    assert len(secrets) == 4
    assert not any(report["ready"] for report in inspect_profiles(tmp_path))


def test_init_does_not_overwrite_existing_credentials(tmp_path):
    initialize(tmp_path)
    original = (tmp_path / f"{PROFILES[0].name}.env").read_bytes()
    with pytest.raises(FileExistsError):
        initialize(tmp_path)
    assert (tmp_path / f"{PROFILES[0].name}.env").read_bytes() == original


def test_filled_profiles_are_ready_and_report_does_not_contain_credentials(tmp_path):
    fill(tmp_path)
    reports = inspect_profiles(tmp_path)
    assert all(report["ready"] for report in reports)
    serialized = json.dumps(reports)
    for profile in PROFILES:
        values = read_values(tmp_path, profile)
        for field in ("CONNECTOR_API_TOKEN", "CONNECTOR_CALLBACK_SECRET", "CONNECTOR_INBOX_HMAC_TOKEN"):
            assert values[field] not in serialized


@pytest.mark.parametrize("field,error", [
    ("CONNECTOR_VK_GROUP_ID", "shared_provider_identity"),
    ("CONNECTOR_INBOX_ID", "shared_chatwoot_inbox"),
    ("CONNECTOR_API_TOKEN", "shared_connector_api_token"),
    ("CONNECTOR_BIND_PORT", "shared_connector_bind_port"),
    ("CONNECTOR_PUBLIC_URL", "shared_connector_public_url"),
    ("CONNECTOR_CALLBACK_SECRET", "shared_connector_callback_secret"),
])
def test_production_and_mvp_cannot_share_transport_bindings(tmp_path, field, error):
    fill(tmp_path)
    production, mvp = PROFILES[:2]
    values = read_values(tmp_path, mvp)
    values[field] = read_values(tmp_path, production)[field]
    save(tmp_path, mvp, values)
    reports = inspect_profiles(tmp_path)
    assert error in reports[0]["errors"]
    assert error in reports[1]["errors"]
    assert not reports[0]["ready"] and not reports[1]["ready"]
    assert reports[2]["ready"] and reports[3]["ready"]


def test_missing_mvp_credential_cannot_inherit_production_environment(tmp_path, monkeypatch):
    fill(tmp_path)
    values = read_values(tmp_path, PROFILES[1])
    values.pop("CONNECTOR_API_TOKEN")
    monkeypatch.setenv("CONNECTOR_API_TOKEN", "production-token-must-not-be-inherited")
    with pytest.raises(ValueError):
        settings_from_values(values)


def test_publicly_readable_secret_files_are_rejected(tmp_path):
    initialize(tmp_path)
    path = tmp_path / f"{PROFILES[0].name}.env"
    path.chmod(0o644)
    assert inspect_profiles(tmp_path)[0]["errors"] == ["private_profile_file_unavailable"]


def test_init_rolls_back_partial_profile_creation(tmp_path, monkeypatch):
    original_open = os.open
    calls = 0

    def fail_second(path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic file write failure")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_second)
    with pytest.raises(OSError):
        initialize(tmp_path)
    assert not list(Path(tmp_path).glob("*.env"))


def test_aliases_of_the_same_provider_id_are_not_isolated(tmp_path):
    fill(tmp_path)
    values = read_values(tmp_path, PROFILES[1])
    values["CONNECTOR_VK_GROUP_ID"] = "001"
    save(tmp_path, PROFILES[1], values)
    reports = inspect_profiles(tmp_path)
    assert "shared_provider_identity" in reports[0]["errors"]
    assert not reports[1]["ready"]


@pytest.mark.parametrize("index", range(4), ids=[p.name for p in PROFILES])
async def test_each_bot_accepts_its_own_messages_and_rejects_the_other_profile(
        tmp_path, index, http, redis, client_factory):
    fill(tmp_path)
    profile = PROFILES[index]
    settings = settings_from_values(read_values(tmp_path, profile))
    other = settings_from_values(read_values(tmp_path, PROFILES[index ^ 1]))
    queue = SimpleNamespace(enqueue=AsyncMock(), healthy=AsyncMock(return_value=True))
    client = await client_factory(create_application(settings, load_provider(settings, http, redis), queue))

    def inbound(configuration):
        if profile.provider == "vk":
            payload = {"type": "message_new", "group_id": configuration.vk_group_id,
                       "secret": configuration.vk_secret.get_secret_value(), "object": {
                           "message": {"id": 10, "peer_id": 42, "from_id": 42, "text": "Тест"}}}
            return json.dumps(payload).encode(), {}
        payload = {"object": "whatsapp_business_account", "entry": [{
            "id": configuration.whatsapp_business_id, "changes": [{"field": "messages", "value": {
                "metadata": {"phone_number_id": configuration.whatsapp_phone_id},
                "messages": [{"id": "wamid.test", "from": "79990000001", "type": "text",
                              "timestamp": str(int(time.time())), "text": {"body": "Тест"}}]}}]}]}
        raw = json.dumps(payload).encode()
        signature = hmac.new(configuration.whatsapp_app_secret.get_secret_value().encode(),
                             raw, hashlib.sha256).hexdigest()
        return raw, {"X-Hub-Signature-256": "sha256=" + signature}

    own_body, own_headers = inbound(settings)
    path = "/webhooks/" + profile.provider
    assert (await client.post(path, data=own_body, headers=own_headers)).status == 200
    assert queue.enqueue.await_count == 1
    other_body, other_headers = inbound(other)
    response = await client.post(path, data=other_body, headers=other_headers)
    assert response.status == (401 if profile.provider == "vk" else 200)
    assert queue.enqueue.await_count == 1

    callback = "/webhooks/chatwoot/" + settings.callback_secret.get_secret_value()
    outgoing = {"event": "message_created", "id": 11, "message_type": "outgoing", "private": False,
                "account": {"id": settings.account_id}, "inbox": {"id": settings.inbox_id},
                "conversation": {"id": 7}}
    assert (await client.post(callback, json=outgoing)).status == 204
    assert queue.enqueue.await_count == 2
    foreign = outgoing | {"inbox": {"id": other.inbox_id}}
    assert (await client.post(callback, json=foreign)).status == 204
    assert queue.enqueue.await_count == 2
    assert (await client.post("/webhooks/chatwoot/" + other.callback_secret.get_secret_value(),
                              json=outgoing)).status == 404
