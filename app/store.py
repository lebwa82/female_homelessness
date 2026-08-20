from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any
from uuid import uuid4

from app import db
from app.config import settings
from app.domain import IncomingMessage, RiskAssessment


@dataclass
class ConversationRecord:
    id: int
    channel: str
    platform_user_id: int
    chat_id: int
    username: str | None
    state: str = "greeting"
    need: str | None = None
    pending_aid_id: str | None = None
    pending_contact_method: str | None = None
    pending_city: str | None = None
    pending_district: str | None = None


@dataclass
class StoredAidRequest:
    id: int
    conversation_id: int
    aid_id: str
    contact_method: str | None
    contact_value: str | None
    city: str | None = None
    district: str | None = None


@dataclass
class StoredEscalation:
    conversation_id: int
    assessment: RiskAssessment

    @property
    def level(self):  # type: ignore[no-untyped-def]
        return self.assessment.level


@dataclass
class StoredFollowupJob:
    conversation_id: int
    aid_request_id: int
    due_at: datetime
    kind: str = "followup"
    status: str = "pending"


@dataclass
class InMemoryConversationStore:
    conversations: dict[int, ConversationRecord] = field(default_factory=dict)
    messages: list[tuple[int, str, str, dict[str, Any]]] = field(default_factory=list)
    aid_requests: list[StoredAidRequest] = field(default_factory=list)
    escalations: list[StoredEscalation] = field(default_factory=list)
    followup_jobs: list[StoredFollowupJob] = field(default_factory=list)
    agent_runs: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)
    risk_assessments: list[tuple[int, RiskAssessment]] = field(default_factory=list)
    actions: list[tuple[int, str, str]] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: count(1), repr=False)

    async def ensure(self, incoming: IncomingMessage) -> ConversationRecord:
        record = self.conversations.get(incoming.platform_user_id)
        if record is None:
            record = ConversationRecord(
                id=next(self._ids),
                channel=incoming.channel,
                platform_user_id=incoming.platform_user_id,
                chat_id=incoming.chat_id,
                username=incoming.username,
            )
            self.conversations[incoming.platform_user_id] = record
        else:
            record.chat_id = incoming.chat_id
            record.username = incoming.username
        return record

    async def update(self, record: ConversationRecord, **values: str | None) -> ConversationRecord:
        for key, value in values.items():
            setattr(record, key, value)
        return record

    async def append_message(
        self, record: ConversationRecord, role: str, content: str, audit: dict[str, Any] | None = None
    ) -> None:
        self.messages.append((record.id, role, content, audit or {}))

    async def history(self, record: ConversationRecord) -> tuple[tuple[str, str], ...]:
        return tuple((role, content) for conversation_id, role, content, _ in self.messages if conversation_id == record.id)

    async def record_agent_run(self, record: ConversationRecord, agent_name: str, audit: dict[str, Any]) -> None:
        self.agent_runs.append((record.id, agent_name, audit))

    async def record_risk(self, record: ConversationRecord, assessment: RiskAssessment) -> None:
        self.risk_assessments.append((record.id, assessment))

    async def record_action(self, record: ConversationRecord, kind: str, status: str) -> None:
        self.actions.append((record.id, kind, status))

    async def create_escalation(self, record: ConversationRecord, assessment: RiskAssessment) -> None:
        self.escalations.append(StoredEscalation(record.id, assessment))

    async def create_aid_request(
        self,
        record: ConversationRecord,
        aid_id: str,
        contact_method: str | None,
        contact_value: str | None,
        city: str | None = None,
        district: str | None = None,
    ) -> StoredAidRequest:
        request = StoredAidRequest(
            id=next(self._ids),
            conversation_id=record.id,
            aid_id=aid_id,
            contact_method=contact_method,
            contact_value=contact_value,
            city=city,
            district=district,
        )
        self.aid_requests.append(request)
        self.followup_jobs.append(
            StoredFollowupJob(
                record.id,
                request.id,
                datetime.now(UTC) + timedelta(seconds=settings.followup_delay_seconds),
            )
        )
        return request

    async def delete_data(self, record: ConversationRecord) -> None:
        self.messages = [item for item in self.messages if item[0] != record.id]
        request_ids = {item.id for item in self.aid_requests if item.conversation_id == record.id}
        self.aid_requests = [item for item in self.aid_requests if item.conversation_id != record.id]
        self.followup_jobs = [item for item in self.followup_jobs if item.aid_request_id not in request_ids]
        record.state = "greeting"
        record.need = None
        record.pending_aid_id = None
        record.pending_contact_method = None
        record.pending_city = None
        record.pending_district = None

    async def cancel_pending_reminder(self, record: ConversationRecord) -> None:
        self.followup_jobs = [
            job
            for job in self.followup_jobs
            if not (job.conversation_id == record.id and job.kind == "followup_reminder" and job.status == "pending")
        ]


