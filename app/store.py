from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any
from uuid import uuid4

from app import db
from app.config import settings
from app.domain import (
    EscalationCause,
    EscalationRequest,
    IncomingMessage,
    RiskAssessment,
    RiskLevel,
)


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
    pending_offer: str | None = None


@dataclass
class StoredAidRequest:
    id: int
    conversation_id: int
    aid_id: str
    contact_method: str | None
    contact_value: str | None
    city: str | None = None
    district: str | None = None
    request_key: str | None = None


@dataclass
class StoredEscalation:
    conversation_id: int
    request: EscalationRequest

    @property
    def cause(self) -> EscalationCause:
        return self.request.cause

    @property
    def level(self) -> RiskLevel | None:
        return self.request.level

    @property
    def categories(self) -> tuple[str, ...]:
        return self.request.categories

    @property
    def reason(self) -> str:
        return self.request.reason


@dataclass
class StoredFollowupJob:
    conversation_id: int
    aid_request_id: int
    due_at: datetime
    kind: str = "followup"
    status: str = "pending"


@dataclass
class StoredCallbackClaim:
    status: str
    lease_token: str | None
    lease_expires_at: datetime | None


@dataclass
class InMemoryConversationStore:
    conversations: dict[int, ConversationRecord] = field(default_factory=dict)
    messages: list[tuple[int, str, str, dict[str, Any]]] = field(default_factory=list)
    aid_requests: list[StoredAidRequest] = field(default_factory=list)
    escalations: list[StoredEscalation] = field(default_factory=list)
    followup_jobs: list[StoredFollowupJob] = field(default_factory=list)
    agent_runs: list[tuple[int, str, dict[str, Any]]] = field(default_factory=list)
    risk_assessments: list[tuple[int, RiskAssessment]] = field(default_factory=list)
    actions: list[tuple[int, str, str, dict[str, Any]]] = field(default_factory=list)
    callback_claims: dict[tuple[int, str, str], StoredCallbackClaim] = field(default_factory=dict)
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

    async def record_action(
        self, record: ConversationRecord, kind: str, status: str, audit: dict[str, Any] | None = None
    ) -> None:
        self.actions.append((record.id, kind, status, audit or {}))

    async def claim_callback(
        self,
        record: ConversationRecord,
        callback_id: str,
        message_id: int | None,
    ) -> str | None:
        key = (record.id, callback_id, str(message_id) if message_id is not None else "missing")
        current = self.callback_claims.get(key)
        now = datetime.now(UTC)
        if current is not None:
            is_expired = (
                current.status == "processing"
                and current.lease_expires_at is not None
                and current.lease_expires_at <= now
            )
            if current.status == "completed" or (current.status == "processing" and not is_expired):
                return None
        lease_token = uuid4().hex
        self.callback_claims[key] = StoredCallbackClaim(
            status="processing",
            lease_token=lease_token,
            lease_expires_at=now + timedelta(minutes=5),
        )
        return lease_token

    async def complete_callback(
        self,
        record: ConversationRecord,
        callback_id: str,
        message_id: int | None,
        lease_token: str,
    ) -> None:
        key = (record.id, callback_id, str(message_id) if message_id is not None else "missing")
        claim = self.callback_claims.get(key)
        if claim is not None and claim.status == "processing" and claim.lease_token == lease_token:
            claim.status = "completed"
            claim.lease_token = None
            claim.lease_expires_at = None

    async def fail_callback(
        self,
        record: ConversationRecord,
        callback_id: str,
        message_id: int | None,
        lease_token: str,
    ) -> None:
        key = (record.id, callback_id, str(message_id) if message_id is not None else "missing")
        claim = self.callback_claims.get(key)
        if claim is not None and claim.status == "processing" and claim.lease_token == lease_token:
            claim.status = "failed"
            claim.lease_token = None
            claim.lease_expires_at = None

    async def create_escalation(self, record: ConversationRecord, request: EscalationRequest) -> StoredEscalation:
        if request.request_key is not None:
            for escalation in self.escalations:
                if escalation.request.request_key == request.request_key:
                    return escalation
        escalation = StoredEscalation(record.id, request)
        self.escalations.append(escalation)
        return escalation

    async def create_aid_request(
        self,
        record: ConversationRecord,
        aid_id: str,
        contact_method: str | None,
        contact_value: str | None,
        city: str | None = None,
        district: str | None = None,
        request_key: str | None = None,
    ) -> StoredAidRequest:
        if request_key is not None:
            for request in self.aid_requests:
                if request.request_key == request_key:
                    return request
        request = StoredAidRequest(
            id=next(self._ids),
            conversation_id=record.id,
            aid_id=aid_id,
            contact_method=contact_method,
            contact_value=contact_value,
            city=city,
            district=district,
            request_key=request_key,
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
        self.callback_claims = {key: claim for key, claim in self.callback_claims.items() if key[0] != record.id}
        record.state = "greeting"
        record.need = None
        record.pending_aid_id = None
        record.pending_contact_method = None
        record.pending_city = None
        record.pending_district = None
        record.pending_offer = None

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
            updated = self._record_from_row(row)
            record.__dict__.update(updated.__dict__)
            return record

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

    async def record_action(
        self, record: ConversationRecord, kind: str, status: str, audit: dict[str, Any] | None = None
    ) -> None:
        await db.record_action(record.id, kind, status, audit)

    async def claim_callback(
        self,
        record: ConversationRecord,
        callback_id: str,
        message_id: int | None,
    ) -> str | None:
        return await db.claim_callback_execution(record.id, callback_id, message_id)

    async def complete_callback(
        self,
        record: ConversationRecord,
        callback_id: str,
        message_id: int | None,
        lease_token: str,
    ) -> None:
        await db.complete_callback_execution(record.id, callback_id, message_id, lease_token)

    async def fail_callback(
        self,
        record: ConversationRecord,
        callback_id: str,
        message_id: int | None,
        lease_token: str,
    ) -> None:
        await db.fail_callback_execution(record.id, callback_id, message_id, lease_token)

    async def create_escalation(self, record: ConversationRecord, request: EscalationRequest) -> Any:
        return await db.create_escalation(record.id, request)

    async def create_aid_request(
        self,
        record: ConversationRecord,
        aid_id: str,
        contact_method: str | None,
        contact_value: str | None,
        city: str | None = None,
        district: str | None = None,
        request_key: str | None = None,
    ) -> StoredAidRequest:
        request_key = request_key or uuid4().hex
        async with db.Session() as session:
            result = await session.execute(
                db.postgres_insert(db.AidRequest)
                .values(
                    conversation_id=record.id,
                    aid_id=aid_id,
                    request_key=request_key,
                    city=city,
                    district=district,
                )
                .on_conflict_do_nothing(index_elements=(db.AidRequest.request_key,))
                .returning(db.AidRequest.id)
            )
            request_id = result.scalar_one_or_none()
            if request_id is None:
                existing = await session.execute(
                    db.select(db.AidRequest).where(db.AidRequest.request_key == request_key)
                )
                request = existing.scalar_one()
                await session.commit()
                return StoredAidRequest(
                    id=request.id,
                    conversation_id=record.id,
                    aid_id=request.aid_id,
                    contact_method=contact_method,
                    contact_value=contact_value,
                    city=request.city,
                    district=request.district,
                    request_key=request_key,
                )
            request = await session.get(db.AidRequest, request_id)
            if request is None:
                raise LookupError(f"aid request {request_id} is missing")
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
                request_key=request_key,
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
            pending_offer=row.pending_offer,
        )
