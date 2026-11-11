import io
from datetime import UTC, datetime, timedelta
from html import escape
from unittest.mock import AsyncMock, Mock, call
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from botocore.exceptions import ClientError

from analytics import hourly_export
from analytics.hourly_export import publish
from analytics.shared_s3 import TeamShare, parse_signed_share

BUCKET = "a1b2c3d4-nevidimy-analytics"
STAMP = "2026-11-11T12:00:00+00:00"
FILES = {"latest.xlsx": b"synthetic workbook bytes", "latest.html": b"<p>Synthetic report</p>"}
SHARE_ID = "12345678-1234-1234-1234-123456789abc"


def signed_object(name, identifier=SHARE_ID):
    query = urlencode({
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": "synthetic",
        "X-Amz-Date": "20990101T000000Z", "X-Amz-Expires": "86400", "X-Amz-SignedHeaders": "host",
        "X-Amz-Signature": "a" * 64,
    })
    return f"https://s3.twcstorage.ru/{BUCKET}/team-share/{identifier}/{name}?{query}"


def old_dashboard(url):
    return f'<a id="download-excel" class="pill" href="{escape(url, quote=True)}">Скачать Excel</a>'


def test_refresh_same_pair_and_same_excel_link(ready_files, monkeypatch):
    share = parse_signed_share(signed_object("dashboard.html"), BUCKET, "dashboard.html")
    excel = signed_object("latest.xlsx")
    existing = old_dashboard(excel)
    expected = existing + '<script id="metrics-data" type="application/json">{"fresh":true}</script>'
    (ready_files / "snapshot.json").write_text('{"fresh": true}', encoding="utf-8")
    renderer = Mock(return_value=expected)
    monkeypatch.setattr(hourly_export, "render_shared_dashboard", renderer)
    client = Mock()
    client.get_object.return_value = {"Body": io.BytesIO(existing.encode())}
    sizes = {share.key("latest.xlsx"): 1}
    uploads = []

    def put(**params):
        uploads.append((params["Key"], params["Body"].read(), params["ContentDisposition"], params["CacheControl"]))
        sizes[params["Key"]] = params["ContentLength"]

    client.put_object.side_effect = put
    client.head_object.side_effect = lambda *, Bucket, Key: {
        "ContentLength": sizes[Key], "Metadata": {"snapshot-utc": STAMP},
    }
    assert hourly_export.refresh_team_share(client, BUCKET, ready_files, STAMP, share) is True
    client.get_object.assert_called_once_with(Bucket=BUCKET, Key=share.key("dashboard.html"))
    renderer.assert_called_once_with(existing, {"fresh": True})
    assert uploads == [
        (share.key("latest.xlsx"), FILES["latest.xlsx"], 'attachment; filename="latest.xlsx"', "private, no-store"),
        (share.key("dashboard.html"), expected.encode(), "inline", "private, no-store"),
    ]
    client.generate_presigned_url.assert_not_called()


def test_expired_team_share_does_not_call_s3(ready_files):
    client = Mock()
    expired = TeamShare(SHARE_ID, datetime.now(UTC) - timedelta(seconds=1))
    assert hourly_export.refresh_team_share(client, BUCKET, ready_files, STAMP, expired) is False
    assert not client.mock_calls


def test_foreign_workbook_link_prevents_updates(ready_files):
    share = parse_signed_share(signed_object("dashboard.html"), BUCKET, "dashboard.html")
    client = Mock()
    foreign = signed_object("latest.xlsx", identifier="98765432-1234-1234-1234-123456789abc")
    client.get_object.return_value = {"Body": io.BytesIO(old_dashboard(foreign).encode())}
    with pytest.raises(ValueError, match="outside the configured share"):
        hourly_export.refresh_team_share(client, BUCKET, ready_files, STAMP, share)
    client.put_object.assert_not_called()


def test_deleted_workbook_not_recreated(ready_files, monkeypatch):
    share = parse_signed_share(signed_object("dashboard.html"), BUCKET, "dashboard.html")
    client = Mock()
    existing = old_dashboard(signed_object("latest.xlsx"))
    client.get_object.return_value = {"Body": io.BytesIO(existing.encode())}
    client.head_object.side_effect = policy_error("NoSuchKey")
    monkeypatch.setattr(hourly_export, "render_shared_dashboard", Mock(return_value=existing))
    (ready_files / "snapshot.json").write_text('{}', encoding="utf-8")
    with pytest.raises(ClientError, match="NoSuchKey"):
        hourly_export.refresh_team_share(client, BUCKET, ready_files, STAMP, share)
    client.put_object.assert_not_called()


@pytest.mark.parametrize("change", [
    lambda url: url + "&versionId=synthetic",
    lambda url: url.replace("s3.twcstorage.ru", "example.invalid"),
    lambda url: url.replace("X-Amz-Expires=86400", "X-Amz-Expires=604801"),
    lambda url: url.replace("dashboard.html", "../dashboard.html"),
])
def test_share_requires_unversioned_key_and_known_scope(change):
    with pytest.raises(ValueError, match="analytics"):
        parse_signed_share(change(signed_object("dashboard.html")), BUCKET, "dashboard.html")


def policy_error(code):
    return ClientError({"Error": {"Code": code, "Message": "Synthetic error"}}, "GetBucketPolicy")


@pytest.fixture
def ready_files(tmp_path):
    for name, data in FILES.items():
        (tmp_path / name).write_bytes(data)
    return tmp_path


