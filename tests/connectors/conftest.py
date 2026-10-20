from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.connectors.config import ConnectorSettings


@pytest.fixture
def settings_factory():
    def create(provider="vk", **changes):
        values = {
            "provider": provider, "chatwoot_url": "https://cw.example.org", "account_id": 1,
            "inbox_id": 2, "api_token": "api-secret", "inbox_identifier": "inbox-id",
            "inbox_hmac_token": "hmac-secret", "callback_secret": "callback_" + "a" * 32,
            "vk_group_id": 3, "vk_token": "vk-token", "vk_secret": "vk-secret", "vk_confirmation": "confirm",
            "whatsapp_phone_id": "1234", "whatsapp_business_id": "5678", "whatsapp_token": "wa-token",
            "whatsapp_app_secret": "app-secret", "whatsapp_verify_token": "verify-secret",
            "whatsapp_api_version": "v25.0",
        }
        return ConnectorSettings(**(values | changes))
    return create


@pytest.fixture
def http():
    return SimpleNamespace(json=AsyncMock(), download=AsyncMock(return_value=b"%PDF-test"))


@pytest.fixture
async def client_factory():
    clients = []

    async def create(app):
        client = TestClient(TestServer(app))
        clients.append(client)
        await client.start_server()
        return client

    yield create
    for client in clients:
        await client.close()


class MemoryRedis:
    """Test double for receipts; never a substitute for the real Redis queue check."""
    def __init__(self):
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, **_kwargs):
        self.values[key] = str(value)

    async def eval(self, _script, _count, key, timestamp):
        if int(timestamp) > int(self.values.get(key, "0")):
            self.values[key] = str(timestamp)


@pytest.fixture
def redis():
    return MemoryRedis()
