"""Return only test certificates to the available state for repeated testing."""

import asyncio

from scripts.certificate_pdf_runtime import services


async def run() -> None:
    inventory, _ = services()
    try:
        await inventory.initialize()
        count = await inventory.reset_test()
    finally:
        await inventory.close()
    print(f"Reset {count} test certificates")


if __name__ == "__main__":
    asyncio.run(run())
