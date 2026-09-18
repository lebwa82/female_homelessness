"""Link already-issued Chatwoot certificates to verified contact IDs."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.chatwoot.certificates import CertificateInventory, database_url
from app.config import settings


async def link_file(path: Path) -> tuple[int, int, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError("the JSON root must be a list")
    if not settings.certificate_database_password or not settings.certificate_identity_key:
        raise RuntimeError("certificate database and identity keys are required")
    rows = []
    for row in payload:
        if not isinstance(row, dict) or set(row) != {"serial_number", "contact_id"}:
            raise ValueError("each mapping needs serial_number and contact_id")
        if not isinstance(row["serial_number"], str) or not row["serial_number"].strip():
            raise ValueError("serial_number must be a nonempty string")
        if type(row["contact_id"]) is not int or row["contact_id"] <= 0:
            raise ValueError("contact_id must be a positive integer")
        rows.append(row)
    inventory = CertificateInventory(
        database_url(settings.certificate_database_password),
        identity_key=settings.certificate_identity_key,
        account_id=settings.chatwoot_account_id,
    )
    try:
        await inventory.initialize()
        linked = unchanged = 0
        for row in rows:
            if await inventory.link_legacy(row["serial_number"], row["contact_id"]):
                linked += 1
            else:
                unchanged += 1
        return linked, unchanged, await inventory.unlinked_count()
    finally:
        await inventory.close()


async def _main(path: Path) -> None:
    linked, unchanged, remaining = await link_file(path)
    print(f"Linked: {linked}; unchanged: {unchanged}; unlinked remaining: {remaining}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("json_file", type=Path)
    args = parser.parse_args()
    asyncio.run(_main(args.json_file))
