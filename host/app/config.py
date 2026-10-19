"""Environment settings shared by the web process and operator CLI."""

import re
from pathlib import Path
from typing import Literal

from pydantic import AnyHttpUrl, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", populate_by_name=True)

    environment: Literal["development", "production"] = Field(
        default="production", validation_alias="SES_ENV"
    )
    data_dir: Path = Field(default=Path("data"), validation_alias="SES_DATA_DIR")
    base_url: AnyHttpUrl = Field(default="https://localhost", validation_alias="SES_BASE_URL")
    retention_days: int = Field(default=7, ge=1, validation_alias="EVENT_RETENTION_DAYS")
    session_secret: SecretStr | None = Field(default=None, validation_alias="SESSION_SECRET")
    log_level: Literal["debug", "info", "warning", "error", "critical"] = Field(
        default="info", validation_alias="SES_LOG_LEVEL"
    )
    ntfy_url: AnyHttpUrl = Field(default="https://ntfy.sh", validation_alias="NTFY_URL")
    ntfy_topic: SecretStr | None = Field(default=None, validation_alias="NTFY_TOPIC")
    ntfy_send_images: bool = Field(default=False, validation_alias="NTFY_SEND_IMAGES")
    notification_cooldown_seconds: int = Field(
        default=60, ge=0, le=86400, validation_alias="SES_NOTIFICATION_COOLDOWN_SECONDS"
    )
    notification_max_attempts: int = Field(
        default=8, ge=1, le=100, validation_alias="SES_NOTIFICATION_MAX_ATTEMPTS"
    )
    session_hours: int = Field(default=24, ge=1, le=720, validation_alias="SES_SESSION_HOURS")
    login_attempts: int = Field(default=10, ge=1, validation_alias="SES_LOGIN_ATTEMPTS")
    login_window_seconds: int = Field(
        default=300, ge=1, validation_alias="SES_LOGIN_WINDOW_SECONDS"
    )
    ingest_per_minute: int = Field(default=60, ge=1, validation_alias="SES_INGEST_PER_MINUTE")
    max_image_bytes: int = Field(
        default=5 * 1024 * 1024, ge=1, le=5 * 1024 * 1024, validation_alias="SES_MAX_IMAGE_BYTES"
    )
    max_image_pixels: int = Field(
        default=20_000_000, ge=1, le=20_000_000, validation_alias="SES_MAX_IMAGE_PIXELS"
    )

    @property
    def secure_cookies(self) -> bool:
        return self.environment == "production"

    @field_validator("ntfy_topic", mode="before")
    @classmethod
    def empty_topic_disables_delivery(cls, value):
        """Treat an empty environment value as an unconfigured relay."""
        return None if value == "" else value

    @model_validator(mode="after")
    def validate_deployment(self) -> "Settings":
        if self.environment == "production":
            if self.base_url.scheme != "https":
                raise ValueError("SES_BASE_URL must use HTTPS in production")
            secret = self.session_secret.get_secret_value() if self.session_secret else ""
            if len(secret) < 32 or secret.startswith("replace-"):
                raise ValueError("Set SESSION_SECRET to a random value of at least 32 characters")
        if self.ntfy_send_images:
            raise ValueError(
                "ntfy image publishing is not implemented; keep NTFY_SEND_IMAGES=false"
            )
        # Publish URLs are origins, so credentials or query strings cannot reach push links.
        for url in (self.base_url, self.ntfy_url):
            if (
                url.username
                or url.password
                or url.query
                or url.fragment
                or url.path not in (None, "/")
            ):
                raise ValueError("SES_BASE_URL and NTFY_URL must be origins without credentials")
        if self.environment == "production" and self.ntfy_url.scheme != "https":
            raise ValueError("NTFY_URL must use HTTPS in production")
        if self.ntfy_topic:
            topic = self.ntfy_topic.get_secret_value()
            if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", topic) or topic.startswith("replace-"):
                raise ValueError("NTFY_TOPIC must be a random topic of 32-128 URL-safe characters")
        return self

    @property
    def database_path(self) -> Path:
        return self.data_dir / "ses.sqlite3"
