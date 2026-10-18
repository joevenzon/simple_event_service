"""Browser login, account and logout routes with persistent private sessions."""

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app.auth import issue_token, token_digest, verify_password
from app.db import utc_now
from app.schemas import SessionView
from app.security import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    Human,
    check_login_csrf,
    check_session_csrf,
    csrf_token,
    issue_login_csrf,
    session_expiry,
    throttle,
)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


@router.get("/login", include_in_schema=False)
def login_page(request: Request):
    """Render an escaped sign-in form and set its short-lived CSRF cookie."""
    csrf = issue_login_csrf(request)
    response = templates.TemplateResponse(request, "login.html", {"csrf_token": csrf})
    response.set_cookie(
        LOGIN_COOKIE,
        csrf,
        max_age=600,
        httponly=True,
        secure=request.app.state.settings.secure_cookies,
        samesite="lax",
    )
    return response


@router.post("/login", include_in_schema=False)
async def login(request: Request):
    """Validate bounded form fields and CSRF before verifying human credentials."""
    async with request.form(max_files=0, max_fields=3, max_part_size=4096) as form:
        if sorted(form.keys()) != ["csrf_token", "password", "username"] or any(
            len(form.getlist(key)) != 1 or not isinstance(form[key], str) for key in form
        ):
            raise HTTPException(422, "Expected username, password and csrf_token")
        username, password, csrf = form["username"], form["password"], form["csrf_token"]

    check_login_csrf(request, csrf)
    if not 1 <= len(username) <= 100 or not 1 <= len(password) <= 1024:
        raise HTTPException(422, "Invalid credential length")
    # Argon2 and SQLite work run off the event loop.
    return await run_in_threadpool(complete_login, request, username, password)


def complete_login(request: Request, username: str, password: str):
    """Throttle password attempts and replace this browser's session after authentication."""
    settings = request.app.state.settings
    address = request.client.host if request.client else "unknown"
    throttle(
        request,
        ["login-ip:" + token_digest(address), "login-user:" + token_digest(username)],
        settings.login_attempts,
        settings.login_window_seconds,
    )

    # Unknown users still perform Argon2 verification to avoid a fast existence check.
    database = request.app.state.database
    with database.connect() as connection:
        user = connection.execute(
            "SELECT * FROM human_users WHERE username = ?", (username,)
        ).fetchone()
    stored_hash = user["password_hash"] if user else request.app.state.dummy_password_hash
    valid = verify_password(password, stored_hash)
    if not valid or user is None or user["disabled"]:
        raise HTTPException(401, "Invalid username or password")

    token, digest = issue_token()
    with database.transaction() as connection:
        # Recheck after password verification so a concurrent disable takes effect.
        current = connection.execute(
            "SELECT * FROM human_users WHERE id = ?", (user["id"],)
        ).fetchone()
        if current is None or current["disabled"] or current["password_hash"] != stored_hash:
            raise HTTPException(401, "Invalid username or password")

        # Rotate only the presented session, preserving independent sessions on other devices.
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now(),))
        connection.execute(
            "DELETE FROM sessions WHERE secret_hash = ?",
            (token_digest(request.cookies.get(SESSION_COOKIE, "")),),
        )
        connection.execute(
            """INSERT INTO sessions(id, user_id, secret_hash, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (str(uuid4()), user["id"], digest, session_expiry(settings.session_hours), utc_now()),
        )

    # Raw session credentials leave the server only in the HttpOnly cookie.
    response = RedirectResponse("/account", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_hours * 3600,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
    )
    response.delete_cookie(
        LOGIN_COOKIE, secure=settings.secure_cookies, httponly=True, samesite="lax"
    )
    return response


@router.get("/api/v1/session", response_model=SessionView)
def session(request: Request, human: Human):
    """Return the current username and a CSRF token for authenticated browser actions."""
    return {
        "username": human["username"],
        "csrf_token": csrf_token(request, request.cookies[SESSION_COOKIE], "session"),
    }


@router.get("/account", include_in_schema=False)
def account(request: Request, human: Human):
    """Render the authenticated account and logout form with escaped user text."""
    return templates.TemplateResponse(request, "account.html", session(request, human))


@router.post("/logout", include_in_schema=False)
async def logout(request: Request, human: Human):
    """Revoke this session after CSRF verification and expire the browser's cookie."""
    csrf = request.headers.get("x-csrf-token")
    if csrf is None:
        async with request.form(max_files=0, max_fields=1, max_part_size=1024) as form:
            csrf = form.get("csrf_token", "")
            if not isinstance(csrf, str) or len(form.getlist("csrf_token")) != 1:
                raise HTTPException(403, "Invalid CSRF token")

    check_session_csrf(request, csrf)
    await run_in_threadpool(revoke_session, request, human["session_id"])
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(
        SESSION_COOKIE,
        secure=request.app.state.settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )
    return response


def revoke_session(request: Request, session_id: str) -> None:
    """Delete one session so replaying its cookie immediately loses authorization."""
    with request.app.state.database.transaction() as connection:
        connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
