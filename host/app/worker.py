"""Deliver durable notifications and run scheduled event retention and file cleanup."""

import argparse
import json
import logging
import random
import signal
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.cleanup import CleanupSchedule
from app.config import Settings
from app.db import Database, utc_now

logger = logging.getLogger(__name__)
CLAIM_SECONDS = 120
POLL_SECONDS = 2


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def heartbeat(database: Database, now: str) -> None:
    """Persist worker activity for internal readiness checks, without contacting ntfy."""
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO worker_status(id, heartbeat_at) VALUES (1, ?)
                ON CONFLICT(id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at""",
            (now,),
        )


def check_worker(database: Database) -> None:
    """Reject readiness when the worker has not reported within its claim timeout."""
    with database.connect() as connection:
        row = connection.execute("SELECT heartbeat_at FROM worker_status WHERE id = 1").fetchone()
    if row is None or datetime.fromisoformat(row["heartbeat_at"]) < datetime.now(UTC) - timedelta(
        seconds=CLAIM_SECONDS
    ):
        raise RuntimeError("Notification worker unavailable")


def claim_delivery(database: Database, settings: Settings, now: str):
    """Reserve one due job and count its attempt before publishing outside the writer lock."""
    cutoff = timestamp(datetime.fromisoformat(now) - timedelta(days=settings.retention_days))
    with database.transaction() as connection:
        # A crash during the final attempt still consumes the retry budget.
        connection.execute(
            """UPDATE notification_deliveries SET state = 'failed', claim_token = NULL,
                last_error = 'Attempt limit reached after interrupted delivery'
                WHERE state = 'pending' AND next_attempt_at <= ? AND attempts >= ?""",
            (now, settings.notification_max_attempts),
        )
        row = connection.execute(
            """SELECT d.id AS delivery_id, d.attempts, d.ntfy_priority, e.*
                FROM notification_deliveries d JOIN events e ON e.id = d.event_id
                WHERE d.state = 'pending' AND d.next_attempt_at <= ? AND e.received_at >= ?
                ORDER BY d.next_attempt_at, d.id LIMIT 1""",
            (now, cutoff),
        ).fetchone()
        if row is None:
            return None

        token = str(uuid4())
        connection.execute(
            """UPDATE notification_deliveries SET attempts = attempts + 1,
                last_attempt_at = ?, next_attempt_at = ?, claim_token = ? WHERE id = ?""",
            (
                now,
                timestamp(datetime.fromisoformat(now) + timedelta(seconds=CLAIM_SECONDS)),
                token,
                row["delivery_id"],
            ),
        )
        return dict(row) | {"claim_token": token, "attempts": row["attempts"] + 1}


def retry_delay(attempts: int, retry_after: str | None, now: datetime) -> float:
    """Combine capped exponential jitter with the provider's minimum retry time."""
    delay = random.uniform(0.5, 1.0) * min(3600, 5 * 2 ** min(attempts - 1, 10))
    if retry_after:
        try:
            seconds = float(retry_after)
        except ValueError:
            try:
                seconds = (parsedate_to_datetime(retry_after).astimezone(UTC) - now).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 0
        # Limit malformed values while honoring valid provider dates/deltas.
        if 0 <= seconds <= 365 * 86400:
            delay = max(delay, seconds)
    return delay


def publish_payload(job, settings: Settings) -> dict:
    """Expose only approved text, severity tags, and an authenticated event-page link."""
    message = f"{job['channel']}: {job['message']}"
    # ntfy messages are limited in bytes; truncation cannot split a UTF-8 character.
    message = message.encode("utf-8")[:4000].decode("utf-8", errors="ignore")
    return {
        "topic": settings.ntfy_topic.get_secret_value(),
        "title": job["title"],
        "message": message,
        "priority": job["ntfy_priority"],
        "tags": [job["severity"]],
        "click": f"{str(settings.base_url).rstrip('/')}/events/{job['id']}",
    }


def process_once(database: Database, settings: Settings, client: httpx.Client) -> bool:
    """Try one due delivery; return whether a job was handled, leaving retries durable."""
    heartbeat(database, utc_now())
    if settings.ntfy_topic is None:
        return False
    job = claim_delivery(database, settings, utc_now())
    if job is None:
        return False

    state, error, provider_id, retry_after = "pending", None, None, None
    try:
        # Bound response bytes and never follow redirects to another relay.
        with client.stream(
            "POST",
            str(settings.ntfy_url),
            json=publish_payload(job, settings),
            timeout=10,
            follow_redirects=False,
        ) as response:
            if 200 <= response.status_code < 300:
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=4096):
                    body.extend(chunk)
                    if len(body) > 16384:
                        break
                try:
                    data = json.loads(body) if len(body) <= 16384 else None
                except (ValueError, UnicodeError):
                    data = None
                if isinstance(data, dict) and isinstance(data.get("id"), str):
                    provider_id = data["id"][:200]
                state = "sent_to_ntfy"
            else:
                error = f"ntfy HTTP {response.status_code}"
                retry_after = response.headers.get("Retry-After")
                if response.status_code != 429 and not 500 <= response.status_code < 600:
                    state = "failed"
    except httpx.RequestError:
        # Exception strings and response bodies can contain private URLs or payloads.
        error = "ntfy network request failed"

    now = datetime.now(UTC)
    if state == "pending" and job["attempts"] >= settings.notification_max_attempts:
        state = "failed"
    next_attempt = timestamp(
        now + timedelta(seconds=retry_delay(job["attempts"], retry_after, now))
    )
    with database.transaction() as connection:
        connection.execute(
            """UPDATE notification_deliveries SET state = ?, next_attempt_at = ?,
                provider_response_id = ?, last_error = ?, sent_at = ?, claim_token = NULL
                WHERE id = ? AND claim_token = ? AND state = 'pending'""",
            (
                state,
                next_attempt,
                provider_id,
                error,
                timestamp(now) if state == "sent_to_ntfy" else None,
                job["delivery_id"],
                job["claim_token"],
            ),
        )
    logger.info(
        "Notification event_id=%s outcome=%s attempts=%s", job["id"], state, job["attempts"]
    )
    return True


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check persisted worker heartbeat")
    args = parser.parse_args(argv)
    try:
        settings = Settings()
    except ValidationError:
        parser.exit(2, "Invalid SES settings; see docs/operations.md\n")
    database = Database(settings.database_path)
    if args.check:
        try:
            check_worker(database)
        except (sqlite3.Error, OSError, RuntimeError):
            parser.exit(1, "Notification worker unavailable\n")
        return

    logging.basicConfig(level=settings.log_level.upper())
    database.initialize()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    cleanup = CleanupSchedule(database, settings)
    with httpx.Client() as client:
        while not stop.is_set():
            cleaning = cleanup.tick()
            try:
                handled = process_once(database, settings, client)
            except (sqlite3.Error, OSError):
                logger.warning("Notification storage unavailable")
                handled = False
            if not handled and not cleaning:
                stop.wait(POLL_SECONDS)


if __name__ == "__main__":
    main()
