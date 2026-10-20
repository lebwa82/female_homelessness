"""Configuration belongs to one connector process, never to the Telegram settings."""

from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConnectorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONNECTOR_", extra="ignore")

    provider: Literal["vk", "whatsapp"]
    chatwoot_url: str
    account_id: int = Field(gt=0)
    inbox_id: int = Field(gt=0)
    api_token: SecretStr
    inbox_identifier: str
    inbox_hmac_token: SecretStr
    callback_secret: SecretStr = Field(min_length=32, max_length=128)
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")
    listen_host: str = "0.0.0.0"
    listen_port: int = Field(default=8090, ge=1, le=65535)
    attachment_hosts: str = ""
    max_attachment_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    receipt_ttl_seconds: int = Field(default=30 * 86400, ge=86400)
    vk_group_id: int = 0
    vk_token: SecretStr = SecretStr("")
    vk_secret: SecretStr = SecretStr("")
    vk_confirmation: SecretStr = SecretStr("")
    vk_api_version: str = "5.199"
    whatsapp_phone_id: str = ""
    whatsapp_business_id: str = ""
    whatsapp_token: SecretStr = SecretStr("")
    whatsapp_app_secret: SecretStr = SecretStr("")
    whatsapp_verify_token: SecretStr = SecretStr("")
    # Required explicitly: Graph API versions have a finite support lifetime.
    whatsapp_api_version: str = ""

    @model_validator(mode="after")
    def validate_connector(self):
        url = urlsplit(self.chatwoot_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username:
            raise ValueError("invalid Chatwoot URL")
        if url.path not in {"", "/"} or url.query or url.fragment:
            raise ValueError("Chatwoot URL must be an origin")
        secret = self.callback_secret.get_secret_value()
        if not all(c.isascii() and (c.isalnum() or c in "_-") for c in secret):
            raise ValueError("callback secret must be URL safe")
        names = ["api_token", "inbox_identifier", "inbox_hmac_token"]
        names += (["vk_group_id", "vk_token", "vk_secret", "vk_confirmation"]
                  if self.provider == "vk" else [
                      "whatsapp_phone_id", "whatsapp_business_id", "whatsapp_token",
                      "whatsapp_app_secret", "whatsapp_verify_token", "whatsapp_api_version",
                  ])
        for name in names:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                value = value.get_secret_value()
            if not value:
                raise ValueError(f"missing connector setting: {name}")
        if self.provider == "vk" and self.vk_group_id <= 0:
            raise ValueError("invalid VK group id")
        if self.provider == "whatsapp":
            import re
            if not re.fullmatch(r"v\d+\.\d+", self.whatsapp_api_version):
                raise ValueError("invalid Graph API version")
            if not self.whatsapp_phone_id.isascii() or not self.whatsapp_phone_id.isdigit():
                raise ValueError("invalid WhatsApp phone id")
        return self

    @property
    def scope(self) -> str:
        identity = self.vk_group_id if self.provider == "vk" else self.whatsapp_phone_id
        return f"{self.provider}:{identity}"
