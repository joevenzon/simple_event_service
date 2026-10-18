import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password, issue_token
from app.config import Settings
from app.db import Database, utc_now
from app.main import create_app

PASSWORD = "a sufficiently long password"


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, environment="development", data_dir=tmp_path / "data")


@pytest.fixture
def database(settings):
    database = Database(settings.database_path)
    database.initialize()
    return database


@pytest.fixture(scope="session")
def password_hash():
    return hash_password(PASSWORD)


@pytest.fixture
def credentials(database, password_hash):
    token, digest = issue_token()
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO clients(id, name, token_hash, allowed_prefixes_json, created_at)
            VALUES ('producer', 'Producer', ?, '["backup", "frigate"]', ?)""",
            (digest, utc_now()),
        )
        connection.execute(
            """INSERT INTO human_users(id, username, password_hash, created_at)
            VALUES ('admin-id', 'admin', ?, ?)""",
            (password_hash, utc_now()),
        )
    return {"Authorization": "Bearer " + token}


@pytest.fixture
def client(settings, credentials):
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


@pytest.fixture
def login(client):
    def perform(username="admin", password=PASSWORD):
        client.get("/login")
        return client.post(
            "/login",
            data={
                "username": username,
                "password": password,
                "csrf_token": client.cookies["ses_login_csrf"],
            },
            follow_redirects=False,
        )

    return perform


@pytest.fixture
def human(client, login):
    assert login().status_code == 303
    return client


@pytest.fixture
def event():
    return {
        "event_key": "event-one",
        "channel": "backup.status.freespace",
        "severity": "info",
        "title": "Free space",
        "message": "Disk is healthy.",
    }
