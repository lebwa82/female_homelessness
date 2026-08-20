"""Curated, expiry-aware reference information for the support agent."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path

RESOURCE_PATH = Path(__file__).parent.parent / "knowledge" / "verified_resources.json"


@dataclass(frozen=True)
class KnowledgeArticle:
    id: str
    topics: tuple[str, ...]
    region: str
    status: str
    owner: str
    source_title: str
    source_url: str
    verified_at: date
    expires_at: date
    text: str


@cache
def _articles() -> tuple[KnowledgeArticle, ...]:
    data = json.loads(RESOURCE_PATH.read_text(encoding="utf-8"))
    return tuple(
        KnowledgeArticle(
            id=row["id"],
            topics=tuple(row["topics"]),
            region=row["region"],
            status=row["status"],
            owner=row["owner"],
            source_title=row["source_title"],
            source_url=row["source_url"],
            verified_at=date.fromisoformat(row["verified_at"]),
            expires_at=date.fromisoformat(row["expires_at"]),
            text=row["text"],
        )
        for row in data["articles"]
    )


def find_verified_articles(query: str, *, today: date | None = None) -> tuple[KnowledgeArticle, ...]:
    """Return only approved, non-expired articles with an explicit topic match."""
    checked_on = today or datetime.now(UTC).date()
    words = set(re.findall(r"[а-яёa-z0-9-]{3,}", query.lower()))
    if not words:
        return ()
    matches = []
    for article in _articles():
        if article.status != "approved" or article.expires_at < checked_on:
            continue
        topic_words = {
            word
            for topic in article.topics
            for word in re.findall(r"[а-яёa-z0-9-]{3,}", topic.lower())
        }
        if words.intersection(topic_words):
            matches.append(article)
    return tuple(matches[:3])


def format_verified_context(articles: tuple[KnowledgeArticle, ...]) -> str:
    if not articles:
        return "Проверенной справки по этому вопросу нет. Не придумывай маршрут; мягко предложи помощь человека."
    return "\n\n".join(
        "\n".join(
            (
                f"Справка: {article.text}",
                f"Источник: {article.source_title} — {article.source_url}",
                f"Проверено: {article.verified_at.isoformat()}",
            )
        )
        for article in articles
    )


def find_verified_answer(query: str) -> str | None:
    """Compatibility helper for callers that need a single factual reference."""
    articles = find_verified_articles(query)
    return articles[0].text if articles else None
