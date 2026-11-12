"""Neutral consultation feedback fixtures; no external delivery or real dialogues."""
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_chatwoot_service import FakeChatwoot, StubGateway, event, ordinary_evaluation

from app.chatwoot.service import ChatwootAgentService
from app.config import settings
from app.domain import (
    DiagnosticStatus,
    RiskLevel,
    SafetyDiagnostic,
    SafetyEscalation,
    SupportIntent,
)
from app.scenario import CERTIFICATES, CONSULTATION_IDS, SCREENS, ScenarioFlow, variant_for

NOW = datetime(2026, 11, 12, 10, tzinfo=UTC)
REQUEST = 'a' * 24
REVIEW = f'review:{REQUEST}'
WELLBEING = f'wellbeing:{REQUEST}'
TEXT = 'Synthetic open consultation feedback.'
TARGETS = {'s7': 's71', 's71': 's72', 's72': 's73', 's721': 's73',
           's73': 's74', 's52': 's54', 's74': 's75'}


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(settings, 'llm_enabled', True)
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway, duty_team_id=9)
    service._scenario_effects.clock = lambda: NOW
    return api, gateway, service


def seed(api, aid, screen='s71'):
    key = WELLBEING if screen == 's52' else REVIEW
    context = {'aid_id': aid, 'request_key': REQUEST, 'job_key': key}
    job_context = {'aid_id': aid, 'request_key': REQUEST}
    attrs = api.conversation['custom_attributes']
    attrs.update({
        'workflow_state': 'scenario',
        'scenario': {**context, 'screen': screen, 'variant': variant_for(screen, context),
                     'awaiting_text': False},
        'scenario_requests': {REQUEST: {'aid_id': aid, 'state': 'completed'}},
        'scenario_followups': {
            REVIEW: {'screen': 's7', 'state': 'answered' if screen == 's52' else 'sent',
                     'due_at': NOW.isoformat(), 'context': job_context},
            WELLBEING: {'screen': 's52', 'state': 'sent' if screen == 's52' else 'pending',
                        'due_at': (NOW+timedelta(days=4)).isoformat(), 'context': job_context},
            'unrelated': {'screen': 's5', 'state': 'pending',
                          'due_at': (NOW+timedelta(days=2)).isoformat(),
                          'context': {'aid_id': 'food_card'}},
        },
    })
    return attrs


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
async def test_written_yes_continues_to_second_question(runtime, aid):
    api, gateway, service = runtime
    seed(api, aid)

    assert await service.process(event('Да'))

    attrs = api.conversation['custom_attributes']
    assert attrs['scenario']['screen'] == 's72'
    assert api.replies[-1]['text'] == SCREENS['s72']['texts'][0]
    assert gateway.calls == 1
    assert any('Ответ: Да' in n and 'Текст сообщения: Да' in n for n in api.notes)


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
@pytest.mark.parametrize('screen,target', TARGETS.items())
async def test_open_text_is_saved_without_guessing_a_button_and_continues(runtime, aid, screen, target):
    api, gateway, service = runtime
    attrs = seed(api, aid, screen)
    before = deepcopy(attrs['scenario_followups'])
    key = WELLBEING if screen == 's52' else REVIEW

    assert await service.process(event(TEXT))

    attrs = api.conversation['custom_attributes']
    assert attrs['scenario']['screen'] == target
    assert attrs['scenario']['aid_id'] == aid
    assert attrs['scenario']['request_key'] == REQUEST
    assert attrs['scenario']['job_key'] == key
    assert attrs['scenario_followups'][key]['state'] == 'answered'
    assert attrs['scenario_followups']['unrelated'] == before['unrelated']
    if key == REVIEW:
        assert attrs['scenario_followups'][WELLBEING] == before[WELLBEING]
    notes = [n for n in api.notes if TEXT in n]
    assert len(notes) == 1 and 'Ответ:' not in notes[0]
    assert api.replies[-1]['text'] == SCREENS[target]['texts'][0]
    assert not await service.process(event(TEXT))
    assert len(api.replies) == 1 and gateway.calls == 1


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
@pytest.mark.parametrize('screen,text,target', [
    ('s7', 'Да', 's71'), ('s7', 'нет', 'i2'), ('s7', 'Не сейчас', 'i2'),
    ('s71', '  ДА!  ', 's72'), ('s71', 'нет.', 's72'), ('s71', 'не знаю', 's72'),
    ('s72', 'Нет!', 's721'), ('s72', '\n Нет!\n', 's721'),
    ('s73', 'Не знаю', 's74'), ('s721', 'Продолжить', 's73'),
    ('s74', 'Пропустить', 's75'), ('s52', 'Стало легче', 's53'),
    ('s52', 'Пока так же', 's54'), ('s52', 'Стало тяжелее', 's55'),
    ('s52', 'Не хочу отвечать', 'i2'),
])
async def test_exact_text_labels_follow_existing_button_branches(runtime, aid, screen, text, target):
    api, gateway, service = runtime
    seed(api, aid, screen)

    assert await service.process(event(text))

    attrs = api.conversation['custom_attributes']
    assert attrs['scenario']['screen'] == target
    assert gateway.calls == 1
    assert sum('Текст сообщения: '+text in n for n in api.notes) == 1
    if screen == 's72':
        assert any(k.startswith('scenario-alert:') for k in api.note_keys)


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
async def test_all_three_questions_and_optional_feedback_accept_written_answers(runtime, aid):
    api, gateway, service = runtime
    attrs = seed(api, aid)
    original_wellbeing = deepcopy(attrs['scenario_followups'][WELLBEING])
    for message_id, (answer, target) in enumerate([
        ('Да', 's72'), ('Да', 's73'), (TEXT, 's74'), ('Synthetic final feedback.', 's75'),
    ], 41):
        assert await service.process(event(answer, message_id))
        assert api.conversation['custom_attributes']['scenario']['screen'] == target
    assert len(api.replies) == 4 and gateway.calls == 4
    assert api.conversation['custom_attributes']['scenario_followups'][WELLBEING] == original_wellbeing


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
@pytest.mark.parametrize('screen', tuple(TARGETS))
@pytest.mark.parametrize('guard', ('safety', 'human', 'safety_unavailable', 'support_unavailable'))
@pytest.mark.parametrize('text', (TEXT, 'Нет'))
async def test_policy_guards_preempt_text_answer_and_advancement(runtime, aid, screen, guard, text):
    api, gateway, service = runtime
    seed(api, aid, screen)
    evaluation = ordinary_evaluation()
    if guard == 'safety':
        evaluation = replace(evaluation, safety=SafetyDiagnostic(
            level=RiskLevel.CRITICAL, escalation=SafetyEscalation.HANDOFF))
    elif guard == 'human':
        evaluation = replace(evaluation, support=evaluation.support.model_copy(update={
            'intent': SupportIntent.EXPLICIT_HUMAN_REQUEST}))
    else:
        agent = guard.removesuffix('_unavailable')
        evaluation = replace(evaluation, **{agent: None, f'{agent}_status': DiagnosticStatus.UNAVAILABLE})
    gateway.result = evaluation

    assert await service.process(event(text))

    assert not any('Текст сообщения: '+text in n for n in api.notes)
    if guard in {'safety', 'human'}:
        assert api.conversation['custom_attributes']['handoff_requested'] is True
        assert api.teams == [9]


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
@pytest.mark.parametrize('screen', tuple(TARGETS))
@pytest.mark.parametrize('text', (TEXT, 'Да'))
async def test_staff_owned_chat_is_silent_without_survey_changes(runtime, aid, screen, text):
    api, gateway, service = runtime
    attrs = seed(api, aid, screen)
    before = deepcopy(attrs)
    await api.assign_human(23, 4)
    api.conversation['status'] = 'open'

    assert not await service.process(event(text))

    assert gateway.calls == 0 and not api.replies
    assert api.conversation['custom_attributes']['scenario_followups'] == before['scenario_followups']
    assert not any('Текст сообщения: '+text in n for n in api.notes)


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
async def test_staff_takeover_during_model_check_blocks_late_answer(runtime, aid):
    api, _, service = runtime
    seed(api, aid)

    class TakeoverGateway:
        async def evaluate(self, context):
            await api.assign_human(23, 4)
            return ordinary_evaluation()

    service._gateway = TakeoverGateway()
    assert not await service.process(event('Да'))
    assert api.conversation['custom_attributes']['scenario']['screen'] == 's71'
    assert not api.replies
    assert not any('Текст сообщения: Да' in n for n in api.notes)


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
async def test_human_and_home_buttons_remain_available_after_written_answer(runtime, aid):
    api, gateway, service = runtime
    seed(api, aid)
    assert await service.process(event(TEXT))
    assert {'human'} <= {c.id for c in api.replies[-1]['choices']}
    assert any(c.id.startswith('home:') for c in api.replies[-1]['choices'])
    assert await service.process(event('human', 42))
    assert gateway.calls == 1
    assert api.conversation['custom_attributes']['handoff_requested'] is True
    assert api.teams == [9]


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
async def test_written_human_request_uses_existing_handoff(runtime, aid):
    api, gateway, service = runtime
    seed(api, aid)
    assert await service.process(event('Поговорить с человеком'))
    assert gateway.calls == 1
    assert api.conversation['custom_attributes']['handoff_requested'] is True
    assert api.teams == [9]
    assert api.conversation['custom_attributes']['scenario']['screen'] != 's72'


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
async def test_written_home_navigation_is_not_recorded_as_survey_answer(runtime, aid):
    api, _, service = runtime
    attrs = seed(api, aid)
    original = deepcopy(attrs['scenario_followups'][WELLBEING])
    assert await service.process(event('Вернуться в начало'))
    assert api.conversation['custom_attributes']['scenario']['screen'] == 's2'
    assert api.conversation['custom_attributes']['scenario_followups'][WELLBEING] == original
    assert not any('Текст сообщения: Вернуться в начало' in n for n in api.notes)


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
async def test_retry_after_failed_note_does_not_skip_question_or_duplicate_answer(runtime, aid):
    api, _, service = runtime
    seed(api, aid)
    add_note = api.add_private_note
    failed = False

    async def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError('synthetic note failure')
        return await add_note(*args, **kwargs)

    api.add_private_note = fail_once
    with pytest.raises(RuntimeError, match='synthetic note failure'):
        await service.process(event('Да'))
    assert await service.process(event('Да'))
    assert api.conversation['custom_attributes']['scenario']['screen'] == 's72'
    assert len(api.replies) == 1
    assert sum('Текст сообщения: Да' in n for n in api.notes) == 1


@pytest.mark.parametrize('aid', CONSULTATION_IDS)
@pytest.mark.parametrize('screen', ('s34', 's35', 's38', 's2', 's75'))
async def test_regular_consultation_navigation_does_not_capture_text_as_survey(aid, screen):
    flow = ScenarioFlow(SimpleNamespace(store=None))
    record = SimpleNamespace(scenario={'aid_id': aid, 'screen': screen})
    assert await flow.text(record, TEXT, 'synthetic') is None


@pytest.mark.parametrize('aid', CERTIFICATES)
async def test_new_question_handler_does_not_capture_certificate_context(aid):
    flow = ScenarioFlow(SimpleNamespace(store=None))
    record = SimpleNamespace(scenario={'aid_id': aid, 'screen': 's71'})
    assert await flow.text(record, TEXT, 'synthetic') is None