class PostgresConversationStore:
    def __init__(self, identity_hash_key: str | None = None) -> None:
        self._identity_hash_key = identity_hash_key or settings.identity_hash_key

    async def ensure(self, incoming: IncomingMessage) -> ConversationRecord:
        row = await db.get_or_create_conversation_record(
            incoming.channel,
            incoming.platform_user_id,
            incoming.chat_id,
            incoming.username,
            self._identity_hash_key,
        )
        return self._record_from_row(row)

    async def update(self, record: ConversationRecord, **values: str | None) -> ConversationRecord:
        async with db.Session() as session:
            row = await session.get(db.Conversation, record.id)
            if row is None:
                raise LookupError(f"conversation {record.id} is missing")
            for key, value in values.items():
                if key == "need":
                    row.requested_help = value
                else:
                    setattr(row, key, value)
            await session.commit()
            await session.refresh(row)
            return self._record_from_row(row)

    async def append_message(
        self, record: ConversationRecord, role: str, content: str, audit: dict[str, Any] | None = None
    ) -> None:
        await db.append_message(record.id, role, content, audit)

    async def history(self, record: ConversationRecord) -> tuple[tuple[str, str], ...]:
        return tuple(await db.load_history(record.id))

    async def record_agent_run(self, record: ConversationRecord, agent_name: str, audit: dict[str, Any]) -> None:
        await db.record_agent_run(record.id, agent_name, audit)

    async def record_risk(self, record: ConversationRecord, assessment: RiskAssessment) -> None:
        await db.record_risk_assessment(record.id, assessment)

    async def record_action(self, record: ConversationRecord, kind: str, status: str) -> None:
        await db.record_action(record.id, kind, status)

    async def create_escalation(self, record: ConversationRecord, assessment: RiskAssessment) -> None:
        await db.create_escalation(record.id, assessment)

    async def create_aid_request(
        self,
        record: ConversationRecord,
        aid_id: str,
        contact_method: str | None,
        contact_value: str | None,
        city: str | None = None,
        district: str | None = None,
    ) -> StoredAidRequest:
        async with db.Session() as session:
            request = db.AidRequest(
                conversation_id=record.id,
                aid_id=aid_id,
                request_key=uuid4().hex,
                city=city,
                district=district,
            )
            session.add(request)
            await session.flush()
            if contact_method is not None:
                session.add(
                    db.ContactPoint(
                        aid_request_id=request.id,
                        method=contact_method,
                        value=contact_value,
                        expires_at=datetime.now(UTC) + timedelta(days=30),
                    )
                )
            session.add(
                db.FollowupJob(
                    conversation_id=record.id,
                    aid_request_id=request.id,
                    kind="followup",
                    due_at=datetime.now(UTC) + timedelta(seconds=settings.followup_delay_seconds),
                )
            )
            await session.commit()
            return StoredAidRequest(
                id=request.id,
                conversation_id=record.id,
                aid_id=aid_id,
                contact_method=contact_method,
                contact_value=contact_value,
                city=city,
                district=district,
            )

    async def delete_data(self, record: ConversationRecord) -> None:
        await db.delete_conversation_data(record.id)

    async def cancel_pending_reminder(self, record: ConversationRecord) -> None:
        await db.cancel_followup_reminders(record.id)

    @staticmethod
    def _record_from_row(row: db.Conversation) -> ConversationRecord:
        return ConversationRecord(
            id=row.id,
            channel=row.channel,
            platform_user_id=row.channel_user_id,
            chat_id=row.chat_id or row.channel_user_id,
            username=row.username,
            state=row.state,
            need=row.requested_help,
            pending_aid_id=row.pending_aid_id,
            pending_contact_method=row.pending_contact_method,
            pending_city=row.pending_city,
            pending_district=row.pending_district,
        )
