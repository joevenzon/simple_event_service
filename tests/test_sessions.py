import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.auth import token_digest
from app.cli import build_parser, run
from app.main import create_app
from app.security import LOGIN_COOKIE, SESSION_COOKIE


def test_login_secure_cookie_hash_and_logout(human, database):
    token = human.cookies[SESSION_COOKIE]
    with database.connect() as connection:
        row = connection.execute("SELECT * FROM sessions").fetchone()
    assert row["secret_hash"] == token_digest(token)
    assert token not in tuple(row)
    session = human.get("/api/v1/session").json()
    assert session["username"] == "admin"
    assert token not in str(session)
    assert (
        human.post(
            "/logout", headers={"X-CSRF-Token": session["csrf_token"]}, follow_redirects=False
        ).status_code
        == 303
    )
    assert SESSION_COOKIE not in human.cookies
    human.cookies.set(SESSION_COOKIE, token)
    assert human.get("/api/v1/events").status_code == 401
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_login_and_logout_cookie_flags(settings, credentials):
    settings.environment = "production"
    settings.session_secret = SecretStr("x" * 40)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        response = client.get("/login")
        assert "Secure" in response.headers["set-cookie"]
        csrf = client.cookies[LOGIN_COOKIE]
        response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "a sufficiently long password",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        cookie = next(
            value
            for value in response.headers.get_list("set-cookie")
            if value.startswith(SESSION_COOKIE + "=")
        )
        for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age=86400"):
            assert flag in cookie
        assert client.get("http://testserver/api/v1/events").status_code == 400
        assert client.get("http://testserver/healthz").status_code == 200


@pytest.mark.parametrize("mode", ["expired", "revoked", "disabled"])
def test_invalid_sessions_cannot_access_history_or_images(human, database, mode):
    if mode == "revoked":
        run(build_parser().parse_args(["revoke-sessions", "admin"]), database)
    with database.transaction() as connection:
        if mode == "expired":
            connection.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00.000000Z'")
        elif mode == "disabled":
            connection.execute("UPDATE human_users SET disabled = 1")
    for path in (
        "/api/v1/events",
        f"/api/v1/events/{uuid4()}",
        f"/api/v1/events/{uuid4()}/image",
        "/api/v1/session",
        "/account",
    ):
        assert human.get(path).status_code == 401


def test_revocation_cli(database, credentials):
    assert (
        run(build_parser().parse_args(["revoke-sessions", "admin"]), database)
        == "User sessions revoked."
    )
    with pytest.raises(ValueError, match="User not found"):
        run(build_parser().parse_args(["revoke-sessions", "missing"]), database)


@pytest.mark.parametrize("bearer", [False, True])
def test_anonymous_and_client_tokens_cannot_read_history(client, credentials, event, bearer):
    response = client.post("/api/v1/events", json=event, headers=credentials)
    event_id = response.json()["id"]
    headers = credentials if bearer else {}
    for path in (
        "/api/v1/events",
        f"/api/v1/events/{event_id}",
        f"/api/v1/events/{event_id}/image",
        "/api/v1/session",
        "/account",
    ):
        denied = client.get(path, headers=headers)
        assert denied.status_code == 401
        assert "Disk is healthy" not in denied.text


def test_human_session_cannot_ingest(human, event):
    assert human.post("/api/v1/events", json=event).status_code == 401


def test_session_rotation_and_restart(settings, human, login):
    old = human.cookies[SESSION_COOKIE]
    assert login().status_code == 303
    new = human.cookies[SESSION_COOKIE]
    assert old != new
    with TestClient(create_app(settings), base_url="https://testserver") as restarted:
        restarted.cookies.set(SESSION_COOKIE, old)
        assert restarted.get("/api/v1/events").status_code == 401
        restarted.cookies.set(SESSION_COOKIE, new)
        assert restarted.get("/api/v1/events").status_code == 200


@pytest.mark.parametrize("csrf", ["", "wrong", "é"])
def test_login_csrf_required(client, csrf):
    client.get("/login")
    response = client.post(
        "/login",
        data={"username": "admin", "password": "a sufficiently long password", "csrf_token": csrf},
    )
    assert response.status_code == 403
    assert SESSION_COOKIE not in client.cookies


def test_login_csrf_requires_cookie_and_unexpired_signature(client, monkeypatch):
    client.get("/login")
    csrf = client.cookies[LOGIN_COOKIE]
    client.cookies.clear()
    data = {"username": "admin", "password": "a sufficiently long password", "csrf_token": csrf}
    assert client.post("/login", data=data).status_code == 403
    client.cookies.set(LOGIN_COOKIE, csrf)
    monkeypatch.setattr("app.security.time.time", lambda: int(csrf.split(".")[0]) + 601)
    assert client.post("/login", data=data).status_code == 403


@pytest.mark.parametrize("csrf", ["", "wrong", "é"])
def test_logout_csrf_required(human, csrf):
    assert human.post("/logout", data={"csrf_token": csrf}).status_code == 403
    assert human.get("/api/v1/session").status_code == 200
    assert human.get("/logout").status_code == 405


def test_logout_accepts_form_token(human):
    csrf = human.get("/api/v1/session").json()["csrf_token"]
    assert (
        human.post("/logout", data={"csrf_token": csrf}, follow_redirects=False).status_code == 303
    )


def test_csrf_is_bound_to_session(human, login):
    csrf = human.get("/api/v1/session").json()["csrf_token"]
    assert login().status_code == 303
    assert human.post("/logout", data={"csrf_token": csrf}).status_code == 403


def test_login_throttling_survives_restart_and_recovers(client, login, settings, database):
    settings.login_attempts = 2
    assert login(password="wrong").status_code == 401
    assert login(username="unknown", password="wrong").status_code == 401
    response = login()
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0
    with TestClient(create_app(settings), base_url="https://testserver") as restarted:
        restarted.get("/login")
        data = {
            "username": "admin",
            "password": "a sufficiently long password",
            "csrf_token": restarted.cookies[LOGIN_COOKIE],
        }
        assert restarted.post("/login", data=data).status_code == 429
    with database.transaction() as connection:
        connection.execute("UPDATE rate_limits SET expires_at = ?", (time.time() - 1,))
    assert login().status_code == 303


def test_bad_login_does_not_disclose_user_existence(client, login, database):
    unknown = login(username="unknown", password="wrong")
    invalid = login(password="wrong")
    assert unknown.status_code == invalid.status_code == 401
    assert unknown.json() == invalid.json()
    assert "wrong" not in invalid.text
    with database.transaction() as connection:
        connection.execute("UPDATE human_users SET disabled = 1")
    assert login().status_code == 401
