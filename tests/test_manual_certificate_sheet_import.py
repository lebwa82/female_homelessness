"""Synthetic offline checks of the historical sheet importer."""

import hashlib
import json
import ssl
import stat
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from scripts import import_manual_certificate_sheet as importer

GENERATED = datetime(2026, 10, 27, 12, tzinfo=UTC)


def document():
    return {"source": {"spreadsheet_id": "synthetic_sheet", "sheet_name": "Sheet1", "gid": 0},
            "rows": [{
                "source_row": 2, "conversation_id": 100, "internal_conversation_id": 200,
                "contact_id": 300, "source_message_id": 400, "aid_id": "children_card",
                "provider_slug": "ozon", "issued_on": "2026-10-20",
                "source_message_created_at": "2026-10-20 08:00:00",
                "telegram_acceptance_verified": True, "human_assigned": False,
                "client_name": "SYNTHETIC_PRIVATE_NAME", "raw_type": "UNNEEDED_RAW_TYPE",
            }]}


def prepared():
    return importer.prepare_source(document(), "a" * 64, generated_at=GENERATED, expected_count=1)


def backup():
    return {"verified": True, "sha256": "b" * 64, "archive_path": "/synthetic/backup.dump",
            "toc_entries": 12}


class Result:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value

    def mappings(self):
        return self

    def all(self):
        return self.value


class Connection:
    def __init__(self, actual=None):
        self.actual = actual
        self.queries = []
        self.rolled_back = False

    async def execute(self, query, params=None):
        sql = str(query)
        self.queries.append((sql, params))
        if "FROM inboxes" in sql:
            return Result(5)
        if "FROM messages m" in sql:
            return Result([] if self.actual is None else [self.actual])
        return Result(None)

    async def begin(self):
        return self

    async def rollback(self):
        self.rolled_back = True


def actual_message(**changes):
    return {"id": 400, "created_at": datetime.fromisoformat("2026-10-20T08:00:00"), "status": 0,
            "source_id": "SYNTHETIC_TELEGRAM_RECEIPT", "internal_conversation_id": 200,
            "conversation_id": 100, "contact_id": 300, "human_assigned": True,
            "is_pdf": True, **changes}


def test_prepare_normalizes_sql_utc_and_discards_private_cells():
    rows, source, manifest = prepared()
    assert rows[0]["source_message_created_at"] == "2026-10-20T08:00:00+00:00"
    assert source["snapshot_sha256"] == "a" * 64
    assert manifest["apply"] is False
    assert "SYNTHETIC_PRIVATE_NAME" not in repr((rows, source, manifest))
    assert "UNNEEDED_RAW_TYPE" not in repr((rows, source, manifest))


def test_private_source_requires_exact_permissions_regular_file_and_no_symlink(tmp_path):
    source = tmp_path / "source.json"
    raw = json.dumps(document()).encode()
    source.write_bytes(raw)
    source.chmod(0o600)
    parsed, digest = importer.read_private_json(source)
    assert parsed == document()
    assert digest == hashlib.sha256(raw).hexdigest()
    source.chmod(0o640)
    with pytest.raises(ValueError):
        importer.read_private_json(source)
    source.chmod(0o600)
    link = tmp_path / "link.json"
    link.symlink_to(source)
    with pytest.raises(OSError):
        importer.read_private_json(link)
    with pytest.raises((OSError, ValueError)):
        importer.read_private_json(tmp_path)


