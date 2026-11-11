"""Build two private analytics artifacts from one read-only Chatwoot snapshot."""

from __future__ import annotations

import asyncio
import io
import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

import asyncpg
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from analytics.chat_export_xlsx import ExportTable, write_workbook
from analytics.internal_html import render_html
from analytics.shared_dashboard import render_shared_dashboard
from analytics.shared_s3 import TeamShare, parse_signed_share, workbook_link

# This is the approved schema of the previous export. Do not automatically
# include new columns or other workspaces when Chatwoot adds them.
EXPORT_COLUMNS = {
    "conversations": (
        "id", "account_id", "inbox_id", "status", "assignee_id", "created_at", "updated_at", "contact_id",
        "display_id", "contact_last_seen_at", "agent_last_seen_at", "additional_attributes", "contact_inbox_id",
        "uuid", "identifier", "last_activity_at", "team_id", "campaign_id", "snoozed_until", "custom_attributes",
        "assignee_last_seen_at", "first_reply_created_at", "priority", "sla_policy_id", "waiting_since",
        "cached_label_list", "assignee_agent_bot_id", "ai_assignee_type", "status_changed_at",
    ),
    "messages": (
        "id", "content", "account_id", "inbox_id", "conversation_id", "message_type", "created_at", "updated_at",
        "private", "status", "source_id", "content_type", "content_attributes", "sender_type", "sender_id",
        "external_source_ids", "additional_attributes", "processed_message_content", "sentiment",
    ),
}
ARTIFACTS = (
    ("latest.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("latest.html", "text/html; charset=utf-8"),
)
ENDPOINT = "https://s3.twcstorage.ru"


async def export_table(connection: asyncpg.Connection, table: str, account_id: int) -> ExportTable:
    names = EXPORT_COLUMNS[table]
    schema = await connection.fetch(
        """SELECT column_name, udt_name FROM information_schema.columns
           WHERE table_schema = 'public' AND table_name = $1""",
        table,
    )
    types = {column["column_name"]: column["udt_name"] for column in schema}
    if not types.keys() >= set(names):
        raise ValueError("Export schema differs from the previous Chatwoot workbook")
    # The table and columns are from the fixed internal allowlist above.
    fields = ",".join(f'"{name}"::text' for name in names)
    rows = connection.cursor(
        f'SELECT {fields} FROM public."{table}" WHERE account_id = $1 ORDER BY id',
        account_id,
        prefetch=250,
    )
    return ExportTable(table, tuple((name, types[name]) for name in names), rows)


async def build(directory: Path, *, keep_snapshot: bool = False) -> tuple[str, dict[str, int]]:
    account_id = int(os.environ["ANALYTICS_ACCOUNT_ID"])
    inbox_id = int(os.environ["ANALYTICS_INBOX_ID"])
    bot_name = os.environ["ANALYTICS_BOT_NAME"]
    excluded = [int(value) for value in os.environ["ANALYTICS_EXCLUDED_DISPLAY_IDS"].split()]
    # Match the live Chatwoot/libpq verification policy using asyncpg's
    # verify-full: validate both the provider's CA chain and the host name.
    tls = urlencode({"sslmode": "verify-full", "sslrootcert": os.environ["ANALYTICS_PG_CA"]})
    connection = await asyncpg.connect(
        f"postgresql://?{tls}",
        host=os.environ["ANALYTICS_PG_HOST"],
        port=int(os.environ.get("ANALYTICS_PG_PORT", "5432")),
        database=os.environ["ANALYTICS_PG_DATABASE"],
        user=os.environ["ANALYTICS_PG_USER"],
        password=os.environ["ANALYTICS_PG_PASSWORD"],
        timeout=10,
        server_settings={"application_name": "nevidimy-hourly-analytics"},
    )
    try:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            await connection.execute("SET LOCAL statement_timeout = '60s'")
            await connection.execute("SET LOCAL TIME ZONE 'UTC'")
            await connection.execute("SET LOCAL DateStyle = 'ISO, YMD'")
            sql = Path(__file__).with_name("internal_metrics.sql").read_text(encoding="utf-8")
            raw = await connection.fetchval(sql, account_id, inbox_id, bot_name, excluded)
            html = render_html(json.loads(raw))
            stamp = await connection.fetchval("SELECT transaction_timestamp()")
            tables = [await export_table(connection, name, account_id) for name in EXPORT_COLUMNS]
            counts = await write_workbook(directory / "latest.xlsx", tables, stamp)
            (directory / "latest.html").write_text(html, encoding="utf-8")
            if keep_snapshot:
                (directory / "snapshot.json").write_text(raw, encoding="utf-8")
    finally:
        await connection.close(timeout=5)
    return stamp.isoformat(), counts


def assert_private_bucket(client, bucket: str) -> None:
    grants = client.get_bucket_acl(Bucket=bucket)["Grants"]
    if not grants or any(grant["Grantee"]["Type"] != "CanonicalUser" for grant in grants):
        raise ValueError("Analytics bucket has unexpected ACL grants")
    try:
        client.get_bucket_policy(Bucket=bucket)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchBucketPolicy":
            raise
    else:
        raise ValueError("Analytics bucket has an unexpected bucket policy")


def put_verified(client, bucket, key, stream, size, content_type, disposition, stamp):
    client.put_object(
        Bucket=bucket, Key=key, Body=stream, ContentLength=size, ContentType=content_type,
        ContentDisposition=disposition, CacheControl="private, no-store", Metadata={"snapshot-utc": stamp},
    )
    head = client.head_object(Bucket=bucket, Key=key)
    if head["ContentLength"] != size or head["Metadata"].get("snapshot-utc") != stamp:
        raise ValueError("Published object did not match the build")


def publish(client, bucket: str, directory: Path, stamp: str) -> None:
    if bucket != "nevidimy-analytics" and not bucket.endswith("-nevidimy-analytics"):
        raise ValueError("Expected the dedicated analytics bucket")
    assert_private_bucket(client, bucket)
    files = [(directory / key, content_type) for key, content_type in ARTIFACTS]
    if any(not path.is_file() or not path.stat().st_size for path, _ in files):
        raise ValueError("Both complete artifacts are required before publishing")
    for path, content_type in files:
        size = path.stat().st_size
        with path.open("rb") as data:
            put_verified(client, bucket, path.name, data, size, content_type, f'attachment; filename="{path.name}"', stamp)


def configured_share() -> TeamShare | None:
    identifier = os.environ.get("ANALYTICS_TEAM_SHARE_ID", "")
    expiry = os.environ.get("ANALYTICS_TEAM_SHARE_EXPIRES_AT", "")
    if not identifier and not expiry:
        return None
    share = TeamShare(identifier, datetime.fromisoformat(expiry))
    return share if share.active() else None


def refresh_team_share(client, bucket: str, directory: Path, stamp: str, share: TeamShare) -> bool:
    """Refresh only the existing share, after the normal private publication."""
    if not share.active():
        return False
    html_key, xlsx_key = share.key("dashboard.html"), share.key("latest.xlsx")
    # Both objects must already exist. Do not recreate an intentionally removed share.
    with client.get_object(Bucket=bucket, Key=html_key)["Body"] as stream:
        previous = stream.read(1_000_001)
    if len(previous) > 1_000_000:
        raise ValueError("Shared analytics dashboard is too large")
    template = previous.decode("utf-8")
    excel_url = workbook_link(template)
    excel = parse_signed_share(excel_url, bucket, "latest.xlsx")
    if excel.identifier != share.identifier or excel.expires_at < share.expires_at:
        raise ValueError("Workbook link is outside the configured share")
    snapshot = json.loads((directory / "snapshot.json").read_text(encoding="utf-8"))
    updated = render_shared_dashboard(template, snapshot)
    if workbook_link(updated) != excel_url:
        raise ValueError("Dashboard update changed the workbook link")
    html = updated.encode("utf-8")
    workbook = directory / "latest.xlsx"
    size = workbook.stat().st_size
    if size <= 0:
        raise ValueError("Missing analytics workbook")
    client.head_object(Bucket=bucket, Key=xlsx_key)
    if not share.active():
        return False
    with workbook.open("rb") as data:
        put_verified(client, bucket, xlsx_key, data, size, ARTIFACTS[0][1], 'attachment; filename="latest.xlsx"', stamp)
    put_verified(client, bucket, html_key, io.BytesIO(html), len(html), ARTIFACTS[1][1], "inline", stamp)
    return True


def main() -> None:
    os.umask(0o077)
    stage = "build"
    try:
        with TemporaryDirectory(dir=os.environ.get("RUNTIME_DIRECTORY")) as temporary:
            directory = Path(temporary)
            share = configured_share()
            stamp, counts = asyncio.run(build(directory, keep_snapshot=share is not None))
            stage = "publish"
            client = boto3.client(
                "s3",
                endpoint_url=ENDPOINT,
                region_name="ru-1",
                aws_access_key_id=os.environ["ANALYTICS_S3_ACCESS_KEY"],
                aws_secret_access_key=os.environ["ANALYTICS_S3_SECRET_KEY"],
                config=Config(
                    signature_version="s3v4", connect_timeout=10, read_timeout=30,
                    request_checksum_calculation="when_required", response_checksum_validation="when_required",
                    s3={"addressing_style": "path"},
                    retries={"mode": "standard", "total_max_attempts": 2},
                ),
            )
            bucket = os.environ["ANALYTICS_S3_BUCKET"]
            publish(client, bucket, directory, stamp)
            stage = "shared dashboard"
            refreshed = share is not None and refresh_team_share(client, bucket, directory, stamp, share)
    except Exception as error:  # noqa: BLE001 - backend errors can include SQL values or credentials.
        raise SystemExit(f"Analytics {stage} failed: {type(error).__name__}") from None
    print(json.dumps({"snapshot_utc": stamp, "rows": counts, "files": [k for k, _ in ARTIFACTS],
                      "team_share_refreshed": refreshed}))


if __name__ == "__main__":
    main()
