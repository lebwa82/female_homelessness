"""Render the aggregate from internal_metrics.sql as an offline internal report."""

from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape

_COHORTS = ("all", "native")
_CERTIFICATES = (
    ("food", "Продукты"),
    ("children", "Товары для детей"),
    ("hygiene", "Лекарства / гигиена"),
    ("hostel", "Оплата хостела"),
)
_SERVICES = (
    ("legal", "Консультация юриста"),
    ("psychologist", "Консультация психолога"),
    ("peer", "Разговор с равной консультанткой"),
)


@dataclass(frozen=True)
class _Metric:
    key: str
    title: str
    note: str
    categories: tuple[tuple[str, str], ...] = ()


_SECTIONS = (
    (
        "Начало обращения",
        (
            _Metric(
                "started",
                "Начали общение",
                "В истории есть команда /start. Повторные запуски не увеличивают число.",
            ),
            _Metric(
                "menu_answered",
                "Нажали кнопку в меню выбора потребности",
                "Нижняя граница: учитываем кнопки выбора потребности и другие нажатия, "
                "связь которых с этим меню сохранилась.",
            ),
        ),
    ),
    (
        "Выбор и подтверждённая помощь",
        (
            _Metric(
                "certificate_selected",
                "Выбрали сертификат",
                "Есть нажатие на конкретную категорию. Выбор не означает выдачу.",
                _CERTIFICATES,
            ),
            _Metric(
                "service_selected",
                "Выбрали консультацию",
                "Выбрали услугу или подтвердили запрос. Это не запись на время "
                "и не факт оказанной консультации.",
                _SERVICES,
            ),
            _Metric(
                "certificate_sent",
                "Подтверждена отправка сертификата",
                "Telegram принял отправку; прочтение и использование не подтверждены. "
                "Часть исторических PDF не имеет точной категории.",
                (*_CERTIFICATES, ("unknown", "Категория не определена")),
            ),
            _Metric(
                "service_completed",
                "Подтверждено завершение консультации",
                "Есть явная отметка о завершении с датой и подтверждающим сообщением. "
                "Без неё консультация не учитывается, даже если разговор закрыт.",
                _SERVICES,
            ),
        ),
    ),
    (
        "Опросы о результате",
        (
            _Metric(
                "followup_sent",
                "Подтверждена отправка опроса или напоминания",
                "Telegram принял отправку. Напоминания о бездействии в меню не учитываем.",
                (
                    ("checkin", "Основной опрос о результате"),
                    ("reminder", "Напоминание об опросе"),
                    ("consultation", "Опрос после консультации"),
                ),
            ),
            _Metric(
                "followup_replies",
                "Ответили на вопросы о результате",
                "«Пока не получилось воспользоваться» включает и отсутствие связи "
                "со специалисткой. Молчание не говорит об изменении состояния.",
                (
                    ("better", "Стало легче"),
                    ("same", "Пока так же"),
                    ("worse", "Стало тяжелее"),
                    ("not_used", "Пока не получилось воспользоваться"),
                ),
            ),
        ),
    ),
)
_CATEGORIES = {
    metric.key: {"_total", *([key for key, _ in metric.categories] or ["total"])}
    for _, metrics in _SECTIONS
    for metric in metrics
}

_STYLE = """
  :root{color-scheme:light;--ink:#262323;--paper:#fff;--soft:#f3f3f1;
        --muted:#666262;--line:#d8d6d4}
  *{box-sizing:border-box}
  body{margin:0;background:var(--soft);color:var(--ink);
       font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  main{max-width:920px;margin:auto;padding:clamp(16px,4vw,48px)}
  header,footer{padding:clamp(16px,4vw,32px);background:var(--ink);color:var(--paper)}
  h1{font-size:clamp(28px,6vw,54px);line-height:1.08;margin:8px 0 16px}
  h2{font-size:clamp(20px,4vw,28px);margin:0 0 12px;line-height:1.2}
  p{max-width:72ch;margin:0 0 8px}
  p:last-child{margin-bottom:0}
  section{margin:32px 0}
  table{border-collapse:collapse;width:100%;background:var(--paper);overflow-wrap:anywhere}
  th,td{padding:8px clamp(6px,2vw,18px);vertical-align:top}
  th{text-align:left}
  thead{background:var(--ink);color:var(--paper);font-size:.85em}
  thead th:not(:first-child){width:22%;text-align:right}
  td{text-align:right;font-variant-numeric:tabular-nums}
  .metric{border-top:1px solid var(--line)}
  .metric td{font-size:1.35em;font-weight:700}
  .note,.breakdown th{font-size:.88em;color:var(--muted);font-weight:400}
  .note{display:block;margin-top:4px}
  .breakdown th{padding-inline-start:clamp(12px,3vw,34px)}
  footer{font-size:.93em}
  @media print{
    body,header,footer,thead{background:white;color:black}
    main,header,footer{padding:0}
    tr{break-inside:avoid}
  }
"""


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("Expected an ISO timestamp in the metrics snapshot")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Expected an ISO timestamp in the metrics snapshot") from None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Expected a non-negative count in the metrics snapshot")
    return value


def _aggregate_rows(value: object) -> list[dict]:
    if value is None:
        return []  # json_agg returns NULL when there are no rows.
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Expected an array of aggregate rows in the metrics snapshot")
    return value


