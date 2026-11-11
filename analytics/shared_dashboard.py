"""Refresh the aggregate data in the published offline dashboard without rebuilding it."""

import json
from html.parser import HTMLParser
from urllib.parse import quote, unquote
from zoneinfo import ZoneInfo

from analytics.internal_html import _COHORTS, _SECTIONS, _integer, _read_counts, _timestamp

_SCRIPT = '<script id="metrics-data" type="application/json">'
_DOWNLOAD = '<a id="download-data" href="data:application/json;charset=utf-8,'
_DOWNLOAD_END = '" download="nevidima-metrics-all.json">'
_EXCEL = '<a id="download-excel" class="pill" href="https://s3.twcstorage.ru/'
_EXCEL_END = '" rel="noreferrer" referrerpolicy="no-referrer">'
_MARKERS = {
    "metrics-data": ("id", "type"),
    "download-data": ("id", "href", "download"),
    "download-excel": ("id", "class", "href", "rel", "referrerpolicy"),
}
_MSK = ZoneInfo("Europe/Moscow")
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
_MAX_JS_INTEGER = 2**53 - 1
_METRICS = {metric.key: metric for _, metrics in _SECTIONS for metric in metrics}
_SCRIPT_ESCAPES = {ord(c): f"\\u{ord(c):04x}" for c in "<>&\u2028\u2029"}


