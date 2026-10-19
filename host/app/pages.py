"""Private server-rendered status screens sharing the authenticated query implementation."""

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError, field_validator

from app.clients import client_summaries
from app.queries import event_detail, events
from app.schemas import Severity
from app.security import SESSION_COOKIE, csrf_token, require_human

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
SEVERITIES = ("debug", "info", "warning", "error", "critical")


def browser_human(request: Request):
    """Send signed-out browsers to login while preserving the requested private page."""
    try:
        return require_human(request)
    except HTTPException as exc:
        if exc.status_code != 401:
            raise
        destination = request.url.path
        if request.url.query:
            destination += "?" + request.url.query
        raise HTTPException(
            303, headers={"Location": "/login?" + urlencode({"next": destination})}
        ) from None


BrowserHuman = Annotated[object, Depends(browser_human)]


class FeedFilters(BaseModel):
    """Accept browser filter fields with empty controls treated as unselected."""

    channel: str | None = Field(default=None, max_length=255)
    subtree: bool = True
    severity: Severity | None = None
    client_id: str | None = Field(default=None, max_length=80)
    date_from: date | None = None
    date_through: date | None = None
    search: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=512)

    @field_validator(
        "channel",
        "severity",
        "client_id",
        "date_from",
        "date_through",
        "search",
        "cursor",
        mode="before",
    )
    @classmethod
    def blank_is_unselected(cls, value):
        return None if value == "" else value

    def query_arguments(self) -> dict:
        """Convert inclusive UTC calendar dates into the API's half-open time range."""
        try:
            lower = datetime.combine(self.date_from, time(), UTC) if self.date_from else None
            upper = (
                datetime.combine(self.date_through, time(), UTC) + timedelta(days=1)
                if self.date_through
                else None
            )
        except OverflowError:
            raise HTTPException(422, "Date is outside the supported range") from None
        return self.model_dump(exclude={"date_from", "date_through"}) | {
            "received_from": lower,
            "received_before": upper,
        }


def feed_url(**filters) -> str:
    """Build an internal feed URL, dropping empty controls and obsolete cursors."""
    values = {key: value for key, value in filters.items() if value is not None and value != ""}
    return "/events" + ("?" + urlencode(values) if values else "")


def breadcrumbs(channel: str) -> list[dict]:
    """Link each channel segment to its boundary-aware subtree filter."""
    parts = channel.split(".")
    return [
        {"label": part, "url": feed_url(channel=".".join(parts[: index + 1]))}
        for index, part in enumerate(parts)
    ]


templates.env.globals.update(breadcrumbs=breadcrumbs, feed_url=feed_url)


def render(request: Request, human, template: str, context: dict, status_code: int = 200):
    """Render escaped private content and a session-bound logout form."""
    return templates.TemplateResponse(
        request,
        template,
        {
            "username": human["username"],
            "csrf_token": csrf_token(request, request.cookies[SESSION_COOKIE], "session"),
            "severities": SEVERITIES,
            **context,
        },
        status_code=status_code,
    )


@router.get("/", include_in_schema=False)
def dashboard(request: Request, human: BrowserHuman):
    """Show recent events, retained severity totals, and current client heartbeat states."""
    with request.app.state.database.connect() as connection:
        counts = {
            row["severity"]: row["total"]
            for row in connection.execute(
                "SELECT severity, COUNT(*) AS total FROM events GROUP BY severity"
            )
        }
    page = events(request, limit=10)
    return render(
        request,
        human,
        "dashboard.html",
        {
            "counts": counts,
            "total": sum(counts.values()),
            "page": page,
            "clients": client_summaries(request.app.state.database),
        },
    )


@router.get("/events", include_in_schema=False)
def event_feed(request: Request, human: BrowserHuman):
    """Render paginated history with the same filters and ordering as the JSON API."""
    filters = dict(request.query_params)
    clients = client_summaries(request.app.state.database)
    try:
        selected = FeedFilters.model_validate(filters)
        page = events(request, **selected.query_arguments())
    except (ValidationError, HTTPException) as exc:
        error = "Check the filter values and date range."
        if isinstance(exc, HTTPException):
            error = str(exc.detail)
        return render(
            request,
            human,
            "events.html",
            {
                "filters": filters,
                "clients": clients,
                "error": error,
                "page": {"items": [], "next_cursor": None},
            },
            status_code=422,
        )

    # Retain every selected filter on the next page; a new form submission resets the cursor.
    next_url = feed_url(**(selected.model_dump() | {"cursor": page["next_cursor"]}))
    return render(
        request,
        human,
        "events.html",
        {
            "filters": selected.model_dump(mode="json"),
            "clients": clients,
            "page": page,
            "next_url": next_url,
            "first_url": feed_url(**selected.model_dump(exclude={"cursor"})),
        },
    )


@router.get("/events/{event_id}", include_in_schema=False)
def detail(request: Request, event_id: UUID, human: BrowserHuman):
    """Display full event contents and its private image after checking the session."""
    return render(request, human, "event_detail.html", {"event": event_detail(request, event_id)})


@router.get("/clients", include_in_schema=False)
def client_status(request: Request, human: BrowserHuman):
    """Display heartbeat health, publication scopes and last event times privately."""
    return render(
        request,
        human,
        "clients.html",
        {
            "clients": client_summaries(request.app.state.database),
        },
    )
