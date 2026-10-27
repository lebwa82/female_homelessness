"""Import a closed historical sheet snapshot; never send or activate follow-ups."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import ssl
import stat
import sys
import uuid
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.chatwoot.manual_certificate_history import (
    ManualHistoricalIssuance,
    import_manual_history,
    initialize_manual_schema,
    verify_manual_history,
)
from app.chatwoot.manual_certificate_manifest import build_followup_manifest

ACCOUNT_ID = 1


def timestamp(value: str | datetime) -> datetime:
    if isinstance(value, str) and (len(value) <= 10 or value[10] not in {"T", " "}):
        raise ValueError("source timestamp must include a date and time")
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def read_private_json(path: Path) -> tuple[dict, str]:
    """Read an owner-only regular snapshot without following its final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600
                or metadata.st_uid != os.geteuid()):
            raise ValueError("source must be an owned mode-0600 regular file")
        raw = stream.read()
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("source must contain a JSON object")  # noqa: TRY004 - validation API
    return parsed, hashlib.sha256(raw).hexdigest()


def prepare_source(document: dict, digest: str, *, generated_at: datetime,
                   expected_count: int) -> tuple[list[dict], dict, dict]:
    metadata = {**document["source"], "snapshot_sha256": digest}
    normalized = []
    for value in document["rows"]:
        row = dict(value)
        row["source_message_created_at"] = timestamp(row["source_message_created_at"]).isoformat()
        normalized.append(row)
    manifest = build_followup_manifest(normalized, metadata, generated_at=generated_at,
                                       expected_count=expected_count)
    return manifest_rows(manifest), manifest["source"], manifest


def manifest_rows(manifest: dict) -> list[dict]:
    """Rebuild normalized rows from the allowlist, dropping arbitrary sheet cells."""
    return [{
        field: job[field] for field in (
            "source_row", "conversation_id", "internal_conversation_id", "contact_id",
            "source_message_id", "aid_id", "provider_slug", "telegram_acceptance_verified",
            "source_message_created_at",
        )
    } | {"issued_on": job["source_date"], "human_assigned": job["currently_human_assigned"]}
        for job in manifest["jobs"]]


def validate_backup_attestation(document: dict) -> dict:
    digest, archive = document.get("sha256"), document.get("archive_path")
    entries = document.get("toc_entries")
    if (document.get("verified") is not True or not isinstance(digest, str)
            or not re.fullmatch(r"[a-fA-F0-9]{64}", digest)
            or not isinstance(archive, str) or not Path(archive).is_absolute()
            or type(entries) is not int or entries <= 0):
        raise ValueError("a verified backup attestation is required")
    return {"verified": True, "sha256": digest.lower(), "archive_path": archive,
            "toc_entries": entries}


def database_settings(environment: Mapping[str, str]) -> tuple[URL, dict, str]:
    """Resolve only explicitly supplied database settings; never import app config."""
    identity_key = environment.get("CERTIFICATE_IDENTITY_KEY", "")
    if not identity_key:
        raise ValueError("certificate identity key is required")
    if environment.get("CERTIFICATE_DATABASE_URL"):
        url = make_url(environment["CERTIFICATE_DATABASE_URL"])
        if url.get_backend_name() != "postgresql":
            raise ValueError("only PostgreSQL is supported")
        if not all((url.host, url.database, url.username, url.password)):
            raise ValueError("complete database settings are required")
        if url.query.get("sslmode", "verify-full") != "verify-full":
            raise ValueError("verify-full is required")
        url = url.set(drivername="postgresql+asyncpg")
        url = url.difference_update_query(["sslmode", "sslrootcert", "ssl"])
    else:
        values = {field: environment.get(f"CERTIFICATE_DATABASE_{field.upper()}", "")
                  for field in ("host", "port", "name", "user", "password")}
        if not all(values.values()):
            raise ValueError("complete database settings are required")
        url = URL.create("postgresql+asyncpg", username=values["user"],
                         password=values["password"], host=values["host"],
                         port=int(values["port"]), database=values["name"])
    ca_path = environment.get("CERTIFICATE_DATABASE_SSL_ROOT_CERT")
    if not ca_path:
        raise ValueError("an explicit database root certificate is required")
    # Match asyncpg's verify-full construction. Python's create_default_context
    # additionally enables strict X.509 flags that reject this provider's CA.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=ca_path)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return url, {"ssl": context}, identity_key


