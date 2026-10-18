"""Independent machine/session authentication and persistent request throttles."""

import hashlib
import hmac
import math
import secrets
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from app.auth import token_digest, verify_token
from app.db import utc_now

SESSION_COOKIE = "ses_session"
LOGIN_COOKIE = "ses_login_csrf"


def throttle(request: Request, keys: list[str], limit: int, seconds: int) -> None:
    """Atomically count attempts across processes and reject full windows with Retry-After."""
    now = time.time()
    retry = 0
    with request.app.state.database.transaction() as connection:
        connection.execute("DELETE FROM rate_limits WHERE expires_at <= ?", (now,))
        for key in keys:
            row = connection.execute("SELECT * FROM rate_limits WHERE key = ?", (key,)).fetchone()
            if row is not None and row["attempts"] >= limit:
                retry = max(retry, math.ceil(row["expires_at"] - now))

        # Either increment every applicable bucket or leave all buckets unchanged.
        if not retry:
            for key in keys:
                connection.execute(
                    """INSERT INTO rate_limits(key, attempts, expires_at) VALUES (?, 1, ?)
                    ON CONFLICT(key) DO UPDATE SET attempts = attempts + 1""",
                    (key, now + seconds),
                )

    if retry:
        raise HTTPException(429, "Too many requests", headers={"Retry-After": str(max(1, retry))})


def require_client(request: Request):
    """Authenticate an enabled producer and charge its persistent ingest rate limit."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    row = None
    if scheme.lower() == "bearer" and 1 <= len(token) <= 256:
        with request.app.state.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM clients WHERE token_hash = ? AND enabled = 1", (token_digest(token),)
            ).fetchone()

    if row is None or not verify_token(token, row["token_hash"]):
        raise HTTPException(
            401, "Invalid client credentials", headers={"WWW-Authenticate": "Bearer"}
        )
    throttle(request, ["ingest:" + row["id"]], request.app.state.settings.ingest_per_minute, 60)
    return row


def require_human(request: Request):
    """Require an unexpired stored session for an enabled human user on every read."""
    token = request.cookies.get(SESSION_COOKIE, "")
    row = None
    if 1 <= len(token) <= 256:
        with request.app.state.database.connect() as connection:
            row = connection.execute(
                """SELECT sessions.id AS session_id, human_users.id AS user_id,
                    human_users.username FROM sessions
                    JOIN human_users ON human_users.id = sessions.user_id
                    WHERE secret_hash = ? AND expires_at > ? AND disabled = 0""",
                (token_digest(token), utc_now()),
            ).fetchone()

    if row is None:
        raise HTTPException(401, "Human login required")
    return row


def csrf_token(request: Request, value: str, purpose: str) -> str:
    """Derive a purpose-specific CSRF token without exposing the underlying credential."""
    return hmac.new(
        request.app.state.csrf_secret, f"{purpose}:{value}".encode(), hashlib.sha256
    ).hexdigest()


def issue_login_csrf(request: Request) -> str:
    """Create an expiring signed nonce for the login form and its companion cookie."""
    value = f"{int(time.time())}.{secrets.token_urlsafe(32)}"
    return value + "." + csrf_token(request, value, "login")


def check_login_csrf(request: Request, submitted: str) -> None:
    """Require the same signed, unexpired login nonce in both cookie and form."""
    cookie = request.cookies.get(LOGIN_COOKIE, "")
    try:
        value, signature = cookie.rsplit(".", 1)
        age = time.time() - int(value.split(".", 1)[0])
        valid = (
            0 <= age <= 600
            and hmac.compare_digest(signature, csrf_token(request, value, "login"))
            and hmac.compare_digest(cookie, submitted)
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise HTTPException(403, "Invalid CSRF token")


def check_session_csrf(request: Request, submitted: str) -> None:
    """Reject mutating browser requests whose CSRF token belongs to another session."""
    expected = csrf_token(request, request.cookies.get(SESSION_COOKIE, ""), "session")
    if not hmac.compare_digest(expected.encode(), submitted.encode()):
        raise HTTPException(403, "Invalid CSRF token")


def session_expiry(hours: int) -> str:
    """Return the absolute UTC expiry in the database's sortable timestamp format."""
    return (
        (datetime.now(UTC) + timedelta(hours=hours))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


Human = Annotated[sqlite3.Row, Depends(require_human)]
Client = Annotated[sqlite3.Row, Depends(require_client)]