class _DashboardMarkers(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tags: dict[str, str] = {}

    def handle_starttag(self, tag, attrs) -> None:
        for name, value in attrs:
            if name == "id" and value in _MARKERS:
                if value in self.tags or tuple(key for key, _ in attrs) != _MARKERS[value]:
                    raise ValueError("Duplicate or malformed shared dashboard marker")
                self.tags[value] = self.get_starttag_text()


def _read_dashboard(html: str) -> tuple[str, str, dict]:
    parser = _DashboardMarkers()
    parser.feed(html)
    parser.close()
    tags = parser.tags
    download = tags.get("download-data", "")
    excel = tags.get("download-excel", "")
    if (
        tags.get("metrics-data") != _SCRIPT
        or not download.startswith(_DOWNLOAD)
        or not download.endswith(_DOWNLOAD_END)
        or not excel.startswith(_EXCEL)
        or not excel.endswith(_EXCEL_END)
        or any(html.count(tag) != 1 for tag in tags.values())
    ):
        raise ValueError("Missing or malformed shared dashboard marker")
    workbook_path, _, query = excel[len(_EXCEL) : -len(_EXCEL_END)].partition("?")
    if not workbook_path.endswith("/latest.xlsx") or not query or "#" in workbook_path + query:
        raise ValueError("Malformed shared dashboard workbook link")

    _, _, tail = html.partition(_SCRIPT)
    raw, close, _ = tail.partition("</script>")
    if not close:
        raise ValueError("Missing closing tag for shared dashboard metrics")
    try:
        data = json.loads(raw)
        if data != json.loads(
            unquote(download[len(_DOWNLOAD) : -len(_DOWNLOAD_END)], errors="strict")
        ):
            raise ValueError("Shared dashboard JSON copies do not match")
    except UnicodeError, json.JSONDecodeError:
        raise ValueError("Malformed shared dashboard JSON") from None
    return raw, download, data


def _period_label(snapshot: dict) -> str:
    first, last = snapshot["first_message"], snapshot["last_message"]
    if first is None and last is None:
        return "Вся доступная история: нет сообщений в выборке"
    first, last = _timestamp(first).astimezone(_MSK), _timestamp(last).astimezone(_MSK)
    if first > last:
        raise ValueError("Invalid message period in the metrics snapshot")
    return f"Вся доступная история: {first:%d.%m.%y} — {last:%d.%m.%y}"


def _refresh_payload(previous: dict, snapshot: dict) -> dict:
    if snapshot.get("read_only") != "on" or snapshot.get("scope_verified") is not True:
        raise ValueError("The metrics snapshot did not verify its read-only Telegram scope")
    contacts, counts = _read_counts(snapshot)
    excluded = _integer(snapshot["excluded_contacts"])
    if max(excluded, *contacts.values()) > _MAX_JS_INTEGER:
        raise ValueError("A metrics count exceeds the safe browser integer range")
    checked = _timestamp(snapshot["checked_at"]).astimezone(_MSK)
    meta = {
        "checked_at_label": f"{checked.day} {_MONTHS[checked.month - 1]} {checked.year} г. в "
        f"{checked:%H:%M} МСК",
        "period_label": _period_label(snapshot),
        "scope_note": f"Рабочий Telegram-бот. Исключены известные тестовые контакты: {excluded}. "
        "Перенесённая история включена, но некоторые события в ней не сохранились.",
        "source_note": "Источник — агрегированный снимок рабочей базы Chatwoot. В HTML и JSON "
        "нет переписки, имён, Telegram ID и реквизитов сертификатов.",
    }

    prior_cohorts = previous["cohorts"]
    if not isinstance(prior_cohorts, dict) or prior_cohorts.keys() != set(_COHORTS):
        raise ValueError("Unexpected cohorts in the shared dashboard")
    cohorts = {}
    for key in _COHORTS:
        prior = prior_cohorts[key]
        label, prior_metrics = prior["label"], prior["metrics"]
        if not isinstance(label, str) or not isinstance(prior_metrics, dict):
            raise TypeError("Malformed shared dashboard cohort")
        if prior_metrics.keys() != _METRICS.keys():
            raise ValueError("Unexpected metrics in the shared dashboard")

        metrics = {}
        for metric, definition in _METRICS.items():
            entry = prior_metrics[metric]
            note = entry["note"]
            if not isinstance(note, str):
                raise TypeError("Malformed shared dashboard metric note")
            count = counts.get((key, metric, "_total"), 0)
            item = {"count": count, "status": "observed", "note": note}
            if metric == "menu_answered":
                item["display"] = f"≥{count}"

            if definition.categories:
                rows = entry["breakdown"]
                if not isinstance(rows, list) or [row["key"] for row in rows] != [
                    category for category, _ in definition.categories
                ]:
                    raise ValueError("Unexpected breakdown in the shared dashboard")
                item["breakdown"] = [
                    {
                        "key": category,
                        "label": label,
                        "count": counts.get((key, metric, category), 0),
                        "status": "observed",
                    }
                    for category, label in definition.categories
                ]
            elif "breakdown" in entry:
                raise ValueError("Unexpected breakdown in the shared dashboard")
            metrics[metric] = item
        cohorts[key] = {"label": label, "contacts": contacts[key], "metrics": metrics}
    return {"meta": meta, "cohorts": cohorts}


def render_shared_dashboard(html: str, snapshot: dict) -> str:
    """Update the two JSON copies and refresh text; leave the workbook link and script intact."""
    raw, download, previous = _read_dashboard(html)
    try:
        payload = _refresh_payload(previous, snapshot)
    except KeyError, TypeError:
        raise ValueError("Malformed shared dashboard or metrics snapshot") from None
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    script_data = data.translate(_SCRIPT_ESCAPES)
    markup, _, tail = html.partition(_SCRIPT + raw)
    markup = markup.replace(
        "<p>Страница показывает снимок на указанную дату и не подключается к рабочей базе "
        "при открытии.",
        "<p>Снимок обновляется раз в час. Страница не подключается к рабочей базе при открытии.",
    ).replace(
        '<p id="data-status-note">Переключатель меняет группу внутри одного снимка. '
        "Дата снимка указана вверху; исходные диалоги на этой странице не используются.</p>",
        '<p id="data-status-note">Переключатель меняет группу внутри одного снимка. '
        "Снимок обновляется раз в час; исходные диалоги на этой странице не используются.</p>",
    )
    return (markup + _SCRIPT + script_data + tail).replace(
        download, _DOWNLOAD + quote(data, safe="") + _DOWNLOAD_END, 1
    )
