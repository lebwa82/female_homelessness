"""Validate and import a directory of bearer-certificate PDFs into private S3."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from app.certificate_documents import CertificateObjectRef, parse_certificate_pdf
from scripts.certificate_pdf_runtime import services


async def run(directory: Path) -> None:
    paths = sorted(directory.glob("*.pdf"))
    if not paths:
        raise ValueError("no PDF certificates found")
    parsed = [parse_certificate_pdf(path.read_bytes(), path.name) for path in paths]
    fingerprints = {(item.activation_code, item.serial_number, item.pdf_sha256) for item in parsed}
    if len(fingerprints) != len(parsed):
        raise ValueError("duplicate certificate found in import batch")
    inventory, store = services()
    uploaded: list[CertificateObjectRef] = []
    try:
        await inventory.initialize()
        for item in parsed:
            uploaded.append(await store.upload(item))
        imported, _ = await inventory.import_documents(zip(parsed, uploaded, strict=True))
    except Exception:
        for ref in uploaded:
            await store.delete(ref)
        raise
    finally:
        await inventory.close()
    print(f"Imported {imported} PDF certificates")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if not args.directory.is_dir():
        raise SystemExit("certificate import path must be a directory")
    asyncio.run(run(args.directory))


if __name__ == "__main__":
    main()
