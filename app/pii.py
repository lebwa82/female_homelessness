"""Local Russian PII detection and masking with Presidio and spaCy."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import tldextract
from presidio_analyzer import AnalyzerEngine
from presidio_analyzer.nlp_engine import NlpEngineProvider
from presidio_analyzer.predefined_recognizers import CreditCardRecognizer

from app.pii_patterns import sensitive_matches

NLP_CONFIGURATION = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "ru", "model_name": "ru_core_news_sm"}],
}

_ENTITY_REPLACEMENTS = {
    "PERSON": "[ЧЕЛОВЕК]",
    "LOCATION": "[ЛОКАЦИЯ]",
    "PHONE_NUMBER": "[ТЕЛЕФОН]",
    "EMAIL_ADDRESS": "[EMAIL]",
    "CREDIT_CARD": "[КАРТА]",
    "IP_ADDRESS": "[IP]",
    "URL": "[ССЫЛКА]",
    "RU_SNILS": "[СНИЛС]",
    "RU_INN": "[ИНН]",
    "RU_PASSPORT": "[ПАСПОРТ]",
    "RU_FOREIGN_PASSPORT": "[ПАСПОРТ]",
    "RU_DRIVER_LICENSE": "[ВОДИТЕЛЬСКОЕ_УДОСТОВЕРЕНИЕ]",
    "RU_MILITARY_ID": "[ВОЕННЫЙ_БИЛЕТ]",
    "RU_BIRTH_CERTIFICATE": "[СВИДЕТЕЛЬСТВО_О_РОЖДЕНИИ]",
    "RU_BANK_ACCOUNT": "[БАНКОВСКИЙ_СЧЁТ]",
    "SECRET": "[СЕКРЕТ]",
}
SAFE_PII_ENTITY_TYPES = frozenset((*_ENTITY_REPLACEMENTS, "TELEGRAM_HANDLE"))
_PLACEHOLDERS = re.compile(
    r"(?<!\w)(?:"
    + "|".join(
        re.escape(value)
        for value in sorted(
            {
                *_ENTITY_REPLACEMENTS.values(),
                "[CONTACT]",
                "[ПЕРСОНАЛЬНЫЕ_ДАННЫЕ]",
                "[SENSITIVE_DELIVERY]",
            },
            key=len,
            reverse=True,
        )
    )
    + r")(?!\w)"
)

# Never let the PII path fetch or refresh the public suffix list.  The extractor is
# deliberately constructed once with bundled data only, so a redaction request is
# always local and deterministic even on a cold host.
tld_extractor = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

# Telegram handles are contacts even when a generic PII recognizer does not know
# about Telegram.  They are collected as spans on the original text and merged
# with Presidio spans in one pass, so placeholders cannot be redetected as PII.
_TELEGRAM_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{5,32}\b")
_URL_CANDIDATE = re.compile(
    r"""(?i)(?<!\w)(?:(?:[a-z][a-z0-9+.-]{1,20}://|mailto:)[^\s<>"'\x60]+"""
    r"""|[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:[/?\x23][^\s<>"'\x60]*)?)"""
)


@dataclass(frozen=True)
class RedactionResult:
    text: str
    audit: dict[str, Any]


@lru_cache(maxsize=1)
def analyzer() -> AnalyzerEngine:
    provider = NlpEngineProvider(nlp_configuration=NLP_CONFIGURATION)
    engine = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["ru"])
    engine.registry.add_recognizer(
        CreditCardRecognizer(
            supported_language="ru", context=["карта", "карты", "карточка", "номер карты"]
        )
    )
    return engine


