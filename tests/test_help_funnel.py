import json

import pytest

from analytics.help_funnel import EVENT_FIELDS, METRIC_VERSION, ReportInputError, build_report, main


def row(key="anonymous-a", **changes):
    return {
        "contact_key": key,
        "imported": False,
        "excluded": False,
        "message_count": 1,
        **{name: [] for name in EVENT_FIELDS},
        **changes,
    }


def moment(hour):
    return f"2026-10-24T{hour:02}:00:00Z"


def test_empty_report_has_null_rates_and_no_contacts():
    report = build_report([])
    assert report["metric_version"] == METRIC_VERSION
    assert report["measurement"] == "observed_strict"
    assert report["counts"] == {
        "input_rows": 0, "included_contacts": 0, "excluded_contacts": 0,
    }
    for cohort in report["cohorts"].values():
        assert cohort["contacts"] == 0
        for metric in ("h2", "h3"):
            assert cohort[metric]["conversion_pct"] is None
            assert cohort[metric]["unknown"] == {"outcome_contacts": 0, "entry_contacts": 0}
        assert all(cohort["subset_checks"].values())


def test_h2_counts_unique_contacts_only_when_continue_is_strictly_after_start():
    rows = [
        row("converted", starts=[moment(3), moment(1)], continues=[moment(2), moment(2)]),
        row("before", starts=[moment(2)], continues=[moment(1)]),
        row("equal", starts=[moment(1)], continues=[moment(1)]),
        row("missing", starts=[moment(1)]),
        row("no-start", continues=[moment(2)]),
    ]
    cohort = build_report(rows)["cohorts"]["all"]
    assert cohort["h2"] == {
        "denominator_contacts": 4,
        "numerator_contacts": 1,
        "conversion_pct": 25.0,
        "unknown": {"outcome_contacts": 3, "entry_contacts": 1},
    }
    assert cohort["coverage"]["continues_without_prior_observed_start_contacts"] == 3


def test_h3_denominator_is_answered_menu_not_shown_menu_or_selected_aid():
    rows = [
        row("certificate", menu_shown=[moment(1)], menu_answers=[moment(2)],
            aid_selected=[moment(3)], certificate_delivered=[moment(4)]),
        row("consultation", menu_shown=[moment(1)], menu_answers=[moment(2)],
            consultation_completed=[moment(3)]),
        row("both", menu_answers=[moment(2)], certificate_delivered=[moment(3)],
            consultation_completed=[moment(4)]),
        row("unobserved", menu_answers=[moment(2)]),
        row("shown-only", menu_shown=[moment(1)], certificate_delivered=[moment(3)]),
        row("selected-only", aid_selected=[moment(3)], consultation_completed=[moment(4)]),
    ]
    cohort = build_report(rows)["cohorts"]["all"]
    assert cohort["h3"] == {
        "denominator_contacts": 4,
        "numerator_contacts": 3,
        "conversion_pct": 75.0,
        "unknown": {"outcome_contacts": 1, "entry_contacts": 2},
        "certificate_after_answer_contacts": 2,
        "consultation_after_answer_contacts": 2,
        "both_outcomes_after_answer_contacts": 1,
    }
    assert cohort["coverage"]["menu_shown_contacts"] == 3
    assert cohort["coverage"]["aid_selected_contacts"] == 2
    assert cohort["coverage"]["aid_selected_after_menu_answer_contacts"] == 1
    assert cohort["coverage"]["menu_answers_without_observed_menu_contacts"] == 2
    assert all(cohort["subset_checks"].values())


def test_h3_requires_order_when_menu_is_observed_and_never_counts_prior_help():
    rows = [
        row("before-menu", menu_shown=[moment(3)], menu_answers=[moment(2)],
            certificate_delivered=[moment(4)]),
        row("equal-menu", menu_shown=[moment(2)], menu_answers=[moment(2)],
            certificate_delivered=[moment(4)]),
        row("prior-help", menu_shown=[moment(1)], menu_answers=[moment(3)],
            consultation_completed=[moment(2)]),
        row("equal-help", menu_answers=[moment(3)], certificate_delivered=[moment(3)]),
        row("later-valid-answer", menu_shown=[moment(4), moment(2)],
            menu_answers=[moment(1), moment(3)], certificate_delivered=[moment(5)]),
    ]
    cohort = build_report(rows)["cohorts"]["all"]
    assert cohort["h3"]["denominator_contacts"] == 3
    assert cohort["h3"]["numerator_contacts"] == 1
    assert cohort["h3"]["conversion_pct"] == 33.33
    assert cohort["coverage"]["menu_answers_without_prior_observed_menu_contacts"] == 2


