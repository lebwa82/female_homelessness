"""Pure incident transitions; no network, filesystem, or sensitive payloads."""

import math
from datetime import datetime, timedelta, timezone

CHECKS = ("telegram_poll", "telegram_proxy", "agent", "queues", "host")
FAILURE_THRESHOLDS = dict(zip(CHECKS, (1, 2, 2, 2, 1), strict=True))
REMINDER_SECONDS = 1800
REASONS = frozenset(
    {
        "ok",
        "poll_stale",
        "poll_missing",
        "poll_invalid",
        "redis_unavailable",
        "proxy_unavailable",
        "telegram_unavailable",
        "telegram_identity_mismatch",
        "agent_unavailable",
        "queue_stalled",
        "queue_metadata_invalid",
        "queue_group_missing",
        "observation_failed",
        "container_stopped",
        "tunnel_unavailable",
        "host_ok",
        "probe_failed",
        "probe_timeout",
        "configuration_error",
    }
)
_CHECK_FIELDS = {
    "ok",
    "reason",
    "failures",
    "successes",
    "pending_since",
    "incident_sequence",
    "event_id",
    "started_at",
    "last_alert_at",
}


def _timestamp(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False


def _initial_check() -> dict:
    return {
        "ok": None,
        "reason": "observation_failed",
        "failures": 0,
        "successes": 0,
        "pending_since": None,
        "incident_sequence": 0,
        "event_id": None,
        "started_at": None,
        "last_alert_at": None,
    }


def _event_id(check: str, sequence: int, started_at: float) -> str:
    return f"{check}:{sequence}:{float(started_at).hex()}"


def _validated_copy(previous: dict, now: float) -> dict:
    """Reject damaged state rather than accidentally resetting an active incident."""
    if not isinstance(previous, dict):
        raise ValueError("monitor state must be a mapping")  # noqa: TRY004 - persistence contract
    if not previous:
        return {"version": 1, "updated_at": now, "checks": {c: _initial_check() for c in CHECKS}}
    if set(previous) != {"version", "updated_at", "checks"}:
        raise ValueError("invalid monitor state fields")
    if type(previous["version"]) is not int or previous["version"] != 1:
        raise ValueError("unsupported monitor state version")
    updated_at = previous["updated_at"]
    if not _timestamp(updated_at) or updated_at > now:
        raise ValueError("invalid monitor state timestamp")
    checks = previous["checks"]
    if not isinstance(checks, dict) or set(checks) != set(CHECKS):
        raise ValueError("invalid monitor state checks")
    copied = {}
    for check in CHECKS:
        item = checks[check]
        if not isinstance(item, dict) or set(item) != _CHECK_FIELDS:
            raise ValueError("invalid monitor check fields")
        if item["ok"] is not None and type(item["ok"]) is not bool:
            raise ValueError("invalid monitor status")
        if not isinstance(item["reason"], str) or item["reason"] not in REASONS:
            raise ValueError("invalid monitor reason")
        for field in ("failures", "successes", "incident_sequence"):
            if type(item[field]) is not int or item[field] < 0:
                raise ValueError("invalid monitor counter")
        failures, successes = item["failures"], item["successes"]
        if failures and successes:
            raise ValueError("conflicting monitor streaks")
        for field in ("pending_since", "started_at", "last_alert_at"):
            value = item[field]
            if value is not None and (not _timestamp(value) or value > updated_at):
                raise ValueError("invalid monitor check timestamp")
        if (item["pending_since"] is None) != (failures == 0):
            raise ValueError("invalid pending monitor failure")
        if item["ok"] is False:
            if (
                item["started_at"] is None
                or item["last_alert_at"] is None
                or item["incident_sequence"] < 1
                or item["event_id"]
                != _event_id(check, item["incident_sequence"], item["started_at"])
                or item["last_alert_at"] < item["started_at"]
                or successes >= 2
            ):
                raise ValueError("invalid active monitor incident")
        elif any(item[field] is not None for field in ("event_id", "started_at", "last_alert_at")):
            raise ValueError("inactive monitor incident has active fields")
        elif failures >= FAILURE_THRESHOLDS[check]:
            raise ValueError("unreported monitor incident")
        copied[check] = dict(item)
    return {"version": 1, "updated_at": now, "checks": copied}


def _observation(observations: dict, check: str) -> tuple[bool, str]:
    item = observations.get(check) if isinstance(observations, dict) else None
    if not isinstance(item, dict) or type(item.get("ok")) is not bool:
        return False, "observation_failed"
    reason = item.get("reason")
    if not isinstance(reason, str) or reason not in REASONS:
        return False, "observation_failed"
    return item["ok"], reason


def _event(check: str, item: dict, kind: str, now: float) -> dict:
    return {
        "check": check,
        "event_id": item["event_id"],
        "kind": kind,
        "reason": "ok" if kind == "recovery" else item["reason"],
        "started_at": item["started_at"],
        "at": now,
        "duration_seconds": now - item["started_at"],
    }


def transition(
    previous: dict, observations: dict[str, dict], now: float
) -> tuple[dict, list[dict], dict]:
    """Advance five checks once, preserving all debounce and incident state.

    Empty state bootstraps a monitor. Unknown status is represented by ``ok=None``;
    heartbeat callers must only send checks whose reported ``ok`` is a bool.
    Poll and queue age limits belong to the collector; these thresholds count
    observations that have already crossed those limits.
    """
    if not _timestamp(now):
        raise ValueError("invalid current monitor time")
    state = _validated_copy(previous, now)
    events = []
    reported = {}
    for check in CHECKS:
        item = state["checks"][check]
        ok, reason = _observation(observations, check)
        if ok:
            item["failures"] = 0
            item["pending_since"] = None
            item["successes"] += 1
            if item["ok"] is False:
                if item["successes"] >= 2:
                    events.append(_event(check, item, "recovery", now))
                    item.update(
                        ok=True, reason=reason, event_id=None, started_at=None, last_alert_at=None
                    )
            else:
                item.update(ok=True, reason=reason)
        else:
            item["successes"] = 0
            if item["failures"] == 0:
                item["pending_since"] = now
            item["failures"] += 1
            if item["ok"] is False:
                item["reason"] = reason
            elif item["failures"] >= FAILURE_THRESHOLDS[check]:
                item["incident_sequence"] += 1
                item.update(
                    ok=False,
                    reason=reason,
                    started_at=item["pending_since"],
                    last_alert_at=now,
                    event_id=_event_id(check, item["incident_sequence"], item["pending_since"]),
                )
                events.append(_event(check, item, "down", now))
            elif item["ok"] is None:
                item["reason"] = reason
        if item["ok"] is False and now - item["last_alert_at"] >= REMINDER_SECONDS:
            events.append(_event(check, item, "reminder", now))
            item["last_alert_at"] = now
        reported[check] = {"ok": item["ok"], "reason": item["reason"]}
    return state, events, reported


_CHECK_LABELS = {
    "telegram_poll": "Приём Telegram",
    "telegram_proxy": "Соединение Telegram",
    "agent": "Агент",
    "queues": "Очереди",
    "host": "Сервер",
}
_KIND_LABELS = {"down": "Сбой", "reminder": "Сбой продолжается", "recovery": "Восстановлено"}
_REASON_LABELS = {
    "ok": "Работа восстановлена",
    "poll_stale": "Давно нет успешного опроса Telegram",
    "poll_missing": "Нет отметки опроса Telegram",
    "poll_invalid": "Некорректная отметка опроса Telegram",
    "redis_unavailable": "Хранилище очередей недоступно",
    "proxy_unavailable": "Прокси недоступен",
    "telegram_unavailable": "Telegram недоступен",
    "telegram_identity_mismatch": "Не совпадает идентификатор бота",
    "agent_unavailable": "Агент недоступен",
    "queue_stalled": "Обработка очередей задерживается",
    "queue_metadata_invalid": "Некорректные данные очередей",
    "queue_group_missing": "Не найдена группа обработки очереди",
    "observation_failed": "Не удалось получить результат проверки",
    "container_stopped": "Контейнер остановлен",
    "tunnel_unavailable": "Туннель прокси недоступен",
    "host_ok": "Сервер доступен",
    "probe_failed": "Не удалось выполнить проверку",
    "probe_timeout": "Превышено время проверки",
    "configuration_error": "Ошибка настройки проверки",
}
_MOSCOW_TIME = timezone(timedelta(hours=3))


def format_telegram_event(event: dict, dashboard_url: str) -> str:
    """Return concise plain text. Telegram requests must omit parse_mode."""
    if not isinstance(event, dict):
        raise ValueError("invalid monitor event")  # noqa: TRY004 - event validation contract
    check, kind = event.get("check"), event.get("kind")
    reason = event.get("reason")
    if (
        check not in CHECKS
        or not isinstance(kind, str)
        or kind not in _KIND_LABELS
        or not isinstance(reason, str)
        or reason not in REASONS
    ):
        raise ValueError("invalid monitor event")
    started_at, at = event.get("started_at"), event.get("at")
    if not _timestamp(started_at) or not _timestamp(at) or at < started_at:
        raise ValueError("invalid monitor event timestamps")
    duration = event.get("duration_seconds")
    if not _timestamp(duration) or duration != at - started_at:
        raise ValueError("invalid monitor event duration")
    try:
        start_text = datetime.fromtimestamp(started_at, _MOSCOW_TIME).strftime("%d.%m.%Y %H:%M")
    except (OSError, OverflowError, ValueError):
        raise ValueError("invalid monitor event timestamps") from None
    # Plain text prevents Telegram markup interpretation; strip line-control
    # characters from the configured dashboard link to preserve message shape.
    url = " ".join(str(dashboard_url).split())
    return (
        f"{_KIND_LABELS[kind]}: {_CHECK_LABELS[check]}\n"
        f"Причина: {_REASON_LABELS[reason]}\nНачало: {start_text} МСК\n"
        f"Длительность: {int(duration)} с\n{url}"
    )
