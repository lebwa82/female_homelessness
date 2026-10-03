"""Exercise real PostgreSQL inventory transactions in a new isolated schema.

Never reads or writes the live inventory, never accesses S3 or sends documents.
Retains its small synthetic schema for inspection, rather than deleting data.
Run inside the configured agent container; credentials never leave its environment.
"""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.certificate_documents import CertificateObjectRef, ParsedCertificatePdf
from app.chatwoot.certificates import CertificateInventory, database_url
from app.config import settings


async def main():
    schema = f"acceptance_{uuid.uuid4().hex}"
    url = database_url(settings.certificate_database_password)
    admin = create_async_engine(url)
    inventory = CertificateInventory(url, identity_key=schema, account_id=0)
    await inventory.close()
    inventory._engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    checks = []

    def check(name, condition):
        checks.append({"name": name, "passed": bool(condition)})
        assert condition, name

    try:
        async with admin.begin() as connection:
            # Identifier consists exclusively of our fixed prefix and UUID hex.
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with inventory._engine.connect() as connection:
            check("isolated_search_path", (await connection.execute(text("SELECT current_schema()"))).scalar() == schema)
        await inventory.initialize()
        check("empty_pool", (await inventory.claim("food_card", "empty", 1)).status == "unavailable")
        now = datetime.now(UTC)
        documents = []
        for index, (nominal, days, starts) in enumerate([
            (3000, -1, -2), (1000, 30, -1), (3000, 3, -1), (3000, 30, 2),
            *[(3000, 30, -1)] * 6,
        ]):
            data = f"synthetic-only-{schema}-{index}".encode()
            digest = hashlib.sha256(data).hexdigest()
            parsed = ParsedCertificatePdf(
                provider_slug="pyaterochka", provider="Synthetic", nominal_rubles=nominal,
                activation_code=f"synthetic-{index}", serial_number=f"synthetic-{index}",
                valid_from=now + timedelta(days=starts), expires_at=now + timedelta(days=days),
                is_test=False, filename=f"synthetic-{index}.pdf", pdf_sha256=digest, pdf_bytes=data,
            )
            ref = CertificateObjectRef(bucket="synthetic-not-uploaded", key=f"{schema}/{index}",
                                       version_id=None, sha256=digest, size=len(data))
            documents.append((parsed, ref))
        check("import", await inventory.import_documents(documents) == (10, 0))
        same = await asyncio.gather(*(inventory.claim("food_card", f"same-{i}", 1) for i in range(8)))
        check("concurrent_same_recipient", all(c.status == "issued" for c in same)
              and len({c.certificate.serial_number for c in same}) == 1)
        first = same[0].certificate
        check("eligibility_filters", first.nominal_rubles == 3000 and first.expires_at > now + timedelta(days=7))
        await inventory.mark_failed(first.issuance_key)
        retry = await inventory.claim("children_card", "different-category", 1)
        check("failed_reuses_document", retry.certificate.serial_number == first.serial_number)
        await inventory.mark_submitted(first.issuance_key, 101)
        check("submitted_limit", (await inventory.claim("children_card", "submitted", 1)).status == "already_issued")
        check("same_effect_retry", (await inventory.claim("food_card", first.issuance_key, 1)).certificate.serial_number == first.serial_number)
        await inventory.mark_delivery_failed(101)
        check("delivery_failure_retry", (await inventory.claim("food_card", "failed-transport", 1)).certificate.serial_number == first.serial_number)
        await inventory.mark_delivered(101)
        await inventory.mark_delivery_failed(101)
        check("late_failure_does_not_reopen", (await inventory.claim("food_card", "after-delivery", 1)).status == "already_issued")
        others = await asyncio.gather(*(inventory.claim("food_card", f"recipient-{i}", i) for i in range(2, 10)))
        issued = [c.certificate.serial_number for c in others if c.status == "issued"]
        check("concurrent_stock_and_eligibility", len(issued) == len(set(issued)) == 5)
        check("exhausted_pool", (await inventory.claim("food_card", "exhausted", 99)).status == "unavailable")
    finally:
        await inventory.close()
        await admin.dispose()
        print(json.dumps({"schema": schema, "checks": checks}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:  # noqa: BLE001 - never disclose SQL parameters or credentials
        print(json.dumps({"error_type": type(error).__name__}))
        raise SystemExit(1) from None
