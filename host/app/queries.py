"""Session-only history, cursor pagination and private snapshot retrieval."""

import base64
import binascii
import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.channels import validate_channel
from app.schemas import EventPage, EventView, Severity
from app.security import require_human

router = APIRouter(dependencies=[Depends(require_human)])


def timestamp(value: datetime) -> str:
    """Normalize an aware date to sortable UTC text or reject invalid filter dates."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(422, "Dates must include a timezone")
    try:
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (ValueError, OverflowError):
        raise HTTPException(422, "Date is outside the supported UTC range") from None


def encode_cursor(row) -> str:
    """Encode the last receipt-time/ID pair without exposing internal storage details."""
    return (
        base64.urlsafe_b64encode(json.dumps([row["received_at"], row["id"]]).encode())
        .decode()
        .rstrip("=")
    )


def decode_cursor(cursor: str) -> tuple[str, str]:
    """Validate a pagination position before using it as bound SQL parameters."""
    try:
        values = json.loads(
            base64.b64decode(cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True)
        )
        if (
            not isinstance(values, list)
            or len(values) != 2
            or not all(isinstance(v, str) for v in values)
        ):
            raise ValueError
        received_at = timestamp(datetime.fromisoformat(values[0]))
        return received_at, str(UUID(values[1]))
    except (ValueError, TypeError, binascii.Error):
        raise HTTPException(422, "Invalid pagination cursor") from None


def event_response(row) -> dict:
    """Expose public event fields and private image URLs without digests or disk names."""
    return {
        key: row[key]
        for key in (
            "id",
            "client_id",
            "event_key",
            "channel",
            "severity",
            "title",
            "message",
            "occurred_at",
            "received_at",
            "image_id",
            "notification_state",
        )
    } | {
        "tags": json.loads(row["tags_json"]),
        "metadata": json.loads(row["metadata_json"]),
        "image_url": f"/api/v1/events/{row['id']}/image" if row["image_id"] else None,
    }


EVENT_SELECT = """SELECT events.*, COALESCE(delivery.state, 'not_selected') AS notification_state
    FROM events LEFT JOIN notification_deliveries AS delivery
    ON delivery.event_id = events.id AND delivery.provider = 'ntfy'"""


@router.get("/api/v1/events", response_model=EventPage)
def events(
    request: Request,
    channel: Annotated[str | None, Query(max_length=255)] = None,
    subtree: bool = True,
    severity: Severity | None = None,
    client_id: Annotated[str | None, Query(max_length=80)] = None,
    received_from: datetime | None = None,
    received_before: datetime | None = None,
    search: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
):
    """List session-protected events using literal filters and stable descending cursors."""
    clauses, parameters = [], []
    if channel is not None:
        try:
            validate_channel(channel)
        except ValueError:
            raise HTTPException(422, "Invalid channel filter") from None
        if subtree:
            # '_' is legal in channels, so SQL LIKE would need escaping. A lexical
            # range from prefix+'.' through prefix+'/' uses ASCII segment boundaries.
            clauses.append("(channel = ? OR (channel >= ? AND channel < ?))")
            parameters.extend([channel, channel + ".", channel + "/"])
        else:
            clauses.append("channel = ?")
            parameters.append(channel)

    # All user-provided filter values are bound parameters; field names are server constants.
    for name, value in (("severity", severity), ("client_id", client_id)):
        if value is not None:
            clauses.append(f"{name} = ?")
            parameters.append(value)

    if received_from is not None:
        clauses.append("received_at >= ?")
        parameters.append(timestamp(received_from))
    if received_before is not None:
        clauses.append("received_at < ?")
        parameters.append(timestamp(received_before))
    if (
        received_from is not None
        and received_before is not None
        and received_from >= received_before
    ):
        raise HTTPException(422, "received_from must precede received_before")

    if search is not None:
        clauses.append("(instr(lower(title), lower(?)) > 0 OR instr(lower(message), lower(?)) > 0)")
        parameters.extend([search, search])
    if cursor is not None:
        received_at, event_id = decode_cursor(cursor)
        clauses.append("(received_at, events.id) < (?, ?)")
        parameters.extend([received_at, event_id])

    # Fetch one extra row to decide whether a following page exists.
    sql = EVENT_SELECT + (" WHERE " + " AND ".join(clauses) if clauses else "")
    sql += " ORDER BY received_at DESC, events.id DESC LIMIT ?"
    with request.app.state.database.connect() as connection:
        rows = connection.execute(sql, [*parameters, limit + 1]).fetchall()

    page = rows[:limit]
    return {
        "items": [event_response(row) for row in page],
        "next_cursor": encode_cursor(page[-1]) if len(rows) > limit else None,
    }


@router.get("/api/v1/events/{event_id}", response_model=EventView)
def event_detail(request: Request, event_id: UUID):
    """Return an immutable event and delivery state to an authenticated human."""
    with request.app.state.database.connect() as connection:
        row = connection.execute(EVENT_SELECT + " WHERE events.id = ?", (str(event_id),)).fetchone()
    if row is None:
        raise HTTPException(404, "Event not found")
    return event_response(row)


@router.get(
    "/api/v1/events/{event_id}/image",
    response_class=FileResponse,
    responses={200: {"content": {"image/jpeg": {}}}},
)
def event_image(request: Request, event_id: UUID):
    """Serve one private JPEG by event ID without accepting client-provided paths."""
    with request.app.state.database.connect() as connection:
        row = connection.execute(
            """SELECT images.disk_name FROM events JOIN images ON images.id = events.image_id
            WHERE events.id = ?""",
            (str(event_id),),
        ).fetchone()
    if row is None:
        raise HTTPException(404, "Image not found")
    path = request.app.state.settings.data_dir / "images" / row["disk_name"]
    if not path.is_file():
        raise HTTPException(404, "Image not found")
    return FileResponse(
        path, media_type="image/jpeg", filename=f"{event_id}.jpg", content_disposition_type="inline"
    )
