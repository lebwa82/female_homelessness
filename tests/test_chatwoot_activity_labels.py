import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_chatwoot_contracts import _message_event
from test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation

from app.catalog import get_aid_item
from app.chatwoot.contracts import parse_message_created
from app.chatwoot.scenario_effects import EVENT_TITLES, scenario_note
from app.chatwoot.service import ChatwootAgentService, _history_after_epoch

ROOT = Path(__file__).resolve().parents[1]
ATTRIBUTE = "nevidimy_telegram_callback_data"


def callback_fixture(*, name="nevidima_bot", data="sc:s35:0:4", keyboard=None):
    if keyboard is None:
        keyboard = [[{"text": "Задать вопрос юристу", "callback_data": "sc:s35:0:4"}]]
    return {"bot_name": name, "params": {"callback_query": {
        "data": data, "message": {"reply_markup": {"inline_keyboard": keyboard}},
    }}}


def ruby_outputs(fixtures):
    ruby = shutil.which("ruby")
    if not ruby:
        pytest.skip("Ruby is required for the native Telegram callback adapter probe")
    result = subprocess.run(
        [ruby, str(ROOT / "tests/timeweb_telegram_callback_probe.rb")],
        input=json.dumps(fixtures, ensure_ascii=False), text=True, capture_output=True,
        check=False, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("name", ["nevidima_bot", "nevidima_bot_mvp_bot"])
def test_clicked_button_is_readable_but_agent_gets_original_action(name):
    message, = ruby_outputs([callback_fixture(name=name)])
    assert message["content"] == "Нажата кнопка: «Задать вопрос юристу»."
    assert message["content_attributes"] == {ATTRIBUTE: "sc:s35:0:4", "in_reply_to_external_id": 19}
    payload = {**_message_event(), **message}
    assert parse_message_created(payload).content == "sc:s35:0:4"
    assert _history_after_epoch(({**message, "id": 1, "message_type": 0},), 0, 2) == (
        ("user", "Нажата кнопка: «Задать вопрос юристу»."),
    )


@pytest.mark.parametrize("keyboard", [[], None, "malformed", [[None, "malformed"]],
                                       [[{"text": "Wrong button", "callback_data": "human"}]],
                                       [[{"text": "", "callback_data": "sc:s35:0:4"}]]])
def test_missing_button_title_is_explicit_and_does_not_lose_callback(keyboard):
    fixture = callback_fixture()
    fixture["params"]["callback_query"]["message"]["reply_markup"]["inline_keyboard"] = keyboard
    message, = ruby_outputs([fixture])
    assert message["content"] == "Нажата кнопка Telegram (название недоступно)."
    assert parse_message_created({**_message_event(), **message}).content == "sc:s35:0:4"


def test_ordinary_messages_and_other_bots_are_not_rewritten():
    fixtures = [{"bot_name": "nevidima_bot", "params": {"message": {"text": "test input"}}},
                callback_fixture(name="other_bot")]
    ordinary, unrelated = ruby_outputs(fixtures)
    assert ordinary == {"content": "test input", "content_attributes": {"in_reply_to_external_id": 19}}
    assert unrelated == {"content": "sc:s35:0:4", "content_attributes": {"in_reply_to_external_id": 19}}
    assert parse_message_created({**_message_event(), **unrelated}).content == "sc:s35:0:4"


def test_label_is_taken_from_matching_button_not_first_button():
    fixture = callback_fixture(keyboard=[
        [{"text": "Другая кнопка", "callback_data": "human"}],
        [{"text": "Задать вопрос юристу", "callback_data": "sc:s35:0:4"}],
    ])
    message, = ruby_outputs([fixture])
    assert message["content"] == "Нажата кнопка: «Задать вопрос юристу»."


@pytest.mark.parametrize("action", ["sc:s35:0:4", "need:legal", "aid:legal_consultation",
                                    "back:4", "home:4", "human", "certificate:confirm", "followup:better", "continue"])
def test_all_callback_families_keep_original_action_and_show_button_title(action):
    message, = ruby_outputs([callback_fixture(data=action, keyboard=[[
        {"text": "Тестовый выбор", "callback_data": action},
    ]])])
    assert message["content"] == "Нажата кнопка: «Тестовый выбор»."
    assert parse_message_created({**_message_event(), **message}).content == action


@pytest.mark.parametrize("action", [None, {}, [], 42, True, "", " ", "\n", "\ud800", "x" * 65, "я" * 33])
def test_malformed_action_metadata_fails_closed(action):
    payload = {**_message_event(), "content_attributes": {ATTRIBUTE: action}}
    assert parse_message_created(payload) is None


def test_other_channels_cannot_override_text_with_telegram_action_metadata():
    payload = {**_message_event(),
               "conversation": {"id": 23, "channel": "Channel::Api"},
               "content_attributes": {ATTRIBUTE: "human"}}
    assert parse_message_created(payload).content == "test input"


def test_native_conversation_channel_takes_precedence_over_legacy_inbox_hint():
    payload = {**_message_event(),
               "conversation": {"id": 23, "channel": "Channel::Api"},
               "inbox": {"id": 3, "channel_type": "Channel::Telegram"},
               "content_attributes": {ATTRIBUTE: "human"}}
    assert parse_message_created(payload).content == "test input"


def test_legacy_payload_with_inbox_channel_hint_is_still_supported():
    payload = {**_message_event(), "conversation": {"id": 23},
               "inbox": {"id": 3, "channel_type": "Channel::Telegram"},
               "content_attributes": {ATTRIBUTE: "need:housing"}}
    assert parse_message_created(payload).content == "need:housing"


async def test_every_category_click_reaches_html_scenario_without_a_model_call():
    from app.scenario import NEEDS, SCREENS

    for screen, need in NEEDS.items():
        api = FakeChatwoot()
        gateway = StubGateway(ordinary_evaluation())
        service = ChatwootAgentService(api, gateway=gateway, duty_team_id=9)
        await service.process(event("/start", 42))
        await service.process(event("continue", 43))
        choice = next(c for c in api.replies[-1]["choices"] if c.id == f"need:{need.value}")
        message, = ruby_outputs([callback_fixture(data=choice.id, keyboard=[[
            {"text": choice.label, "callback_data": choice.id},
        ]])])
        parsed = parse_message_created({**_message_event(), **message, "id": 44})
        assert await service.process(parsed)
        assert api.conversation["custom_attributes"]["scenario"]["screen"] == screen
        assert api.replies[-1]["text"] == SCREENS[screen]["texts"][0]
        assert gateway.calls == 0
        assert not await service.process(parsed)
        assert len(api.replies) == 3


def test_consultation_note_names_the_action_service_and_request_without_internal_keys():
    note = scenario_note({"kind": "consultation_requested", "key": "a" * 24,
                          "aid_id": "legal_consultation"})
    assert note == ("Создана заявка на консультацию\nВид помощи: "
                    + get_aid_item("legal_consultation").label + "\nID заявки: " + "a" * 24)
    assert "consultation_requested" not in note
    assert "aid_id" not in note


def test_answer_note_uses_button_label_not_index_and_preserves_reference():
    note = scenario_note({"kind": "survey_answer", "key": "b" * 24, "answer": 0,
                          "label": "Тестовый ответ", "screen": "s7", "job_key": "review:test",
                          "request_id": "a" * 24})
    assert note == "Получен ответ на опрос\nОтвет: Тестовый ответ\nID заявки: " + "a" * 24


@pytest.mark.parametrize("kind", tuple(EVENT_TITLES))
def test_all_known_events_have_readable_notes_and_do_not_invent_a_request(kind):
    note = scenario_note({"kind": kind, "key": "c" * 24, "text": "Тестовый текст"})
    assert note.startswith(EVENT_TITLES[kind] + "\n")
    assert "Текст сообщения: Тестовый текст" in note
    assert ("ID заявки:" in note) == (kind == "consultation_requested")


async def test_readable_click_still_creates_one_consultation_without_replaying_on_retry():
    api = FakeChatwoot()
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()), duty_team_id=9)
    for index, value in enumerate(("/start", "continue", "need:legal", "aid:legal_consultation"), 42):
        await service.process(event(value, index))
    action = next(c.id for c in api.replies[-1]["choices"] if c.id.startswith("sc:s35:0:"))
    message, = ruby_outputs([callback_fixture(data=action, keyboard=[[
        {"text": "Задать вопрос юристу", "callback_data": action},
    ]])])
    parsed = parse_message_created({**_message_event(), **message, "id": 46})
    assert await service.process(parsed)
    requests = api.conversation["custom_attributes"]["scenario_requests"]
    assert len(requests) == 1
    assert next(iter(requests.values()))["aid_id"] == "legal_consultation"
    assert any(note.startswith("Создана заявка на консультацию") for note in api.notes)
    counts = (len(api.replies), len(api.notes))
    assert not await service.process(parsed)
    assert (len(api.replies), len(api.notes)) == counts


