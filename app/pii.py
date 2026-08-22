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
from presidio_anonymizer import AnonymizerEngine
from presidio_anonymizer.entities import OperatorConfig

NLP_CONFIGURATION = {
    "nlp_engine_name": "spacy",
    "models": [{"lang_code": "ru", "model_name": "ru_core_news_sm"}],
}

OPERATORS = {
    "PERSON": OperatorConfig("replace", {"new_value": "[ЧЕЛОВЕК]"}),
    "LOCATION": OperatorConfig("replace", {"new_value": "[ЛОКАЦИЯ]"}),
    "PHONE_NUMBER": OperatorConfig("replace", {"new_value": "[ТЕЛЕФОН]"}),
    "EMAIL_ADDRESS": OperatorConfig("replace", {"new_value": "[EMAIL]"}),
    "CREDIT_CARD": OperatorConfig("replace", {"new_value": "[КАРТА]"}),
    "IP_ADDRESS": OperatorConfig("replace", {"new_value": "[IP]"}),
    "URL": OperatorConfig("replace", {"new_value": "[ССЫЛКА]"}),
    "DEFAULT": OperatorConfig("replace", {"new_value": "[ПЕРСОНАЛЬНЫЕ_ДАННЫЕ]"}),
}

# Never let the PII path fetch or refresh the public suffix list.  The extractor is
# deliberately constructed once with bundled data only, so a redaction request is
# always local and deterministic even on a cold host.
tld_extractor = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

# Telegram handles are contacts even when a generic PII recognizer does not know
# about Telegram.  Do this before Presidio so the exact replacement is stable in
# both current and historical model views.
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


@lru_cache(maxsize=1)
def anonymizer() -> AnonymizerEngine:
    return AnonymizerEngine()


def redact_with_audit(text: str) -> RedactionResult:
    """Mask PII locally and return only non-sensitive audit data."""
    masked_text, telegram_handles = _mask_telegram_handles(text)
    masked_text, urls = _mask_urls_with_offline_psl(masked_text)
    results = analyzer().analyze(text=masked_text, language="ru")
    anonymized = anonymizer().anonymize(text=masked_text, analyzer_results=results, operators=OPERATORS)
    entity_counts = Counter(result.entity_type for result in results)
    if telegram_handles:
        entity_counts["TELEGRAM_HANDLE"] += telegram_handles
    if urls:
        entity_counts["URL"] += urls
    return RedactionResult(
        text=anonymized.text,
        audit={
            "engine": "presidio",
            "language": "ru",
            "detected": bool(results) or bool(telegram_handles),
            "entity_counts": dict(sorted(entity_counts.items())),
            "entities_total": len(results) + telegram_handles,
        },
    )


def redact_for_model(text: str) -> str:
    return redact_with_audit(text).text


def _mask_telegram_handles(text: str) -> tuple[str, int]:
    masked_text, replacements = _TELEGRAM_HANDLE.subn("[CONTACT]", text)
    return masked_text, replacements


def _mask_urls_with_offline_psl(text: str) -> tuple[str, int]:
    """Mask URL candidates using the same non-refreshing PSL extractor used at runtime."""
    replacements = 0

    def mask(match: re.Match[str]) -> str:
        nonlocal replacements
        if tld_extractor(match.group()).suffix:
            replacements += 1
            return "[ССЫЛКА]"
        return match.group()

    return _URL_CANDIDATE.sub(mask, text), replacements
