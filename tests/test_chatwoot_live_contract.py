from app.chatwoot.contracts import IncomingChatwootMessage
from app.chatwoot.service import _bot_owns, _history_after_epoch, _is_callback, _seed_conversation


def test_native_chatwoot_agent_bot_assignment_is_not_a_human():
    assert _bot_owns(
        {"status": "pending", "meta": {"assignee_type": "AgentBot", "assignee": {"id": 1}}}
    )


def test_native_chatwoot_human_assignment_stops_bot():
    assert not _bot_owns(
        {"status": "pending", "meta": {"assignee_type": "User", "assignee": {"id": 1}}}
    )
    assert _bot_owns({"status": "open", "meta": {}})
    assert _bot_owns({"status": "pending", "meta": {"team": {"id": 1}}})


def test_navigation_survives_stateless_service_recreation():
    navigation = {"entries": [], "cursor": None, "revision": 4}
    event = IncomingChatwootMessage(1, 1, 1, 1, "back:4")
    seeded = _seed_conversation(
        event, {"custom_attributes": {"workflow_navigation": navigation}}, ()
    )
    assert seeded.record.navigation == navigation
    assert _is_callback(event.content)


def test_chatwoot_activity_is_never_sent_to_model_as_assistant_speech():
    history = _history_after_epoch(
        (
            {"id": 1, "message_type": 2, "content": "Assignment changed", "created_at": 1},
            {"id": 2, "message_type": 0, "content": "Hello", "created_at": 2},
        ),
        0,
        3,
    )
    assert history == (("user", "Hello"),)


def test_certificate_message_is_masked_before_chatwoot_history_reaches_model():
    history = _history_after_epoch(
        (
            {
                "id": 1,
                "message_type": 1,
                "content": "Код активации: BEARER-SECRET",
                "content_attributes": {"bot_sensitive_content": "certificate"},
                "created_at": 1,
            },
        ),
        0,
        2,
    )

    assert history == (("assistant", "[SENSITIVE_DELIVERY]"),)
