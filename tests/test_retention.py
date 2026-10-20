"""Verify retention boundaries, crash recovery, file safety, and automatic scheduling."""

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_ingest import submit_image
from test_notifications import command

import app.cleanup as cleanup_module
import app.worker as worker_module
from app.cleanup import CleanupResult, CleanupSchedule, cleanup_steps
from app.db import Database
from app.images import PreparedImage, write_image
from app.worker import claim_delivery, timestamp

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def cleanup(database, settings, **kwargs):
    """Finish an incremental cleanup pass and return its committed deletion totals."""
    result = CleanupResult()
    for _ in cleanup_steps(database, settings, result, now=NOW, **kwargs):
        pass
    return result


def age_event(database, event_id, received_at):
    """Set host receipt time independently of the producer's occurrence time."""
    with database.transaction() as connection:
        connection.execute(
            "UPDATE events SET received_at = ? WHERE id = ?", (timestamp(received_at), event_id)
        )


@pytest.mark.parametrize("retention_days", [7, 9])
def test_receipt_cutoff_cascades_all_delivery_states_and_removes_private_files(
    client, credentials, event, database, settings, human, retention_days
):
    settings.retention_days = retention_days
    command(database, "add-notification-rule", "backup")
    expired = []
    cutoff = NOW - timedelta(days=retention_days)
    for state in ("pending", "sent_to_ntfy", "failed", "suppressed_cooldown"):
        response = submit_image(client, credentials, event | {"event_key": state})
        assert response.status_code == 201
        event_id = response.json()["id"]
        assert human.get(f"/api/v1/events/{event_id}/thumbnail").status_code == 200
        age_event(database, event_id, cutoff - timedelta(microseconds=1))
        with database.transaction() as connection:
            connection.execute(
                "UPDATE notification_deliveries SET state = ? WHERE event_id = ?", (state, event_id)
            )
        expired.append(event_id)

    # An old producer clock must not expire a recently received event or the boundary.
    kept = submit_image(
        client,
        credentials,
        event | {"event_key": "boundary", "occurred_at": "2000-01-01T00:00:00Z"},
    ).json()["id"]
    age_event(database, kept, cutoff)
    assert human.get(f"/api/v1/events/{kept}/thumbnail").status_code == 200
    recent = client.post(
        "/api/v1/events", headers=credentials, json=event | {"event_key": "recent"}
    ).json()["id"]
    age_event(database, recent, NOW)

    result = cleanup(database, settings, batch_size=2)
    assert result == CleanupResult(expired_events=4, orphan_images=4, pruned_files=8)
    with database.connect() as connection:
        assert {row[0] for row in connection.execute("SELECT id FROM events")} == {kept, recent}
        assert connection.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM clients").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM human_users").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    for event_id in expired:
        for suffix in ("", "/image", "/thumbnail"):
            assert human.get(f"/api/v1/events/{event_id}{suffix}").status_code == 404
    assert human.get(f"/api/v1/events/{kept}/image").status_code == 200
    assert human.get(f"/api/v1/events/{kept}/thumbnail").status_code == 200
    assert cleanup(database, settings) == CleanupResult()


def test_orphan_rows_files_temporary_uploads_and_empty_dates_are_pruned(
    client, credentials, event, database, settings
):
    event_id = submit_image(client, credentials, event).json()["id"]
    # Model an interrupted old writer leaving metadata without an event reference.
    with database.transaction() as connection:
        connection.execute("UPDATE events SET image_id = NULL WHERE id = ?", (event_id,))

    for directory in ("images", "thumbnails"):
        parent = settings.data_dir / directory / "2000/01/01"
        parent.mkdir(parents=True)
        (parent / "unreferenced.jpg").write_bytes(b"orphan")
        (parent / ".upload-interrupted").write_bytes(b"partial")
    unrelated = settings.data_dir / "backup.jpg"
    unrelated.write_bytes(b"preserve")

    result = cleanup(database, settings)
    assert result == CleanupResult(orphan_images=1, pruned_files=5)
    assert unrelated.read_bytes() == b"preserve"
    for directory in ("images", "thumbnails"):
        assert list((settings.data_dir / directory).iterdir()) == []
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0


