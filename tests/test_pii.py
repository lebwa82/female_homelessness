from app.pii import redact_for_model, redact_with_audit


def test_redacts_russian_name_and_phone_locally() -> None:
    original = "Меня зовут Анна Иванова, мой телефон +7 999 123-45-67."
    redacted = redact_for_model(original)

    assert "Анна" not in redacted
    assert "999" not in redacted
    assert "[ЧЕЛОВЕК]" in redacted
    assert "[ТЕЛЕФОН]" in redacted


def test_pii_audit_has_categories_but_no_detected_values() -> None:
    result = redact_with_audit("Меня зовут Анна Иванова, мой телефон +7 999 123-45-67.")

    assert result.audit["detected"] is True
    assert result.audit["entity_counts"] == {"PERSON": 1, "PHONE_NUMBER": 1}
    assert "Анна" not in str(result.audit)
    assert "999" not in str(result.audit)
