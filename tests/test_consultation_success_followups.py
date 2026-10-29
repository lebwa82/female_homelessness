"""Neutral staff-confirmation fixtures; no external messages or real conversations."""
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_chatwoot_service import FakeChatwoot, StubGateway, ordinary_evaluation

from app.chatwoot.contracts import CONSULTATION_SUCCESS, StaffMessage, parse_staff_message
from app.chatwoot.event_queue import DurableEventQueue
from app.chatwoot.scenario_effects import daytime
from app.chatwoot.service import ChatwootAgentService
from app.config import settings
from app.scenario import SCREENS

NOW = datetime(2026, 10, 29, 10, tzinfo=UTC)
KEY = 'a' * 24
AIDS = ('psychologist_3_sessions', 'legal_consultation')


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(settings, 'llm_enabled', True)
    api = FakeChatwoot()
    service = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()), duty_team_id=9)
    service._scenario_effects.clock = lambda: NOW
    return api, service


def attrs(api):
    return api.conversation['custom_attributes']


def completion(aid, message_id=70, **kwargs):
    if isinstance(kwargs.get('occurred_at'), datetime):
        kwargs['occurred_at'] = kwargs['occurred_at'].isoformat()
    return StaffMessage(message_id, 23, 4, consultation_completed=True,
                        consultation_aid_id=aid, **kwargs)


def seed(api, aid):
    attrs(api)['scenario_requests'] = {
        KEY: {'aid_id': aid, 'state': 'requested', 'requested_at': (NOW-timedelta(days=12)).isoformat()}
    }
    attrs(api)['scenario_followups'] = {
        f'check:{KEY}': {'screen': 's5', 'state': 'pending', 'due_at': NOW.isoformat(),
                        'context': {'aid_id': aid, 'request_key': KEY}},
        f'reminder:check:{KEY}': {'screen': 's59', 'state': 'pending', 'due_at': NOW.isoformat(),
                                 'context': {'aid_id': aid, 'request_key': KEY}},
        'unrelated': {'screen': 's5', 'state': 'cancelled', 'due_at': NOW.isoformat(),
                      'context': {'aid_id': 'food_card'}},
    }
    attrs(api)['consultation_ends_at'] = '01.10.2026 09:00'  # Must not anchor either new job.


@pytest.mark.parametrize('aid', AIDS)
async def test_new_consultation_request_waits_for_staff_confirmation(runtime, aid):
    api, service = runtime
    await service._scenario_effects.apply(23, 41, [(23, 'scenario_event', 'completed', {
        'key': KEY, 'kind': 'consultation_requested', 'aid_id': aid,
    })])
    assert attrs(api)['scenario_requests'][KEY]['state'] == 'requested'
    assert attrs(api)['scenario_followups'] == {}
    service._scenario_effects.clock = lambda: NOW+timedelta(days=10)
    assert not await service.send_due_followup(23)
    await service.process(completion(aid))
    jobs = attrs(api)['scenario_followups']
    assert jobs[f'wellbeing:{KEY}']['due_at'] == (NOW+timedelta(days=14)).isoformat()


async def test_peer_consultation_contact_check_is_unchanged(runtime):
    api, service = runtime
    await service._scenario_effects.apply(23, 41, [(23, 'scenario_event', 'completed', {
        'key': KEY, 'kind': 'consultation_requested', 'aid_id': 'peer_consultation',
    })])
    assert attrs(api)['scenario_followups'][f'check:{KEY}']['screen'] == 's5'
    assert attrs(api)['scenario_followups'][f'check:{KEY}']['due_at'] == (NOW+timedelta(days=4)).isoformat()


async def test_certificate_delivery_does_not_cancel_consultation_wellbeing(runtime):
    api, service = runtime
    seed(api, AIDS[0])
    await service.process(completion(AIDS[0]))
    await service._scenario_effects.certificate_delivered(23, 'synthetic', {
        'aid_id': 'food_card', 'expires_at': (NOW+timedelta(days=7)).isoformat(),
    })
    assert attrs(api)['scenario_followups'][f'wellbeing:{KEY}']['state'] == 'pending'
    assert attrs(api)['scenario_followups']['certificate:synthetic']['state'] == 'pending'


@pytest.mark.parametrize('command,aid', CONSULTATION_SUCCESS.items())
def test_success_only_accepts_private_authenticated_staff(command, aid):
    payload = {'event': 'message_created', 'message_type': 'outgoing', 'private': True,
               'id': 70, 'conversation': {'id': 23}, 'sender': {'type': 'user', 'id': 4},
               'content': command, 'created_at': NOW.isoformat()}
    parsed = parse_staff_message(payload)
    assert parsed.consultation_completed and parsed.consultation_aid_id == aid
    assert parsed.occurred_at == NOW.isoformat()
    assert parse_staff_message({**payload, 'created_at': NOW.timestamp()}).occurred_at == NOW.isoformat()
    assert not parse_staff_message({**payload, 'private': False}).consultation_completed
    for role in ('contact', 'agent_bot'):
        assert parse_staff_message({**payload, 'sender': {'type': role, 'id': 4}}) is None
    assert parse_staff_message({**payload, 'message_type': 'incoming'}) is None


