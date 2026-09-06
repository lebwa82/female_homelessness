from app.config import Settings


def test_runtime_label_comes_from_app_env() -> None:
    assert Settings(APP_ENV="development").runtime_label() == "локальная версия"
    assert Settings(APP_ENV="production").runtime_label() == "серверная версия"
    assert Settings(APP_ENV="test").runtime_label() == "тестовая версия"


def test_env_alias_is_supported() -> None:
    assert Settings(ENV="production").runtime_label() == "серверная версия"


def test_followup_and_retention_settings_are_configurable_for_test_runs() -> None:
    settings = Settings(
        followup_delay_seconds=60,
        followup_reminder_seconds=120,
        message_retention_days=7,
        worker_poll_seconds=2,
    )

    assert settings.followup_delay_seconds == 60
    assert settings.followup_reminder_seconds == 120
    assert settings.message_retention_days == 7
    assert settings.worker_poll_seconds == 2


def test_default_database_url_matches_the_local_compose_port(monkeypatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings(_env_file=None).database_url.endswith(":5433/women_help")


def test_chatwoot_requires_a_url_safe_webhook_secret() -> None:
    configured = Settings(
        CHATWOOT_BASE_URL="https://chat.example.test",
        CHATWOOT_ACCOUNT_ID=1,
        CHATWOOT_READ_TOKEN="read-token",
        CHATWOOT_BOT_TOKEN="bot-token",
        CHATWOOT_WEBHOOK_SECRET="too-short",
        CHATWOOT_DUTY_TEAM_ID=2,
    )

    assert configured.chatwoot_configuration_error() == "invalid CHATWOOT_WEBHOOK_SECRET"
