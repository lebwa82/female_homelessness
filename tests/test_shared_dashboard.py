import json
from urllib.parse import quote, unquote

import pytest

from analytics.internal_html import _SECTIONS
from analytics.shared_dashboard import render_shared_dashboard

_SCRIPT = '<script id="metrics-data" type="application/json">'
_DOWNLOAD = '<a id="download-data" href="data:application/json;charset=utf-8,'
_EXCEL = (
    '<a id="download-excel" class="pill" href="https://s3.twcstorage.ru/fake-bucket/'
    "team-share/test/latest.xlsx?X-Amz-Credential=fixture%2Fscope&amp;"
    'X-Amz-Signature=not-a-credential" rel="noreferrer" referrerpolicy="no-referrer">'
)
_METHOD = (
    "Страница показывает снимок на указанную дату и не подключается к рабочей базе при открытии."
)
_STATUS = "Дата снимка указана вверху; исходные диалоги на этой странице не используются."
_METRICS = (
    "started",
    "menu_answered",
    "certificate_selected",
    "service_selected",
    "certificate_sent",
    "service_completed",
    "followup_sent",
    "followup_replies",
)


@pytest.fixture
def previous():
    return {
        "meta": {"checked_at_label": "Устаревшая дата"},
        "cohorts": {
            cohort: {
                "label": label,
                "contacts": 999,
                "metrics": {
                    metric.key: {
                        "count": 99,
                        "status": "observed",
                        "note": metric.note,
                        **(
                            {
                                "breakdown": [
                                    {"key": key, "label": label, "count": 99, "status": "observed"}
                                    for key, label in metric.categories
                                ]
                            }
                            if metric.categories
                            else {}
                        ),
                    }
                    for _, metrics in _SECTIONS
                    for metric in metrics
                },
            }
            for cohort, label in [("all", "Вся история"), ("native", "Без перенесённых диалогов")]
        },
    }


def _html(previous):
    data = json.dumps(previous, ensure_ascii=False)
    return (
        "<!doctype html>\r\n"
        '<meta http-equiv="Content-Security-Policy" content="script-src \'sha256-fixture\'">'
        "<style>@font-face{src:url(data:font/woff;base64,FAKEFONT)}</style>"
        '<img src="data:image/svg+xml;base64,FAKELOGO">'
        f"{_EXCEL}Скачать Excel</a><p>{_METHOD}</p>"
        f'<p id="data-status-note">Переключатель меняет группу внутри одного снимка. {_STATUS}</p>'
        f'{_DOWNLOAD}{quote(data, safe="")}" download="nevidima-metrics-all.json">JSON</a>'
        f"{_SCRIPT}{data.replace('<', r'\u003c')}</script>"
        "<script>const source = JSON.parse(document.getElementById('metrics-data').textContent);"
        "\r\n</script>"
    )


def _json_copies(html):
    embedded = html.split(_SCRIPT, 1)[1].split("</script>", 1)[0]
    fallback = html.split(_DOWNLOAD, 1)[1].split('"', 1)[0]
    return embedded, fallback


@pytest.fixture
def snapshot():
    return {
        "checked_at": "2026-11-12T22:15:00+00:00",
        "read_only": "on",
        "scope_verified": True,
        "first_message": "2026-10-01T21:00:00",
        "last_message": "2026-11-12T21:00:00+00:00",
        "excluded_contacts": 2,
        "cohorts": [{"cohort": "all", "contacts": 12}, {"cohort": "native", "contacts": 8}],
        "counts": [
            {"cohort": cohort, "metric": metric, "category": "_total", "n": total}
            for metric in _METRICS
            for cohort, total in [("all", 5), ("native", 3)]
        ]
        + [{"cohort": "all", "metric": "certificate_sent", "category": "unknown", "n": 2}],
    }


def test_updates_both_json_copies_and_only_intended_html(previous, snapshot):
    before = _html(previous)
    after = render_shared_dashboard(before, snapshot)
    old_data, old_href = _json_copies(before)
    new_data, new_href = _json_copies(after)
    data = json.loads(new_data)
    assert data == json.loads(unquote(new_href))
    assert data["meta"]["checked_at_label"] == "13 ноября 2026 г. в 01:15 МСК"
    assert data["meta"]["period_label"] == "Вся доступная история: 02.10.26 — 13.11.26"
    assert "тестовые контакты: 2" in data["meta"]["scope_note"]
    assert list(data["cohorts"]) == ["all", "native"]
    for cohort, contacts, total in [("all", 12, 5), ("native", 8, 3)]:
        group = data["cohorts"][cohort]
        assert group["contacts"] == contacts
        assert tuple(group["metrics"]) == _METRICS
        assert all(metric["count"] == total for metric in group["metrics"].values())
        assert group["metrics"]["menu_answered"]["display"] == f"≥{total}"
        unknown = group["metrics"]["certificate_sent"]["breakdown"][-1]
        assert unknown == {
            "key": "unknown",
            "label": "Категория не определена",
            "count": 2 if cohort == "all" else 0,
            "status": "observed",
        }
    assert _EXCEL in after
    assert (
        before.replace(old_data, new_data)
        .replace(old_href, new_href)
        .replace(
            _METHOD,
            "Снимок обновляется раз в час. Страница не подключается к рабочей базе при открытии.",
        )
        .replace(
            _STATUS,
            "Снимок обновляется раз в час; исходные диалоги на этой странице не используются.",
        )
        == after
    )
    assert render_shared_dashboard(after, snapshot) == after


