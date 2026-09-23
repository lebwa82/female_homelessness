from pathlib import Path

from app.certificate_documents import parse_certificate_pdf
from app.chatwoot.certificates import POOL_AIDS
from scripts.generate_test_certificate_pdfs import generate


def test_generated_pdf_batch_is_unique_parseable_and_has_expected_pool_mapping(
    tmp_path: Path,
) -> None:
    paths = generate(tmp_path, 2)
    parsed = [parse_certificate_pdf(path.read_bytes(), path.name) for path in paths]

    assert len(parsed) == 4
    assert all(item.is_test for item in parsed)
    assert len({item.activation_code for item in parsed}) == 4
    assert len({item.serial_number for item in parsed}) == 4
    assert POOL_AIDS["pyaterochka"][1] == ("food_card", "children_card")
    assert POOL_AIDS["ozon"][1] == ("medicine_card", "hostel_3_nights")
