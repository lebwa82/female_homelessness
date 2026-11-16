from types import SimpleNamespace

import pytest

from app import certificate_documents as documents

NUMBER = "TESTCERTIFICATE01XYZ"


def parse_with_number(monkeypatch, field, qr_values):
    text = f"Пятёрочка TEST / НЕ ДЕЙСТВИТЕЛЕН\n{field}\n01.01.2025 - 31.12.2030\n2000 ₽"
    page = SimpleNamespace(extract_text=lambda: text)
    monkeypatch.setattr(documents, "PdfReader", lambda _: SimpleNamespace(
        is_encrypted=False, pages=[page],
    ))
    monkeypatch.setattr(documents, "_decode_qr_values", lambda _: qr_values)
    return documents.parse_certificate_pdf(b"%PDF-synthetic-only", "synthetic.pdf")


@pytest.mark.parametrize("field", [
    f"Номер:\n{NUMBER[:17]} {NUMBER[17:]}",
    f"  НОМЕР И QR-PAYLOAD\r\n{NUMBER[:17]}\u00a0{NUMBER[17:]} ",
    f"Номер {NUMBER}",
    f"Номер: {NUMBER}\nСледующее поле: пример",
])
def test_complete_printed_number_matches_qr(monkeypatch, field):
    parsed = parse_with_number(monkeypatch, field, (NUMBER,))
    assert parsed.activation_code == parsed.serial_number == NUMBER


@pytest.mark.parametrize("field,qr_value,serial_number", [
    (f"Номер:\n{NUMBER[:17]} {NUMBER[17:]}", NUMBER[:17], NUMBER),
    (f"Номер: {NUMBER[:17]}\n{NUMBER[17:]}", NUMBER, NUMBER[:17]),
    (f"Номер: {NUMBER}", "DIFFERENTQR123", NUMBER),
    (f"Номер: {NUMBER}", "A", NUMBER),
    (f"Номер: {NUMBER}", "A" * 256, NUMBER),
    (f"Номер: {NUMBER[:6]}\u00a0{NUMBER[6:]}", "DIFFERENTQR123", NUMBER),
    (f"Номер: {NUMBER}\nСледующее поле: пример", "DIFFERENTQR123", NUMBER),
])
def test_printed_number_and_qr_are_independent(monkeypatch, field, qr_value, serial_number):
    parsed = parse_with_number(monkeypatch, field, (qr_value,))
    assert parsed.activation_code == qr_value
    assert parsed.serial_number == serial_number
    assert parsed.nominal_rubles == 2000
    assert parsed.valid_from.date().isoformat() == "2025-01-01"
    assert parsed.expires_at.date().isoformat() == "2030-12-31"


@pytest.mark.parametrize("field,qr_values", [
    (f"Номер: {NUMBER} extra!", (NUMBER,)),
    (f"Номер: {NUMBER}.", (NUMBER,)),
    (f"Номер: {NUMBER}\nНомер: DIFFERENT", (NUMBER,)),
    ("Номер:\n", (NUMBER,)),
    ("Номер: ТЕСТ123", (NUMBER,)),
    (f"Номер: {NUMBER}", (NUMBER, "OTHER")),
    (f"Номер: {NUMBER}", ()),
    (f"Серийный номер: {NUMBER}", (NUMBER,)),
])
def test_rejects_ambiguous_or_invalid_numbers(monkeypatch, field, qr_values):
    with pytest.raises(ValueError):
        parse_with_number(monkeypatch, field, qr_values)


@pytest.mark.parametrize("qr_value", [
    "", "ТЕСТ123", "https://example.invalid/TEST123", "TEST 123", "TEST-123",
])
def test_rejects_empty_non_ascii_or_url_qr(monkeypatch, qr_value):
    with pytest.raises(ValueError):
        parse_with_number(monkeypatch, f"Номер: {NUMBER}", (qr_value,))


def test_ozon_keeps_its_independent_activation_and_serial_fields(monkeypatch):
    text = (
        "Ozon TEST / НЕ ДЕЙСТВИТЕЛЕН\nКод активации OZON-ACTIVATION\n"
        "Серийный номер OZON-SERIAL\nАктивировать до 31.12.2030\n2000 ₽"
    )
    page = SimpleNamespace(extract_text=lambda: text)
    monkeypatch.setattr(documents, "PdfReader", lambda _: SimpleNamespace(
        is_encrypted=False, pages=[page],
    ))
    monkeypatch.setattr(documents, "_decode_qr_values", lambda _: pytest.fail(
        "Ozon parsing must not decode a Pyaterochka QR",
    ))

    parsed = documents.parse_certificate_pdf(b"%PDF-synthetic-only", "synthetic.pdf")

    assert parsed.provider_slug == "ozon"
    assert parsed.activation_code == "OZON-ACTIVATION"
    assert parsed.serial_number == "OZON-SERIAL"
