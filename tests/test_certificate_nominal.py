from types import SimpleNamespace

import pytest

from app import certificate_documents


def parse_amount(monkeypatch, provider, amount):
    if provider == "ozon":
        text = (
            "Ozon\nКод активации INVALID-TEST-ONLY\n"
            "Серийный номер INVALID-SERIAL\nАктивировать до 31.12.2030\n"
        )
    else:
        text = "Пятёрочка\nНомер INVALIDTESTONLY\n01.01.2025 - 31.12.2030\n"
        monkeypatch.setattr(
            certificate_documents, "_decode_qr_values", lambda _: ("INVALIDTESTONLY",),
        )
    page = SimpleNamespace(extract_text=lambda: text + amount)
    monkeypatch.setattr(
        certificate_documents, "PdfReader",
        lambda _: SimpleNamespace(is_encrypted=False, pages=[page]),
    )
    return certificate_documents.parse_certificate_pdf(b"%PDF-synthetic-test-only", "test.pdf")


@pytest.mark.parametrize("provider", ["ozon", "pyaterochka"])
@pytest.mark.parametrize("amount,expected", [
    ("2000 ₽", 2000), ("2 000 ₽", 2000), ("2\u00a0000 ₽", 2000),
    ("2\u202f000 ₽", 2000), ("2\u2009000 ₽", 2000), ("2\t000 руб.", 2000),
    ("2 000 руб.", 2000), ("2\u202f000 РУБ.", 2000), ("1 250 ₽", 1250),
    ("500 ₽", 500), ("1 000 000 ₽", 1_000_000), ("2  000 ₽", 2000),
    ("1\u2007250 ₽", 1250), ("1\u2002250 ₽", 1250), ("1\u2003250 ₽", 1250),
    ("2000\n₽", 2000), ("2 000\nруб.", 2000),
    ("Серийный номер 123456\n2000 ₽", 2000),
    ("Код активации 123456\n2 000 ₽", 2000),
    ("Номер 123456\n2000 ₽", 2000),
])
def test_parse_complete_nominal_before_staging(monkeypatch, provider, amount, expected):
    parsed = parse_amount(monkeypatch, provider, amount)
    assert parsed.nominal_rubles == expected
    assert parsed.provider_slug == provider


@pytest.mark.parametrize("provider", ["ozon", "pyaterochka"])
@pytest.mark.parametrize("amount", [
    "0 ₽", "0 000 ₽", "-2000 ₽", "- 2000 ₽", "−2000 ₽", "+2000 ₽",
    "12 34 ₽", "1 23 000 ₽", "2000,50 ₽", "2000.50 ₽", "2.000 ₽",
    "2147483648 ₽", "неизвестно ₽",
    "1\n250 ₽", "1\r\n250 ₽", "-\n2000 ₽",
    "Номинал: 1\n250 ₽", "Номинал: -\n2000 ₽", "1,\n250 ₽",
])
def test_invalid_nominal_is_rejected_before_staging(monkeypatch, provider, amount):
    with pytest.raises(ValueError):
        parse_amount(monkeypatch, provider, amount)