@pytest.mark.parametrize('aid', AIDS)
async def test_click_schedules_independent_hour_and_four_days(runtime, aid):
    api, service = runtime
    seed(api, aid)
    original = deepcopy(attrs(api)['scenario_followups']['unrelated'])
    await api.assign_human(23, 4)
    api.conversation['status'] = 'open'

    await service.process(completion(aid))

    jobs = attrs(api)['scenario_followups']
    assert jobs[f'review:{KEY}']['screen'] == 's7'
    assert jobs[f'review:{KEY}']['due_at'] == (NOW+timedelta(hours=1)).isoformat()
    assert jobs[f'wellbeing:{KEY}']['screen'] == 's52'
    assert jobs[f'wellbeing:{KEY}']['due_at'] == (NOW+timedelta(days=4)).isoformat()
    assert jobs[f'check:{KEY}']['state'] == 'cancelled'
    assert jobs[f'reminder:check:{KEY}']['state'] == 'cancelled'
    assert jobs['unrelated'] == original
    assert attrs(api)['scenario_requests'][KEY]['state'] == 'completed'
    assert attrs(api)['scenario_requests'][KEY]['success_marked_at'] == NOW.isoformat()
    assert api.conversation['assignee_id'] == 4
    assert api.conversation['status'] == 'open'
    assert api.replies == []


@pytest.mark.parametrize('aid', AIDS)
async def test_both_jobs_survive_restart_and_review_answer_does_not_cancel_wellbeing(runtime, aid):
    api, service = runtime
    seed(api, aid)
    await service.process(completion(aid))
    restarted = ChatwootAgentService(api, gateway=StubGateway(ordinary_evaluation()))
    restarted._scenario_effects.clock = lambda: NOW+timedelta(hours=1)
    assert await restarted.send_due_followup(23)
    assert attrs(api)['scenario']['screen'] == 's7'
    assert SCREENS['s7']['texts'][AIDS.index(aid)] == api.replies[-1]['text']
    assert not await restarted.send_due_followup(23)
    # Any first-survey answer stops only that survey's reminder/receipt.
    await restarted._scenario_effects.answer_current(api.conversation)
    assert attrs(api)['scenario_followups'][f'wellbeing:{KEY}']['state'] == 'pending'
    restarted._scenario_effects.clock = lambda: NOW+timedelta(days=4)
    assert await restarted.send_due_followup(23)
    assert api.replies[-1]['text'] == SCREENS['s52']['texts'][0]
    assert attrs(api)['scenario']['aid_id'] == aid
    assert not await restarted.send_due_followup(23)
    assert len(api.replies) == 2
    assert not any(k.startswith(('reminder:review:', 'reminder:wellbeing:'))
                   for k in attrs(api)['scenario_followups'])


@pytest.mark.parametrize('aid', AIDS)
async def test_late_queue_processing_anchors_to_macro_creation_not_processing(runtime, aid):
    api, service = runtime
    seed(api, aid)
    service._scenario_effects.clock = lambda: NOW+timedelta(days=2)

    await service.process(completion(aid, occurred_at=NOW))

    jobs = attrs(api)['scenario_followups']
    assert jobs[f'review:{KEY}']['due_at'] == (NOW+timedelta(hours=1)).isoformat()
    assert jobs[f'wellbeing:{KEY}']['due_at'] == (NOW+timedelta(days=4)).isoformat()


async def test_durable_queue_roundtrip_preserves_click_time_and_legacy_event_schema(runtime):
    api, service = runtime
    seed(api, AIDS[0])
    redis = SimpleNamespace(xadd=AsyncMock())
    queue = DurableEventQueue(redis, service, namespace='synthetic', lanes=1)
    await queue.enqueue(StaffMessage(69, 23, 4))
    legacy = json.loads(redis.xadd.call_args.args[1]['payload'])
    assert 'consultation_aid_id' not in legacy and 'occurred_at' not in legacy
    original = completion(AIDS[0], occurred_at=NOW)
    await queue.enqueue(original)
    fields = redis.xadd.call_args.args[1]
    assert StaffMessage(**json.loads(fields['payload'])) == original
    redis.xreadgroup = AsyncMock(return_value=[('synthetic:0', [('1-0', fields)])])
    transaction = MagicMock()
    transaction.execute = AsyncMock()
    pipeline = MagicMock()
    pipeline.__aenter__ = AsyncMock(return_value=transaction)
    pipeline.__aexit__ = AsyncMock(return_value=None)
    redis.pipeline = MagicMock(return_value=pipeline)
    service._scenario_effects.clock = lambda: NOW+timedelta(days=2)

    assert await queue.consume_one('synthetic:0')

    assert attrs(api)['scenario_followups'][f'wellbeing:{KEY}']['due_at'] == (NOW+timedelta(days=4)).isoformat()
    transaction.execute.assert_awaited_once()