@pytest.fixture
def s3():
    client = Mock()
    client.get_bucket_acl.return_value = {"Grants": [{"Grantee": {"Type": "CanonicalUser"}}]}
    client.get_bucket_policy.side_effect = policy_error("NoSuchBucketPolicy")
    client.head_object.side_effect = lambda *, Bucket, Key: {
        "ContentLength": len(FILES[Key]),
        "Metadata": {"snapshot-utc": STAMP},
    }
    return client


@pytest.mark.parametrize("name", FILES)
@pytest.mark.parametrize("empty", [False, True], ids=["missing", "empty"])
def test_publish_requires_both_files_before_first_upload(s3, ready_files, name, empty):
    path = ready_files / name
    if empty:
        path.write_bytes(b"")
    else:
        path.unlink()

    with pytest.raises(ValueError, match="Both complete artifacts"):
        publish(s3, BUCKET, ready_files, STAMP)

    s3.put_object.assert_not_called()
    s3.head_object.assert_not_called()


@pytest.mark.parametrize("bucket", ["a1b2c3d4-production-assets", "a1b2c3d4-nevidimy-analytics-public"])
def test_publish_rejects_a_different_bucket(s3, ready_files, bucket):
    with pytest.raises(ValueError, match="dedicated analytics bucket"):
        publish(s3, bucket, ready_files, STAMP)

    assert not s3.mock_calls


@pytest.mark.parametrize("grant_type", ["Group", None], ids=["group-grant", "empty-grants"])
def test_publish_rejects_unexpected_bucket_acl(s3, ready_files, grant_type):
    grants = s3.get_bucket_acl.return_value["Grants"]
    if grant_type:
        grants.append({"Grantee": {"Type": grant_type}})
    else:
        grants.clear()

    with pytest.raises(ValueError, match="unexpected ACL grants"):
        publish(s3, BUCKET, ready_files, STAMP)

    s3.put_object.assert_not_called()


def test_publish_rejects_any_bucket_policy(s3, ready_files):
    s3.get_bucket_policy.side_effect = None
    s3.get_bucket_policy.return_value = {"Policy": '{"Statement": []}'}

    with pytest.raises(ValueError, match="unexpected bucket policy"):
        publish(s3, BUCKET, ready_files, STAMP)

    s3.put_object.assert_not_called()


def test_publish_does_not_treat_failed_policy_inspection_as_absence(s3, ready_files):
    s3.get_bucket_policy.side_effect = policy_error("AccessDenied")

    with pytest.raises(ClientError, match="AccessDenied"):
        publish(s3, BUCKET, ready_files, STAMP)

    s3.put_object.assert_not_called()


def test_publish_uploads_both_files_with_private_cache_headers_and_reads_them_back(s3, ready_files):
    uploads = []
    s3.put_object.side_effect = lambda *, Body, **fields: uploads.append((Body.read(), fields))

    publish(s3, BUCKET, ready_files, STAMP)

    content_types = {
        "latest.xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "latest.html": "text/html; charset=utf-8",
    }
    assert uploads == [
        (
            data,
            {
                "Bucket": BUCKET,
                "Key": key,
                "ContentLength": len(data),
                "ContentType": content_types[key],
                "ContentDisposition": f'attachment; filename="{key}"',
                "CacheControl": "private, no-store",
                "Metadata": {"snapshot-utc": STAMP},
            },
        )
        for key, data in FILES.items()
    ]
    assert s3.mock_calls[:2] == [
        call.get_bucket_acl(Bucket=BUCKET),
        call.get_bucket_policy(Bucket=BUCKET),
    ]
    assert s3.head_object.call_args_list == [call(Bucket=BUCKET, Key=key) for key in FILES]


@pytest.mark.parametrize("bad_field", ["ContentLength", "Metadata"])
def test_publish_fails_if_readback_does_not_match(s3, ready_files, bad_field):
    correct_head = s3.head_object.side_effect
    first = correct_head(Bucket=BUCKET, Key="latest.xlsx")
    second = correct_head(Bucket=BUCKET, Key="latest.html")
    second[bad_field] = 1 if bad_field == "ContentLength" else {"snapshot-utc": "stale"}
    s3.head_object.side_effect = [first, second]

    with pytest.raises(ValueError, match="Published object did not match the build"):
        publish(s3, BUCKET, ready_files, STAMP)

    assert s3.put_object.call_count == 2


@pytest.mark.asyncio
async def test_source_connection_uses_provider_ca_and_verify_full_without_retry(monkeypatch, tmp_path):
    env = {
        "ACCOUNT_ID": "1", "INBOX_ID": "5", "BOT_NAME": "test_bot", "EXCLUDED_DISPLAY_IDS": "",
        "PG_CA": "/run/test/provider ca.crt", "PG_HOST": "pg.test.invalid", "PG_DATABASE": "testdb",
        "PG_USER": "reader", "PG_PASSWORD": "test-only",
    }
    for key, value in env.items():
        monkeypatch.setenv("ANALYTICS_" + key, value)
    connect = AsyncMock(side_effect=ConnectionError("synthetic verification failure"))
    monkeypatch.setattr(hourly_export.asyncpg, "connect", connect)

    with pytest.raises(ConnectionError, match="synthetic verification failure"):
        await hourly_export.build(tmp_path)

    connect.assert_awaited_once()
    args, kwargs = connect.await_args
    assert parse_qs(urlsplit(args[0]).query) == {"sslmode": ["verify-full"], "sslrootcert": [env["PG_CA"]]}
    assert kwargs["host"] == env["PG_HOST"]
    assert "ssl" not in kwargs  # No separate argument can override the DSN mode.
    assert not list(tmp_path.iterdir())
