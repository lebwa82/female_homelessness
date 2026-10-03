"""Scenario requests and follow-ups, durably stored in Chatwoot attributes/notes.

One Agent Bot process serializes updates per conversation. No second database,
message history copy, external scheduler, or timer lost at a process restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.catalog import get_aid_item
from app.scenario import CERTIFICATES

MSK = ZoneInfo("Europe/Moscow")
NOTIFY = {"consultation_requested", "consultation_retry", "other_request",
          "certificate_waitlist", "certificate_problem", "negative_feedback", "human_message"}


def daytime(value: datetime) -> datetime:
    local = value.astimezone(MSK)
    if local.hour >= 20:
        local = (local + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
    elif local.hour < 10:
        local = local.replace(hour=10, minute=0, second=0, microsecond=0)
    return local.astimezone(UTC)


def schedule(jobs: dict, key: str, screen: str, due: datetime, context: dict) -> dict:
    if key in jobs:
        return jobs
    return {**jobs, key: {"screen": screen, "due_at": daytime(due).isoformat(),
                         "state": "pending", "context": context}}


def attrs(conversation: dict) -> dict:
    return conversation.get("custom_attributes") or {}


def returning_after_day(messages: tuple[dict, ...], message_id: int, now: datetime) -> bool:
    previous = []
    for message in messages:
        if (message.get("id", 0) >= message_id or message.get("private")
                or message.get("message_type") not in {0, 1, "incoming", "outgoing"}):
            continue
        value = message.get("created_at")
        try:
            stamp = (datetime.fromtimestamp(value, UTC) if isinstance(value, (int, float))
                     else datetime.fromisoformat(value))
            if stamp.tzinfo is not None:
                previous.append(stamp)
        except (TypeError, ValueError, OverflowError):
            continue
    return bool(previous and now - max(previous) >= timedelta(days=1))


class ScenarioEffects:
    def __init__(self, api, queues, *, clock=None, can_route=lambda _: True):
        self.api, self.queues = api, queues
        self.clock = clock or (lambda: datetime.now(UTC))
        self.can_route = can_route

    async def apply(self, conversation_id: int, message_id: int, actions: list) -> None:
        for _, kind, _, payload in actions:
            if kind != "scenario_event":
                continue
            current = attrs(await self.api.get_conversation(conversation_id))
            receipts = dict(current.get("scenario_event_receipts") or {})
            key = payload["key"]
            if key in receipts:
                continue
            event_kind = payload["kind"]
            requests = dict(current.get("scenario_requests") or {})
            jobs = dict(current.get("scenario_followups") or {})
            now = self.clock()
            if event_kind == "consultation_requested":
                request = requests.setdefault(key, {
                    "aid_id": payload["aid_id"], "state": "requested",
                    "requested_at": now.isoformat(),
                })
                jobs = schedule(jobs, f"check:{key}", "s5",
                                datetime.fromisoformat(request["requested_at"]) + timedelta(days=4),
                                {"aid_id": payload["aid_id"], "request_key": key})
                if current.get("scenario_certificate"):
                    jobs[f"check:{key}"] = {**jobs[f"check:{key}"], "state": "cancelled"}
            job_key = payload.get("job_key")
            if job_key and job_key in jobs:
                jobs[job_key] = {**jobs[job_key], "state": "answered"}
                reminder = f"reminder:{job_key}"
                if reminder in jobs:
                    jobs[reminder] = {**jobs[reminder], "state": "cancelled"}
            # Save business facts before acknowledgement. They are not part of
            # the navigable workflow and cannot be undone by Back or /clear.
            await self.api.set_custom_attributes(conversation_id, {
                "scenario_requests": requests, "scenario_followups": jobs,
            })
            details = {k: v for k, v in payload.items() if k not in {"key", "kind"} and v is not None}
            await self.api.add_private_note(
                conversation_id, f"Сценарий: {event_kind}; запрос {key}\n"
                + "\n".join(f"{k}: {v}" for k, v in details.items()), event_key=f"scenario:{key}",
            )
            if event_kind in NOTIFY:
                if self.queues.default_id is None:
                    raise RuntimeError("scenario_notification_queue_missing")
                item = get_aid_item(payload.get("aid_id", ""))
                # Route from the actual service/request, never opaque button IDs.
                history = (("user", item.label if item else payload.get("text") or event_kind),)
                if event_kind in {"negative_feedback", "certificate_problem", "consultation_retry"}:
                    specialist = requests.get(payload.get("request_id"), {}).get("specialist_id")
                    mention = f"[Специалист](mention://user/{specialist}/staff) " if specialist else ""
                    await self.api.add_private_note(
                        conversation_id,
                        f"[Дежурные](mention://team/{self.queues.default_id}/queue) "
                        + (mention if event_kind == "consultation_retry" else "")
                        + f"Требует внимания: {event_kind}; запрос {key}.",
                        event_key=f"scenario-alert:{key}",
                    )
                else:
                    await self.queues.route(
                        await self.api.get_conversation(conversation_id), history, message_id,
                        can_route=self.can_route,
                    )
            receipts[key] = event_kind
            changes = {"scenario_event_receipts": receipts}
            if payload.get("screen") == "s7":
                changes["consultation_reviewed"] = True
            await self.api.set_custom_attributes(conversation_id, changes)

    async def certificate_delivered(self, conversation_id: int, key: str, context: dict) -> None:
        current = attrs(await self.api.get_conversation(conversation_id))
        jobs = schedule(dict(current.get("scenario_followups") or {}), f"certificate:{key}",
                        "s5", self.clock() + timedelta(days=4), context)
        # When both kinds exist, block 5 asks about the certificate. Block 7
        # remains a separate review after an explicitly completed consultation.
        for job_key, job in jobs.items():
            if (job["screen"] in {"s5", "s59"} and job["state"] == "pending"
                    and job["context"].get("aid_id") not in CERTIFICATES):
                jobs[job_key] = {**job, "state": "cancelled"}
        await self.api.set_custom_attributes(conversation_id, {
            "scenario_followups": jobs, "scenario_certificate": context,
        })

    async def complete(self, event) -> bool:
        current = attrs(await self.api.get_conversation(event.conversation_id))
        requests = dict(current.get("scenario_requests") or {})
        selected = [key for key, value in requests.items()
                    if value["state"] == "requested" and (not event.request_id or key == event.request_id)]
        # A retried completion must reuse its original business event.
        selected += [key for key, value in requests.items()
                     if value.get("completed_message_id") == event.message_id and key not in selected]
        if len(selected) > 1 or event.request_id and not selected:
            await self.api.add_private_note(
                event.conversation_id,
                "Уточните завершённый запрос приватной командой "
                "[women-help:consultation-completed:ID]. Доступные ID: " + ", ".join(selected),
                event_key=f"completion-ambiguous:{event.message_id}",
            )
            return False
        jobs = dict(current.get("scenario_followups") or {})
        if selected:
            key = selected[0]
            request = requests[key]
            completed_at = request.get("completed_at", self.clock().isoformat())
            requests[key] = {**request, "state": "completed", "completed_at": completed_at,
                             "completed_message_id": event.message_id}
            jobs = schedule(jobs, f"review:{key}", "s7",
                            datetime.fromisoformat(completed_at) + timedelta(hours=2),
                            {"aid_id": request["aid_id"], "request_key": key, "completed": True})
            check = f"check:{key}"
            if check in jobs:
                jobs[check] = {**jobs[check], "context": {**jobs[check]["context"], "completed": True}}
        await self.api.set_custom_attributes(event.conversation_id, {
            "scenario_requests": requests, "scenario_followups": jobs,
            "reopen_with_bot": True, "scenario": {}, "workflow_state": "open_conversation",
        })
        return True

    async def due(self, conversation: dict) -> tuple[str, dict] | None:
        current = attrs(conversation)
        jobs = dict(current.get("scenario_followups") or {})
        now = self.clock()
        if daytime(now) > now:
            return None
        for key, job in sorted(jobs.items(), key=lambda pair: pair[1]["due_at"]):
            if job["state"] != "pending" or datetime.fromisoformat(job["due_at"]) > now:
                continue
            expiry = job["context"].get("expires_at")
            if expiry and datetime.fromisoformat(expiry) <= now:
                jobs[key] = {**job, "state": "expired"}
                await self.api.set_custom_attributes(conversation["id"], {"scenario_followups": jobs})
                continue
            return key, job
        return None

    async def sent(self, conversation_id: int, key: str) -> None:
        current = attrs(await self.api.get_conversation(conversation_id))
        jobs = dict(current.get("scenario_followups") or {})
        job = jobs[key]
        if job["state"] != "pending":
            return
        jobs[key] = {**job, "state": "sent", "sent_at": self.clock().isoformat()}
        # Exactly one reminder for block 5; no idle or consultation-review nudges.
        if job["screen"] == "s5":
            context = job["context"]
            due = daytime(self.clock() + timedelta(days=2))
            expiry = context.get("expires_at")
            if not expiry or due < datetime.fromisoformat(expiry):
                jobs = schedule(jobs, f"reminder:{key}", "s59", due, context)
        await self.api.set_custom_attributes(conversation_id, {"scenario_followups": jobs})

    async def answer_current(self, conversation: dict) -> None:
        """Any reply to a survey, even free text or Human, stops its reminder."""
        current = attrs(conversation)
        job_key = (current.get("scenario") or {}).get("job_key")
        if not job_key:
            return
        jobs = dict(current.get("scenario_followups") or {})
        for key in (job_key, f"reminder:{job_key}"):
            if key in jobs:
                jobs[key] = {**jobs[key], "state": "answered"}
        changes = {"scenario_followups": jobs}
        if job_key.startswith("review:"):
            changes["consultation_reviewed"] = True
        await self.api.set_custom_attributes(conversation["id"], changes)

    @staticmethod
    def context(conversation: dict, job_key: str, job: dict) -> dict:
        current = attrs(conversation)
        context = {**job["context"], "job_key": job_key,
                   "consultation_reviewed": current.get("consultation_reviewed", False)}
        if context.get("aid_id") not in CERTIFICATES:
            request = (current.get("scenario_requests") or {}).get(context.get("request_key"), {})
            context["completed"] = request.get("state") == "completed"
        return context