@pytest.mark.parametrize('aid', AIDS)
async def test_repeated_click_preserves_schedule_and_current_conversation(runtime, aid):
    api, service = runtime
    seed(api, aid)
    await service.process(completion(aid))
    before = deepcopy(attrs(api)['scenario_followups'])
    attrs(api)['scenario'] = {'screen': 's2'}
    await api.assign_human(23, 4)
    await api.set_status(23, 'open')
    service._scenario_effects.clock = lambda: NOW+timedelta(days=1)

    await service.process(completion(aid, 71))
    await service.process(completion(aid, 71))

    assert attrs(api)['scenario_followups'] == before
    assert attrs(api)['scenario'] == {'screen': 's2'}
    assert api.conversation['assignee_id'] == 4
    assert api.conversation['status'] == 'open'


@pytest.mark.parametrize('aid', AIDS)
async def test_manual_consultation_without_client_request_can_be_marked_once(runtime, aid):
    api, service = runtime
    await service.process(completion(aid))
    requests = attrs(api)['scenario_requests']
    assert len(requests) == 1
    key, request = next(iter(requests.items()))
    assert request['source'] == 'staff_confirmation'
    assert 'requested_at' not in request
    assert set(attrs(api)['scenario_followups']) == {f'review:{key}', f'wellbeing:{key}'}
    before = deepcopy(attrs(api)['scenario_followups'])
    await service.process(completion(aid, 71))
    assert attrs(api)['scenario_followups'] == before


async def test_retry_after_saved_jobs_and_lost_note_keeps_first_click_time(runtime):
    api, service = runtime
    seed(api, AIDS[0])
    add_note = api.add_private_note

    async def lose_first_note(*args, **kwargs):
        raise RuntimeError('synthetic private-note failure')

    api.add_private_note = lose_first_note
    with pytest.raises(RuntimeError, match='synthetic private-note failure'):
        await service.process(completion(AIDS[0]))
    before = deepcopy(attrs(api)['scenario_followups'])
    api.add_private_note = add_note
    service._scenario_effects.clock = lambda: NOW+timedelta(days=1)

    await service.process(completion(AIDS[0]))

    assert attrs(api)['scenario_followups'] == before
    assert attrs(api)['scenario_requests'][KEY]['success_marked_at'] == NOW.isoformat()
    assert api.conversation['status'] == 'pending'


async def test_second_mark_does_not_replace_a_survey_already_sent(runtime):
    api, service = runtime
    seed(api, AIDS[0])
    await service.process(completion(AIDS[0]))
    service._scenario_effects.clock = lambda: NOW+timedelta(hours=1)
    await service.send_due_followup(23)
    before = deepcopy(attrs(api)['scenario_followups'])
    await service.process(completion(AIDS[0], 71))
    assert attrs(api)['scenario_followups'] == before
    assert not await service.send_due_followup(23)
    assert len(api.replies) == 1


async def test_four_day_wellbeing_does_not_require_an_answer_to_the_first_survey(runtime):
    api, service = runtime
    seed(api, AIDS[0])
    await service.process(completion(AIDS[0]))
    service._scenario_effects.clock = lambda: NOW+timedelta(hours=1)
    await service.send_due_followup(23)
    service._scenario_effects.clock = lambda: NOW+timedelta(days=4)
    assert await service.send_due_followup(23)
    assert attrs(api)['scenario']['screen'] == 's52'
    assert len(api.replies) == 2


async def test_kind_filter_and_explicit_id_never_complete_wrong_consultation(runtime):
    api, service = runtime
    seed(api, AIDS[0])
    attrs(api)['scenario_requests']['b'*24] = {'aid_id': AIDS[1], 'state': 'requested'}
    await service.process(completion(AIDS[1]))
    assert attrs(api)['scenario_requests'][KEY]['state'] == 'requested'
    assert attrs(api)['scenario_requests']['b'*24]['state'] == 'completed'
    assert f'review:{KEY}' not in attrs(api)['scenario_followups']


