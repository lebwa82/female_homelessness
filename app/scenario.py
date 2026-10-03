"""HTML copy and explicit, side-effect-aware transitions for the active Chatwoot flow."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.domain import AgentTurn, Choice, ConversationState, NeedKind

SCREENS = json.loads(Path(__file__).with_name("scenario_copy.json").read_text())
NEEDS = dict(zip(("s21", "s22", "s23", "s24", "s25", "s36"), NeedKind, strict=True))
AIDS = {
    "s31": "food_card", "s32": "medicine_card", "s33": "hostel_3_nights",
    "s37": "children_card", "s34": "psychologist_3_sessions",
    "s35": "legal_consultation", "s38": "peer_consultation",
}
AID_SCREENS = {value: key for key, value in AIDS.items()}
CONSULTATIONS = {"s34": "s34b", "s35": "s35b", "s38": "s38b"}
CERTIFICATES = ("food_card", "medicine_card", "hostel_3_nights", "children_card")
CONSULTATION_IDS = ("psychologist_3_sessions", "legal_consultation", "peer_consultation")
TEXT_TARGETS = {"s35d": "s35c", "s36": "s36b", "s511": "s52", "s57": "s57",
                "s6": "s61", "s74": "s75", "i1": "i1"}
HUMAN = Choice(id="human", label="💬 Поговорить с человеком")


def copy_text(screen: str, variant: int = 0) -> str:
    return SCREENS[screen]["texts"][variant]


def variant_for(screen: str, context: dict) -> int:
    aid = context.get("aid_id")
    if screen == "s5":
        if aid in CERTIFICATES:
            return CERTIFICATES.index(aid)
        if context.get("completed"):
            return 7
        return 4 + CONSULTATION_IDS.index(aid)
    if screen == "s51":
        return CERTIFICATES.index(aid) if aid in CERTIFICATES else 0
    if screen in {"s56", "s59"}:
        return int(aid not in CERTIFICATES)
    if screen == "s7":
        return CONSULTATION_IDS.index(aid)
    return 0


def button_indices(screen: str, variant: int) -> range:
    size = len(SCREENS[screen]["buttons"])
    if screen in {"s5", "s59"}:
        return range(variant * 4, variant * 4 + 4)
    if screen in {"s51", "s7"}:
        return range(variant * 2, variant * 2 + 2)
    if screen == "s56":
        return range(variant * 3, variant * 3 + 3)
    if screen == "i9":
        return (range(2), range(2, 6), range(6, 8))[variant]
    return range(size)


def render(screen: str, *, revision: int = 0, context: dict | None = None,
           variant: int | None = None) -> AgentTurn:
    context = context or {}
    variant = variant_for(screen, context) if variant is None else variant
    texts = SCREENS[screen]["texts"]
    body = copy_text(screen, variant)
    if screen in {"s31b", "s32b", "s33b", "s37b"} and "code" not in context:
        body = "Сертификат сохранён в предыдущем сообщении этого чата."
    if screen == "s51":
        body = texts[0] + "\n\n" + texts[variant + 1]
    for placeholder, key in (("[ДАТА]", "expiry_date"), ("[НОМЕР СЕРТИФИКАТА]", "code")):
        body = body.replace(placeholder, str(context.get(key, "указано в сообщении с сертификатом")))
    choices = []
    for index in button_indices(screen, variant):
        button = SCREENS[screen]["buttons"][index]
        label, target = button["label"], button["target"]
        if label == "Вернуться на шаг назад" or target == "i1" and screen != "i1":
            continue
        if context.get("awaiting_text") and label.startswith("Написать"):
            continue
        if screen == "i8" and variant == 1 and index == 0:
            continue
        if screen in {"s57", "i1"} and variant == 1 and label.startswith("Написать"):
            continue
        callback = f"sc:{screen}:{index}:{revision}"
        if screen == "s1":
            callback = "continue" if target == "s2" else "pause"
        elif screen == "s2" and target in NEEDS:
            callback = f"need:{NEEDS[target].value}"
        elif screen in NEEDS and target in AIDS:
            callback = f"aid:{AIDS[target]}"
        elif screen in AIDS and AIDS[screen] in CERTIFICATES:
            callback = "certificate:confirm"
        choices.append(Choice(id=callback, label=label))
    return AgentTurn(text=body, choices=(*choices, HUMAN), audit={"scenario_screen": screen})


class ScenarioFlow:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    async def show(self, record, screen: str, *, variant: int | None = None, **context):
        data = {**record.scenario, **context, "screen": screen, "awaiting_text": False}
        data["variant"] = variant_for(screen, data) if variant is None else variant
        state = ConversationState.SCENARIO.value
        if screen in NEEDS and screen != "s36":
            state = ConversationState.CHOOSING_AID.value
        if screen == "s2":
            state = ConversationState.DISCOVERING_NEED.value
        if screen == "s1":
            data = {"screen": "s1", "variant": 0}
            state = ConversationState.GREETING.value
        if screen == "s1a":
            state = ConversationState.CLOSED.value
        if screen in AIDS and AIDS[screen] in CERTIFICATES:
            state = ConversationState.CERTIFICATE_PREVIEW.value
        need = NEEDS.get(screen)
        await self.store.update(record, state=state, scenario=data,
                                need=need.value if need else record.need,
                                pending_aid_id=AIDS.get(screen))
        return self.current(record)

    def current(self, record):
        data = record.scenario
        return render(data["screen"], revision=record.navigation.get("revision", 0),
                      context=data, variant=data.get("variant", 0))

    async def event(self, record, kind: str, request_key: str, **payload):
        key = hashlib.sha256(f"{record.id}:{request_key}:{kind}".encode()).hexdigest()[:24]
        await self.store.record_action(record, "scenario_event", "completed",
                                      {"key": key, "kind": kind, **payload}, effect_key=key)
        return key

    async def callback(self, record, callback: str, request_key: str) -> AgentTurn | None:
        data = record.scenario
        screen = data.get("screen")
        if callback.startswith("sc:"):
            if not screen or callback not in {c.id for c in self.current(record).choices}:
                return await self.service._state_turn(record)
            index = int(callback.split(":")[2])
            button = SCREENS[screen]["buttons"][index]
            target, label = button["target"], button["label"]
            if data.get("job_key"):
                await self.event(record, "survey_answer", request_key, job_key=data["job_key"],
                                 screen=screen, answer=index, label=label)
            if screen in CONSULTATIONS:
                aid = AIDS[screen]
                key = await self.event(record, "consultation_requested",
                                       data.get("draft_key", request_key), aid_id=aid)
                return await self.show(record, target, aid_id=aid, request_key=key)
            if screen == "s35b" and target == "s35c":
                await self.event(record, "legal_topic", request_key,
                                 request_id=data.get("request_key"), answer=label)
                return await self.show(record, target, variant=int(label == "Пропустить"))
            if screen in TEXT_TARGETS and label.startswith("Написать"):
                await self.store.update(record, scenario={**data, "awaiting_text": True})
                return self.current(record)
            if screen == "i8" and index == 0:
                await self.event(record, "certificate_waitlist", request_key, aid_id=data.get("aid_id"))
                return await self.show(record, "i8", variant=1, submitted=True)
            if target == "s561":
                await self.event(record, "consultation_retry", request_key,
                                 request_id=data.get("request_key"), aid_id=data.get("aid_id"))
            if target == "s721":
                await self.event(record, "negative_feedback", request_key,
                                 request_id=data.get("request_key"), job_key=data.get("job_key"))
            if screen == "s35d" and label == "Пропустить":
                return await self.show(record, "s35c", variant=1)
            if target == "s6" and data.get("consultation_reviewed"):
                target = "i2"
            if target == "i2":
                await self.event(record, "survey_closed", request_key, job_key=data.get("job_key"))
                return await self.show(record, target, variant=int(not data.get("aid_id")))
            return await self.show(record, target, **(
                {"draft_key": request_key, "job_key": None} if target in AIDS else {}
            ))
        if callback == "continue" and record.state == ConversationState.GREETING.value:
            return await self.show(record, "s2")
        if callback == "pause" and record.state == ConversationState.GREETING.value:
            return await self.show(record, "s1a")
        if callback.startswith("need:") and record.state in {
            "greeting", "discovering_need", "open_conversation", "choosing_aid",
        }:
            target = next((k for k, v in NEEDS.items() if v.value == callback[5:]), None)
            return await self.show(record, target) if target else None
        if callback.startswith("aid:") and record.state == "choosing_aid":
            target = AID_SCREENS.get(callback[4:])
            # No invented service from a stale/unrelated category button.
            if not screen:
                screen = next((k for k, v in NEEDS.items() if v.value == record.need), "s2")
            allowed = {b["target"] for b in SCREENS.get(screen, {}).get("buttons", [])}
            return (await self.show(record, target, draft_key=request_key, job_key=None)
                    if target in allowed else await self.show(record, screen))
        if callback.startswith("extra:"):
            # Migrate already-rendered buttons without creating an immediate request.
            target = {"extra:legal": "s35", "extra:psychologist": "s34"}.get(callback)
            if target:
                return await self.show(record, target, draft_key=request_key, job_key=None)
        if callback == "support:psychologist" and record.pending_offer == "psychologist":
            return await self.show(record, "s34", draft_key=request_key, job_key=None)
        if callback == "human":
            await self.service._human_turn(record, "button", request_key=request_key)
            return await self.show(record, "i1")
        return None

    async def text(self, record, value: str, request_key: str) -> AgentTurn | None:
        data = record.scenario
        screen = data.get("screen")
        # Explicit fields accept text directly too; clicking 'write' only opens the field.
        if screen not in TEXT_TARGETS or screen == "i1" and data.get("variant") == 1:
            return None
        if screen == "s57" and data.get("variant") == 1:
            return None
        kinds = {"s35d": "legal_topic", "s36": "other_request", "s511": "certificate_usage",
                 "s57": "certificate_problem", "s6": "general_feedback",
                 "s74": "consultation_feedback", "i1": "human_message"}
        await self.event(record, kinds[screen], request_key, text=value,
                         request_id=data.get("request_key"), job_key=data.get("job_key"),
                         aid_id=data.get("aid_id"))
        return await self.show(record, TEXT_TARGETS[screen],
                               variant=1 if screen in {"s57", "i1"} else 0)


def certificate_context(certificate) -> dict:
    received = certificate.issued_at or datetime.now(UTC)
    # Test inventory follows the agreed HTML product contract. Real inventory
    # must satisfy the same supplier terms before external launch.
    expiry = received + timedelta(days=7) if certificate.is_test else certificate.expires_at
    return {"aid_id": certificate.aid_id, "code": certificate.activation_code,
            "expires_at": expiry.isoformat(),
            "expiry_date": expiry.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y")}
