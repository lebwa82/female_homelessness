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


def test_transcript_preserves_full_dialogue() -> None:
    assert format_transcript([("user", "Мне нужна помощь"), ("assistant", "Я рядом")]) == (
        "Пользователь: Мне нужна помощь\n\nБот: Я рядом\n\nСформулируй следующую короткую реплику бота."
    )
