from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_YANDEX_CLOUD_FOLDER_ID = "b1gepl5pmqr8f7hbu3bj"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    staff_telegram_chat_id: int | None = None
    database_url: str = "postgresql+asyncpg://helper:helper@localhost:5432/women_help"
    llm_enabled: bool = True
    yandex_ai_api_key: str = ""
    yandex_cloud_folder_id: str = DEFAULT_YANDEX_CLOUD_FOLDER_ID
    yandex_ai_model: str = "qwen3.6-35b-a3b"

    @field_validator("staff_telegram_chat_id", mode="before")
    @classmethod
    def blank_chat_id_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("yandex_cloud_folder_id", mode="before")
    @classmethod
    def blank_folder_id_uses_project_default(cls, value: object) -> object:
        return DEFAULT_YANDEX_CLOUD_FOLDER_ID if value == "" else value


settings = Settings()
