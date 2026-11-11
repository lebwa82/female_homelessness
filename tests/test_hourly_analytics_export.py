from unittest.mock import AsyncMock, Mock, call
from urllib.parse import parse_qs, urlsplit

import pytest
from botocore.exceptions import ClientError

from analytics import hourly_export
from analytics.hourly_export import publish

BUCKET = "a1b2c3d4-nevidimy-analytics"
STAMP = "2026-11-11T12:00:00+00:00"
FILES = {"latest.xlsx": b"synthetic workbook bytes", "latest.html": b"<p>Synthetic report</p>"}


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
