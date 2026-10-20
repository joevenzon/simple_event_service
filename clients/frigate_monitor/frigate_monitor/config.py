"""Validate a standalone YAML configuration and resolve secrets from the environment."""

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


def channel(value: str) -> str:
    """Validate the public SES channel contract without importing host code."""
    parts = value.split(".")
    if (
        len(value) > 255
        or not 1 <= len(parts) <= 12
        or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", part) for part in parts)
    ):
        raise ValueError("Invalid SES channel")
    return value


def secret(name: str) -> str:
    """Read a required secret without embedding its value in error messages."""
    value = os.environ.get(name, "")
    if not value or any(ord(c) < 32 for c in value):
        raise ValueError(f"Set a nonempty secret in environment variable {name}")
    return value


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FrigateConfig(ConfigModel):
    api_base_url: HttpUrl
    api_auth: Literal["login", "none"] = "login"
    username_env: str = "FRIGATE_USERNAME"
    password_env: str = "FRIGATE_PASSWORD"
    ca_file: Path | None = None
    mqtt_host: str = Field(min_length=1)
    mqtt_port: int = Field(default=1883, ge=1, le=65535)
    mqtt_client_id: str = Field(default="ses-frigate-home", min_length=1, max_length=128)
    mqtt_topic: str = "frigate/reviews"
    mqtt_username_env: str | None = None
    mqtt_password_env: str | None = None
    mqtt_tls: bool = False
    mqtt_ca_file: Path | None = None
    camera_channels: dict[str, str] = Field(min_length=1, max_length=100)

    @field_validator("mqtt_topic")
    @classmethod
    def exact_topic(cls, value):
        if not value or any(c in value for c in ("+", "#", "\x00")):
            raise ValueError("Use one exact review topic")
        return value

    @model_validator(mode="after")
    def validate_connections(self):
        if self.api_auth == "login" and self.api_base_url.scheme != "https":
            raise ValueError("Frigate login requires HTTPS")
        if bool(self.mqtt_username_env) != bool(self.mqtt_password_env):
            raise ValueError("Configure both MQTT username and password environment names")
        return self


class SESConfig(ConfigModel):
    host_url: HttpUrl
    token_env: str = "SES_CLIENT_TOKEN"
    client_id: str = Field(default="frigate-home", min_length=1, max_length=128)
    heartbeat_seconds: int = Field(default=60, ge=10, le=3600)
    outbox_path: Path = Path("data/frigate-outbox.sqlite3")
    allow_loopback_http: bool = False

    @model_validator(mode="after")
    def require_tls(self):
        if self.host_url.scheme != "https" and not (
            self.allow_loopback_http and self.host_url.host in ("127.0.0.1", "localhost", "[::1]")
        ):
            raise ValueError("SES uploads require HTTPS (HTTP is allowed only for loopback tests)")
        return self


class AlertsConfig(ConfigModel):
    only_labels: list[Literal["person"]] = Field(default=["person"], min_length=1, max_length=1)
    channel_suffix: Literal["person"] = "person"
    initial_snapshot_wait_seconds: float = Field(default=2, ge=0, le=60)
    max_snapshot_retry_seconds: float = Field(default=15, ge=0, le=300)
    snapshot_retry_seconds: float = Field(default=2, gt=0, le=60)

    @model_validator(mode="after")
    def ordered_waits(self):
        if self.initial_snapshot_wait_seconds > self.max_snapshot_retry_seconds:
            raise ValueError("Initial snapshot wait must fit within the retry window")
        return self


class Settings(ConfigModel):
    frigate: FrigateConfig
    ses: SESConfig
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)

    @model_validator(mode="after")
    def validate_routes(self):
        # Origin-only URLs keep credentials out of URLs and prevent ambiguous API joins.
        for url in (self.frigate.api_base_url, self.ses.host_url):
            if url.username or url.password or url.query or url.fragment or url.path != "/":
                raise ValueError("API URLs must be origins without credentials or paths")

        for camera, prefix in self.frigate.camera_channels.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", camera):
                raise ValueError("Invalid Frigate camera identifier")
            channel(f"{prefix}.{self.alerts.channel_suffix}")
        return self


def load_settings(path: Path) -> Settings:
    """Read bounded YAML; validation mode needs neither credentials nor network access."""
    if path.stat().st_size > 65536:
        raise ValueError("Configuration exceeds 64 KiB")
    return Settings.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
