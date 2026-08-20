from typing import Literal
from urllib.parse import parse_qs, quote, urlparse

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_YANDEX_CLOUD_FOLDER_ID = "b1gepl5pmqr8f7hbu3bj"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    telegram_proxy_url: str = ""
    app_env: Literal["development", "production", "test"] = Field(
        default="development", validation_alias=AliasChoices("APP_ENV", "ENV")
    )
    build_version: str = "dev"
    database_url: str = "postgresql+asyncpg://helper:helper@localhost:5433/women_help"
    llm_enabled: bool = True
    yandex_ai_api_key: str = ""
    yandex_cloud_folder_id: str = DEFAULT_YANDEX_CLOUD_FOLDER_ID
    yandex_ai_model: str = "qwen3.6-35b-a3b"
    identity_hash_key: str = "women-help-mvp"
    followup_delay_seconds: int = Field(default=7 * 24 * 60 * 60, ge=1)
    followup_reminder_seconds: int = Field(default=48 * 60 * 60, ge=1)
    message_retention_days: int = Field(default=30, ge=1)
    worker_poll_seconds: int = Field(default=15, ge=1)

    @field_validator("yandex_cloud_folder_id", mode="before")
    @classmethod
    def blank_folder_id_uses_project_default(cls, value: object) -> object:
        return DEFAULT_YANDEX_CLOUD_FOLDER_ID if value == "" else value

    def resolved_telegram_proxy_url(self) -> str | None:
        """Return an aiogram-compatible HTTP(S)/SOCKS proxy URL.

        `tg://proxy` is an MTProto link for Telegram client apps, not an HTTP
        proxy for Bot API. `tg://socks` can be safely converted to SOCKS5.
        """
        value = self.telegram_proxy_url.strip()
        if not value:
            return None

        parsed = urlparse(value)
        if parsed.scheme == "tg" and parsed.netloc == "socks":
            query = parse_qs(parsed.query)
            server = query.get("server", [""])[0]
            port = query.get("port", [""])[0]
            if not server or not port:
                raise ValueError("tg://socks must contain server and port.")
            username = query.get("user", [""])[0]
            password = query.get("pass", [""])[0]
            credentials = ""
            if username or password:
                credentials = f"{quote(username, safe='')}:{quote(password, safe='')}@"
            return f"socks5://{credentials}{server}:{port}"

        if parsed.scheme == "tg" and parsed.netloc == "proxy":
            raise ValueError(
                "tg://proxy is an MTProto proxy link. Use an HTTP(S), SOCKS4/5, or tg://socks URL."
            )
        if parsed.scheme not in {"http", "https", "socks4", "socks4a", "socks5"}:
            raise ValueError("TELEGRAM_PROXY_URL must use http(s), socks4(a), socks5, or tg://socks.")
        return value

    def runtime_label(self) -> str:
        return {
            "development": "локальная версия",
            "production": "серверная версия",
            "test": "тестовая версия",
        }[self.app_env]


settings = Settings()
