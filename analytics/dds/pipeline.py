"""One bounded extraction/publication run with a fixed source identity."""

import hmac
from datetime import timedelta

from analytics.dds.source import read_snapshot
from analytics.dds.store import check_contract, load_lock, publish
from analytics.dds.transform import project


async def run_once(
    source,
    target,
    scope,
    identity_key: bytes,
    *,
    full=False,
    batch_size=500,
    source_identity="chatwoot",
    overlap_hours=48,
):
    if not isinstance(source_identity, str) or not source_identity or len(source_identity) > 128:
        raise ValueError("source identity is required")
    if type(overlap_hours) is not int or not 1 <= overlap_hours <= 168:
        raise ValueError("overlap must be between 1 and 168 hours")
    if len(identity_key) < 32:
        raise ValueError("identity key too short")
    key = hmac.digest(identity_key, ("source:" + source_identity).encode(), "sha256")
    async with load_lock(target):
        state = await check_contract(target, scope, key)
        since = (
            None if full or state is None else state["watermark"] - timedelta(hours=overlap_hours)
        )
        snapshot = await read_snapshot(source, scope, since=since, batch_size=batch_size)
        projection = project(snapshot, scope, key)
        return await publish(target, projection, snapshot, scope, key, batch_size=batch_size)