def test_private_artifacts_are_inert_mode_600_in_mode_700_directory(tmp_path):
    directory = tmp_path / "output"
    _, _, manifest = prepared()
    importer.write_private_artifacts(directory, {"followup-manifest.json": manifest,
                                                "reconciliation-report.json": {"count": 1}})
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in ("followup-manifest.json", "reconciliation-report.json"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert json.loads((directory / "followup-manifest.json").read_text())["apply"] is False
    directory.chmod(0o755)
    with pytest.raises(ValueError):
        importer.write_private_artifacts(directory, {})


@pytest.mark.parametrize("field,value", [
    ("verified", False), ("sha256", "invalid"), ("archive_path", "relative.dump"),
    ("toc_entries", 0), ("toc_entries", True),
])
def test_backup_attestation_fails_closed(field, value):
    with pytest.raises(ValueError):
        importer.validate_backup_attestation({**backup(), field: value})


def test_database_settings_verify_full_without_app_settings(monkeypatch):
    contexts = []

    class Context:
        def __init__(self, protocol):
            assert protocol == ssl.PROTOCOL_TLS_CLIENT
            self.check_hostname = False
            self.verify_mode = ssl.CERT_NONE
            self.loaded_ca = None
            contexts.append(self)

        def load_verify_locations(self, *, cafile):
            assert cafile == "/synthetic/ca.crt"
            self.loaded_ca = cafile

    monkeypatch.setattr(importer.ssl, "SSLContext", Context)
    environment = {"CERTIFICATE_DATABASE_URL":
                   "postgresql://synthetic:synthetic@db.invalid:5432/test?sslmode=verify-full",
                   "CERTIFICATE_DATABASE_SSL_ROOT_CERT": "/synthetic/ca.crt",
                   "CERTIFICATE_IDENTITY_KEY": "synthetic-key"}
    url, arguments, key = importer.database_settings(environment)
    assert url.drivername == "postgresql+asyncpg"
    assert "sslmode" not in url.query
    assert arguments["ssl"].check_hostname is True
    assert arguments["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert arguments["ssl"].loaded_ca == "/synthetic/ca.crt"
    assert contexts == [arguments["ssl"]]
    assert key == "synthetic-key"
    with pytest.raises(ValueError):
        importer.database_settings({**environment, "CERTIFICATE_DATABASE_SSL_ROOT_CERT": ""})
    with pytest.raises(ValueError):
        importer.database_settings({**environment, "CERTIFICATE_DATABASE_URL":
                                    "postgresql://x:x@db.invalid/test?sslmode=disable"})


@pytest.mark.asyncio
async def test_preflight_keeps_receipt_only_in_core_evidence_and_refreshes_assignment():
    rows, source, _ = prepared()
    connection = Connection(actual_message())
    records, refreshed = await importer.preflight(connection, rows, source, expected_inbox=5)
    record = records[0]
    assert record.accepted_at == datetime(2026, 10, 20, 8, tzinfo=UTC)
    assert record.evidence["telegram_source_id"] == "SYNTHETIC_TELEGRAM_RECEIPT"
    assert record.evidence["date_precision"] == "day"
    assert record.evidence["confirmation"] == "telegram"
    assert refreshed[0]["human_assigned"] is True
    manifest = importer.build_followup_manifest(refreshed, source, generated_at=GENERATED,
                                               expected_count=1)
    assert "SYNTHETIC_TELEGRAM_RECEIPT" not in repr(manifest)
    sql = connection.queries[-1][0]
    assert "lower(b.filename) LIKE '%.pdf'" in sql
    assert "m.content" not in sql
    assert "b.content_type = 'application/pdf'" in sql
    assert "contact.account_id = :account_id" in sql


@pytest.mark.asyncio
async def test_sheet_attestation_does_not_fabricate_acceptance():
    rows, source, _ = prepared()
    rows[0]["telegram_acceptance_verified"] = False
    records, _ = await importer.preflight(Connection(actual_message(source_id=None)), rows, source,
                                          expected_inbox=5)
    assert records[0].accepted_at is None
    assert records[0].evidence["confirmation"] == "sheet"


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"contact_id": 999}, {"conversation_id": 999}, {"internal_conversation_id": 999},
    {"created_at": datetime.fromisoformat("2026-10-20T09:00:00")}, {"is_pdf": False},
    {"source_id": None}, {"source_id": ""}, {"source_id": "  "}, {"status": 3},
])
async def test_preflight_rejects_changed_or_insufficient_evidence(changes):
    rows, source, _ = prepared()
    with pytest.raises(ValueError):
        await importer.preflight(Connection(actual_message(**changes)), rows, source,
                                 expected_inbox=5)


class Engine:
    def __init__(self):
        self.connection = Connection()
        self.committed = False

    @asynccontextmanager
    async def begin(self):
        yield self.connection
        self.committed = True

    @asynccontextmanager
    async def connect(self):
        assert self.committed
        yield self.connection


