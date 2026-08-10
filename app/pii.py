"""Local Russian PII detection and masking with Presidio and spaCy."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

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
    results = analyzer().analyze(text=text, language="ru")
    anonymized = anonymizer().anonymize(text=text, analyzer_results=results, operators=OPERATORS)
    entity_counts = Counter(result.entity_type for result in results)
    return RedactionResult(
        text=anonymized.text,
        audit={
            "engine": "presidio",
            "language": "ru",
            "detected": bool(results),
            "entity_counts": dict(sorted(entity_counts.items())),
            "entities_total": len(results),
        },
    )


def redact_for_model(text: str) -> str:
    return redact_with_audit(text).text
