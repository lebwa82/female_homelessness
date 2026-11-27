import json
from pathlib import Path

from analytics.dds import catalog as dds_catalog

DDS = Path(__file__).resolve().parents[2] / "analytics" / "dds"
FACTS = {
    "fact_conversation",
    "fact_message",
    "fact_consultation_request",
    "fact_followup",
    "fact_certificate",
    "fact_certificate_request",
    "fact_csat",
}


def test_catalog_covers_the_approved_source_requirements():
    catalog = dds_catalog.load_catalog()
    tables = catalog["tables"]
    assert {name for name in tables if name.startswith("fact_")} == FACTS
    assert [requirement["id"] for requirement in catalog["requirements"]] == list(range(2, 107))
    assert len({item["source_requirement"] for item in catalog["requirements"]}) == 105
    assert {
        36: "conversations.custom_attributes.scenario_requests.<entry_key>",
        43: "conversations.custom_attributes.scenario_followups.<entry_key>",
        85: "women_help_certificate_requests.source_event_keys[*]",
    }.items() <= {
        item["id"]: item["source_requirement"] for item in catalog["requirements"]
    }.items()
    assert dds_catalog.validate_catalog() == []


def test_catalog_is_typed_and_excludes_sensitive_columns():
    tables = dds_catalog.load_catalog()["tables"]
    for table in tables.values():
        columns = table["columns"]
        assert columns["account_id"]["type"] == "bigint"
        assert columns["account_id"]["nullable"] is False
        assert {"row_hash", "loaded_at", "deleted_at"}.isdisjoint(columns)
        assert {
            "content",
            "custom_attributes",
            "email",
            "phone_number",
            "activation_code",
            "recipient_hash",
            "feedback_message",
            "source_event_keys",
        }.isdisjoint(columns)
        assert all(columns[key]["nullable"] is False for key in table["primary_key"])
    assert tables["fact_certificate"]["columns"]["issued_on"]["type"] == "date"
    assert tables["fact_certificate"]["columns"]["delivered_at"]["type"] == "timestamptz"
    assert tables["fact_certificate"]["columns"]["contact_key"]["nullable"]
    assert tables["fact_followup"]["columns"]["request_key"]["nullable"]
    assert tables["fact_conversation"]["columns"]["status"]["type"] == "integer"
    assert tables["fact_message"]["columns"]["status"]["type"] == "integer"
    assert tables["fact_csat"]["columns"]["rating"]["enum"] == [1, 2, 3, 4, 5]
    assert "expired" in tables["fact_followup"]["columns"]["state"]["enum"]


def test_catalog_validates_broken_join_and_mapping_targets(monkeypatch):
    catalog = dds_catalog.load_catalog()
    catalog["requirements"][0]["targets"] = ["dim_contact.unknown_column"]
    catalog["tables"]["fact_message"]["relationships"][0]["target_columns"] = ["unknown_column"]
    monkeypatch.setattr(dds_catalog, "load_catalog", lambda: catalog)
    errors = dds_catalog.validate_catalog()
    assert any("dim_contact.unknown_column" in error for error in errors)
    assert any("fact_message" in error and "unknown_column" in error for error in errors)


def test_checked_in_schema_matches_generated_migration():
    migration = dds_catalog.schema_sql()
    assert (DDS / "schema.sql").read_text() == migration
    assert "COMMENT ON TABLE dds.fact_certificate IS" in migration
    assert "COMMENT ON COLUMN dds.fact_certificate.issued_on IS" in migration
    assert "PRIMARY KEY (account_id, pool_key, aid_id)" in migration


def test_metrics_distinguish_activity_from_actual_help():
    assert (DDS / "metrics.json").is_file(), "DDS metric definitions are missing"
    metrics = json.loads((DDS / "metrics.json").read_text())["metrics"]
    assert metrics["consultation_requests_created"]["date"] == "requested_at"
    assert metrics["consultation_requests_completed"]["date"] == "completed_at"
    assert "record_kind" in metrics["certificate_records_by_status"]["group_by"]
    assert metrics["actual_help"]["available"] is False
    assert metrics["actual_help"]["formula"] is None


def test_certificate_metrics_document_scoped_coverage_and_unknown_stock():
    catalog = dds_catalog.load_catalog()["tables"]
    certificate = catalog["fact_certificate"]
    assert certificate["scope"]["attribution"] == [
        "historical_account_conversation",
        "send_message",
    ]
    assert certificate["scope"]["inventory_coverage"] == "attributable_records_only"
    assert "остаток" in certificate["limitations"]
    assert catalog["dim_certificate_pool"]["scope"]["attribution"] == [
        "attributable_certificate",
        "scoped_certificate_request",
    ]

    metrics = json.loads((DDS / "metrics.json").read_text())["metrics"]
    assert metrics["certificate_records_by_status"]["coverage"] == "attributable_records_only"
    assert metrics["certificate_stock"]["available"] is False
    assert metrics["certificate_stock"]["formula"] is None
    assert "Остаток сертификатов неизвестен" in (DDS / "README.md").read_text()


def test_dated_metrics_expose_completeness_denominator():
    metrics = json.loads((DDS / "metrics.json").read_text())["metrics"]
    assert metrics["consultation_date_coverage"]["denominator"] == "all_active_scoped_requests"
    assert (
        metrics["followup_sent_date_coverage"]["denominator"] == "active_sent_or_answered_followups"
    )
    sql = (DDS / "examples.sql").read_text()
    assert "requested_at IS NULL" in sql and "sent_at IS NULL" in sql