async def set_transaction_limits(connection, *, read_only: bool) -> None:
    if read_only:
        await connection.execute(text("SET TRANSACTION READ ONLY"))
    await connection.execute(text("SET LOCAL statement_timeout = '30s'"))
    await connection.execute(text("SET LOCAL lock_timeout = '10s'"))
    await connection.execute(text("SET LOCAL idle_in_transaction_session_timeout = '60s'"))


async def invariant_snapshot(connection, *, conversation_ids: list[int]) -> dict:
    """Aggregate only real-PDF stock and hashes of follow-ups; fetch no document fields."""
    absent = (await connection.execute(text("""
        SELECT to_regclass('public.women_help_certificate_requests') IS NULL
    """))).scalar_one()
    if absent is not True:
        raise ValueError("requests table must remain absent for this narrow import")
    has_kind = (await connection.execute(text("""
        SELECT EXISTS (SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'women_help_pdf_certificates'
              AND column_name = 'record_kind')
    """))).scalar_one()
    predicate = "record_kind = 'certificate'" if has_kind else "TRUE"
    stock = (await connection.execute(text(f"""
        SELECT status, is_test, count(*) AS count
        FROM women_help_pdf_certificates WHERE {predicate}
        GROUP BY status, is_test ORDER BY status, is_test
    """))).mappings().all()
    followups = (await connection.execute(text("""
        SELECT id, md5(COALESCE(custom_attributes->'scenario_followups', '{}'::jsonb)::text)
            AS digest, md5(COALESCE(assignee_id::text, 'null')) AS assignment_digest
        FROM conversations WHERE account_id = :account_id
            AND id = ANY(:conversation_ids) ORDER BY id
    """), {"account_id": ACCOUNT_ID, "conversation_ids": conversation_ids})).mappings().all()
    if len(followups) != len(conversation_ids):
        raise ValueError("protected conversations are absent or ambiguous")
    encoded = json.dumps([dict(row) for row in followups], sort_keys=True).encode()
    return {"requests_table_absent": True, "real_pdf_status_counts": [dict(row) for row in stock],
            "followups_count": len(followups),
            "followups_sha256": hashlib.sha256(encoded).hexdigest()}


