import json
import socket
from types import SimpleNamespace

import pytest

from app import pii
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


@pytest.mark.parametrize(
    ("original", "secret", "marker"),
    [
        ("Карта: 4111 1111 1111 1111", "4111", "[КАРТА]"),
        ("Карта: 4111111111111111", "4111111111111111", "[КАРТА]"),
        ("Почта: test.user@example.com", "test.user", "[EMAIL]"),
        ("Почта: person+help@example.org", "person+help", "[EMAIL]"),
        ("mailto:person@example.org", "person", "[EMAIL]"),
        ("Паспорт: 4510 123456", "123456", "[ПАСПОРТ]"),
        ("Паспорт РФ: 45 10 123456", "123456", "[ПАСПОРТ]"),
        ("Паспорт серия 4510 номер 123456", "123456", "[ПАСПОРТ]"),
        ("Паспорт: серия: 45 10, номер: 123456", "123456", "[ПАСПОРТ]"),
        ("Загранпаспорт: 72 1234567", "1234567", "[ПАСПОРТ]"),
        ("Заграничный паспорт: 721234567", "721234567", "[ПАСПОРТ]"),
        ("СНИЛС: 112-233-445 95", "112", "[СНИЛС]"),
        ("СНИЛС: 112-233-445 96", "112", "[СНИЛС]"),
        ("112-233-445 95", "112", "[СНИЛС]"),
        ("11223344595", "11223344595", "[СНИЛС]"),
        ("ИНН: 500100732259", "500100732259", "[ИНН]"),
        ("ИНН: 500100732250", "500100732250", "[ИНН]"),
        ("500100732259", "500100732259", "[ИНН]"),
        ("7707083893", "7707083893", "[ИНН]"),
        ("Водительское удостоверение: 77 12 345678", "345678", "[ВОДИТЕЛЬСКОЕ_УДОСТОВЕРЕНИЕ]"),
        ("Военный билет: АБ 1234567", "1234567", "[ВОЕННЫЙ_БИЛЕТ]"),
        ("Свидетельство о рождении: IV-МЮ №123456", "123456", "[СВИДЕТЕЛЬСТВО_О_РОЖДЕНИИ]"),
        ("Расчётный счёт: 40817810099910004312", "40817810099910004312", "[БАНКОВСКИЙ_СЧЁТ]"),
        ("р/с 40817 8100 9991 0004 312", "40817", "[БАНКОВСКИЙ_СЧЁТ]"),
        ("Пароль: ExampleSecret123!", "ExampleSecret123!", "[СЕКРЕТ]"),
        ('{"password": "value with spaces, punctuation"}', "value with spaces", "[СЕКРЕТ]"),
        ("POSTGRES_PASSWORD='synthetic secret'", "synthetic secret", "[СЕКРЕТ]"),
        ("API_KEY=synthetic_key_123456789", "synthetic_key_123456789", "[СЕКРЕТ]"),
        ("ключ шифрования: synthetic_encryption_key", "synthetic_encryption_key", "[СЕКРЕТ]"),
        ("Authorization: Bearer synthetic_api_token_123456789", "synthetic_api_token", "[СЕКРЕТ]"),
        ("Authorization: Basic dGVzdDpzeW50aGV0aWM=", "dGVzdDpzeW50aGV0aWM", "[СЕКРЕТ]"),
        ("-Password 'synthetic pass phrase'", "synthetic pass phrase", "[СЕКРЕТ]"),
        ('ConvertTo-SecureString "synthetic secret" -AsPlainText', "synthetic secret", "[СЕКРЕТ]"),
        (
            "ConvertTo-SecureString -String 'synthetic secret' -AsPlainText",
            "synthetic secret",
            "[СЕКРЕТ]",
        ),
        (
            "ConvertTo-SecureString -AsPlainText -Force -String 'synthetic secret'",
            "synthetic secret",
            "[СЕКРЕТ]",
        ),
        ("ghp_" + "a" * 36, "a" * 36, "[СЕКРЕТ]"),
        ("sk-proj-" + "a" * 40, "a" * 40, "[СЕКРЕТ]"),
        ("1234567890:" + "a" * 35, "a" * 35, "[СЕКРЕТ]"),
        (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c3ludGhldGljX3NpZ25hdHVyZQ",
            "eyJzdWIiOiJ0ZXN0In0",
            "[СЕКРЕТ]",
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nsynthetic_key_material\n-----END PRIVATE KEY-----",
            "synthetic_key_material",
            "[СЕКРЕТ]",
        ),
        (
            "-----BEGIN RSA PRIVATE KEY-----\nsynthetic_key_material",
            "synthetic_key_material",
            "[СЕКРЕТ]",
        ),
    ],
)
def test_sensitive_values_are_removed_from_text_and_audit(original, secret, marker) -> None:
    result = redact_with_audit(original)
    assert secret not in result.text
    assert secret not in json.dumps(result.audit, ensure_ascii=False)
    assert marker in result.text
    assert result.audit["entities_total"] == sum(result.audit["entity_counts"].values())
    assert redact_for_model(result.text) == result.text


