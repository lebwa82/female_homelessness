"""Synthetic free-text replies continue certificate surveys after policy guards."""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

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
from app.scenario import CERTIFICATES, CONSULTATION_IDS, SCREENS, variant_for

NOW = datetime(2026, 10, 28, 10, tzinfo=UTC)
JOB = "certificate:synthetic-survey"
TEXT = "Synthetic certificate feedback."
SCREENS_WITH_TEXT = ("s5", "s51", "s59", "s511")


@pytest.fixture
def runtime(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", True)
    api = FakeChatwoot()
    gateway = StubGateway(ordinary_evaluation())
    service = ChatwootAgentService(api, gateway=gateway, duty_team_id=9)
    service._scenario_effects.clock = lambda: NOW
    return api, gateway, service


def seed(api, screen="s5", aid_id="medicine_card", historical=True):
    context = {"aid_id": aid_id, "historical_survey": historical, "job_key": JOB}
    job = {"screen": "s5", "state": "sent", "due_at": NOW.isoformat(),
           "context": {"aid_id": aid_id, "historical_survey": historical}}
    attrs = api.conversation["custom_attributes"]
    attrs.update({
        "workflow_state": "scenario",
        "scenario": {**context, "screen": screen, "variant": variant_for(screen, context),
                     "awaiting_text": False},
        "scenario_followups": {
            JOB: job,
            f"reminder:{JOB}": {**job, "screen": "s59", "state": "pending",
                                "due_at": (NOW + timedelta(days=2)).isoformat()},
            "unrelated": {"screen": "s7", "state": "pending", "context": {}},
        },
    })
    return attrs


@pytest.mark.parametrize("screen", SCREENS_WITH_TEXT)
@pytest.mark.parametrize("aid_id", CERTIFICATES)
@pytest.mark.parametrize("historical", (True, False))
async def test_free_text_continues_to_wellbeing_once(runtime, screen, aid_id, historical):
    api, gateway, service = runtime
    attrs = seed(api, screen, aid_id, historical)
    unrelated = deepcopy(attrs["scenario_followups"]["unrelated"])

    assert await service.process(event(TEXT))

    attrs = api.conversation["custom_attributes"]
    assert api.replies[-1]["text"] == SCREENS["s52"]["texts"][0]
    assert {"Стало легче", "Пока так же", "Стало тяжелее"} <= {
        choice.label for choice in api.replies[-1]["choices"]
    }
    assert attrs["scenario"]["screen"] == "s52"
    assert attrs["scenario"]["job_key"] == JOB
    assert attrs["scenario"]["aid_id"] == aid_id
    assert attrs["scenario"]["historical_survey"] is historical
    assert attrs["scenario_followups"][JOB]["state"] == "answered"
    assert attrs["scenario_followups"][f"reminder:{JOB}"]["state"] in {"answered", "cancelled"}
    assert attrs["scenario_followups"]["unrelated"] == unrelated
    assert sum(TEXT in note for note in api.notes) == 1
    assert not await service.process(event(TEXT))
    assert len(api.replies) == 1
    assert gateway.calls == 1
    assert sum(TEXT in note for note in api.notes) == 1


async def test_wellbeing_button_still_completes_the_next_step(runtime):
    api, _, service = runtime
    seed(api)
    await service.process(event(TEXT))
    answer = next(c for c in api.replies[-1]["choices"] if c.label == "Стало легче")

    assert await service.process(event(answer.id, 42))

    assert api.conversation["custom_attributes"]["scenario"]["screen"] == "s53"
    assert len(api.replies) == 2


@pytest.mark.parametrize("screen", ("s5", "s51", "s59"))
@pytest.mark.parametrize("guard", ("safety", "human", "safety_unavailable", "support_unavailable"))
async def test_policy_guards_preempt_survey_continuation(runtime, screen, guard):
    api, gateway, service = runtime
    seed(api, screen)
    evaluation = ordinary_evaluation()
    if guard == "safety":
        evaluation = replace(evaluation, safety=SafetyDiagnostic(
            level=RiskLevel.CRITICAL, escalation=SafetyEscalation.HANDOFF,
        ))
    elif guard == "human":
        evaluation = replace(evaluation, support=evaluation.support.model_copy(update={
            "intent": SupportIntent.EXPLICIT_HUMAN_REQUEST,
        }))
    else:
        agent = guard.removesuffix("_unavailable")
        evaluation = replace(evaluation, **{agent: None, f"{agent}_status": DiagnosticStatus.UNAVAILABLE})
    gateway.result = evaluation

    assert await service.process(event(TEXT))

    attrs = api.conversation["custom_attributes"]
    assert api.replies[-1]["text"] != SCREENS["s52"]["texts"][0]
    assert attrs.get("scenario", {}).get("screen") != "s52"
    assert not any(TEXT in note for note in api.notes)
    if guard in {"safety", "human"}:
        assert attrs["handoff_requested"] is True
        assert api.teams == [9]


@pytest.mark.parametrize("screen", ("s5", "s51", "s59"))
async def test_assigned_staff_stays_silent_without_model_or_job_changes(runtime, screen):
    api, gateway, service = runtime
    seed(api, screen)
    before = deepcopy(api.conversation["custom_attributes"]["scenario_followups"])
    api.conversation["status"] = "open"
    await api.assign_human(23, 4)

    assert not await service.process(event(TEXT))

    assert api.replies == []
    assert gateway.calls == 0
    assert api.conversation["assignee_id"] == 4
    assert api.conversation["custom_attributes"]["scenario_followups"] == before


async def test_staff_takeover_during_evaluation_blocks_late_survey_question(runtime):
    api, _, service = runtime
    seed(api)

    class TakeoverGateway:
        async def evaluate(self, context):
            await api.assign_human(23, 4)
            return ordinary_evaluation()

    service._gateway = TakeoverGateway()

    assert not await service.process(event(TEXT))

    assert api.replies == []
    assert api.conversation["assignee_id"] == 4
    assert api.conversation["custom_attributes"]["scenario"]["screen"] == "s5"
    assert not any(TEXT in note for note in api.notes)


async def test_explicit_human_button_does_not_continue_survey(runtime):
    api, gateway, service = runtime
    seed(api)

    assert await service.process(event("human"))

    assert gateway.calls == 0
    assert api.conversation["custom_attributes"]["handoff_requested"] is True
    assert api.replies[-1]["text"] != SCREENS["s52"]["texts"][0]
    assert api.teams == [9]


@pytest.mark.parametrize("screen", ("s5", "s59"))
@pytest.mark.parametrize("aid_id", CONSULTATION_IDS)
async def test_consultation_text_does_not_enter_certificate_wellbeing(runtime, screen, aid_id):
    api, _, service = runtime
    seed(api, screen, aid_id)

    await service.process(event(TEXT))

    assert api.replies[-1]["text"] != SCREENS["s52"]["texts"][0]
    assert not any(TEXT in note for note in api.notes)
