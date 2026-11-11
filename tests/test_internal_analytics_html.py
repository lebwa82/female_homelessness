from html.parser import HTMLParser

import pytest

from analytics.internal_html import render_html

METRICS = (
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
def snapshot():
    return {
        "checked_at": "2026-10-29T01:30:00+03:00",
        "read_only": "on",
        "scope_verified": True,
        "first_message": "2026-10-01T00:00:00",
        "last_message": "2026-10-28T21:00:00",
        "excluded_contacts": 2,
        "cohorts": [{"cohort": "all", "contacts": 12}, {"cohort": "native", "contacts": 8}],
        "counts": [
            {"cohort": cohort, "metric": metric, "category": "_total", "n": total}
            for metric in METRICS
            for cohort, total in [("all", 5), ("native", 3)]
        ]
        + [
            {"cohort": "all", "metric": "started", "category": "total", "n": 5},
            {"cohort": "all", "metric": "certificate_sent", "category": "unknown", "n": 2},
            {"cohort": "native", "metric": "certificate_sent", "category": "unknown", "n": 1},
        ],
        "quality": {"menu_has_only_need_buttons": True, "followup_unconfirmed_contacts": 1},
    }


class _ReportParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_renders_original_aggregate_shape_with_both_cohorts(snapshot):
    report = render_html(snapshot)
    parser = _ReportParser()
    parser.feed(report)

    assert [attrs["data-metric"] for _, attrs in parser.tags if "data-metric" in attrs] == list(
        METRICS
    )
    assert ("time", {"datetime": "2026-10-28T22:30:00+00:00"}) in parser.tags
    assert "28.10.2026 22:30 UTC" in report
    assert "01.10.2026 — 28.10.2026 UTC" in report
    assert "Вся история — <strong>12</strong>" in report
    assert "Без перенесённых диалогов — <strong>8</strong>" in report
    assert "<td>≥5</td><td>≥3</td>" in report
    assert report.count("<td>5</td><td>3</td>") == 7
    assert "Категория не определена</th><td>2</td><td>1</td>" in report
    assert "не засчитаны" in report
    assert "контактов с такими отметками — 1" in report


def test_zero_counts_and_missing_native_cohort(snapshot):
    snapshot.update(counts=None, cohorts=[{"cohort": "all", "contacts": 4}])
    report = render_html(snapshot)
    assert "Без перенесённых диалогов — <strong>0</strong>" in report
    assert "<td>≥0</td><td>≥0</td>" in report
    assert "Категория не определена</th><td>0</td><td>0</td>" in report


def test_verified_empty_snapshot(snapshot):
    snapshot.update(counts=None, cohorts=None, first_message=None, last_message=None)
    snapshot["quality"]["followup_unconfirmed_contacts"] = 0
    assert "Период сообщений: нет сообщений в выборке" in render_html(snapshot)


@pytest.mark.parametrize("field,value", [("read_only", "off"), ("scope_verified", False)])
def test_unverified_snapshot_is_not_rendered(snapshot, field, value):
    snapshot[field] = value
    with pytest.raises(ValueError, match="read-only Telegram scope"):
        render_html(snapshot)


@pytest.mark.parametrize("n", [13, -1, True, "5", "<img onerror=alert(1)>"])
def test_bad_counts_are_rejected(snapshot, n):
    snapshot["counts"][0]["n"] = n
    with pytest.raises(ValueError):
        render_html(snapshot)


def test_native_must_be_a_subset(snapshot):
    snapshot["counts"][1]["n"] = 6
    with pytest.raises(ValueError, match="exceeds the full cohort"):
        render_html(snapshot)


def test_offline_report_does_not_embed_unknown_snapshot_fields(snapshot):
    extra = '</script><img src="https://example.invalid/pixel" onerror="alert(1)">'
    snapshot["extra"] = extra
    snapshot["counts"][0]["extra"] = extra
    report = render_html(snapshot)
    parser = _ReportParser()
    parser.feed(report)
    assert "example.invalid" not in report
    assert "default-src 'none'" in report
    assert all(
        tag not in {"script", "link", "iframe", "object", "img", "form"}
        and not {"src", "href"} & attrs.keys()
        for tag, attrs in parser.tags
    )
