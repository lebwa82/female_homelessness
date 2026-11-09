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


@pytest.mark.parametrize("field,qr_values", [
    (f"Номер:\n{NUMBER[:17]} {NUMBER[17:]}", (NUMBER[:17],)),
    (f"Номер: {NUMBER[:17]}\n{NUMBER[17:]}", (NUMBER,)),
    (f"Номер: {NUMBER} extra", (NUMBER,)),
    (f"Номер: {NUMBER}.", (NUMBER,)),
    (f"Номер: {NUMBER}\nНомер: DIFFERENT", (NUMBER,)),
    (f"Номер: {NUMBER}", (NUMBER, "OTHER")),
    (f"Номер: {NUMBER}", ()),
    (f"Серийный номер: {NUMBER}", (NUMBER,)),
])
def test_rejects_partial_ambiguous_or_invalid_numbers(monkeypatch, field, qr_values):
    with pytest.raises(ValueError):
        parse_with_number(monkeypatch, field, qr_values)
