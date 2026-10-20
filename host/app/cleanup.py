"""Expire event history and reconcile private files in bounded, restart-safe batches."""

import argparse
import errno
import logging
import os
import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import batched
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings
from app.db import Database

logger = logging.getLogger(__name__)
BATCH_SIZE = 100
CLEANUP_INTERVAL_SECONDS = 3600
CLEANUP_RETRY_SECONDS = 60


@dataclass
class CleanupResult:
    """Count committed deletions and recoverable filesystem failures in a cleanup pass."""

    expired_events: int = 0
    orphan_images: int = 0
    pruned_files: int = 0
    errors: int = 0


def cleanup_steps(
    database: Database,
    settings: Settings,
    result: CleanupResult,
    *,
    now: datetime | None = None,
    batch_size: int = BATCH_SIZE,
) -> Iterator[None]:
    """Delete expired history and orphan files, yielding between bounded write transactions.

    The cutoff uses host receipt time. Committing row deletions before pruning files
    lets the next pass recover files left by a crash or an unsuccessful unlink.
    """
    if batch_size < 1:
        raise ValueError("Cleanup batch size must be positive")
    cutoff = (
        ((now or datetime.now(UTC)) - timedelta(days=settings.retention_days))
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )

    # Foreign-key cascades remove every delivery state, including pending retries.
    while True:
        with database.transaction() as connection:
            deleted = connection.execute(
                """DELETE FROM events WHERE id IN (
                    SELECT id FROM events WHERE received_at < ?
                    ORDER BY received_at, id LIMIT ?
                )""",
                (cutoff, batch_size),
            ).rowcount
        result.expired_events += deleted
        if not deleted:
            break
        yield

    # Include metadata stranded by interrupted or older writes, regardless of age.
    while True:
        with database.transaction() as connection:
            deleted = connection.execute(
                """DELETE FROM images WHERE id IN (
                    SELECT images.id FROM images WHERE NOT EXISTS (
                        SELECT 1 FROM events WHERE events.image_id = images.id
                    ) LIMIT ?
                )""",
                (batch_size,),
            ).rowcount
        result.orphan_images += deleted
        if not deleted:
            break
        yield

    # Only these private trees are managed; database files and backups are untouched.
    for directory in ("images", "thumbnails"):
        root = settings.data_dir / directory
        if root.is_symlink():
            result.errors += 1
            logger.warning("Cleanup skipped a symlinked private storage root")
            continue
        if not root.exists():
            continue

        def scan_error(error: OSError) -> None:
            result.errors += 1
            logger.warning("Cleanup could not scan private storage")

        for parent, _, filenames in os.walk(root, topdown=False, onerror=scan_error):
            for names in batched(filenames, batch_size):
                # Ingest and thumbnail generation use this same writer lock. Check
                # each reference after taking it, rather than trusting a stale scan.
                with database.transaction() as connection:
                    for name in names:
                        path = Path(parent) / name
                        disk_name = path.relative_to(root).as_posix()
                        referenced = connection.execute(
                            "SELECT 1 FROM images WHERE disk_name = ?", (disk_name,)
                        ).fetchone()
                        if referenced:
                            continue

                        try:
                            path.unlink()
                            result.pruned_files += 1
                        except FileNotFoundError:
                            pass
                        except OSError:
                            result.errors += 1
                            logger.warning("Cleanup could not remove an orphan file")
                yield

            # Never follow directory symlinks or race a writer creating a date tree.
            if Path(parent) != root:
                with database.transaction():
                    try:
                        Path(parent).rmdir()
                    except OSError as exc:
                        if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST, errno.ENOENT):
                            result.errors += 1
                            logger.warning("Cleanup could not remove an empty directory")
                yield


def log_result(result: CleanupResult) -> None:
    """Report cleanup totals without exposing private filenames or event content."""
    logger.info(
        "Cleanup expired_events=%s orphan_images=%s pruned_files=%s errors=%s",
        result.expired_events,
        result.orphan_images,
        result.pruned_files,
        result.errors,
    )


class CleanupSchedule:
    """Advance cleanup one batch per worker tick, retrying storage failures after a minute."""

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.next_run = 0.0
        self.steps: Iterator[None] | None = None
        self.result = CleanupResult()

    def tick(self) -> bool:
        """Run one due batch and return whether more cleanup work may remain."""
        try:
            if self.steps is None:
                if time.monotonic() < self.next_run:
                    return False
                self.result = CleanupResult()
                self.steps = cleanup_steps(self.database, self.settings, self.result)

            next(self.steps)
            return True
        except StopIteration:
            log_result(self.result)
            delay = CLEANUP_RETRY_SECONDS if self.result.errors else CLEANUP_INTERVAL_SECONDS
        except (sqlite3.Error, OSError):
            logger.warning("Cleanup storage unavailable; retrying in one minute")
            delay = CLEANUP_RETRY_SECONDS

        self.steps = None
        self.next_run = time.monotonic() + delay
        return False


def main(argv: list[str] | None = None) -> None:
    """Run an immediate cleanup pass with the same settings as the supervised worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        settings = Settings()
    except ValidationError:
        parser.exit(2, "Invalid SES settings; see docs/operations.md\n")

    logging.basicConfig(level=settings.log_level.upper())
    database = Database(settings.database_path)
    result = CleanupResult()
    try:
        database.initialize()
        for _ in cleanup_steps(database, settings, result):
            pass
    except (sqlite3.Error, OSError):
        parser.exit(1, "Cleanup storage unavailable; retry after resolving storage errors\n")
    log_result(result)
    if result.errors:
        parser.exit(1, "Cleanup incomplete; orphan files will be retried on the next pass\n")


if __name__ == "__main__":
    main()