def redact_with_audit(text: str) -> RedactionResult:
    """Mask original-text spans once and return only non-sensitive audit data."""
    protected = tuple(match.span() for match in _PLACEHOLDERS.finditer(text))
    # Keep offsets stable while preventing NLP from treating marker labels as
    # new names/entities. A non-whitespace filler also stops regex whitespace
    # from skipping a marker and consuming an unrelated following field.
    detection_text = _PLACEHOLDERS.sub(lambda match: "x" * len(match.group()), text)
    custom_spans = [
        *_telegram_spans(detection_text),
        *_url_spans(detection_text),
        *(
            _RedactionSpan(
                match.start,
                match.end,
                match.entity_type,
                _ENTITY_REPLACEMENTS[match.entity_type],
            )
            for match in sensitive_matches(detection_text)
        ),
    ]
    presidio_spans = [
        _RedactionSpan(
            result.start,
            result.end,
            result.entity_type,
            _ENTITY_REPLACEMENTS.get(result.entity_type, "[ПЕРСОНАЛЬНЫЕ_ДАННЫЕ]"),
        )
        for result in analyzer().analyze(text=detection_text, language="ru")
    ]
    custom_spans = [
        part for span in custom_spans for part in _outside_placeholders(span, protected, text)
    ]
    presidio_spans = [
        part for span in presidio_spans for part in _outside_placeholders(span, protected, text)
    ]
    # Merge the full covered ranges. Dropping an overlapping recognizer could
    # leave the rest of an email, credential-bearing URL or document exposed.
    selected = _non_overlapping((*custom_spans, *presidio_spans), custom_count=len(custom_spans))
    pieces: list[str] = []
    position = 0
    for span in selected:
        pieces.extend((text[position : span.start], span.replacement))
        position = span.end
    pieces.append(text[position:])
    entity_counts = Counter(span.entity_type for span in selected)
    return RedactionResult(
        text="".join(pieces),
        audit={
            "engine": "presidio",
            "language": "ru",
            "detected": bool(selected),
            "entity_counts": dict(sorted(entity_counts.items())),
            "entities_total": len(selected),
        },
    )


def redact_for_model(text: str) -> str:
    return redact_with_audit(text).text


@dataclass(frozen=True)
class _RedactionSpan:
    start: int
    end: int
    entity_type: str
    replacement: str


def _outside_placeholders(
    span: _RedactionSpan, protected: tuple[tuple[int, int], ...], text: str
) -> tuple[_RedactionSpan, ...]:
    """Preserve markers without discarding sensitive text on either side."""
    if not any(span.start < end and start < span.end for start, end in protected):
        return (span,)
    pieces: list[_RedactionSpan] = []
    start = span.start
    for left, right in (*protected, (span.end, span.end)):
        end = min(left, span.end)
        # A regex may include JSON closing delimiters after an existing marker.
        if start < end and text[start:end].strip(" \t\r\n,;:{}[]()\"'"):
            pieces.append(_RedactionSpan(start, end, span.entity_type, span.replacement))
        start = max(start, right)
        if start >= span.end:
            break
    return tuple(pieces)


def _telegram_spans(text: str) -> tuple[_RedactionSpan, ...]:
    return tuple(
        _RedactionSpan(match.start(), match.end(), "TELEGRAM_HANDLE", "[CONTACT]")
        for match in _TELEGRAM_HANDLE.finditer(text)
    )


def _url_spans(text: str) -> tuple[_RedactionSpan, ...]:
    """Find URL spans using the same non-refreshing PSL extractor used at runtime."""
    return tuple(
        _RedactionSpan(match.start(), match.end(), "URL", "[ССЫЛКА]")
        for match in _URL_CANDIDATE.finditer(text)
        if (
            tld_extractor(match.group()).suffix
            or "://" in match.group()
            or match.group().lower().startswith("mailto:")
        )
    )


def _non_overlapping(
    spans: tuple[_RedactionSpan, ...], *, custom_count: int
) -> tuple[_RedactionSpan, ...]:
    """Union overlaps and retain a meaningful marker for each connected range."""
    priority = {"SECRET": 100, "EMAIL_ADDRESS": 90, "URL": 80, "TELEGRAM_HANDLE": 80}
    ranked = sorted(
        (
            (
                span,
                (
                    priority.get(span.entity_type, 85 if span.entity_type.startswith("RU_") else 0),
                    index < custom_count,
                ),
            )
            for index, span in enumerate(spans)
            if span.end > span.start
        ),
        key=lambda item: (item[0].start, item[0].end),
    )
    selected: list[_RedactionSpan] = []
    current_rank = (-1, False)
    for span, rank in ranked:
        if not selected or span.start >= selected[-1].end:
            selected.append(span)
            current_rank = rank
            continue
        current = selected[-1]
        marker = span if rank > current_rank else current
        selected[-1] = _RedactionSpan(
            current.start, max(current.end, span.end), marker.entity_type, marker.replacement
        )
        current_rank = max(current_rank, rank)
    return tuple(selected)