@pytest.mark.parametrize(
    "url",
    [
        "https://testuser:ExampleSecret123@example.com/account",
        "https://testuser:ExampleSecret123@localhost:8080/account",
        "postgresql://testuser:ExampleSecret123@internal:5432/db",
        "https://example.com?token=ExampleSecret123",
        "https://example.com:8443/path#ExampleSecret123",
        "http://127.0.0.1:8080/?token=ExampleSecret123",
    ],
)
def test_complete_urls_include_credentials_ports_queries_and_fragments(url) -> None:
    result = redact_with_audit(url)
    assert "ExampleSecret123" not in result.text
    assert "testuser" not in result.text
    assert result.text in {"[ССЫЛКА]", "[EMAIL]", "[СЕКРЕТ]"}


def test_chained_overlaps_cannot_leave_a_sensitive_suffix(monkeypatch) -> None:
    # A short custom domain overlaps a full email, which overlaps a longer span.
    results = [
        SimpleNamespace(start=0, end=21, entity_type="EMAIL_ADDRESS"),
        SimpleNamespace(start=19, end=27, entity_type="PERSON"),
    ]
    monkeypatch.setattr(pii, "analyzer", lambda: SimpleNamespace(analyze=lambda **_: results))
    result = redact_with_audit("test.user@example.comSECRET!")
    assert result.text == "[EMAIL]!"


@pytest.mark.parametrize(
    "text",
    [
        "Номер заявки 123456, сумма 1500 рублей, встреча в 15:30.",
        "Как поменять пароль от почты?",
        "Нужна помощь с документами и банковским счётом.",
        "112-233-445 96",
        "500100732250",
        "000000000000",
    ],
)
def test_ordinary_text_and_unlabeled_invalid_identifiers_are_preserved(text) -> None:
    assert redact_for_model(text) == text


def test_redaction_is_local_even_on_first_analyzer_load(monkeypatch) -> None:
    def network_forbidden(*args, **kwargs):
        raise AssertionError("PII detection attempted network access")

    monkeypatch.setattr(socket.socket, "connect", network_forbidden)
    pii.analyzer.cache_clear()
    result = redact_with_audit("СНИЛС: 112-233-445 95; https://example.org?token=synthetic_secret")
    assert "112" not in result.text
    assert "synthetic_secret" not in result.text


def test_redacting_existing_markers_is_idempotent() -> None:
    first = redact_for_model("ИНН: 500100732259; password=synthetic_password; @sample_contact")
    assert redact_for_model(first) == first


def test_a_marker_cannot_hide_an_adjacent_secret_suffix() -> None:
    result = redact_for_model("password=[СЕКРЕТ]synthetic_tail")
    assert "synthetic_tail" not in result


def test_a_marker_does_not_allow_sensitive_text_around_it_to_escape(monkeypatch) -> None:
    value = "synthetic_left [EMAIL] synthetic_right"
    result = SimpleNamespace(start=0, end=len(value), entity_type="PERSON")
    monkeypatch.setattr(pii, "analyzer", lambda: SimpleNamespace(analyze=lambda **_: [result]))
    redacted = redact_for_model(value)
    assert "synthetic_left" not in redacted
    assert "synthetic_right" not in redacted
    assert "[EMAIL]" in redacted


def test_persisted_audit_keeps_new_categories_and_never_copies_arbitrary_keys() -> None:
    from app.db import sanitize_agent_audit

    sanitized = sanitize_agent_audit(
        {
            "pii_redaction": {
                "engine": "presidio",
                "language": "ru",
                "detected": True,
                "entities_total": 9,
                "entity_counts": {
                    "RU_SNILS": 1,
                    "RU_INN": 2,
                    "SECRET": 3,
                    "synthetic_sensitive_key": 1,
                    "another_sensitive_key": 2,
                },
            }
        }
    )
    assert sanitized["pii_redaction"]["entity_counts"] == {
        "RU_SNILS": 1,
        "RU_INN": 2,
        "SECRET": 3,
        "OTHER": 3,
    }
    assert "synthetic_sensitive_key" not in json.dumps(sanitized)
    assert "another_sensitive_key" not in json.dumps(sanitized)


@pytest.mark.asyncio
async def test_both_llm_inputs_mask_documents_cards_and_secrets() -> None:
    from app.agents import AgentCallResult, AgentContext, YandexAgentGateway

    captured = []

    async def call(agent_name, instructions, input_text):
        captured.append((agent_name, input_text))
        return AgentCallResult(payload={}, audit={"status": "error"})

    result = await YandexAgentGateway(call=call).evaluate(
        AgentContext(
            history=(
                ("user", "https://user:synthetic_url_secret@localhost/account"),
                (
                    "user",
                    "Паспорт: 4510 123456; карта 4111 1111 1111 1111; password=synthetic_password",
                ),
            ),
            state="open_conversation",
        )
    )
    assert {name for name, _ in captured} == {"risk", "support"}
    for _, text in captured:
        for sensitive in ("synthetic_url_secret", "123456", "4111", "synthetic_password"):
            assert sensitive not in text
    audit = json.dumps([result.safety_audit, result.support_audit])
    assert "synthetic_password" not in audit
    assert "123456" not in audit


@pytest.mark.asyncio
async def test_masking_failure_never_sends_a_provider_request(monkeypatch) -> None:
    from app.agents import AgentContext, YandexAgentGateway

    calls = []

    def broken_redactor(text):
        raise RuntimeError("synthetic redaction failure")

    async def call(*args):
        calls.append(args)
        raise AssertionError("provider must not be called")

    monkeypatch.setattr("app.agents.redact_with_audit", broken_redactor)
    await YandexAgentGateway(call=call).evaluate(
        AgentContext(
            history=(("user", "password=synthetic_password"),),
            state="open_conversation",
        )
    )
    assert calls == []