async def preflight(connection, rows: list[dict], source: dict, *,
                    expected_inbox: int) -> tuple[list[ManualHistoricalIssuance], list[dict]]:
    if type(expected_inbox) is not int or expected_inbox <= 0:
        raise ValueError("expected inbox must be a positive integer")
    inbox = (await connection.execute(text("""
        SELECT id FROM inboxes WHERE id = :inbox_id AND account_id = :account_id
            AND channel_type = 'Channel::Telegram'
    """), {"inbox_id": expected_inbox, "account_id": ACCOUNT_ID})).scalar_one()
    if inbox != expected_inbox:
        raise ValueError("Telegram inbox identity mismatch")
    records, refreshed = [], []
    for item in rows:
        found = (await connection.execute(text("""
            SELECT m.id, m.created_at, m.status, m.source_id,
                c.id AS internal_conversation_id, c.display_id AS conversation_id,
                c.contact_id, (c.assignee_id IS NOT NULL) AS human_assigned,
                EXISTS (SELECT 1 FROM attachments a
                    JOIN active_storage_attachments aa ON aa.record_type = 'Attachment'
                        AND aa.record_id = a.id AND aa.name = 'file'
                    JOIN active_storage_blobs b ON b.id = aa.blob_id
                    WHERE a.message_id = m.id AND lower(b.filename) LIKE '%.pdf'
                        AND b.content_type = 'application/pdf') AS is_pdf
            FROM messages m JOIN conversations c ON c.id = m.conversation_id
            JOIN contacts contact ON contact.id = c.contact_id
            WHERE m.id = :message_id AND m.account_id = :account_id
                AND c.account_id = :account_id AND contact.account_id = :account_id
                AND m.inbox_id = :inbox_id AND c.inbox_id = :inbox_id
                AND m.message_type = 1 AND NOT m.private
        """), {"message_id": item["source_message_id"], "account_id": ACCOUNT_ID,
               "inbox_id": expected_inbox})).mappings().all()
        if len(found) != 1:
            raise ValueError("source message linkage is absent or ambiguous")
        actual = found[0]
        for field in ("contact_id", "conversation_id", "internal_conversation_id"):
            if actual[field] != item[field]:
                raise ValueError("source message linkage mismatch")
        created = timestamp(actual["created_at"])
        if created != timestamp(item["source_message_created_at"]) or actual["is_pdf"] is not True:
            raise ValueError("source attachment or timestamp mismatch")
        receipt = actual["source_id"]
        accepted = actual["status"] is not None and actual["status"] != 3 and bool(
            isinstance(receipt, str) and receipt.strip(),
        )
        if accepted is not item["telegram_acceptance_verified"]:
            raise ValueError("source Telegram acceptance differs from snapshot")
        reference = f"sheet:{source['spreadsheet_id']}:0:{item['source_row']}"
        evidence = {
            "source": {"spreadsheet_id": source["spreadsheet_id"], "sheet_name": "Sheet1",
                       "gid": 0, "row": item["source_row"],
                       "url": f"https://docs.google.com/spreadsheets/d/{source['spreadsheet_id']}"
                              f"/edit#gid=0&range=A{item['source_row']}",
                       "snapshot_sha256": source["snapshot_sha256"]},
            "date_precision": "day", "confirmation": "telegram" if accepted else "sheet",
            "telegram_acceptance_verified": accepted,
            "source_message_created_at": created.isoformat(),
            "telegram_source_id": receipt,
        }
        records.append(ManualHistoricalIssuance(
            account_id=ACCOUNT_ID, contact_id=item["contact_id"],
            conversation_id=item["conversation_id"], source_message_id=item["source_message_id"],
            issued_on=date.fromisoformat(item["issued_on"]),
            accepted_at=created if accepted else None, provider_slug=item["provider_slug"],
            aid_id=item["aid_id"], source_reference=reference, evidence=evidence,
        ))
        refreshed.append({**item, "human_assigned": bool(actual["human_assigned"])})
    return records, refreshed


def private_output_directory(directory: Path) -> None:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if stat.S_IMODE(metadata.st_mode) != 0o700 or metadata.st_uid != os.geteuid():
            raise ValueError("output directory must be owned and mode 0700")
    finally:
        os.close(descriptor)


