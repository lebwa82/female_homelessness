"""Breaker-round-5 regressions.

Every adapter in this module is synthetic.  The tests never contact Telegram,
an LLM provider, Podman, a deployment target, or a real database.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app import bot, domain, worker
from app.agents import AgentCallResult, YandexAgentGateway
from app.domain import (
    AgentTurn,
    ConversationState,
    DeliveryAuthorization,
    DiagnosticStatus,
    HardSignalKind,
    IncomingMessage,
    PolicyContext,
    PolicyEffect,
    ResolvedTurn,
    SupportDiagnostic,
)
from app.policy import resolve_turn
from app.safety import assess_local_risk_from_signals
from app.service import ConversationService
from app.signals import extract_signals
from app.store import InMemoryConversationStore


def incoming(text: str, message_id: int = 1201) -> IncomingMessage:
    return IncomingMessage(
        platform_user_id=3201,
        chat_id=4201,
        username="synthetic",
        text=text,
        message_id=message_id,
    )


def gateway(calls: list[str] | None = None) -> YandexAgentGateway:
    async def call(name: str, _: str, __: str) -> AgentCallResult:
        if calls is not None:
            calls.append(name)
        if name == "risk":
            return AgentCallResult(
                payload={"level": "none", "rationale": "synthetic"},
                audit={"status": "completed"},
            )
        return AgentCallResult(
            payload={"intent": "open_conversation", "draft_text": "synthetic"},
            audit={"status": "completed"},
        )

    return YandexAgentGateway(call=call)


class FailingCriticalOutcomeStore(InMemoryConversationStore):
    async def save_text_outcome(self, *args: object, **kwargs: object) -> None:
        await super().save_text_outcome(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("synthetic critical outcome failure")


class FailingAcknowledgementStore(InMemoryConversationStore):
    fail_ack_once = True

    async def acknowledge_text_outcome(self, *args: object, **kwargs: object) -> None:
        if self.fail_ack_once:
            self.fail_ack_once = False
            raise RuntimeError("synthetic post-send acknowledgement failure")
        await super().acknowledge_text_outcome(*args, **kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unpersisted_critical_keeps_identity_evidence_and_confirmed_delete_denies_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the authorization branch would visibly send after deletion."""
    store = FailingCriticalOutcomeStore()
    service = ConversationService(store=store, gateway=gateway())
    update = incoming("не хочу жить")
    original = await store.ensure(update)

    turn = await service.handle_text(update)

    assert turn.audit["skip_outbound_persistence"] is True
    assert turn.audit["critical_delivery"] is True
    assert turn.audit["conversation_id"] == original.id
    assert turn.audit["conversation_generation"] == original.generation

    await service.delete(incoming("/delete", 1202))
    monkeypatch.setattr(bot, "conversation_service", service)
    message = SimpleNamespace(answer=AsyncMock())

    await bot.send_turn(message, update, turn)

    message.answer.assert_not_awaited()


@pytest.mark.asyncio
async def test_unpersisted_critical_enters_tristate_guard_and_fails_open_only_on_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping the guard would pass the send assertion but miss the entry assertion."""
    entered = False

    @asynccontextmanager
    async def unavailable(_: IncomingMessage, __: AgentTurn):
        nonlocal entered
        entered = True
        raise RuntimeError("synthetic authorization outage")
        yield DeliveryAuthorization.UNAVAILABLE  # pragma: no cover

    adapter = SimpleNamespace(
        delivery_authorization=unavailable,
        record_outbound=AsyncMock(),
    )
    monkeypatch.setattr(bot, "conversation_service", adapter)
    message = SimpleNamespace(answer=AsyncMock())
    turn = AgentTurn(
        text="canonical critical",
        audit={
            "skip_outbound_persistence": True,
            "critical_delivery": True,
            "conversation_id": 1,
            "conversation_generation": 0,
        },
    )

    await bot.send_turn(message, incoming("не хочу жить"), turn)

    assert entered is True
    message.answer.assert_awaited_once()
    adapter.record_outbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_pending_critical_outcome_fails_open_when_worker_authorization_is_unavailable() -> None:
    """Worker replay must match the direct adapter's canonical crisis availability rule."""
    update = incoming("", 1209)
    turn = AgentTurn(
        text="canonical critical",
        audit={
            "critical_delivery": True,
            "conversation_id": 1,
            "conversation_generation": 0,
        },
    )

    @asynccontextmanager
    async def unavailable(_: IncomingMessage, __: AgentTurn):
        yield DeliveryAuthorization.UNAVAILABLE

    service = SimpleNamespace(
        store=SimpleNamespace(
            pending_text_outcomes=AsyncMock(
                return_value=(SimpleNamespace(incoming=update, turn=turn),)
            )
        ),
        delivery_authorization=unavailable,
        record_outbound=AsyncMock(),
        record_delivery_ambiguity=AsyncMock(),
    )
    telegram = SimpleNamespace(send_message=AsyncMock())

    assert await worker.run_pending_outcomes(telegram, service) == 1
    telegram.send_message.assert_awaited_once()
    service.record_outbound.assert_awaited_once_with(update, turn)


