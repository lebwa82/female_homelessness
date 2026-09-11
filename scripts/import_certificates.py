"""Import bearer certificates without exposing their codes in command output."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select

from app import db
from app.catalog import get_aid_item

MOSCOW_TIME = ZoneInfo("Europe/Moscow")


def _expiry(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%d.%m.%Y").replace(
            hour=23,
            minute=59,
            tzinfo=MOSCOW_TIME,
        )
    except ValueError as error:
        raise ValueError("activate_by must use DD.MM.YYYY") from error


def _validated(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError("each certificate must be a JSON object")
    required = {
        "aid_id",
        "provider",
        "nominal_rubles",
        "activation_code",
        "activate_by",
        "serial_number",
    }
    if set(raw) != required:
        raise ValueError(f"certificate fields must be exactly: {', '.join(sorted(required))}")
    item = get_aid_item(str(raw["aid_id"]))
    if item is None or item.fulfillment != "certificate":
        raise ValueError("aid_id must refer to certificate-based aid")
    nominal = raw["nominal_rubles"]
    if not isinstance(nominal, int) or isinstance(nominal, bool) or nominal <= 0:
        raise ValueError("nominal_rubles must be a positive integer")
    values = {
        "aid_id": item.id,
        "provider": str(raw["provider"]).strip(),
        "nominal_rubles": nominal,
        "activation_code": str(raw["activation_code"]).strip(),
        "expires_at": _expiry(str(raw["activate_by"])),
        "serial_number": str(raw["serial_number"]).strip(),
    }
    if not values["provider"] or not values["activation_code"] or not values["serial_number"]:
        raise ValueError("provider, activation_code and serial_number must not be blank")
    if values["expires_at"] <= datetime.now(UTC):
        raise ValueError("expired certificates cannot be imported")
    return values


async def import_file(path: Path) -> tuple[int, int]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise TypeError("the JSON root must be a list")
    rows = [_validated(item) for item in payload]
    imported = 0
    duplicates = 0
    async with db.repository_session() as session:
        for values in rows:
            result = await session.execute(
                db.postgres_insert(db.Certificate)
                .values(**values)
                .on_conflict_do_nothing()
                .returning(db.Certificate.id)
            )
            if result.scalar_one_or_none() is not None:
                imported += 1
                continue
            existing = await session.scalar(
                select(db.Certificate).where(
                    or_(
                        db.Certificate.activation_code == values["activation_code"],
                        db.Certificate.serial_number == values["serial_number"],
                    )
                )
            )
            comparable = {key: getattr(existing, key, None) for key in values}
            if comparable != values:
                raise ValueError("conflicting activation code or serial number")
            duplicates += 1
        await db.finish_repository_write(session)
    return imported, duplicates


async def _main(path: Path) -> None:
    await db.init_db()
    imported, duplicates = await import_file(path)
    print(f"Imported: {imported}; unchanged duplicates: {duplicates}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("json_file", type=Path)
    args = parser.parse_args()
    asyncio.run(_main(args.json_file))
