"""Queue routing and retry-safe team mentions; all durable state is in Chatwoot."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import uuid4

from app.chatwoot.routing import QueueRouter, RouteDecision, Team, YandexQueueRouter


def team_id(conversation: dict) -> int | None:
    value = (conversation.get("meta", {}).get("team") or {}).get("id")
    return value or conversation.get("team_id") or conversation.get("assignee_team_id")


class QueueCoordinator:
    def __init__(self, api: Any, default_id: int | None, router: QueueRouter | None = None):
        self.api, self.default_id = api, default_id
        self.router = router or YandexQueueRouter()

    async def catalog(self) -> tuple[Team, ...]:
        records = await self.api.get_teams()
        return tuple(
            Team(t["id"], t["name"], t.get("description") or "")
            for t in records if type(t.get("id")) is int and isinstance(t.get("name"), str)
        )

    async def sync(self, conversation: dict) -> None:
        """A manual Team dropdown change is authoritative, including removal."""
        attrs = conversation.get("custom_attributes") or {}
        current = team_id(conversation)
        if attrs.get("routing_assignment_pending"):
            if current == attrs.get("routing_team_id"):
                await self.api.set_custom_attributes(conversation["id"], {
                    "routing_assignment_pending": False,
                })
            elif current == attrs.get("routing_previous_team_id"):
                # The assignment request failed before application. Its original
                # incoming event retries the saved decision, not a new model call.
                return
        if current != attrs.get("routing_team_id"):
            changes = {
                "routing_team_id": current, "routing_origin": "manual",
                "routing_assignment_pending": False,
            }
            if current:
                teams = await self.catalog()
                selected = next((t for t in teams if t.id == current), None)
                if selected:
                    changes["routing_notice"] = self._notice(selected, f"transfer:{uuid4().hex}")
            else:
                changes["routing_notice"] = None
            await self.api.set_custom_attributes(conversation["id"], changes)
            attrs = {**attrs, **changes}
        await self._flush(conversation["id"], attrs)

    @staticmethod
    def _notice(team: Team, key: str) -> dict:
        return {"team_id": team.id, "name": team.name, "key": key, "sent": False}

    async def _flush(self, conversation_id: int, attrs: dict) -> None:
        notice = attrs.get("routing_notice")
        if not notice or notice.get("sent"):
            return
        # ID-based native mentions expand the current team membership in Chatwoot.
        name = notice["name"].replace("[", "").replace("]", "")
        await self.api.add_private_note(
            conversation_id,
            f"[{name}](mention://team/{notice['team_id']}/queue): "
            "обращение ожидает специалистку. Назначьте разговор на себя перед ответом. "
            "После подключения человека бот ждёт явного «Вернуть боту».",
            event_key=notice["key"],
        )
        await self.api.set_custom_attributes(conversation_id, {
            "routing_notice": {**notice, "sent": True},
        })

    async def route(
        self, conversation: dict, history, message_id: int, *, urgent=False, can_route
    ) -> int | None:
        teams = await self.catalog()
        default = next((t for t in teams if t.id == self.default_id), None)
        if default is None:
            raise RuntimeError("default_queue_missing")
        attrs = conversation.get("custom_attributes") or {}
        manual = attrs.get("routing_origin") == "manual"
        current = team_id(conversation)
        selected = next((t for t in teams if t.id == current), None) if manual else None
        decision = RouteDecision(default.id, {"reason": "default"})
        if attrs.get("routing_message_id") == message_id:
            selected = next((t for t in teams if t.id == attrs.get("routing_team_id")), None)
        if selected:
            decision = RouteDecision(selected.id, {"reason": "existing_route_preserved"})
        elif not urgent and not manual:
            memberships = await asyncio.gather(*(self.api.get_team_members(t.id) for t in teams))
            available = tuple(t for t, members in zip(teams, memberships, strict=True) if members)
            decision = await self.router.choose(history, available, default.id)
            selected = next((t for t in available if t.id == decision.team_id), None)
        selected = selected or default
        # Re-read after the network/model call. A human takeover or manual
        # transfer always wins over a decision based on the previous snapshot.
        latest = await self.api.get_conversation(conversation["id"])
        if not can_route(latest):
            return None
        if team_id(latest) != current:
            await self.sync(latest)
            return team_id(latest)
        # A retry uses the same key, even if the note was created before a lost response.
        notice = self._notice(selected, f"handoff:{message_id}")
        await self.api.set_custom_attributes(conversation["id"], {
            "routing_team_id": selected.id,
            "routing_message_id": message_id,
            "routing_previous_team_id": current,
            "routing_assignment_pending": current != selected.id,
            "routing_origin": "manual" if manual else "auto",
            "routing_last_decision": {**decision.audit, "team_id": selected.id},
            "routing_notice": notice,
        })
        if current != selected.id:
            await self.api.assign_team(conversation["id"], selected.id)
            await self.api.set_custom_attributes(conversation["id"], {
                "routing_assignment_pending": False,
            })
        await self._flush(conversation["id"], {"routing_notice": notice})
        return selected.id
