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


def review_job(jobs: dict, key: str, request: dict, end: datetime) -> dict:
    """Move an unsent review, but never send a second review for the same request."""
    job_key = f"review:{key}"
    if jobs.get(job_key, {}).get("state") in {"sent", "answered"}:
        return jobs
    return {**jobs, job_key: {
        "screen": "s7", "due_at": daytime(end + timedelta(hours=2)).isoformat(),
        "state": "pending", "context": {"aid_id": request["aid_id"], "request_key": key},
    }}


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
            jobs = review_job(jobs, key, request, datetime.fromisoformat(completed_at))
            check = f"check:{key}"
            if check in jobs:
                jobs[check] = {**jobs[check], "context": {**jobs[check]["context"], "completed": True}}
        await self.api.set_custom_attributes(event.conversation_id, {
            "scenario_requests": requests, "scenario_followups": jobs,
            "reopen_with_bot": True, "scenario": {}, "workflow_state": "open_conversation",
        })
        return True

    async def plan_consultation(self, event) -> None:
        """Explicit staff macros use sidebar fields; never infer dates from dialogue."""
        current = attrs(await self.api.get_conversation(event.conversation_id))
        last_message = current.get("consultation_schedule_last_message_id", 0)
        if event.message_id <= last_message:
            if event.message_id == last_message and current.get("consultation_schedule_note"):
                await self.api.add_private_note(event.conversation_id, current["consultation_schedule_note"],
                                                event_key=f"consultation-schedule:{event.message_id}")
            return
        requests = dict(current.get("scenario_requests") or {})
        selected_id = str(current.get("consultation_request_id") or "").strip()
        selected = [key for key, value in requests.items()
                    if value["state"] in {"requested", "completed"}
                    and (key == selected_id if selected_id else value["state"] == "requested")]
        changes = {}
        if len(selected) != 1:
            note = ("Опрос не изменён. Укажите ID заявки в поле «ID консультации» и повторите макрос. "
                    "Доступные ID: " + ", ".join(requests))
        else:
            key = selected[0]
            request = requests[key]
            jobs = dict(current.get("scenario_followups") or {})
            job_key = f"review:{key}"
            job = jobs.get(job_key, {})
            # Reconcile a successful send whose HTTP acknowledgement was lost.
            delivered = job.get("state") in {"sent", "answered"} or await self.api.has_reply_for_turn(
                event.conversation_id, f"scenario-followup:{job_key}",
            )
            if delivered:
                note = f"Опрос по заявке {key} уже отправлен; повторной отправки не будет."
            elif event.consultation_schedule == "cancel":
                if job:
                    jobs[job_key] = {**job, "state": "cancelled"}
                requests[key] = {**request, "appointment_state": "cancelled"}
                changes = {"scenario_followups": jobs, "scenario_requests": requests}
                note = f"Опрос по заявке {key} отменён. Заявка и переписка сохранены."
            else:
                value = str(current.get("consultation_ends_at") or "").strip()
                try:
                    end = datetime.strptime(value, "%d.%m.%Y %H:%M").replace(tzinfo=MSK)
                except ValueError:
                    note = ("Опрос не изменён. Укажите окончание встречи в формате "
                            "ДД.ММ.ГГГГ ЧЧ:ММ (МСК) и повторите макрос.")
                else:
                    jobs = review_job(jobs, key, request, end)
                    requests[key] = {**request, "appointment_ends_at": end.astimezone(UTC).isoformat(),
                                     "appointment_state": "scheduled"}
                    changes = {"scenario_followups": jobs, "scenario_requests": requests}
                    due = datetime.fromisoformat(jobs[job_key]["due_at"]).astimezone(MSK)
                    note = (f"Опрос по заявке {key} запланирован на {due:%d.%m.%Y %H:%M} МСК. "
                            "Встреча ещё не отмечена состоявшейся. При переносе измените время "
                            "и повторите макрос; при отмене используйте «Отменить опрос консультации».")
        # Commit the result with the business change. A lost note acknowledgement
        # must not reread edited sidebar fields and silently reschedule the meeting.
        await self.api.set_custom_attributes(event.conversation_id, {
            **changes, "consultation_schedule_last_message_id": event.message_id,
            "consultation_schedule_note": note,
        })
        await self.api.add_private_note(event.conversation_id, note,
                                        event_key=f"consultation-schedule:{event.message_id}")

    async def menu_delivered(self, conversation_id: int, message_id: int) -> None:
        current = attrs(await self.api.get_conversation(conversation_id))
        if (current.get("scenario") or {}).get("screen") != "s2":
            return
        jobs = dict(current.get("scenario_followups") or {})
        key = f"idle:{current.get('context_epoch', 0)}"
        updated = schedule(jobs, key, "i4", self.clock() + timedelta(hours=1),
                           {"menu_message_id": message_id})
        if updated != jobs:
            await self.api.set_custom_attributes(conversation_id, {"scenario_followups": updated})

    async def cancel_idle(self, conversation: dict, *, before_message: int | None = None) -> dict:
        current = attrs(conversation)
        jobs = dict(current.get("scenario_followups") or {})
        changed = False
        for key, job in jobs.items():
            if (job.get("screen") == "i4" and job["state"] == "pending"
                    and (before_message is None or job["context"]["menu_message_id"] < before_message)):
                jobs[key] = {**job, "state": "cancelled"}
                changed = True
        if changed:
            await self.api.set_custom_attributes(conversation["id"], {"scenario_followups": jobs})
            return {**conversation, "custom_attributes": {**current, "scenario_followups": jobs}}
        return conversation

    async def due(self, conversation: dict) -> tuple[str, dict] | None:
        current = attrs(conversation)
        jobs = dict(current.get("scenario_followups") or {})
        now = self.clock()
        if daytime(now) > now:
            return None
        # A due post-meeting review takes precedence over an older contact check.
        for key, job in sorted(jobs.items(), key=lambda p: (p[1]["screen"] != "s7", p[1]["due_at"])):
            if job["state"] != "pending" or datetime.fromisoformat(job["due_at"]) > now:
                continue
            if job["screen"] == "i4":
                # Also check stored messages: an incoming webhook may still be
                # queued. Never nudge a user who has already answered.
                messages = await self.api.get_messages(conversation["id"])
                scenario = current.get("scenario") or {}
                waiting = (scenario.get("screen") == "s2" or scenario.get("job_key") == key
                           and scenario.get("screen") == "i4")
                activity = any(m.get("id", 0) > job["context"]["menu_message_id"]
                               and not m.get("private")
                               and (m.get("message_type") in {0, "incoming"}
                                    or (m.get("sender") or {}).get("type") == "user")
                               for m in messages)
                if not waiting or activity:
                    conversation = await self.cancel_idle(conversation)
                    jobs = dict(attrs(conversation).get("scenario_followups") or {})
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
        if job["screen"] == "s7":
            check = f"check:{job['context'].get('request_key')}"
            for obsolete in (check, f"reminder:{check}"):
                if jobs.get(obsolete, {}).get("state") == "pending":
                    jobs[obsolete] = {**jobs[obsolete], "state": "cancelled"}
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