def test_json_script_cannot_break_out_and_snapshot_extra_fields_are_not_exported(
    previous, snapshot
):
    note = '</script><img src="https://example.invalid/pixel" onerror="alert(1)"> & \u2028\u2029'
    note += _STATUS
    previous["cohorts"]["all"]["metrics"]["started"]["note"] = note
    snapshot["extra"] = "not-approved-snapshot-field"
    snapshot["counts"][0]["extra"] = "not-approved-count-field"
    after = render_shared_dashboard(_html(previous), snapshot)
    data, fallback = _json_copies(after)
    assert note == json.loads(data)["cohorts"]["all"]["metrics"]["started"]["note"]
    assert json.loads(data) == json.loads(unquote(fallback))
    assert not any(char in data for char in "<>&\u2028\u2029")
    assert "not-approved-" not in after
    assert after.count("</script>") == 2


def test_zero_counts_and_empty_sql_aggregate(previous, snapshot):
    snapshot.update(cohorts=None, counts=None, first_message=None, last_message=None)
    data = json.loads(_json_copies(render_shared_dashboard(_html(previous), snapshot))[0])
    assert data["meta"]["period_label"] == "Вся доступная история: нет сообщений в выборке"
    for group in data["cohorts"].values():
        assert group["contacts"] == 0
        assert all(metric["count"] == 0 for metric in group["metrics"].values())


@pytest.mark.parametrize("n", [True, -1, 6, 2**53])
def test_invalid_breakdown_counts_are_rejected(previous, snapshot, n):
    snapshot["counts"][-1]["n"] = n
    with pytest.raises(ValueError):
        render_shared_dashboard(_html(previous), snapshot)


def test_browser_count_limit(previous, snapshot):
    snapshot["cohorts"][0]["contacts"] = 2**53
    with pytest.raises(ValueError, match="safe browser integer range"):
        render_shared_dashboard(_html(previous), snapshot)


def test_unverified_scope_and_invalid_period(previous, snapshot):
    with pytest.raises(ValueError, match="did not verify"):
        render_shared_dashboard(_html(previous), {**snapshot, "scope_verified": False})
    with pytest.raises(ValueError, match="Invalid message period"):
        render_shared_dashboard(
            _html(previous), {**snapshot, "last_message": "2026-09-30T00:00:00"}
        )


@pytest.mark.parametrize(
    "alter",
    [
        lambda html: html.replace(_SCRIPT, '<script id="wrong-data" type="application/json">'),
        lambda html: html.replace(_SCRIPT, "<!--" + _SCRIPT, 1) + "-->",
        lambda html: html + "<div id = 'metrics-data'></div>",
        lambda html: html.replace("</script>", "</sCriPt>", 1),
        lambda html: html.replace(
            _DOWNLOAD, '<a id="download-data" href="https://example.invalid/'
        ),
        lambda html: html.replace(_DOWNLOAD + "%7B", _DOWNLOAD + "%00"),
        lambda html: html.replace(_EXCEL, _EXCEL.replace("latest.xlsx", "other.xlsx")),
        lambda html: html.replace(_EXCEL, ""),
    ],
)
def test_rejects_missing_duplicate_or_malformed_markers(previous, snapshot, alter):
    with pytest.raises(ValueError):
        render_shared_dashboard(alter(_html(previous)), snapshot)


@pytest.mark.parametrize("missing", ["cohort", "metric", "breakdown"])
def test_rejects_incomplete_prior_dashboard(previous, snapshot, missing):
    if missing == "cohort":
        del previous["cohorts"]["native"]
    elif missing == "metric":
        del previous["cohorts"]["all"]["metrics"]["started"]
    else:
        previous["cohorts"]["native"]["metrics"]["certificate_sent"]["breakdown"].pop()
    with pytest.raises(ValueError):
        render_shared_dashboard(_html(previous), snapshot)
