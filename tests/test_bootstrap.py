import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.db import Database
from app.main import create_app


def test_liveness_readiness_and_restart_preserve_database(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").json() == {"status": "ready"}
        database = Database(settings.database_path)
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO clients(id, name, token_hash, created_at)
                VALUES ('test', 'Test', 'hash', '2026-10-18T00:00:00.000000Z')"""
            )
    with TestClient(create_app(settings)) as client:
        assert client.get("/readyz").status_code == 200
        with database.connect() as connection:
            assert connection.execute("SELECT count(*) FROM clients").fetchone()[0] == 1
            assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


def test_unavailable_database_does_not_break_liveness(settings, monkeypatch):
    def fail(_self):
        raise sqlite3.OperationalError("private filesystem detail")

    with TestClient(create_app(settings)) as client:
        monkeypatch.setattr(Database, "check_ready", fail)
        assert client.get("/healthz").status_code == 200
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "not_ready"}
        assert "private" not in response.text


def test_transaction_rolls_back_on_failure(database):
    with pytest.raises(sqlite3.IntegrityError), database.transaction() as connection:
        connection.execute(
            """INSERT INTO clients(id, name, token_hash, created_at)
            VALUES ('one', 'same', 'hash-one', 'now')"""
        )
        connection.execute(
            """INSERT INTO clients(id, name, token_hash, created_at)
            VALUES ('two', 'same', 'hash-two', 'now')"""
        )
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM clients").fetchone()[0] == 0


def test_failed_migration_rolls_back_ddl_and_version(database, monkeypatch):
    monkeypatch.setattr(
        db_module,
        "MIGRATIONS",
        (*db_module.MIGRATIONS, (2, ("CREATE TABLE partial (id TEXT)", "INVALID SQL"))),
    )
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.connect() as connection:
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 1
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name = 'partial'").fetchone()
            is None
        )


def test_newer_database_is_rejected(database):
    with database.transaction() as connection:
        connection.execute("INSERT INTO schema_migrations VALUES (2, 'now')")
    with pytest.raises(RuntimeError, match="incompatible"):
        database.initialize()


def test_concurrent_initialization_is_serialized(database):
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: Database(database.path).initialize(), range(4)))
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 1


def test_readiness_check_does_not_change_migration_history(database):
    with database.connect() as connection:
        before = tuple(connection.execute("SELECT * FROM schema_migrations").fetchone())
    database.check_ready()
    with database.connect() as connection:
        assert tuple(connection.execute("SELECT * FROM schema_migrations").fetchone()) == before
