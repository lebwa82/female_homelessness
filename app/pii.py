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
}

# Never let the PII path fetch or refresh the public suffix list.  The extractor is
# deliberately constructed once with bundled data only, so a redaction request is
# always local and deterministic even on a cold host.
tld_extractor = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

# Telegram handles are contacts even when a generic PII recognizer does not know
# about Telegram.  They are collected as spans on the original text and merged
# with Presidio spans in one pass, so placeholders cannot be redetected as PII.
_TELEGRAM_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{5,32}\b")
_URL_CANDIDATE = re.compile(r"(?i)\b(?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[^\s<>()]*)?")


@dataclass(frozen=True)
class RedactionResult:
    text: str
    audit: dict[str, Any]


@lru_cache(maxsize=1)
def analyzer() -> AnalyzerEngine:
    provider = NlpEngineProvider(nlp_configuration=NLP_CONFIGURATION)
    return AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["ru"])


def redact_with_audit(text: str) -> RedactionResult:
    """Mask original-text spans once and return only non-sensitive audit data."""
    custom_spans = [
        *_telegram_spans(text),
        *_url_spans(text),
    ]
    presidio_spans = [
        _RedactionSpan(
            result.start,
            result.end,
            result.entity_type,
            _ENTITY_REPLACEMENTS.get(result.entity_type, "[ПЕРСОНАЛЬНЫЕ_ДАННЫЕ]"),
        )
        for result in analyzer().analyze(text=text, language="ru")
    ]
    # Custom contact/URL detection wins overlaps because it has a reviewed,
    # stable replacement.  Presidio fills only the remaining original ranges.
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
        if tld_extractor(match.group()).suffix
    )


def _non_overlapping(
    spans: tuple[_RedactionSpan, ...], *, custom_count: int
) -> tuple[_RedactionSpan, ...]:
    """Prefer custom spans, then retain only disjoint original-text Presidio spans."""
    custom = sorted(spans[:custom_count], key=lambda span: (span.start, span.end))
    selected: list[_RedactionSpan] = []
    for span in custom:
        if not any(span.start < item.end and item.start < span.end for item in selected):
            selected.append(span)
    for span in sorted(spans[custom_count:], key=lambda span: (span.start, span.end)):
        if span.end <= span.start:
            continue
        if not any(span.start < item.end and item.start < span.end for item in selected):
            selected.append(span)
    return tuple(sorted(selected, key=lambda span: (span.start, span.end)))
