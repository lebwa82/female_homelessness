"""Focused RED/GREEN regressions for final review fix round 2.

All messages are synthetic.  These tests deliberately exercise adapters and
in-memory repositories only; they never contact a provider, Telegram, or a DB.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self

import pytest

from app import bot, db, pii, worker
from app.agents import AgentCallResult, AgentContext, YandexAgentGateway
from app.domain import (
    AgentTurn,
    ConversationState,
    DiagnosticStatus,
    IncomingMessage,
    PolicyContext,
    PolicyEffect,
    RiskLevel,
    SupportDiagnostic,
)
from app.policy import resolve_turn
from app.safety import assess_local_risk_from_signals
from app.service import ConversationService
from app.signals import extract_signals
from app.store import InMemoryConversationStore
from app.worker import DueJob
from scripts.dialogue_eval import load_cases


def incoming(text: str, message_id: int = 901) -> IncomingMessage:
    return IncomingMessage(platform_user_id=101, chat_id=202, text=text, message_id=message_id)


@pytest.mark.asyncio
async def test_outbound_audit_failure_cannot_suppress_canonical_crisis_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``unittest.mock`` keeps this adapter check independent from aiogram.
    from unittest.mock import AsyncMock

    answer = AsyncMock()
    message = SimpleNamespace(answer=answer)

    async def failing_outbound(_: IncomingMessage, __: AgentTurn) -> None:
        raise RuntimeError("synthetic outbound audit failure")

    monkeypatch.setattr(bot, "conversation_service", SimpleNamespace(record_outbound=failing_outbound))
    turn = AgentTurn(text="Телефон доверия — 8-800-2000-122")

    await bot.send_turn(message, incoming("synthetic", 902), turn)

    answer.assert_awaited_once()
    assert "8-800-2000-122" in answer.await_args.args[0]


@pytest.mark.parametrize("text", ("не хочу жить, мне страшно", "не хочу жить, я устала"))
def test_completed_suicidal_clause_with_ordinary_distress_is_critical(text: str) -> None:
    signals = extract_signals(text)

    assert assess_local_risk_from_signals(signals).level is RiskLevel.CRITICAL
    decision = resolve_turn(
        PolicyContext(
            state=ConversationState.OPEN_CONVERSATION.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
            support_status=DiagnosticStatus.UNAVAILABLE,
        )
    )
    assert decision.effect is PolicyEffect.CRITICAL_ESCALATION


@pytest.mark.parametrize("text", ("не хочу жить в этом городе", "не хочу жить с ним"))
def test_completed_suicidal_clause_keeps_location_and_relationship_near_misses_open(text: str) -> None:
    assert assess_local_risk_from_signals(extract_signals(text)).level is RiskLevel.NONE


@pytest.mark.asyncio
async def test_successfully_prepared_critical_turn_makes_exactly_two_diagnostics_and_ignores_them() -> None:
    started: list[str] = []
    release = asyncio.Event()

    async def call(name: str, _: str, __: str) -> AgentCallResult:
        started.append(name)
        await release.wait()
        if name == "risk":
            return AgentCallResult(payload={"level": "none", "rationale": "synthetic"}, audit={"status": "completed"})
        return AgentCallResult(payload={"intent": "open_conversation", "draft_text": "synthetic"}, audit={"status": "completed"})

    service = ConversationService(
        store=InMemoryConversationStore(), gateway=YandexAgentGateway(call=call)
    )
    task = asyncio.create_task(service.handle_text(incoming("не хочу жить, мне страшно", 903)))
    for _ in range(10):
        if len(started) == 2:
            break
        await asyncio.sleep(0)
    release.set()
    turn = await task

    assert sorted(started) == ["risk", "support"]
    assert "8-800-2000-122" in turn.text


@pytest.mark.asyncio
async def test_diagnostic_preparation_failure_starts_no_provider_tasks(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def call(name: str, _: str, __: str) -> AgentCallResult:
        calls.append(name)
        return AgentCallResult(payload={}, audit={})

    gateway = YandexAgentGateway(call=call)
    monkeypatch.setattr("app.agents.format_redacted_transcript", lambda _: (_ for _ in ()).throw(RuntimeError()))

    result = await gateway.evaluate(AgentContext(history=(("user", "synthetic"),), state="open_conversation", catalog=()))

    assert calls == []
    assert result.safety_status is DiagnosticStatus.UNAVAILABLE
    assert result.support_status is DiagnosticStatus.UNAVAILABLE


@pytest.mark.parametrize(
    "draft",
    (
        "Вам уже сегодня передали заявку специалистке.",
        "Заявку специалистке уже передали вам.",
        "Специалистка вам завтра позвонит.",
        "Мы уже оформим вашу заявку.",
    ),
)
def test_draft_guard_rejects_operational_claim_grammar_beyond_literal_phrases(draft: str) -> None:
    signals = extract_signals("мне тяжело")
    decision = resolve_turn(
        PolicyContext(
            state=ConversationState.OPEN_CONVERSATION.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
            support_status=DiagnosticStatus.COMPLETED,
            support=SupportDiagnostic(intent="open_conversation", draft_text=draft),
        )
    )

    assert decision.fallback_reason == "support_draft_guard"


@pytest.mark.parametrize(
    "draft",
    (
        "Если захотите, заявку может оформить специалистка.",
        "Специалистка может, если вы захотите, оформить заявку.",
    ),
)
def test_draft_guard_allows_conditional_operational_possibility(draft: str) -> None:
    signals = extract_signals("мне тяжело")
    decision = resolve_turn(
        PolicyContext(
            state=ConversationState.OPEN_CONVERSATION.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
            support_status=DiagnosticStatus.COMPLETED,
            support=SupportDiagnostic(intent="open_conversation", draft_text=draft),
        )
    )
    assert decision.text == draft


@pytest.mark.asyncio
async def test_processing_followup_is_revalidated_before_delivery_after_delete_race() -> None:
    @dataclass
    class Repository:
        checked: list[int] = field(default_factory=list)
        completed: list[tuple[int, bool]] = field(default_factory=list)

        async def claim_due_jobs(self, _: object) -> list[DueJob]:
            return [DueJob(id=1, conversation_id=2, chat_id=3, kind="followup")]

        async def can_deliver(self, job: DueJob) -> bool:
            self.checked.append(job.id)
            return False

        async def complete_job(self, job: DueJob, success: bool) -> None:
            self.completed.append((job.id, success))

        async def discard_job(self, job: DueJob) -> None:
            self.completed.append((job.id, False))

    repository = Repository()
    fake_bot = SimpleNamespace(send_message=pytest.fail)

    assert await worker.run_due_jobs(fake_bot, repository) == 0
    assert repository.checked == [1]
    assert repository.completed == [(1, False)]


@pytest.mark.asyncio
async def test_worker_iteration_recovers_when_due_job_claiming_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def failed_jobs(_: object, __: object) -> int:
        calls.append("jobs")
        raise RuntimeError("synthetic")

    async def stopped(_: float) -> None:
        calls.append("sleep")
        raise asyncio.CancelledError

    monkeypatch.setattr(worker, "run_due_jobs", failed_jobs)
    monkeypatch.setattr(worker, "purge_expired_content_safely", lambda: _mark(calls, "purge"))
    monkeypatch.setattr(worker.asyncio, "sleep", stopped)

    with pytest.raises(asyncio.CancelledError):
        await worker.worker_loop(SimpleNamespace(), SimpleNamespace())
    assert calls == ["jobs", "purge", "sleep"]


async def _mark(calls: list[str], value: str) -> bool:
    calls.append(value)
    return True


def test_pii_custom_and_presidio_spans_are_single_pass_and_count_urls(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        entity_type = "PERSON"
        start = 0
        end = 9

    monkeypatch.setattr(pii, "analyzer", lambda: SimpleNamespace(analyze=lambda **_: [Result()]))
    value = "synthetic https://example.org @sample_contact"

    result = pii.redact_with_audit(value)

    assert "[[" not in result.text
    assert "example.org" not in result.text
    assert result.audit["detected"] is True
    assert result.audit["entities_total"] == sum(result.audit["entity_counts"].values())


@pytest.mark.asyncio
async def test_retry_after_post_effect_failure_uses_one_stable_text_request_key() -> None:
    class FailingCompletionStore(InMemoryConversationStore):
        failed = False

        async def complete_text(self, record: object, message_id: int | None, lease_token: str) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("synthetic post-effect failure")
            await super().complete_text(record, message_id, lease_token)

    store = FailingCompletionStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(incoming("", 910))
    await store.update(
        record,
        state=ConversationState.COLLECTING_CONTACT_VALUE.value,
        pending_aid_id="legal_consultation",
        pending_contact_method="later",
    )
    update = incoming("later", 911)

    await service.handle_text(update)
    await service.handle_text(update)

    assert len(store.aid_requests) == 1
    assert store.aid_requests[0].request_key is not None


@pytest.mark.asyncio
async def test_retry_after_post_effect_failure_uses_one_stable_text_escalation_key() -> None:
    class FailingCompletionStore(InMemoryConversationStore):
        failed = False

        async def complete_text(self, record: object, message_id: int | None, lease_token: str) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("synthetic post-effect failure")
            await super().complete_text(record, message_id, lease_token)

    store = FailingCompletionStore()
    service = ConversationService(store=store, gateway=_gateway())
    update = incoming("не хочу жить, мне страшно", 912)

    await service.handle_text(update)
    await service.handle_text(update)

    assert len(store.escalations) == 1
    assert store.escalations[0].request.request_key is not None
    assert [kind for _, kind, _, _ in store.actions].count("critical_escalation") == 1
    assert [kind for _, kind, _, _ in store.actions].count("policy_decision") == 1


@pytest.mark.asyncio
async def test_retry_replays_saved_turn_without_rerunning_diagnostics_after_post_effect_failure() -> None:
    class FailingCompletionStore(InMemoryConversationStore):
        failed = False

        async def complete_text(self, record: object, message_id: int | None, lease_token: str) -> None:
            if not self.failed:
                self.failed = True
                raise RuntimeError("synthetic post-effect failure")
            await super().complete_text(record, message_id, lease_token)

    calls: list[str] = []

    async def call(name: str, _: str, __: str) -> AgentCallResult:
        calls.append(name)
        if name == "risk":
            return AgentCallResult(payload={"level": "none", "rationale": "synthetic"}, audit={"status": "completed"})
        return AgentCallResult(payload={"intent": "open_conversation", "draft_text": "synthetic"}, audit={"status": "completed"})

    service = ConversationService(store=FailingCompletionStore(), gateway=YandexAgentGateway(call=call))
    update = incoming("мне тяжело", 913)

    first = await service.handle_text(update)
    await service.record_outbound(update, first)
    replay = await service.handle_text(update)

    assert sorted(calls) == ["risk", "support"]
    assert first.text == "synthetic"
    assert replay.text == "synthetic"
    assert replay.audit["suppress_delivery"] is True


@pytest.mark.asyncio
async def test_stale_bound_outbound_cannot_attach_to_a_new_identity_after_delete() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=_gateway())
    update = incoming("мне тяжело", 915)
    old_turn = await service.handle_text(update)

    await service.delete(incoming("/delete", 916))
    new_record = await store.ensure(update)
    await service.record_outbound(update, old_turn)

    assert new_record.id != old_turn.audit["conversation_id"]
    assert store.messages == []


def _gateway() -> YandexAgentGateway:
    async def call(name: str, _: str, __: str) -> AgentCallResult:
        if name == "risk":
            return AgentCallResult(payload={"level": "none", "rationale": "synthetic"}, audit={"status": "completed"})
        return AgentCallResult(payload={"intent": "open_conversation", "draft_text": "synthetic"}, audit={"status": "completed"})

    return YandexAgentGateway(call=call)


@pytest.mark.asyncio
async def test_human_exit_cancels_already_claimed_reminder() -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(incoming("", 913))
    store.followup_jobs.append(
        SimpleNamespace(conversation_id=record.id, aid_request_id=1, status="processing")
    )

    await service.handle_callback(incoming("human", 914), "human")

    assert store.followup_jobs == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "text"),
    (
        (ConversationState.CHOOSING_AID, "не нужна заявка"),
        (ConversationState.COLLECTING_LOCATION, "не хочу указывать город"),
        (ConversationState.COLLECTING_CONTACT_METHOD, "не хочу оставлять контакт"),
        (ConversationState.COLLECTING_CONTACT_VALUE, "не буду оставлять контакт"),
    ),
)
async def test_state_specific_refusal_exits_before_any_workflow_capture(
    state: ConversationState, text: str
) -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=_gateway())
    record = await store.ensure(incoming("", 917))
    await store.update(
        record,
        state=state.value,
        need="legal",
        pending_aid_id="legal_consultation",
        pending_contact_method="phone",
        pending_city="synthetic-city",
    )

    turn = await service.handle_text(incoming(text, 918 + list(ConversationState).index(state)))

    assert turn.audit.get("suppress_delivery") is not True
    assert record.state == ConversationState.OPEN_CONVERSATION.value
    assert record.pending_aid_id is None and record.pending_contact_method is None


@pytest.mark.asyncio
async def test_legacy_null_expiry_is_backfilled_and_never_model_readable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Result:
        def scalars(self) -> tuple[object, ...]:
            return ()

    class Session:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def execute(self, statement: object) -> Result:
            statements.append(str(statement))
            return Result()

    monkeypatch.setattr(db, "Session", Session)
    assert await db.load_model_history(1) == []
    assert await db.load_active_contact_points(1) == []

    rendered = "\n".join(statements)
    assert "expires_at IS NOT NULL" in rendered


@pytest.mark.asyncio
async def test_migration_backfills_legacy_message_and_contact_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Connection:
        async def run_sync(self, _: object) -> None:
            return None

        async def execute(self, statement: object) -> None:
            statements.append(str(statement))

    class Begin:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(db, "engine", SimpleNamespace(begin=lambda: Begin()))
    await db.init_db()

    migration = "\n".join(statements)
    assert "UPDATE conversation_messages SET expires_at" in migration
    assert "UPDATE contact_points SET expires_at" in migration


def test_deploy_script_never_evaluates_environment_file_and_has_activation_err_trap() -> None:
    source = Path("scripts/deploy_prod.sh").read_text(encoding="utf-8")

    assert ". \"$1\"" not in source
    assert "source \"$1\"" not in source
    assert "DATABASE_URL" in source
    assert "trap" in source and "ERR" in source


def test_deploy_script_runs_staged_gate_without_evaluating_unrelated_environment_lines(tmp_path: Path) -> None:
    """Exercise the production shell script only through local command stubs."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "must-not-exist"
    env_file = tmp_path / "service.env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://synthetic:synthetic@localhost/synthetic\n"
        f"UNRELATED=$(touch {marker})\n",
        encoding="utf-8",
    )
    target = tmp_path / "live"
    target.mkdir()
    (target / "legacy").write_text("synthetic", encoding="utf-8")
    log = tmp_path / "commands.log"

    def stub(name: str, body: str) -> None:
        path = fake_bin / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
        path.chmod(0o755)

    stub(
        "git",
        'case "$1" in status) exit 0;; rev-parse) echo deadbeef;; archive) tar -cf - --files-from /dev/null;; esac\n',
    )
    stub("ssh", 'remote="${!#}"\neval "$remote"\n')
    stub("sudo", 'exec "$@"\n')
    stub("uv", f'printf "uv\\n" >> "{log}"\n')
    stub("just", f'printf "just:%s\\n" "$1" >> "{log}"\n')
    stub("podman", 'printf "healthy\\n"\n')
    stub("systemctl", f'printf "systemctl:%s\\n" "$1" >> "{log}"\n')
    stub("mv", 'if [[ "${1:-}" == "-Tf" ]]; then shift; /bin/rm -f "$2"; fi\nexec /bin/mv "$@"\n')

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "WOMEN_HELP_STAGED_PATH": f"{fake_bin}:/usr/bin:/bin",
        "WOMEN_HELP_ENV_FILE": str(env_file),
    }
    result = subprocess.run(
        ["bash", "scripts/deploy_prod.sh", "stub-host", str(target)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert (target).is_symlink()
    assert {"just:check", "just:scenario-smoke", "just:eval-dialogues", "just:db-assure"} <= set(
        log.read_text(encoding="utf-8").splitlines()
    )


def test_deploy_script_err_trap_restores_moved_legacy_target_on_restart_failure(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    env_file = tmp_path / "service.env"
    env_file.write_text("DATABASE_URL=postgresql+asyncpg://synthetic:synthetic@localhost/synthetic\n", encoding="utf-8")
    target = tmp_path / "live"
    target.mkdir()
    (target / "legacy").write_text("synthetic", encoding="utf-8")

    def stub(name: str, body: str) -> None:
        path = fake_bin / name
        path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
        path.chmod(0o755)

    stub("git", 'case "$1" in status) exit 0;; rev-parse) echo deadbeef;; archive) tar -cf - --files-from /dev/null;; esac\n')
    stub("ssh", 'remote="${!#}"\neval "$remote"\n')
    stub("sudo", 'exec "$@"\n')
    stub("uv", 'exit 0\n')
    stub("just", 'exit 0\n')
    stub("podman", 'printf "healthy\\n"\n')
    stub("systemctl", '[[ "$1" == "restart" ]] && exit 1\nexit 0\n')
    stub("mv", 'if [[ "${1:-}" == "-Tf" ]]; then shift; /bin/rm -f "$2"; fi\nexec /bin/mv "$@"\n')
    result = subprocess.run(
        ["bash", "scripts/deploy_prod.sh", "stub-host", str(target)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "WOMEN_HELP_STAGED_PATH": f"{fake_bin}:/usr/bin:/bin",
            "WOMEN_HELP_ENV_FILE": str(env_file),
        },
    )

    assert result.returncode != 0
    assert target.is_dir() and not target.is_symlink()
    assert (target / "legacy").exists()


def test_evaluator_requires_explicit_soft_lifecycle_expectations(tmp_path: Any) -> None:
    row = {
        "version": 2,
        "id": "soft-lifecycle",
        "group": "test",
        "history": [["user", "мне тяжело"]],
        "initial": {"state": "open_conversation", "pending_offer": None},
        "expected": {
            "behavior": {
                "local_risk": "none", "choice_set": "none", "rendered_callback_ids": ["human"],
                "effect": "none", "side_effects": [], "state_after": "open_conversation", "escalation": False,
                "escalation_cause": None, "escalation_count": 0, "request_count": 0, "copy_contains": None,
                "rule_ids": [],
            },
            "diagnostics": {"safety_levels": ["none"], "support_intents": ["open_conversation"]},
            "soft": {"pending_offer_lifecycle": [None, "psychologist"]},
        },
    }
    path = tmp_path / "case.jsonl"
    import json

    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    cases = load_cases(path)

    assert cases[0].soft["pending_offer_lifecycle"] == (None, "psychologist")
