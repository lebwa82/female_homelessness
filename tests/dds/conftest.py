"""All integration mutations are confined to a random DB on an explicit local test cluster."""

import os
from urllib.parse import urlsplit
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def database():
    dsn = os.environ.get("TEST_DDS_POSTGRES")
    if not dsn:
        pytest.skip("TEST_DDS_POSTGRES local temporary cluster not configured")
    from analytics.dds.__main__ import validate_dsn

    validate_dsn(dsn)
    u = urlsplit(dsn)
    if u.hostname not in ("localhost", "127.0.0.1", "::1"):
        pytest.fail("DDS integration tests accept loopback PostgreSQL only")
    admin = await asyncpg.connect(dsn)
    name = "dds_test_" + uuid4().hex
    await admin.execute(f"CREATE DATABASE {name}")
    conn = await asyncpg.connect(dsn, database=name)
    try:
        yield conn
    finally:
        await conn.close()
        await admin.execute(f"DROP DATABASE {name} WITH (FORCE)")
        await admin.close()
