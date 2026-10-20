"""Store alert intent, frozen attachments, retry progress, and compact deduplication receipts."""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class Outbox:
    """Persist jobs across restarts; callers serialize delivery with the process lock."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported Frigate outbox schema")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS outbox (
                    review_id TEXT PRIMARY KEY,
                    event_json TEXT,
                    image BLOB,
                    state TEXT NOT NULL CHECK(state IN ('snapshot','pending','failed','sent')),
                    created_at REAL NOT NULL,
                    snapshot_deadline REAL NOT NULL,
                    next_attempt_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    sent_at REAL,
                    host_event_id TEXT
                );
                CREATE INDEX IF NOT EXISTS outbox_due ON outbox(state, next_attempt_at);
                PRAGMA user_version = 1;
            """)
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        """Close each connection; full synchronous commits protect acknowledged MQTT input."""
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous = FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def process_lock(self):
        """Hold a portable SQLite lock so only one process can deliver this outbox."""
        lock = sqlite3.connect(str(self.path) + ".lock", timeout=0)
        try:
            try:
                lock.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError:
                raise RuntimeError("Another monitor owns this outbox") from None
            yield
        finally:
            lock.close()

    def enqueue(self, event: dict, alerts, now: float | None = None) -> bool:
        """Keep the first qualifying review forever, including after successful delivery."""
        now = time.time() if now is None else now
        with self.connect() as db:
            result = db.execute(
                """INSERT OR IGNORE INTO outbox
                (review_id, event_json, state, created_at, snapshot_deadline, next_attempt_at)
                VALUES (?, ?, 'snapshot', ?, ?, ?)""",
                (
                    event["metadata"]["review_id"],
                    json.dumps(event, ensure_ascii=False, allow_nan=False),
                    now,
                    now + alerts.max_snapshot_retry_seconds,
                    now + alerts.initial_snapshot_wait_seconds,
                ),
            )
            return result.rowcount == 1

    def due(self, now: float):
        with self.connect() as db:
            return db.execute(
                """SELECT * FROM outbox WHERE state IN ('snapshot','pending')
                AND next_attempt_at <= ? ORDER BY next_attempt_at, review_id LIMIT 1""",
                (now,),
            ).fetchone()

    def freeze(self, row, image: bytes | None, now: float):
        """Commit the final image decision before any SES request can occur."""
        event = json.loads(row["event_json"])
        event["metadata"]["snapshot_available"] = image is not None
        if image is None:
            event["message"] += " Snapshot unavailable after retry window."
        with self.connect() as db:
            db.execute(
                """UPDATE outbox SET event_json = ?, image = ?, state = 'pending',
                next_attempt_at = ?, last_error = NULL
                WHERE review_id = ? AND state = 'snapshot'""",
                (json.dumps(event, ensure_ascii=False), image, now, row["review_id"]),
            )

    def snapshot_retry(self, review_id, when):
        with self.connect() as db:
            db.execute(
                "UPDATE outbox SET next_attempt_at = ? WHERE review_id = ?",
                (when, review_id),
            )

    def begin_upload(self, review_id):
        """Count attempts durably before network I/O; interrupted uploads remain pending."""
        with self.connect() as db:
            db.execute(
                "UPDATE outbox SET attempts = attempts + 1 WHERE review_id = ?", (review_id,)
            )

    def finish(self, review_id, result, now):
        with self.connect() as db:
            if result.accepted_id:
                # Keep only a receipt after acceptance, releasing private media and payloads.
                db.execute(
                    """UPDATE outbox SET state = 'sent', event_json = NULL, image = NULL,
                    sent_at = ?, host_event_id = ?, last_error = NULL WHERE review_id = ?""",
                    (now, result.accepted_id, review_id),
                )
            else:
                db.execute(
                    """UPDATE outbox SET state = ?, next_attempt_at = ?, last_error = ?
                    WHERE review_id = ?""",
                    (
                        "failed" if result.permanent else "pending",
                        now + result.delay,
                        result.error,
                        review_id,
                    ),
                )

    def health(self):
        """Return counts and upload time without payloads, images, or credentials."""
        with self.connect() as db:
            counts = dict(db.execute("SELECT state, COUNT(*) FROM outbox GROUP BY state"))
            sent = db.execute("SELECT MAX(sent_at) FROM outbox").fetchone()[0]
        return {
            "queue_size": counts.get("snapshot", 0) + counts.get("pending", 0),
            "failed_count": counts.get("failed", 0),
            "last_successful_upload_at": datetime.fromtimestamp(sent, UTC).isoformat()
            if sent
            else None,
        }

    def retry_failed(self):
        """Requeue unchanged failed payloads after the operator fixes credentials or scope."""
        with self.connect() as db:
            return db.execute(
                """UPDATE outbox SET state = 'pending', next_attempt_at = 0, last_error = NULL
                WHERE state = 'failed'"""
            ).rowcount
