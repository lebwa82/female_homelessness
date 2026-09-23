"""Parse bearer-certificate PDFs and keep their binary payloads in private S3."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

import boto3
import numpy as np
import pymupdf
import zxingcpp
from pypdf import PdfReader

MOSCOW_TIME = ZoneInfo("Europe/Moscow")
MAX_CERTIFICATE_PDF_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ParsedCertificatePdf:
    provider_slug: str
    provider: str
    nominal_rubles: int
    activation_code: str
    serial_number: str
    valid_from: datetime | None
    expires_at: datetime
    is_test: bool
    filename: str
    pdf_sha256: str
    pdf_bytes: bytes


@dataclass(frozen=True, slots=True)
class CertificateObjectRef:
    bucket: str
    key: str
    version_id: str | None
    sha256: str
    size: int


class CertificateObjectStore(Protocol):
    async def upload(self, parsed: ParsedCertificatePdf) -> CertificateObjectRef: ...

    async def download(self, ref: CertificateObjectRef) -> bytes: ...

    async def delete(self, ref: CertificateObjectRef) -> None: ...


class S3CertificateObjectStore:
    """Small async facade over the S3-compatible blocking SDK."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
    ) -> None:
        self._bucket = bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    async def upload(self, parsed: ParsedCertificatePdf) -> CertificateObjectRef:
        prefix = "test" if parsed.is_test else "real"
        key = f"{prefix}/{uuid4().hex}.pdf"
        md5 = base64.b64encode(hashlib.md5(parsed.pdf_bytes, usedforsecurity=False).digest()).decode()

        def put() -> dict:
            return self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=parsed.pdf_bytes,
                ContentType="application/pdf",
                ContentMD5=md5,
                Metadata={"sha256": parsed.pdf_sha256},
            )

        result = await asyncio.to_thread(put)
        version_id = result.get("VersionId")
        return CertificateObjectRef(
            bucket=self._bucket,
            key=key,
            version_id=version_id if isinstance(version_id, str) else None,
            sha256=parsed.pdf_sha256,
            size=len(parsed.pdf_bytes),
        )

    async def download(self, ref: CertificateObjectRef) -> bytes:
        def get() -> bytes:
            kwargs = {"Bucket": ref.bucket, "Key": ref.key}
            if ref.version_id:
                kwargs["VersionId"] = ref.version_id
            response = self._client.get_object(**kwargs)
            try:
                return response["Body"].read()
            finally:
                response["Body"].close()

        payload = await asyncio.to_thread(get)
        if len(payload) != ref.size or hashlib.sha256(payload).hexdigest() != ref.sha256:
            raise ValueError("certificate object integrity check failed")
        return payload

    async def delete(self, ref: CertificateObjectRef) -> None:
        def remove() -> None:
            kwargs = {"Bucket": ref.bucket, "Key": ref.key}
            if ref.version_id:
                kwargs["VersionId"] = ref.version_id
            self._client.delete_object(**kwargs)

        await asyncio.to_thread(remove)


def parse_certificate_pdf(pdf_bytes: bytes, filename: str) -> ParsedCertificatePdf:
    if not pdf_bytes.startswith(b"%PDF-") or len(pdf_bytes) > MAX_CERTIFICATE_PDF_BYTES:
        raise ValueError("certificate must be a PDF no larger than 10 MiB")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    if reader.is_encrypted or len(reader.pages) != 1:
        raise ValueError("certificate PDF must be one unencrypted page")
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    normalized = re.sub(r"[ \t]+", " ", text)
    if _looks_like_ozon(normalized):
        return _parse_ozon(pdf_bytes, filename, normalized)
    if _looks_like_pyaterochka(normalized):
        return _parse_pyaterochka(pdf_bytes, filename, normalized)
    raise ValueError("unsupported certificate PDF provider")


def _looks_like_ozon(text: str) -> bool:
    return "Ozon" in text and re.search(r"Код активации", text, re.IGNORECASE) is not None


def _looks_like_pyaterochka(text: str) -> bool:
    return "Пятёроч" in text or "5ka.ru" in text


def _parse_ozon(pdf_bytes: bytes, filename: str, text: str) -> ParsedCertificatePdf:
    activation_code = _match(text, r"Код активации\s+([A-Za-z0-9-]+)")
    expires = _date(
        _match(text, r"Активировать (?:по|до)\s+(\d{2}\.\d{2}\.\d{4})"), end=True
    )
    serial_number = _match(text, r"Серийный номер\s+([A-Za-z0-9-]+)")
    nominal = int(_match(text, r"(\d+)\s*(?:₽|руб\.)"))
    return ParsedCertificatePdf(
        provider_slug="ozon",
        provider="Ozon",
        nominal_rubles=nominal,
        activation_code=activation_code,
        serial_number=serial_number,
        valid_from=None,
        expires_at=expires,
        is_test=_is_test(text),
        filename="certificate-ozon.pdf",
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        pdf_bytes=pdf_bytes,
    )


def _parse_pyaterochka(pdf_bytes: bytes, filename: str, text: str) -> ParsedCertificatePdf:
    number = _match(text, r"Номер(?: и QR-PAYLOAD)?\s*:?\s*([A-Za-z0-9]+)")
    date_pair = re.search(
        r"(\d{2}\.\d{2}\.\d{4})\s*[-—]\s*(\d{2}\.\d{2}\.\d{4})", text
    )
    if date_pair is None:
        raise ValueError("certificate validity period was not found")
    qr_values = _decode_qr_values(pdf_bytes)
    if qr_values != (number,):
        raise ValueError("certificate QR does not match its printed number")
    nominal = int(_match(text, r"(\d+)\s*(?:₽|руб\.)"))
    return ParsedCertificatePdf(
        provider_slug="pyaterochka",
        provider="Пятёрочка",
        nominal_rubles=nominal,
        activation_code=number,
        serial_number=number,
        valid_from=_date(date_pair.group(1), end=False),
        expires_at=_date(date_pair.group(2), end=True),
        is_test=_is_test(text),
        filename="certificate-pyaterochka.pdf",
        pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
        pdf_bytes=pdf_bytes,
    )


def _decode_qr_values(pdf_bytes: bytes) -> tuple[str, ...]:
    document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = document[0]
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
        image = np.frombuffer(pixmap.samples, dtype=np.uint8).reshape(
            pixmap.height, pixmap.width, pixmap.n
        )
        values = {
            result.text
            for result in zxingcpp.read_barcodes(image)
            if result.format == zxingcpp.BarcodeFormat.QRCode and result.text
        }
        return tuple(sorted(values))
    finally:
        document.close()


def _match(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    if match is None:
        raise ValueError("required certificate field was not found")
    return match.group(1)


def _date(value: str, *, end: bool) -> datetime:
    parsed = datetime.strptime(f"{value} +0300", "%d.%m.%Y %z").astimezone(MOSCOW_TIME)
    return parsed.replace(
        hour=23 if end else 0,
        minute=59 if end else 0,
        second=59 if end else 0,
        tzinfo=MOSCOW_TIME,
    )


def _is_test(text: str) -> bool:
    return "НЕ ДЕЙСТВИТЕЛЕН" in text.upper() and "TEST" in text.upper()