@pytest.mark.asyncio
async def test_missing_unpersisted_identity_without_tombstone_is_authorization_unavailable() -> (
    None
):
    """Treating every absent row as deletion would suppress a first crisis turn."""
    service = ConversationService(store=InMemoryConversationStore(), gateway=gateway())
    turn = AgentTurn(
        text="canonical critical",
        audit={
            "skip_outbound_persistence": True,
            "critical_delivery": True,
            "conversation_id": 999,
            "conversation_generation": 0,
        },
    )

    async with service.delivery_authorization(incoming("не хочу жить"), turn) as authorization:
        assert authorization is DeliveryAuthorization.UNAVAILABLE


@pytest.mark.asyncio
async def test_successful_send_then_ack_failure_is_observable_reclaimable_at_least_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing ambiguity persistence or reclaim makes this regression fail."""
    calls: list[str] = []
    store = FailingAcknowledgementStore()
    service = ConversationService(store=store, gateway=gateway(calls))
    update = incoming("мне тяжело", 1210)
    turn = await service.handle_text(update)
    business_snapshot = (
        len(store.aid_requests),
        len(store.escalations),
        len(store.actions),
        len(store.messages),
    )
    monkeypatch.setattr(bot, "conversation_service", service)
    first_message = SimpleNamespace(answer=AsyncMock())

    await bot.send_turn(first_message, update, turn)

    first_message.answer.assert_awaited_once()
    record = await store.get(update)
    assert record is not None
    stored = store.text_outcomes[(record.id, "1210")]
    assert stored.delivered is False
    assert stored.delivery_status == "delivery_ambiguous"
    assert stored.delivery_ambiguity_count == 1
    assert await store.pending_text_outcomes() != ()

    retry_bot = SimpleNamespace(send_message=AsyncMock())
    assert await worker.run_pending_outcomes(retry_bot, service) == 1
    assert await worker.run_pending_outcomes(retry_bot, service) == 0

    retry_bot.send_message.assert_awaited_once()
    assert sorted(calls) == ["risk", "support"]
    assert (
        len(store.aid_requests),
        len(store.escalations),
        len(store.actions),
        len(store.messages) - 1,
    ) == business_snapshot
    assert stored.delivered is True
    assert stored.delivery_status == "acknowledged"


@pytest.mark.asyncio
async def test_normal_delivery_ack_suppresses_outbox_replay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=gateway())
    update = incoming("мне тяжело", 1211)
    turn = await service.handle_text(update)
    monkeypatch.setattr(bot, "conversation_service", service)
    message = SimpleNamespace(answer=AsyncMock())

    await bot.send_turn(message, update, turn)

    assert (
        await worker.run_pending_outcomes(SimpleNamespace(send_message=AsyncMock()), service) == 0
    )
    message.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_optional_assistant_audit_failure_cannot_reopen_acknowledged_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class AuditFailureStore(InMemoryConversationStore):
        async def append_message(self, record, role, content, audit=None):  # type: ignore[no-untyped-def]
            if role == "assistant":
                raise RuntimeError("synthetic optional audit failure")
            await super().append_message(record, role, content, audit)

    store = AuditFailureStore()
    service = ConversationService(store=store, gateway=gateway())
    update = incoming("мне тяжело", 1212)
    turn = await service.handle_text(update)
    monkeypatch.setattr(bot, "conversation_service", service)

    await bot.send_turn(SimpleNamespace(answer=AsyncMock()), update, turn)

    assert (
        await worker.run_pending_outcomes(SimpleNamespace(send_message=AsyncMock()), service) == 0
    )
    record = await store.get(update)
    assert record is not None
    assert (await store.load_text_outcome(record, update.message_id))[1] is True


def test_runtime_and_product_docs_adopt_bounded_at_least_once_delivery() -> None:
    assert getattr(domain, "DELIVERY_SEMANTICS", None) == "bounded_at_least_once"
    assert getattr(domain, "DELIVERY_AMBIGUOUS_CATEGORY", None) == "delivery_ambiguous"
    documentation = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "README.md",
            "docs/superpowers/specs/2026-08-21-open-conversation-policy-design.md",
        )
    ).lower()
    assert "bounded at-least-once" in documentation
    assert "post-send/pre-ack" in documentation


@pytest.mark.parametrize(
    ("state", "text"),
    (
        (ConversationState.COLLECTING_LOCATION, "не хочу давать город"),
        (ConversationState.COLLECTING_LOCATION, "город давать не хочу"),
        (ConversationState.COLLECTING_CONTACT_VALUE, "не надо записывать мой телефон"),
        (ConversationState.COLLECTING_CONTACT_VALUE, "мой телефон записывать не надо"),
    ),
)
def test_clause_bound_state_refusal_predicate_families_cancel_workflow(
    state: ConversationState,
    text: str,
) -> None:
    """Deleting either verb family or state selection must fail this table."""
    signals = extract_signals(text, state=state)
    decision = resolve_turn(
        PolicyContext(
            state=state.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
            workflow_value=text,
        )
    )

    assert any(match.kind is HardSignalKind.OPEN_CONVERSATION_REQUEST for match in signals.matches)
    assert decision.effect is PolicyEffect.CANCEL_WORKFLOW


@pytest.mark.parametrize(
    ("state", "text"),
    (
        (ConversationState.COLLECTING_CONTACT_VALUE, "не хочу давать город"),
        (ConversationState.COLLECTING_LOCATION, "не надо записывать мой телефон"),
        (ConversationState.COLLECTING_LOCATION, "хочу давать город"),
        (
            ConversationState.COLLECTING_CONTACT_VALUE,
            "не хочу обсуждать погоду, мой телефон записывайте",
        ),
    ),
)
def test_refusal_predicates_do_not_cross_state_clause_or_polarity(
    state: ConversationState,
    text: str,
) -> None:
    signals = extract_signals(text, state=state)

    assert not any(
        match.kind is HardSignalKind.OPEN_CONVERSATION_REQUEST for match in signals.matches
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "text"),
    (
        (ConversationState.COLLECTING_LOCATION, "не хочу давать город"),
        (ConversationState.COLLECTING_CONTACT_VALUE, "не надо записывать мой телефон"),
    ),
)
async def test_service_refusal_clears_every_pending_value_and_never_captures_phrase(
    state: ConversationState,
    text: str,
) -> None:
    store = InMemoryConversationStore()
    service = ConversationService(store=store, gateway=gateway())
    update = incoming(text, 1220 if state is ConversationState.COLLECTING_LOCATION else 1221)
    record = await store.ensure(update)
    await store.update(
        record,
        state=state.value,
        need="legal",
        pending_aid_id="legal_consultation",
        pending_contact_method="phone",
        pending_city="old-city",
        pending_district="old-district",
        pending_offer="psychologist",
    )
    store.followup_jobs.append(
        SimpleNamespace(
            conversation_id=record.id,
            aid_request_id=1,
            status="processing",
        )
    )

    await service.handle_text(update)

    assert record.state == ConversationState.OPEN_CONVERSATION.value
    assert record.need is None
    assert record.pending_aid_id is None
    assert record.pending_contact_method is None
    assert record.pending_city is None
    assert record.pending_district is None
    assert record.pending_offer is None
    assert store.followup_jobs == []
    assert store.aid_requests == []
    assert all(
        text not in repr(value)
        for value in (
            record.pending_city,
            record.pending_district,
            record.pending_contact_method,
            store.aid_requests,
        )
    )


def _resolve_draft(draft: str) -> ResolvedTurn:
    signals = extract_signals("мне тяжело")
    return resolve_turn(
        PolicyContext(
            state=ConversationState.OPEN_CONVERSATION.value,
            signals=signals,
            local_risk=assess_local_risk_from_signals(signals),
            support_status=DiagnosticStatus.COMPLETED,
            support=SupportDiagnostic(intent="open_conversation", draft_text=draft),
        )
    )


def test_draft_guard_binds_negation_to_operational_predicate() -> None:
    decision = _resolve_draft("Не волнуйтесь я вам позвоню завтра.")

    assert decision.fallback_reason == "support_draft_guard"


def test_draft_guard_carries_leading_conditional_scope_across_comma() -> None:
    draft = "Если захотите, мы позвоним вам завтра."
    decision = _resolve_draft(draft)

    assert decision.text == draft
    assert decision.fallback_reason is None


@pytest.mark.parametrize(
    "draft",
    (
        "Связь с вами плохая.",
        "Запись для вас доступна.",
    ),
)
def test_draft_guard_does_not_treat_operational_stem_nouns_as_predicates(draft: str) -> None:
    decision = _resolve_draft(draft)

    assert decision.text == draft
    assert decision.fallback_reason is None


def _write_deploy_stub(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _deploy_harness_fixture(
    tmp_path: Path,
    *,
    writable_resolved_component: bool,
) -> tuple[dict[str, str], Path, Path, Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    metadata_log = tmp_path / "metadata.log"
    env_file = tmp_path / "service.env"
    env_file.write_text(
        "DATABASE_URL=postgresql+asyncpg://synthetic:synthetic@localhost/synthetic\n",
        encoding="utf-8",
    )
    target = tmp_path / "live"
    target.mkdir()

    _write_deploy_stub(
        fake_bin,
        "git",
        'case "$1" in\n'
        "  status) exit 0;;\n"
        "  rev-parse) echo deadbeef;;\n"
        "  archive) tar -cf - --files-from /dev/null;;\n"
        "esac\n",
    )
    _write_deploy_stub(fake_bin, "ssh", 'remote="${!#}"\neval "$remote"\n')
    _write_deploy_stub(fake_bin, "sudo", 'exec "$@"\n')
    _write_deploy_stub(fake_bin, "just", f'printf "just:%s\\n" "$1" >> "{log}"\n')
    _write_deploy_stub(fake_bin, "podman", 'printf "healthy\\n"\n')
    _write_deploy_stub(fake_bin, "systemctl", "exit 0\n")
    _write_deploy_stub(
        fake_bin,
        "mv",
        'if [[ "${1:-}" == "-Tf" ]]; then shift; /bin/rm -f "$2"; fi\nexec /bin/mv "$@"\n',
    )

    resolved_parent = fake_bin / "resolved"
    resolved_parent.mkdir()
    if writable_resolved_component:
        resolved_parent.chmod(0o777)
    _write_deploy_stub(resolved_parent, "uv-real", f'printf "uv\\n" >> "{log}"\n')
    (fake_bin / "uv-hop").symlink_to("resolved/uv-real")
    (fake_bin / "uv").symlink_to("uv-hop")

    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "WOMEN_HELP_TEST_TOOL_ROOT": str(fake_bin),
        "WOMEN_HELP_TEST_METADATA_LOG": str(metadata_log),
        "WOMEN_HELP_ENV_FILE": str(env_file),
    }
    return environment, target, log, metadata_log


def test_deploy_harness_keeps_verification_and_walks_every_symlink_hop(tmp_path: Path) -> None:
    environment, target, _, metadata_log = _deploy_harness_fixture(
        tmp_path,
        writable_resolved_component=False,
    )

    result = subprocess.run(
        ["bash", "scripts/deploy_prod_test_harness.sh", "stub-host", str(target)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    harness = Path("scripts/deploy_prod_test_harness.sh").read_text(encoding="utf-8")
    assert "VERIFY_ROOT_TOOLS=0" not in harness
    assert result.returncode == 0, result.stderr
    assert metadata_log.exists()
    checked = metadata_log.read_text(encoding="utf-8").splitlines()
    assert str(Path(environment["WOMEN_HELP_TEST_TOOL_ROOT"]) / "uv") in checked
    assert str(Path(environment["WOMEN_HELP_TEST_TOOL_ROOT"]) / "uv-hop") in checked
    assert str(Path(environment["WOMEN_HELP_TEST_TOOL_ROOT"]) / "resolved" / "uv-real") in checked


def test_deploy_verifier_rejects_writable_component_reached_through_symlink_chain(
    tmp_path: Path,
) -> None:
    environment, target, log, _ = _deploy_harness_fixture(
        tmp_path,
        writable_resolved_component=True,
    )

    result = subprocess.run(
        ["bash", "scripts/deploy_prod_test_harness.sh", "stub-host", str(target)],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 6
    assert "writable resolved component" in result.stderr
    assert not log.exists()