def test_interrupted_cleanup_recovers_committed_rows_and_retries_unlink_failures(
    client, credentials, event, database, settings, monkeypatch, caplog
):
    event_id = submit_image(client, credentials, event).json()["id"]
    age_event(database, event_id, NOW - timedelta(days=8))
    result = CleanupResult()
    steps = cleanup_steps(database, settings, result, now=NOW, batch_size=1)
    next(steps)
    steps.close()
    assert result.expired_events == 1

    # A restart still finds the metadata; failed file deletion remains recoverable.
    unlink = Path.unlink

    def fail_unlink(path, *args, **kwargs):
        raise PermissionError("private filesystem details")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    assert cleanup(Database(settings.database_path), settings) == CleanupResult(
        orphan_images=1, errors=1
    )
    assert "private filesystem details" not in caplog.text
    monkeypatch.setattr(Path, "unlink", unlink)
    assert cleanup(Database(settings.database_path), settings) == CleanupResult(pruned_files=1)


def test_failed_database_commit_keeps_event_metadata_and_files(
    client, credentials, event, database, settings, monkeypatch
):
    event_id = submit_image(client, credentials, event).json()["id"]
    age_event(database, event_id, NOW - timedelta(days=8))
    transaction = database.transaction

    @contextmanager
    def fail_commit():
        with transaction() as connection:
            yield connection
            raise sqlite3.OperationalError("commit unavailable")

    monkeypatch.setattr(database, "transaction", fail_commit)
    with pytest.raises(sqlite3.OperationalError):
        cleanup(database, settings)
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 1
    assert len(list((settings.data_dir / "images").rglob("*.jpg"))) == 1


def test_delivery_skips_expired_backlog_while_batched_cleanup_is_in_progress(
    client, credentials, event, database, settings
):
    settings.notification_cooldown_seconds = 0
    command(database, "add-notification-rule", "backup")
    for index in range(3):
        event_id = client.post(
            "/api/v1/events", headers=credentials, json=event | {"event_key": str(index)}
        ).json()["id"]
        age_event(database, event_id, NOW - timedelta(days=8))
    retained = client.post(
        "/api/v1/events", headers=credentials, json=event | {"event_key": "retained"}
    ).json()["id"]
    age_event(database, retained, NOW)
    with database.transaction() as connection:
        connection.execute(
            "UPDATE notification_deliveries SET next_attempt_at = ?", (timestamp(NOW),)
        )

    steps = cleanup_steps(database, settings, CleanupResult(), now=NOW, batch_size=1)
    next(steps)
    assert claim_delivery(database, settings, timestamp(NOW))["id"] == retained
    steps.close()


def test_pruning_waits_for_writer_and_rechecks_newly_committed_references(database, settings):
    # Pause after rename but before committing the reference, as a real ingest does.
    image = PreparedImage(b"normalized", "digest", 1, 1)
    pruning = threading.Event()

    def prune():
        pruning.set()
        return cleanup(database, settings)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with database.transaction() as connection:
            image_id, disk_name, path = write_image(image, settings.data_dir, timestamp(NOW))
            future = executor.submit(prune)
            assert pruning.wait(2)
            connection.execute(
                """INSERT INTO images(id, disk_name, content_type, size_bytes, sha256,
                    width, height, created_at)
                    VALUES (?, ?, 'image/jpeg', 10, 'digest', 1, 1, ?)""",
                (image_id, disk_name, timestamp(NOW)),
            )
            # A thumbnail-style writer has an existing image reference to protect its file.
            connection.execute(
                """INSERT INTO clients(id, name, token_hash, created_at)
                    VALUES ('writer', 'Writer', 'digest', ?)""",
                (timestamp(NOW),),
            )
            connection.execute(
                """INSERT INTO events(id, client_id, event_key, channel, severity, title,
                    message, received_at, image_id, payload_digest)
                    VALUES ('event', 'writer', 'key', 'backup', 'info', '', '', ?, ?, 'digest')""",
                (timestamp(NOW), image_id),
            )
        assert future.result(timeout=5) == CleanupResult()
    assert path.read_bytes() == b"normalized"