async def test_attention_alert_is_readable_and_remains_private_and_idempotent():
    api = FakeChatwoot()
    service = ChatwootAgentService(api, duty_team_id=9)
    actions = [(23, "scenario_event", "completed", {
        "kind": "negative_feedback", "key": "b" * 24, "request_id": "a" * 24,
    })]
    await service._scenario_effects.apply(23, 41, actions)
    assert len(api.notes) == 2
    assert "mention://team/9/queue" in api.notes[1]
    assert "Требует внимания: Отрицательная оценка консультации" in api.notes[1]
    assert "ID заявки: " + "a" * 24 in api.notes[1]
    assert all("negative_feedback" not in note for note in api.notes)
    assert not api.replies
    await service._scenario_effects.apply(23, 41, actions)
    assert len(api.notes) == 2


@pytest.mark.parametrize("method,label", [
    ("current_telegram", "этот чат Telegram"), ("other_telegram", "другой аккаунт Telegram"),
    ("phone", "телефон"), ("email", "электронная почта"), ("later", "указать позже"),
])
async def test_aid_request_note_has_readable_contact_method_and_stays_private(method, label):
    api = FakeChatwoot()
    service = ChatwootAgentService(api)
    request = SimpleNamespace(aid_id="legal_consultation", contact_method=method,
                              contact_value="test@example.invalid", city="Тестовый город",
                              district=None, request_key="fixture")
    seeded = SimpleNamespace(store=SimpleNamespace(aid_requests=[request]))
    await service._persist_aid_requests(23, seeded)
    assert api.notes == [("Создана заявка на помощь\nВид помощи: Юрист или адвокат\n"
                         f"Способ связи: {label}\nКонтакт: test@example.invalid\nМесто: Тестовый город")]
    assert "aid-request:fixture" in api.note_keys
    assert not api.replies
