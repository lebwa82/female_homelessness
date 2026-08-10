from app.config import Settings


def test_runtime_label_comes_from_app_env() -> None:
    assert Settings(APP_ENV="development").runtime_label() == "локальная версия"
    assert Settings(APP_ENV="production").runtime_label() == "серверная версия"
    assert Settings(APP_ENV="test").runtime_label() == "тестовая версия"


def test_env_alias_is_supported() -> None:
    assert Settings(ENV="production").runtime_label() == "серверная версия"
