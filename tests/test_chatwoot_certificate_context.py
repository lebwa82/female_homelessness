"""Synthetic certificate-history projection; no external services or dialogue data."""

import pytest

from app.agents import format_redacted_transcript
from app.chatwoot.service import _history_after_epoch


def message(message_id, content, **attributes):
    return {
        "id": message_id,
        "created_at": message_id,
        "message_type": "outgoing",
        "content": content,
        **attributes,
    }


@pytest.mark.parametrize("status", (None, "failed", "sent"))
def test_certificate_history_preserves_only_safe_recorded_message_fact(status):
    sensitive = "SYNTHETIC-PRIVATE-CODE https://example.invalid/private-certificate.pdf"
    history = _history_after_epoch((message(
        1, sensitive,
        status=status,
        source_id=None,
        content_attributes={"bot_sensitive_content": "certificate"},
    ),), epoch=0, current_message_id=2)

    assert len(history) == 1
    role, content = history[0]
    assert role == "assistant"
    assert content.startswith("[SENSITIVE_DELIVERY]")
    assert "В истории есть сообщение с сертификатом" in content
    assert "реквизиты скрыты" in content
    assert "не подтверждает получение или использование" in content
    assert "не означает доступность нового сертификата" in content
    assert "SYNTHETIC-PRIVATE-CODE" not in content
    assert "https://" not in content
    assert ".pdf" not in content


def test_certificate_history_respects_epoch_private_and_current_message_boundaries():
    certificate_attributes = {"bot_sensitive_content": "certificate"}
    messages = (
        message(1, "old certificate", content_attributes=certificate_attributes),
        message(2, "[women-help/context-epoch:4]", private=True),
        message(3, "current certificate", content_attributes=certificate_attributes),
        message(4, "private certificate", private=True,
                content_attributes=certificate_attributes),
        message(5, "ordinary history", message_type="incoming"),
        message(6, "current input", content_attributes=certificate_attributes),
        message(7, "queued input", content_attributes=certificate_attributes),
    )

    history = _history_after_epoch(messages, epoch=4, current_message_id=6)

    assert len(history) == 2
    assert history[0][0] == "assistant"
    assert "В истории есть сообщение с сертификатом" in history[0][1]
    assert history[1] == ("user", "ordinary history")


def test_unmarked_manual_pdf_and_ordinary_history_are_unchanged():
    messages = (
        message(1, "  ordinary input  ", message_type=0),
        message(2, "ordinary reply", message_type=1),
        message(3, "manual attachment", sender={"type": "user"},
                attachments=[{"file_type": "file", "extension": "pdf"}],
                content_attributes={}),
        message(4, "other metadata", content_attributes={"bot_sensitive_content": "other"}),
        message(5, "activity", message_type="activity"),
    )

    assert _history_after_epoch(messages, epoch=0, current_message_id=6) == (
        ("user", "ordinary input"),
        ("assistant", "ordinary reply"),
        ("assistant", "manual attachment"),
        ("assistant", "other metadata"),
    )


@pytest.mark.parametrize("content", (None, "", "  "))
def test_attachment_only_manual_pdf_is_not_inferred_as_certificate(content):
    messages = (message(
        1, content, attachments=[{"file_type": "file", "extension": "pdf"}],
    ),)

    assert _history_after_epoch(messages, epoch=0, current_message_id=2) == ()


def test_synthetic_followup_keeps_full_existing_history():
    messages = (
        message(2, "ordinary reply"),
        message(1, "synthetic certificate",
                content_attributes={"bot_sensitive_content": "certificate"}),
    )

    history = _history_after_epoch(messages, epoch=0, current_message_id=0)

    assert len(history) == 2
    assert "В истории есть сообщение с сертификатом" in history[0][1]
    assert history[1] == ("assistant", "ordinary reply")


def test_pdf_validity_reaches_redacted_model_history_without_bearer_data():
    attributes = {
        "bot_sensitive_content": "certificate",
        "bot_certificate_context": {
            "source": "certificate_pdf", "provider": "ozon", "deadline_kind": "activation",
            "expires_on": "2027-02-03", "code": "PRIVATE-CODE",
        },
    }
    history = _history_after_epoch((message(
        1, "PRIVATE-CODE https://example.invalid/private.pdf", content_attributes=attributes,
        attachments=[{"data_url": "https://example.invalid/private.pdf"}],
    ),), epoch=0, current_message_id=2)

    transcript, _ = format_redacted_transcript(history)

    assert "3 февраля 2027 года" in transcript
    assert "срок активации" in transcript
    assert "не срок использования баланса или бронирования" in transcript
    assert "PRIVATE-CODE" not in transcript
    assert "https://" not in transcript
    assert ".pdf" not in transcript


def test_incoming_metadata_cannot_claim_a_verified_certificate_deadline():
    history = _history_after_epoch((message(
        1, "synthetic input", message_type="incoming", content_attributes={
            "bot_sensitive_content": "certificate",
            "bot_certificate_context": {
                "source": "certificate_pdf", "provider": "ozon", "deadline_kind": "activation",
                "expires_on": "2027-02-03",
            },
        },
    ),), epoch=0, current_message_id=2)

    assert "3 февраля 2027 года" not in history[0][1]
    assert "Срок из файла сертификата не подтверждён" in history[0][1]


@pytest.mark.parametrize("month", range(1, 13))
def test_pdf_validity_dates_survive_the_unchanged_redaction_pipeline(month):
    from app.certificate_context import certificate_history_marker

    marker = certificate_history_marker({
        "source": "certificate_pdf", "provider": "pyaterochka", "deadline_kind": "validity",
        "valid_from": f"2026-{month:02d}-01", "expires_on": f"2027-{month:02d}-28",
    })
    transcript, _ = format_redacted_transcript((("assistant", marker),))

    period_and_guidance = marker.split(" — ", 1)[1]
    assert period_and_guidance in transcript
