"""The versioned producer contract; normalize before computing idempotency digests."""

import json
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from app.channels import validate_channel

Severity = Literal["debug", "info", "warning", "error", "critical"]
Tag = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$", max_length=40)]


class EventInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event_key: str = Field(min_length=1, max_length=200)
    channel: str
    severity: Severity
    title: str = Field(min_length=1, max_length=200)
    message: str = Field(max_length=4000)
    occurred_at: datetime | None = None
    metadata: dict[str, JsonValue] = Field(default_factory=dict)
    tags: list[Tag] = Field(default_factory=list, max_length=32)
    notify: bool | None = None

    _channel = field_validator("channel")(validate_channel)

    @field_validator("event_key", "title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("Must be nonblank and contain no control characters")
        return value

    @field_validator("occurred_at")
    @classmethod
    def utc_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Timestamp must include a timezone")
            try:
                return value.astimezone(UTC)
            except (ValueError, OverflowError):
                raise ValueError("Timestamp is outside the supported UTC range") from None
        return value

    @field_validator("metadata")
    @classmethod
    def bounded_metadata(cls, value: dict) -> dict:
        if len(canonical_json(value).encode("utf-8")) > 16 * 1024:
            raise ValueError("Metadata exceeds 16 KiB")
        return value


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


class EventAccepted(BaseModel):
    id: str
    status: Literal["accepted"]
    duplicate: bool


class DeliveryView(BaseModel):
    """Expose delivery progress without worker claim tokens or provider response bodies."""

    attempts: int
    last_attempt_at: str | None
    next_attempt_at: str | None
    sent_at: str | None
    last_error: str | None


class EventView(BaseModel):
    id: str
    client_id: str
    event_key: str
    channel: str
    severity: Severity
    title: str
    message: str
    occurred_at: str | None
    received_at: str
    image_id: str | None
    image_url: str | None
    tags: list[str]
    metadata: dict[str, JsonValue]
    notification_state: Literal[
        "not_selected", "pending", "sent_to_ntfy", "failed", "suppressed_cooldown"
    ]
    notification_delivery: DeliveryView | None = None


class EventPage(BaseModel):
    items: list[EventView]
    next_cursor: str | None


class SessionView(BaseModel):
    username: str
    csrf_token: str


class HeartbeatInput(BaseModel):
    """Validate a small health snapshot independently of user-visible events."""

    model_config = ConfigDict(extra="forbid", strict=True)
    health: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("health")
    @classmethod
    def bounded_health(cls, value: dict) -> dict:
        if len(canonical_json(value).encode("utf-8")) > 8 * 1024:
            raise ValueError("Health exceeds 8 KiB")
        return value


class HeartbeatAccepted(BaseModel):
    status: Literal["accepted"]
    received_at: str


class ClientView(BaseModel):
    id: str
    name: str
    enabled: bool
    allowed_prefixes: list[str]
    stale_timeout_seconds: int
    last_seen_at: str | None
    last_heartbeat_at: str | None
    last_event_at: str | None
    health: dict[str, JsonValue]
    status: Literal["healthy", "stale", "never_seen", "disabled"]
