import pytest

from app.certificate_admin import BatchSummary
from app.certificate_admin_bot import _batch_text
from app.config import Settings


def test_batch_summary_counts_every_received_document() -> None:
    summary = BatchSummary(
        batch_id=1,
        pool_slug="ozon",
        status="collecting",
        target_count=15,
        ready=12,
        duplicates=2,
        invalid=1,
    )

    assert summary.received == 15
    rendered = _batch_text(summary)
    assert "лекарства, хостел" in rendered
    assert "получено 15 из ожидаемых 15" in rendered


@pytest.mark.parametrize("value", ["-1", "abc", "1,broken", "1.5"])
def test_certificate_admin_rejects_invalid_bootstrap_ids(value: str) -> None:
    settings = Settings(CERTIFICATE_ADMIN_BOOTSTRAP_OWNER_IDS=value)

    with pytest.raises(ValueError, match="positive IDs"):
        settings.certificate_admin_owner_ids()
