"""Golden contract emitted by real installed Chatwoot serializers, without DB writes."""
import json
from pathlib import Path

from app.chatwoot.contracts import parse_message_created


def test_real_native_webhook_routes_every_category():
    rows = json.loads((Path(__file__).parent / "fixtures/chatwoot_native_callback_webhooks.json").read_text())
    assert len(rows) == 6
    for row in rows:
        assert set(row["inbox"]) == {"id", "name"}
        assert row["conversation"]["channel"] == "Channel::Telegram"
        assert parse_message_created(row).content == row["expected_action"]
