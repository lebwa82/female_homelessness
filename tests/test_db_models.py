import pytest

from app import db
from app.db import (
    ActionExecution,
    AgentRun,
    AidRequest,
    ContactPoint,
    Conversation,
    Escalation,
    FollowupJob,
    RiskAssessmentRecord,
    conversation_identity_hash,
)
from scripts.postgres_assurance import _REQUIRED_INDEXES


def test_new_operational_tables_have_expected_identity_and_audit_columns() -> None:
    assert RiskAssessmentRecord.__tablename__ == "risk_assessments"
    assert AgentRun.__tablename__ == "agent_runs"
    assert ActionExecution.__tablename__ == "action_executions"
    assert AidRequest.__tablename__ == "aid_requests"
    assert ContactPoint.__tablename__ == "contact_points"
    assert Escalation.__tablename__ == "escalations"
    assert FollowupJob.__tablename__ == "followup_jobs"
    assert "metadata" in AgentRun.__table__.c
    assert "request_key" in AidRequest.__table__.c
    assert "request_key" in Escalation.__table__.c
    assert "due_at" in FollowupJob.__table__.c


def test_conversation_identity_hash_is_stable_and_does_not_expose_platform_id() -> None:
    value = conversation_identity_hash("telegram", 987654321, "test-key")

    assert value == conversation_identity_hash("telegram", 987654321, "test-key")
    assert value != conversation_identity_hash("telegram", 987654321, "other-key")
    assert "987654321" not in value
    assert len(value) == 64


def test_conversation_identity_is_unique_per_channel_for_future_chatwoot_adapter() -> None:
    unique_constraints = [
        constraint
        for constraint in Conversation.__table__.constraints
        if constraint.name == "uq_conversations_channel_identity"
    ]

    assert Conversation.__table__.c.channel_user_id.unique is not True
    assert len(unique_constraints) == 1
    assert {column.name for column in unique_constraints[0].columns} == {"channel", "channel_user_id"}


def test_conversation_and_escalation_models_persist_policy_context() -> None:
    assert "pending_offer" in Conversation.__table__.c
    assert "cause" in Escalation.__table__.c
    assert Escalation.__table__.c.cause.default.arg == "safety"
    assert Escalation.__table__.c.level.nullable is True
    assert Escalation.__table__.c.request_key.nullable is True
    assert Escalation.__table__.c.request_key.unique is True


@pytest.mark.asyncio
async def test_init_db_and_assurance_require_callback_lease_scan_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Connection:
        async def run_sync(self, function):  # type: ignore[no-untyped-def]
            del function

        async def execute(self, statement):  # type: ignore[no-untyped-def]
            statements.append(str(statement))

    class Begin:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback) -> None:  # type: ignore[no-untyped-def]
            del exc_type, exc, traceback

    class Engine:
        def begin(self) -> Begin:
            return Begin()

    monkeypatch.setattr(db, "engine", Engine())

    await db.init_db()

    expected_indexes = {
        "ix_callback_executions_status",
        "ix_callback_executions_lease_expires_at",
    }
    assert expected_indexes <= _REQUIRED_INDEXES
    for index_name in expected_indexes:
        assert any(index_name in statement for statement in statements)