def test_pruning_does_not_follow_symlinked_storage(database, settings, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "private.jpg"
    target.write_bytes(b"preserve")
    (settings.data_dir / "images").symlink_to(outside, target_is_directory=True)
    thumbnails = settings.data_dir / "thumbnails"
    thumbnails.mkdir()
    (thumbnails / "linked-directory").symlink_to(outside, target_is_directory=True)
    (thumbnails / "linked-file").symlink_to(target)

    assert cleanup(database, settings) == CleanupResult(pruned_files=1, errors=1)
    assert target.read_bytes() == b"preserve"


def test_schedule_runs_at_startup_hourly_and_retries_storage_failures(
    database, settings, monkeypatch
):
    clock = [100.0]
    monkeypatch.setattr(cleanup_module.time, "monotonic", lambda: clock[0])
    schedule = CleanupSchedule(database, settings)
    assert not schedule.tick()
    assert schedule.next_run == 3700
    clock[0] = 3699
    assert not schedule.tick()
    assert schedule.next_run == 3700

    def broken_steps(*args, **kwargs):
        raise sqlite3.OperationalError("private database details")

    monkeypatch.setattr(cleanup_module, "cleanup_steps", broken_steps)
    clock[0] = 3700
    assert not schedule.tick()
    assert schedule.next_run == 3760


def test_manual_command_and_schedule_retry_failed_files(database, settings, monkeypatch, caplog):
    root = settings.data_dir / "images"
    root.mkdir()
    orphan = root / "orphan.jpg"
    orphan.write_bytes(b"orphan")
    monkeypatch.setattr(cleanup_module, "Settings", lambda: settings)
    caplog.set_level("INFO", logger="app.cleanup")
    cleanup_module.main([])
    assert not orphan.exists()
    assert "pruned_files=1 errors=0" in caplog.text

    orphan.write_bytes(b"orphan")
    unlink = Path.unlink

    def fail_unlink(path):
        raise PermissionError("private filesystem details")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    with pytest.raises(SystemExit) as exc:
        cleanup_module.main([])
    assert exc.value.code == 1
    assert orphan.exists()

    # Recoverable filesystem errors also shorten the automated schedule to one minute.
    monkeypatch.setattr(cleanup_module.time, "monotonic", lambda: 100.0)
    schedule = CleanupSchedule(database, settings)
    while schedule.tick():
        pass
    assert schedule.next_run == 160.0
    assert schedule.result.errors == 1
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(cleanup_module.time, "monotonic", lambda: 160.0)
    while schedule.tick():
        pass
    assert not orphan.exists()
    assert schedule.next_run == 3760.0


def test_worker_automatically_cleans_without_a_topic(
    client, credentials, event, database, settings, monkeypatch
):
    event_id = submit_image(client, credentials, event).json()["id"]
    age_event(database, event_id, datetime.now(UTC) - timedelta(days=8))
    assert settings.ntfy_topic is None
    stop = threading.Event()
    process = worker_module.process_once
    ticks = []

    def process_and_stop(*args):
        ticks.append(1)
        assert len(ticks) < 100
        handled = process(*args)
        if not list((settings.data_dir / "images").rglob("*.jpg")):
            stop.set()
        return handled

    monkeypatch.setattr(worker_module, "Settings", lambda: settings)
    monkeypatch.setattr(worker_module, "threading", SimpleNamespace(Event=lambda: stop))
    monkeypatch.setattr(worker_module.signal, "signal", lambda *_: None)
    monkeypatch.setattr(worker_module, "process_once", process_and_stop)
    worker_module.main([])
    assert len(ticks) >= 3
    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 0
        assert connection.execute("SELECT heartbeat_at FROM worker_status").fetchone()