def test_merge_is_order_independent_deduplicates_events_and_uses_imported_or():
    rows = [
        row("shared", starts=[moment(1)], menu_shown=[moment(1)], message_count=0),
        row("shared", imported=True, continues=[moment(2)], menu_answers=[moment(2)]),
        row("shared", certificate_delivered=[moment(3), moment(3)]),
        row("native", starts=[moment(1)]),
    ]
    report = build_report(rows)
    assert report == build_report(list(reversed(rows)))
    assert report["cohorts"] == build_report(rows + rows)["cohorts"]
    assert report["counts"]["included_contacts"] == 2
    assert report["cohorts"]["native"]["contacts"] == 1
    assert report["cohorts"]["imported"]["contacts"] == 1
    assert report["cohorts"]["imported"]["h2"]["numerator_contacts"] == 1
    assert report["cohorts"]["imported"]["h3"]["numerator_contacts"] == 1
    assert report["cohorts"]["all"]["coverage"]["contacts_with_messages"] == 2
    assert report["subset_checks"]["cohorts_partition_all"]
    for metric in ("h2", "h3"):
        for count in ("numerator_contacts", "denominator_contacts"):
            assert report["cohorts"]["all"][metric][count] == sum(
                report["cohorts"][name][metric][count] for name in ("native", "imported")
            )


def test_any_excluded_row_excludes_whole_contact_in_every_cohort():
    rows = [
        row("excluded", starts=[moment(1)], continues=[moment(2)],
            menu_answers=[moment(2)], consultation_completed=[moment(3)]),
        row("excluded", imported=True, excluded=True),
        row("included", message_count=0),
    ]
    report = build_report(rows)
    assert report["counts"]["excluded_contacts"] == 1
    assert report["cohorts"]["all"]["contacts"] == 1
    assert report["cohorts"]["native"]["contacts"] == 1
    assert report["cohorts"]["imported"]["contacts"] == 0
    assert report["cohorts"]["all"]["coverage"]["contacts_with_messages"] == 0
    assert report["cohorts"]["all"]["h2"]["conversion_pct"] is None


def test_timestamps_are_normalized_to_utc_and_preserve_microseconds():
    rows = [
        row("same-instant", starts=["2026-10-24T03:00:00+03:00"],
            continues=["2026-10-23T20:00:00-04:00"]),
        row("later", starts=["2026-10-24T03:00:00.000001+03:00"],
            continues=["2026-10-24T00:00:00.000002Z"]),
        row("different-date", starts=["2026-10-23T23:59:59Z"],
            continues=["2026-10-24T00:00:00+00:00"]),
    ]
    h2 = build_report(rows)["cohorts"]["all"]["h2"]
    assert h2["denominator_contacts"] == 3
    assert h2["numerator_contacts"] == 2


@pytest.mark.parametrize("value", [
    "2026-10-24", "2026-10-24T01:00:00", "2026-02-30T01:00:00Z",
    "2026-10-24T25:00:00Z", "2026-10-24T01:00:00+25:00",
    "2026-10-24T01:00:00+03:70", "2026-10-24!01:00:00Z", "untrusted-value",
    "2026-10-24T01:00:00.0000001Z", None, 123, True,
])
def test_invalid_or_naive_timestamps_fail_without_echoing_input(value):
    with pytest.raises(ReportInputError, match="^invalid_timestamp$"):
        build_report([row(starts=[value])])


@pytest.mark.parametrize("changes", [
    {"contact_key": ""}, {"contact_key": 123}, {"imported": 1}, {"excluded": "false"},
    {"message_count": -1}, {"message_count": True}, {"message_count": 1.5},
    {"starts": None}, {"menu_answers": "2026-10-24T00:00:00Z"}, {"raw_text": "not-allowed"},
])
def test_invalid_rows_are_rejected(changes):
    with pytest.raises(ReportInputError):
        build_report([row(**changes)])


def test_missing_fields_and_non_array_roots_are_rejected():
    incomplete = row()
    del incomplete["continues"]
    for value in ([incomplete], [None], {"rows": []}, None):
        with pytest.raises(ReportInputError):
            build_report(value)


def test_only_aggregates_are_returned():
    source = row("never-return-this-contact-key", starts=[moment(1)], continues=[moment(2)])
    report = json.dumps(build_report([source]))
    assert source["contact_key"] not in report
    assert moment(1) not in report
    assert moment(2) not in report
    assert "contact_key" not in report


def test_cli_prints_only_aggregates_without_modifying_input(tmp_path, capsys):
    path = tmp_path / "features.json"
    original = json.dumps([row(starts=[moment(1)], continues=[moment(2)])])
    path.write_text(original, encoding="utf-8")
    assert main([str(path)]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["cohorts"]["all"]["h2"]["conversion_pct"] == 100.0
    assert "anonymous-a" not in captured.out
    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("content", ["malformed-json", '[{"contact_key":"private-value"}]'])
def test_cli_errors_do_not_echo_input(tmp_path, capsys, content):
    path = tmp_path / "features.json"
    path.write_text(content, encoding="utf-8")
    assert main([str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "help_funnel: invalid or unreadable feature input\n"


def test_cli_handles_unreadable_file_without_traceback(tmp_path, capsys):
    assert main([str(tmp_path / "missing.json")]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "help_funnel: invalid or unreadable feature input\n"
