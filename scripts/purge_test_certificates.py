"""Delete test certificate objects and their database records."""

import asyncio

from scripts.certificate_pdf_runtime import services


async def run() -> None:
    inventory, store = services()
    try:
        await inventory.initialize()
        refs = await inventory.test_object_refs()
        for ref in refs:
            await store.delete(ref)
        count = await inventory.purge_test_records()
        await inventory.purge_legacy_test_inventory()
    finally:
        await inventory.close()
    print(f"Purged {count} test certificates")


if __name__ == "__main__":
    asyncio.run(run())
