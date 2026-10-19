"""Select host-approved notifications and commit durable jobs alongside event ingestion."""

import sqlite3
from datetime import datetime, timedelta
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.channels import channel_matches, validate_channel
from app.config import Settings
from app.schemas import EventInput, Severity

SEVERITIES = {
    name: rank for rank, name in enumerate(("debug", "info", "warning", "error", "critical"))
}
NTFY_PRIORITIES = {"debug": 1, "info": 3, "warning": 4, "error": 5, "critical": 5}


class NotificationRule(BaseModel):
    """Validate operator policy independently of producer publication permissions."""

    model_config = ConfigDict(extra="forbid", strict=True)
    channel_prefix: str
    enabled: bool = True
    min_severity: Severity = "info"
    ntfy_priority: int | None = Field(default=None, ge=1, le=5)
    rule_priority: int = 0
    sort_order: int = 0

    @field_validator("channel_prefix")
    @classmethod
    def prefix_or_all(cls, value: str) -> str:
        return value if value == "*" else validate_channel(value)


def select_rule(connection: sqlite3.Connection, channel: str):
    """Return the first boundary-matching rule, including explicit suppression rules."""
    rules = connection.execute(
        "SELECT * FROM notification_rules ORDER BY rule_priority DESC, sort_order ASC, id ASC"
    ).fetchall()
    matches = [
        r
        for r in rules
        if r["channel_prefix"] == "*" or channel_matches(r["channel_prefix"], channel)
    ]
    # Stable sorting preserves explicit priority, configured order, and insertion order on ties.
    matches.sort(
        key=lambda r: 0 if r["channel_prefix"] == "*" else len(r["channel_prefix"].split(".")),
        reverse=True,
    )
    return matches[0] if matches else None


def enqueue_notification(
    connection,
    event_id: str,
    client_id: str,
    event: EventInput,
    received_at: str,
    settings: Settings,
) -> None:
    """Apply policy and cooldown inside the event transaction, without network access."""
    rule = select_rule(connection, event.channel)
    if (
        rule is None
        or not rule["enabled"]
        or event.notify is False
        or SEVERITIES[event.severity] < SEVERITIES[rule["min_severity"]]
    ):
        return

    # Reserve cooldown when selected, rather than after delivery, to bound outage backlogs.
    previous = connection.execute(
        "SELECT selected_at FROM notification_cooldowns WHERE client_id = ? AND channel = ?",
        (client_id, event.channel),
    ).fetchone()
    cutoff = datetime.fromisoformat(received_at) - timedelta(
        seconds=settings.notification_cooldown_seconds
    )
    suppressed = previous is not None and datetime.fromisoformat(previous["selected_at"]) > cutoff
    state = "suppressed_cooldown" if suppressed else "pending"
    connection.execute(
        """INSERT INTO notification_deliveries
            (id, event_id, provider, state, next_attempt_at, created_at, ntfy_priority)
            VALUES (?, ?, 'ntfy', ?, ?, ?, ?)""",
        (
            str(uuid4()),
            event_id,
            state,
            received_at,
            received_at,
            rule["ntfy_priority"]
            if rule["ntfy_priority"] is not None
            else NTFY_PRIORITIES[event.severity],
        ),
    )
    if not suppressed:
        connection.execute(
            """INSERT INTO notification_cooldowns(client_id, channel, selected_at) VALUES (?, ?, ?)
                ON CONFLICT(client_id, channel) DO UPDATE SET selected_at = excluded.selected_at""",
            (client_id, event.channel, received_at),
        )
