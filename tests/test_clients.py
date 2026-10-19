from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.auth import issue_token
from app.main import create_app


def test_heartbeat_updates_liveness_without_events(human, credentials, database, event):
    initial = human.get("/api/v1/clients").json()[0]
    assert initial["status"] == "never_seen"
    assert initial["last_heartbeat_at"] is None

    # An event alone must not claim the heartbeat loop is working.
    human.post("/api/v1/events", headers=credentials, json=event)
    summary = human.get("/api/v1/clients").json()[0]
    assert summary["status"] == "never_seen"
    assert summary["last_event_at"] == summary["last_seen_at"]

    health = {"mqtt_connected": True, "queue_size": 2, "nested": {"state": "ok"}}
    result = human.post("/api/v1/heartbeat", headers=credentials, json={"health": health})
    assert result.status_code == 200
    received_at = result.json()["received_at"]
    summary = human.get("/api/v1/clients").json()[0]
    assert summary["status"] == "healthy"
    assert summary["health"] == health
    assert summary["last_seen_at"] == summary["last_heartbeat_at"] == received_at
    assert summary["last_event_at"] <= received_at
    assert summary["allowed_prefixes"] == ["backup", "frigate"]
    assert "token_hash" not in summary
    assert "password_hash" not in summary

    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM notification_deliveries").fetchone()[0] == 0

    # Every heartbeat replaces the snapshot; an empty report clears old fields.
    assert human.post("/api/v1/heartbeat", headers=credentials, json={}).status_code == 200
    assert human.get("/api/v1/clients").json()[0]["health"] == {}


def test_health_survives_restart_and_uses_client_timeout(human, credentials, database, settings):
    assert (
        human.post(
            "/api/v1/heartbeat", headers=credentials, json={"health": {"ok": True}}
        ).status_code
        == 200
    )
    with TestClient(create_app(settings), base_url="https://testserver") as restarted:
        restarted.cookies.update(human.cookies)
        assert restarted.get("/api/v1/clients").json()[0]["health"] == {"ok": True}

    # Use an age safely between two thresholds to avoid dependence on test execution time.
    old = (datetime.now(UTC) - timedelta(seconds=90)).isoformat()
    with database.transaction() as connection:
        connection.execute(
            "UPDATE clients SET last_heartbeat_at = ?, stale_timeout_seconds = 60", (old,)
        )
    assert human.get("/api/v1/clients").json()[0]["status"] == "stale"
    with database.transaction() as connection:
        connection.execute("UPDATE clients SET stale_timeout_seconds = 300")
    assert human.get("/api/v1/clients").json()[0]["status"] == "healthy"
    with database.transaction() as connection:
        connection.execute("UPDATE clients SET enabled = 0")
    assert human.get("/api/v1/clients").json()[0]["status"] == "disabled"


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_heartbeat_requires_client_token(human, headers):
    assert human.post("/api/v1/heartbeat", headers=headers, json={}).status_code == 401


@pytest.mark.parametrize("change", [{"enabled": 0}, {"token_hash": "rotated"}])
def test_heartbeat_rejects_revoked_or_rotated_tokens(client, credentials, database, change):
    field, value = next(iter(change.items()))
    with database.transaction() as connection:
        connection.execute(f"UPDATE clients SET {field} = ?", (value,))
    assert client.post("/api/v1/heartbeat", headers=credentials, json={}).status_code == 401
    with database.connect() as connection:
        assert connection.execute("SELECT last_heartbeat_at FROM clients").fetchone()[0] is None


def test_heartbeat_rechecks_token_inside_transaction(client, credentials, database):
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.clients import heartbeat
    from app.schemas import HeartbeatInput
    from app.security import require_client

    request = Request(
        {
            "type": "http",
            "app": client.app,
            "headers": [(b"authorization", credentials["Authorization"].encode())],
        }
    )
    authenticated = require_client(request)
    with database.transaction() as connection:
        connection.execute("UPDATE clients SET token_hash = 'rotated'")
    with pytest.raises(HTTPException) as error:
        heartbeat(request, authenticated, HeartbeatInput())
    assert error.value.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"health": []},
        {"health": "ok"},
        {"health": {"x": "a" * 8192}},
        {"unexpected": True},
        [],
        None,
    ],
)
def test_heartbeat_validation(client, credentials, body):
    response = client.post("/api/v1/heartbeat", headers=credentials, json=body)
    assert response.status_code == 422


def test_heartbeat_byte_limits_and_nonfinite_values(client, credentials):
    for body, expected in [
        (b'{"health":{"value":NaN}}', 422),
        (b'{"health":{"value":Infinity}}', 422),
        (b"x" * (16 * 1024 + 1), 413),
    ]:
        response = client.post(
            "/api/v1/heartbeat",
            headers=credentials | {"Content-Type": "application/json"},
            content=iter([body]),
        )
        assert response.status_code == expected


def test_heartbeat_rate_limit_is_per_client_and_shared_with_ingest(
    client, credentials, settings, database, event
):
    settings.ingest_per_minute = 1
    assert client.post("/api/v1/heartbeat", headers=credentials, json={}).status_code == 200
    limited = client.post("/api/v1/events", headers=credentials, json=event)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0

    token, digest = issue_token()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO clients(id, name, token_hash, created_at) "
            "VALUES ('other', 'Other', ?, '2026-10-18T00:00:00Z')",
            (digest,),
        )
    assert (
        client.post(
            "/api/v1/heartbeat", headers={"Authorization": "Bearer " + token}, json={}
        ).status_code
        == 200
    )


@pytest.mark.parametrize("bearer", [False, True])
def test_client_status_api_is_human_only(client, credentials, bearer):
    response = client.get("/api/v1/clients", headers=credentials if bearer else {})
    assert response.status_code == 401
    assert "Producer" not in response.text