@pytest.mark.parametrize('selection', ('', 'c'*24))
async def test_ambiguous_or_invalid_selection_does_not_change_ownership_or_jobs(runtime, selection):
    api, service = runtime
    seed(api, AIDS[0])
    attrs(api)['scenario_requests']['b'*24] = {'aid_id': AIDS[0], 'state': 'requested'}
    attrs(api)['consultation_request_id'] = selection
    before = deepcopy(attrs(api)['scenario_followups'])
    await api.assign_human(23, 4)
    await service.process(completion(AIDS[0]))
    assert attrs(api)['scenario_followups'] == before
    assert api.conversation['assignee_id'] == 4
    assert api.statuses == []
    attrs(api)['consultation_request_id'] = KEY
    await service.process(completion(AIDS[0], 71))
    assert attrs(api)['scenario_requests'][KEY]['state'] == 'completed'
    assert attrs(api)['scenario_requests']['b'*24]['state'] == 'requested'


@pytest.mark.parametrize('hour', (4, 16, 19))
async def test_daytime_window_applies_to_both_click_relative_delays(runtime, hour):
    api, service = runtime
    seed(api, AIDS[0])
    click = NOW.replace(hour=hour)
    await service.process(completion(AIDS[0], occurred_at=click))
    jobs = attrs(api)['scenario_followups']
    assert jobs[f'review:{KEY}']['due_at'] == daytime(click+timedelta(hours=1)).isoformat()
    assert jobs[f'wellbeing:{KEY}']['due_at'] == daytime(click+timedelta(days=4)).isoformat()


@pytest.mark.parametrize('screen', ('s7', 's52'))
@pytest.mark.parametrize('guard', ('human', 'snoozed', 'pending_input', 'awaiting_text', 'safety'))
async def test_existing_delivery_guards_hold_both_surveys(runtime, screen, guard):
    api, service = runtime
    seed(api, AIDS[0])
    await service.process(completion(AIDS[0]))
    service._scenario_effects.clock = lambda: NOW+timedelta(days=4)
    jobs = attrs(api)['scenario_followups']
    if screen == 's52':
        jobs[f'review:{KEY}']['state'] = 'answered'
    api.conversation['status'] = 'pending'
    if guard == 'human':
        await api.assign_human(23, 4)
    elif guard == 'snoozed':
        api.conversation['status'] = 'snoozed'
    elif guard == 'pending_input':
        attrs(api)['scenario_pending_input'] = {'message_id': 80}
    elif guard == 'awaiting_text':
        attrs(api)['scenario'] = {'screen': 's74', 'awaiting_text': True}
    else:
        attrs(api)['workflow_state'] = 'safety_escalation'
    assert not await service.send_due_followup(23)
    assert api.replies == []


async def test_completion_does_not_clear_active_safety_handoff(runtime):
    api, service = runtime
    seed(api, AIDS[0])
    attrs(api)['workflow_state'] = 'safety_escalation'
    attrs(api)['scenario'] = {'screen': 'i7'}
    await api.assign_human(23, 4)
    await service.process(completion(AIDS[0]))
    assert attrs(api)['workflow_state'] == 'safety_escalation'
    assert attrs(api)['scenario'] == {'screen': 'i7'}
    assert api.conversation['assignee_id'] == 4
    service._scenario_effects.clock = lambda: NOW+timedelta(days=4)
    assert not await service.send_due_followup(23)


async def test_success_keeps_current_input_and_waits_for_explicit_return_to_bot(runtime):
    api, service = runtime
    seed(api, AIDS[0])
    attrs(api)['scenario'] = {'screen': 's74', 'awaiting_text': True}
    attrs(api)['scenario_pending_input'] = {'message_id': 40}
    await api.assign_human(23, 4)
    api.conversation['status'] = 'open'
    await service.process(completion(AIDS[0]))
    assert attrs(api)['scenario_pending_input'] == {'message_id': 40}
    assert attrs(api)['scenario'] == {'screen': 's74', 'awaiting_text': True}
    service._scenario_effects.clock = lambda: NOW+timedelta(hours=1)
    assert not await service.send_due_followup(23)
    await service.process(StaffMessage(71, 23, 4, return_to_bot=True))
    # Returning ownership does not silently discard an optional unfinished form.
    assert not await service.send_due_followup(23)
    attrs(api)['scenario'] = {}
    assert await service.send_due_followup(23)
    assert attrs(api)['scenario']['screen'] == 's7'


def test_timeweb_macro_definitions_match_authenticated_commands():
    script = (Path(__file__).parents[1]/'deploy/timeweb/consultation-success-macros.rb').read_text()
    for command in CONSULTATION_SUCCESS:
        assert command in script
    assert 'Консультация психолога прошла успешно' in script
    assert 'Консультация юриста прошла успешно' in script
    assert 'existing success macro was edited' in script
