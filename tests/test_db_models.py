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