def write_private_artifacts(directory: Path, artifacts: Mapping[str, dict]) -> None:
    private_output_directory(directory)
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for name, document in artifacts.items():
            if name not in {"followup-manifest.json", "reconciliation-report.json"}:
                raise ValueError("unexpected artifact name")
            temporary = f".{name}.{uuid.uuid4().hex}.tmp"
            target = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=descriptor)
            try:
                with os.fdopen(target, "w") as stream:
                    json.dump(document, stream, ensure_ascii=False, sort_keys=True, indent=2,
                              allow_nan=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, name, src_dir_fd=descriptor, dst_dir_fd=descriptor)
            finally:
                try:
                    os.unlink(temporary, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
    finally:
        os.close(descriptor)


async def execute_import(engine, rows: list[dict], source: dict, *, identity_key: str,
                         generated_at: datetime, mode: str, expected_count: int,
                         expected_inbox: int, backup_attestation: dict | None = None) -> tuple[dict, dict]:
    if mode not in {"dry-run", "apply"}:
        raise ValueError("unknown import mode")
    backup_metadata = (validate_backup_attestation(backup_attestation or {})
                       if mode == "apply" else None)
    # Validation precedes all database work, including callers bypassing the CLI.
    initial_manifest = build_followup_manifest(rows, source, generated_at=generated_at,
                                               expected_count=expected_count)
    rows, source = manifest_rows(initial_manifest), initial_manifest["source"]
    conversation_ids = sorted(row["internal_conversation_id"] for row in rows)
    async with engine.begin() as connection:
        await set_transaction_limits(connection, read_only=mode == "dry-run")
        before = await invariant_snapshot(connection, conversation_ids=conversation_ids)
        records, refreshed = await preflight(connection, rows, source,
                                              expected_inbox=expected_inbox)
        imported = duplicates = verified = 0
        if mode == "apply":
            await initialize_manual_schema(connection)
            imported, duplicates = await import_manual_history(
                connection, records, identity_key=identity_key, account_id=ACCOUNT_ID,
            )
            verified = await verify_manual_history(
                connection, records, identity_key=identity_key, account_id=ACCOUNT_ID,
            )
            rechecked, reassigned = await preflight(connection, rows, source,
                                                     expected_inbox=expected_inbox)
            if rechecked != records or reassigned != refreshed:
                raise ValueError("source evidence changed inside import transaction")
        after = await invariant_snapshot(connection, conversation_ids=conversation_ids)
        if after != before:
            raise ValueError("protected state changed inside import transaction")
    if mode == "apply":
        # Fresh committed readback and an exact repeat; always roll back the latter.
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await set_transaction_limits(connection, read_only=False)
                verified = await verify_manual_history(
                    connection, records, identity_key=identity_key, account_id=ACCOUNT_ID,
                )
                repeated = await import_manual_history(
                    connection, records, identity_key=identity_key, account_id=ACCOUNT_ID,
                )
                if verified != expected_count or repeated != (0, expected_count):
                    raise ValueError("committed history failed repeat verification")
                if await invariant_snapshot(connection, conversation_ids=conversation_ids) != before:
                    raise ValueError("protected state changed after commit")
            finally:
                await transaction.rollback()
    manifest = build_followup_manifest(refreshed, source, generated_at=generated_at,
                                       expected_count=expected_count)
    report = {
        "mode": mode, "apply": mode == "apply", "account_id": ACCOUNT_ID,
        "generated_at": generated_at.astimezone(UTC).isoformat(), "source": source,
        "validated_recipients": len(records), "telegram_verified": sum(
            row["telegram_acceptance_verified"] for row in refreshed),
        "sheet_attested": sum(not row["telegram_acceptance_verified"] for row in refreshed),
        "imported": imported, "duplicates": duplicates, "verified_recipients": verified,
        "source_rows": refreshed,
        "category_counts": dict(sorted(Counter(row["aid_id"] for row in refreshed).items())),
        "provider_counts": dict(sorted(Counter(row["provider_slug"] for row in refreshed).items())),
        "backup_attestation": backup_metadata,
        "protected_state_before": before, "protected_state_after": after,
        "followups_activated": False,
    }
    return manifest, report


async def run(source_path: Path, output_dir: Path, *, mode: str = "dry-run",
              expected_count: int = 39, expected_inbox: int = 5,
              backup_attestation_path: Path | None = None,
              environment: Mapping[str, str] | None = None) -> dict:
    generated_at = datetime.now(UTC)
    document, digest = read_private_json(source_path)
    rows, source, _ = prepare_source(document, digest, generated_at=generated_at,
                                     expected_count=expected_count)
    private_output_directory(output_dir)
    attestation = None
    if mode == "apply":
        if backup_attestation_path is None:
            raise ValueError("apply requires backup attestation")
        attestation = validate_backup_attestation(read_private_json(backup_attestation_path)[0])
    url, connect_args, identity_key = database_settings(os.environ if environment is None
                                                      else environment)
    engine = create_async_engine(url, connect_args=connect_args, pool_pre_ping=True,
                                 hide_parameters=True)
    try:
        manifest, report = await execute_import(
            engine, rows, source, identity_key=identity_key, generated_at=generated_at, mode=mode,
            expected_count=expected_count, expected_inbox=expected_inbox,
            backup_attestation=attestation,
        )
        write_private_artifacts(output_dir, {"followup-manifest.json": manifest,
                                             "reconciliation-report.json": report})
        return {field: report[field] for field in (
            "mode", "validated_recipients", "telegram_verified", "sheet_attested",
            "imported", "duplicates", "verified_recipients", "followups_activated",
        )}
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("dry-run", "apply"), default="dry-run")
    parser.add_argument("--expected-count", type=int, default=39)
    parser.add_argument("--expected-inbox", type=int, default=5)
    parser.add_argument("--backup-attestation", type=Path)
    args = parser.parse_args(argv)
    try:
        result = asyncio.run(run(
            args.source, args.output_dir, mode=args.mode, expected_count=args.expected_count,
            expected_inbox=args.expected_inbox, backup_attestation_path=args.backup_attestation,
        ))
    except Exception as error:  # noqa: BLE001 - never disclose driver parameters or receipt values
        print(json.dumps({"error": type(error).__name__}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
