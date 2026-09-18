"""Import validated certificates into the active Chatwoot inventory."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from app.chatwoot.certificates import CertificateInventory, database_url
from app.config import settings
from scripts.import_certificates import _validated


async def import_file(path: Path) -> tuple[int, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError("the JSON root must be a list")
    if not settings.certificate_database_password:
        raise RuntimeError("CERTIFICATE_DATABASE_PASSWORD is required")
    rows = [_validated(item) for item in payload]
    inventory = CertificateInventory(
        database_url(settings.certificate_database_password),
        identity_key=settings.certificate_identity_key or "import-only",
        account_id=settings.chatwoot_account_id,
    )
    try:
        await inventory.initialize()
        return await inventory.import_rows(rows)
    finally:
        await inventory.close()


async def _main(path: Path) -> None:
    imported, duplicates = await import_file(path)
    print(f"Imported: {imported}; unchanged duplicates: {duplicates}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("json_file", type=Path)
    args = parser.parse_args()
    asyncio.run(_main(args.json_file))