def transaction_helpers(monkeypatch, *, change_snapshot=False, change_source=False):
    calls = []
    synthetic_record = object()

    async def snapshot(connection, *, conversation_ids):
        assert conversation_ids == [200]
        calls.append("snapshot")
        return {"protected": 2 if change_snapshot and calls.count("snapshot") > 1 else 1}

    async def preflight(connection, rows, source, *, expected_inbox):
        calls.append("preflight")
        if change_source and calls.count("preflight") > 1:
            return [object()], rows
        return [synthetic_record], rows

    async def initialize(connection):
        calls.append("initialize")

    async def importing(connection, records, **kwargs):
        calls.append("import")
        return (1, 0) if calls.count("import") == 1 else (0, 1)

    async def verifying(connection, records, **kwargs):
        calls.append("verify")
        return 1

    monkeypatch.setattr(importer, "invariant_snapshot", snapshot)
    monkeypatch.setattr(importer, "preflight", preflight)
    monkeypatch.setattr(importer, "initialize_manual_schema", initialize)
    monkeypatch.setattr(importer, "import_manual_history", importing)
    monkeypatch.setattr(importer, "verify_manual_history", verifying)
    return calls


@pytest.mark.asyncio
async def test_dry_run_is_read_only_and_never_initializes_or_imports(monkeypatch):
    calls = transaction_helpers(monkeypatch)
    rows, source, _ = prepared()
    engine = Engine()
    manifest, report = await importer.execute_import(
        engine, rows, source, identity_key="synthetic", generated_at=GENERATED,
        mode="dry-run", expected_count=1, expected_inbox=5,
    )
    assert calls == ["snapshot", "preflight", "snapshot"]
    assert engine.connection.queries[0][0] == "SET TRANSACTION READ ONLY"
    assert manifest["apply"] is False
    assert report["imported"] == report["duplicates"] == 0
    assert report["validated_recipients"] == 1
    assert report["source_rows"] == rows
    assert report["category_counts"] == {"children_card": 1}
    assert report["provider_counts"] == {"ozon": 1}


@pytest.mark.asyncio
async def test_apply_is_single_transaction_followed_by_verified_rollback_repeat(monkeypatch):
    calls = transaction_helpers(monkeypatch)
    rows, source, _ = prepared()
    engine = Engine()
    manifest, report = await importer.execute_import(
        engine, rows, source, identity_key="synthetic", generated_at=GENERATED,
        mode="apply", expected_count=1, expected_inbox=5, backup_attestation=backup(),
    )
    assert calls == ["snapshot", "preflight", "initialize", "import", "verify", "preflight", "snapshot",
                     "verify", "import", "snapshot"]
    assert engine.committed is True
    assert engine.connection.rolled_back is True
    assert report["imported"] == report["verified_recipients"] == 1
    assert manifest["apply"] is False
    assert report["followups_activated"] is False
    assert report["backup_attestation"] == backup()


@pytest.mark.asyncio
async def test_apply_requires_backup_before_database_work(monkeypatch):
    calls = transaction_helpers(monkeypatch)
    rows, source, _ = prepared()
    engine = Engine()
    with pytest.raises(ValueError, match="backup"):
        await importer.execute_import(
            engine, rows, source, identity_key="synthetic", generated_at=GENERATED,
            mode="apply", expected_count=1, expected_inbox=5,
        )
    assert calls == []
    assert engine.committed is False


@pytest.mark.asyncio
async def test_changed_protected_state_rolls_back_before_commit(monkeypatch):
    transaction_helpers(monkeypatch, change_snapshot=True)
    rows, source, _ = prepared()
    engine = Engine()
    with pytest.raises(ValueError, match="protected state"):
        await importer.execute_import(
            engine, rows, source, identity_key="synthetic", generated_at=GENERATED,
            mode="apply", expected_count=1, expected_inbox=5, backup_attestation=backup(),
        )
    assert engine.committed is False


@pytest.mark.asyncio
async def test_changed_source_evidence_rolls_back_before_commit(monkeypatch):
    transaction_helpers(monkeypatch, change_source=True)
    rows, source, _ = prepared()
    engine = Engine()
    with pytest.raises(ValueError, match="source evidence"):
        await importer.execute_import(
            engine, rows, source, identity_key="synthetic", generated_at=GENERATED,
            mode="apply", expected_count=1, expected_inbox=5, backup_attestation=backup(),
        )
    assert engine.committed is False


def test_main_redacts_exception_details(monkeypatch, capsys):
    async def failing(*args, **kwargs):
        raise ValueError("SYNTHETIC_SECRET_OR_RECEIPT")

    monkeypatch.setattr(importer, "run", failing)
    assert importer.main(["--source", "/synthetic/source.json", "--output-dir", "/synthetic/out"]) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"error": "ValueError"}
    assert "SYNTHETIC_SECRET_OR_RECEIPT" not in captured.err + captured.out