def _read_counts(snapshot: dict) -> tuple[dict[str, int], dict[tuple[str, str, str], int]]:
    contacts = {}
    for row in _aggregate_rows(snapshot["cohorts"]):
        cohort = row["cohort"]
        if cohort not in _COHORTS or cohort in contacts:
            raise ValueError("Unexpected or duplicate cohort in the metrics snapshot")
        contacts[cohort] = _integer(row["contacts"])
    contacts = {key: contacts.get(key, 0) for key in _COHORTS}
    if contacts["native"] > contacts["all"]:
        raise ValueError("The native cohort exceeds the full cohort")

    counts = {}
    for row in _aggregate_rows(snapshot["counts"]):
        cohort, metric, category = key = (row["cohort"], row["metric"], row["category"])
        if cohort not in _COHORTS or category not in _CATEGORIES.get(metric, ()) or key in counts:
            raise ValueError("Unexpected or duplicate count in the metrics snapshot")
        counts[key] = _integer(row["n"])
    for (cohort, metric, category), n in counts.items():
        if not n <= counts.get((cohort, metric, "_total"), 0) <= contacts[cohort]:
            raise ValueError("A metric count exceeds its total or cohort size")
        if cohort == "native" and n > counts.get(("all", metric, category), 0):
            raise ValueError("A native metric count exceeds the full cohort's count")
    return contacts, counts


def render_html(snapshot: dict) -> str:
    """Render only known aggregates; refuse to present unverified data as a snapshot."""
    if snapshot.get("read_only") != "on" or snapshot.get("scope_verified") is not True:
        raise ValueError("The metrics snapshot did not verify its read-only Telegram scope")
    contacts, counts = _read_counts(snapshot)
    checked_at = _timestamp(snapshot["checked_at"])
    first, last = snapshot["first_message"], snapshot["last_message"]
    if first is None and last is None:
        period = "нет сообщений в выборке"
    else:
        first, last = _timestamp(first), _timestamp(last)
        if first > last:
            raise ValueError("Invalid message period in the metrics snapshot")
        period = f"{first:%d.%m.%Y} — {last:%d.%m.%Y} UTC"
    excluded = _integer(snapshot["excluded_contacts"])
    unconfirmed = _integer(snapshot["quality"]["followup_unconfirmed_contacts"])
    if unconfirmed > contacts["all"]:
        raise ValueError("Unconfirmed follow-up count exceeds the full cohort size")

    sections = []
    for title, metrics in _SECTIONS:
        rows = []
        for metric in metrics:
            prefix = "≥" if metric.key == "menu_answered" else ""
            all_n, native_n = (counts.get((cohort, metric.key, "_total"), 0) for cohort in _COHORTS)
            rows.append(
                f'<tr class="metric" data-metric="{escape(metric.key)}">'
                f'<th scope="row">{escape(metric.title)}'
                f'<span class="note">{escape(metric.note)}</span></th>'
                f"<td>{prefix}{all_n}</td><td>{prefix}{native_n}</td></tr>"
            )
            for category, label in metric.categories:
                all_n, native_n = (
                    counts.get((cohort, metric.key, category), 0) for cohort in _COHORTS
                )
                rows.append(
                    f'<tr class="breakdown"><th scope="row">{escape(label)}</th>'
                    f"<td>{all_n}</td><td>{native_n}</td></tr>"
                )
        sections.append(
            f"<section><h2>{escape(title)}</h2><table>"
            '<thead><tr><th scope="col">Показатель</th>'
            '<th scope="col">Вся история</th>'
            '<th scope="col">Без перенесённых диалогов</th></tr></thead>'
            f"<tbody>{''.join(rows)}</tbody></table></section>"
        )

    return f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="referrer" content="no-referrer">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
  <title>Внутренняя аналитика — Невидимый фонд</title>
  <style>{_STYLE}</style>
</head>
<body><main>
  <header>
    <p>Невидимый фонд / внутренний отчёт</p>
    <h1>Аналитика Telegram-бота</h1>
    <p>Снимок: <time datetime="{escape(checked_at.isoformat())}">
      {checked_at:%d.%m.%Y %H:%M} UTC</time></p>
    <p>Период сообщений: {escape(period)}</p>
  </header>
  <section>
    <h2>Какие контакты считаем</h2>
    <p>Вся история — <strong>{contacts["all"]}</strong>.
      Без перенесённых диалогов — <strong>{contacts["native"]}</strong>.
      Исключено известных тестовых контактов: {excluded}.</p>
    <p>Во второй группе нет контактов с импортированными сообщениями, даже если они общались
      с ботом после переноса. Часть событий в перенесённой истории не сохранилась.</p>
    <p>Единица учёта — уникальный контакт Chatwoot. Категории могут пересекаться;
      их сумма не равна числу уникальных контактов. Показатели не образуют воронку.</p>
  </section>
  {"".join(sections)}
  <footer>
    <p>Отметки об отправке опроса без подтверждения Telegram не засчитаны:
      контактов с такими отметками — {unconfirmed}.</p>
    <p>Источник — снимок рабочей базы Chatwoot. Здесь только агрегаты без переписки,
      идентификаторов контактов и реквизитов сертификатов. Внутренние значения не для публикации.</p>
  </footer>
</main></body>
</html>
"""
