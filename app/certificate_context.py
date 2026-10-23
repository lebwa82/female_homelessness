"""Allowlisted facts from a certificate PDF, without its bearer credentials."""

from __future__ import annotations

import re
from datetime import date

_HISTORY_MARKER = (
    "[SENSITIVE_DELIVERY] В истории есть сообщение с сертификатом; реквизиты скрыты. "
    "Это не подтверждает получение или использование и не означает доступность нового сертификата."
)
_DEADLINE_KINDS = {"ozon": "activation", "pyaterochka": "validity"}
_MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def certificate_context_from_pdf(payload: bytes) -> dict[str, str] | None:
    # Reuse the supported-provider parser, including its QR and document checks.
    # Never send the original PDF, extracted text or parser errors to the model.
    from pypdf.errors import PdfReadError

    from app.certificate_documents import MOSCOW_TIME, parse_certificate_pdf

    try:
        parsed = parse_certificate_pdf(payload, "certificate.pdf")
    except (ValueError, PdfReadError, RuntimeError):
        # A context-enrichment failure must not block the existing delivery path.
        return None
    context = {
        "source": "certificate_pdf",
        "provider": parsed.provider_slug,
        "deadline_kind": _DEADLINE_KINDS.get(parsed.provider_slug, ""),
        "expires_on": parsed.expires_at.astimezone(MOSCOW_TIME).date().isoformat(),
    }
    if parsed.valid_from is not None:
        context["valid_from"] = parsed.valid_from.astimezone(MOSCOW_TIME).date().isoformat()
    return validated_certificate_context(context)


def validated_certificate_context(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict) or value.get("source") != "certificate_pdf":
        return None
    provider = value.get("provider")
    if not isinstance(provider, str) or provider not in _DEADLINE_KINDS:
        return None
    if value.get("deadline_kind") != _DEADLINE_KINDS[provider]:
        return None
    result = {
        "source": "certificate_pdf", "provider": provider,
        "deadline_kind": _DEADLINE_KINDS[provider],
    }
    for field in ("expires_on", "valid_from"):
        raw = value.get(field)
        if field == "valid_from" and raw is None:
            continue
        if not isinstance(raw, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return None
        try:
            result[field] = date.fromisoformat(raw).isoformat()
        except ValueError:
            return None
    if result.get("valid_from", "") > result["expires_on"]:
        return None
    return result


def certificate_history_marker(value: object = None) -> str:
    context = validated_certificate_context(value)
    if context is None:
        return (
            f"{_HISTORY_MARKER} Срок из файла сертификата не подтверждён: "
            "предложи свериться с самим сертификатом, не придумывай дату "
            "и не рассчитывай срок от даты выдачи."
        )
    expires = _display_date(context["expires_on"])
    if context["deadline_kind"] == "activation":
        detail = (
            f"Указанный срок активации Ozon — до {expires} включительно. "
            "Это не срок использования баланса или бронирования."
        )
    else:
        starts = context.get("valid_from")
        period = (
            f"с {_display_date(starts)} по {expires}"
            if starts else f"до {expires}"
        )
        detail = f"Указанный срок действия сертификата Пятёрочки — {period} включительно."
    return (
        f"{_HISTORY_MARKER} Данные из файла сертификата в этом сообщении: {detail} "
        "Ориентируйся на этот срок, не рассчитывай его от даты выдачи."
    )


def _display_date(value: str) -> str:
    # Dotted numeric dates can be mistaken for URLs by the normal PII pipeline.
    # A month name preserves the exact date without bypassing that pipeline.
    parsed = date.fromisoformat(value)
    return f"{parsed.day} {_MONTHS[parsed.month - 1]} {parsed.year} года"
