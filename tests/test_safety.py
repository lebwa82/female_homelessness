import pytest

from app.config import Settings
from app.domain import Risk
from app.llm import format_transcript
from app.safety import assess_crisis


def test_detects_acute_crisis() -> None:
    assert assess_crisis("Он меня сейчас избивает").risk is Risk.ACUTE


def test_detects_concern() -> None:
    assert assess_crisis("Мне страшно, нет где ночевать").risk is Risk.CONCERN


def test_accepts_blank_optional_telegram_chat_id() -> None:
    assert Settings(staff_telegram_chat_id="").staff_telegram_chat_id is None


def test_blank_folder_id_uses_project_default() -> None:
    assert Settings(yandex_cloud_folder_id="").yandex_cloud_folder_id


def test_converts_telegram_socks_link_to_proxy_url() -> None:
    settings = Settings(telegram_proxy_url="tg://socks?server=127.0.0.1&port=1080&user=a&pass=b")

    assert settings.resolved_telegram_proxy_url() == "socks5://a:b@127.0.0.1:1080"


def test_rejects_mtproto_proxy_link_for_bot_api() -> None:
    settings = Settings(telegram_proxy_url="tg://proxy?server=127.0.0.1&port=443&secret=test")

    with pytest.raises(ValueError, match="MTProto"):
        settings.resolved_telegram_proxy_url()


def test_transcript_preserves_full_dialogue() -> None:
    assert format_transcript([("user", "Мне нужна помощь"), ("assistant", "Я рядом")]) == (
        "Пользователь: Мне нужна помощь\n\nБот: Я рядом\n\nСформулируй следующую короткую реплику бота."
    )
