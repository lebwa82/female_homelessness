import io
from types import SimpleNamespace

import pytest
from pypdf import PdfReader
from reportlab.pdfgen import canvas

from app import certificate_documents
from scripts.generate_test_certificate_pdfs import _fonts


def synthetic_ozon_pdf(amount="2 000 руб."):
    """Neutral PDF whose draw order differs from its visible layout."""
    _fonts()
    stream = io.BytesIO()
    page = canvas.Canvas(stream)
    page.setFont("CertificateSans", 12)
    for y, line in [
        (800, "Ozon TEST / НЕ ДЕЙСТВИТЕЛЕН"),
        (770, "Код активации INVALID-TEST-ONLY"),
        (740, "Серийный номер INVALID-SERIAL"),
        (710, "Активировать до 31.12.2030"),
        (500, "Наслаждайтесь покупками"),
        (480, "3"),
        (650, amount),
    ]:
        page.drawString(50, y, line)
    page.save()
    return stream.getvalue()


def test_real_pdf_layout_separates_instruction_number_from_nominal():
    payload = synthetic_ozon_pdf()
    plain = PdfReader(io.BytesIO(payload)).pages[0].extract_text()
    assert "3\n2 000 руб." in plain
    with pytest.raises(certificate_documents._AmbiguousNominalError):
        certificate_documents._nominal_rubles(plain)

    parsed = certificate_documents.parse_certificate_pdf(payload, "synthetic.pdf")

    assert parsed.nominal_rubles == 2000
    assert parsed.provider_slug == "ozon"
    assert parsed.activation_code == "INVALID-TEST-ONLY"
    assert parsed.serial_number == "INVALID-SERIAL"
    assert parsed.is_test


@pytest.mark.parametrize("amount", ["0 руб.", "-2000 руб.", "2000,50 руб."])
def test_layout_does_not_make_invalid_nominal_valid(amount):
    with pytest.raises(ValueError):
        certificate_documents.parse_certificate_pdf(synthetic_ozon_pdf(amount), "synthetic.pdf")


@pytest.mark.parametrize("layout_amount", ["1\n250 ₽", "-\n2000 ₽", ""])
def test_ambiguous_or_missing_layout_amount_stays_rejected(monkeypatch, layout_amount):
    fields = (
        "Ozon\nКод активации INVALID-TEST-ONLY\n"
        "Серийный номер INVALID-SERIAL\nАктивировать до 31.12.2030\n"
    )
    modes = []

    def extract_text(**kwargs):
        modes.append(kwargs.get("extraction_mode", "plain"))
        return fields + (layout_amount if kwargs else "3\n2 000 ₽")

    monkeypatch.setattr(certificate_documents, "PdfReader", lambda _: SimpleNamespace(
        is_encrypted=False, pages=[SimpleNamespace(extract_text=extract_text)],
    ))
    with pytest.raises(ValueError):
        certificate_documents.parse_certificate_pdf(b"%PDF-synthetic-only", "synthetic.pdf")
    assert modes == ["plain", "layout"]


def test_normal_pdf_does_not_need_layout_extraction(monkeypatch):
    modes = []

    def extract_text(**kwargs):
        modes.append(kwargs)
        return (
            "Ozon\nКод активации INVALID-TEST-ONLY\n"
            "Серийный номер INVALID-SERIAL\nАктивировать до 31.12.2030\n2000 ₽"
        )

    monkeypatch.setattr(certificate_documents, "PdfReader", lambda _: SimpleNamespace(
        is_encrypted=False, pages=[SimpleNamespace(extract_text=extract_text)],
    ))
    assert certificate_documents.parse_certificate_pdf(
        b"%PDF-synthetic-only", "synthetic.pdf",
    ).nominal_rubles == 2000
    assert modes == [{}]
