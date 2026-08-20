from datetime import date

from app.knowledge import find_verified_articles, format_verified_context


def test_retrieval_returns_only_approved_non_expired_articles() -> None:
    articles = find_verified_articles("у меня забрали документы", today=date(2026, 8, 20))

    assert articles
    assert all(article.status == "approved" for article in articles)
    assert all(article.expires_at >= date(2026, 8, 20) for article in articles)
    assert any("документ" in article.text.lower() for article in articles)


def test_retrieval_returns_no_legal_answer_when_no_verified_topic_exists() -> None:
    assert find_verified_articles("как оформить ипотеку", today=date(2026, 8, 20)) == ()


def test_context_cites_source_and_verification_date_without_legal_conclusion() -> None:
    articles = find_verified_articles("документы", today=date(2026, 8, 20))
    context = format_verified_context(articles)

    assert "Источник:" in context
    assert "Проверено:" in context
    assert "персональной юридической консультацией" not in context.lower()

