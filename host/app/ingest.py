"""Durable immutable ingestion with one serialized idempotency decision."""

import hashlib
import json
import logging
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile

from app.channels import channel_matches
from app.db import utc_now
from app.images import prepare_image, write_image
from app.notifications import enqueue_notification
from app.schemas import EventAccepted, EventInput, canonical_json
from app.security import Client

router = APIRouter()
logger = logging.getLogger(__name__)


def parse_event(raw: str | bytes) -> EventInput:
    """Validate producer JSON without returning private input values in error responses."""
    try:
        return EventInput.model_validate_json(raw)
    except ValidationError as exc:
        # Validation errors can contain event contents; expose locations and messages only.
        raise HTTPException(
            422,
            [
                {"loc": error["loc"], "msg": error["msg"], "type": error["type"]}
                for error in exc.errors()
            ],
        ) from None
    except (ValueError, RecursionError):
        raise HTTPException(422, "Invalid event JSON") from None


@router.post(
    "/api/v1/events",
    status_code=201,
    response_model=EventAccepted,
    responses={200: {"model": EventAccepted, "description": "Identical duplicate"}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"$ref": "#/components/schemas/EventInput"}},
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["event", "image"],
                        "properties": {
                            "event": {
                                "type": "string",
                                "description": "JSON serialized EventInput",
                            },
                            "image": {"type": "string", "format": "binary"},
                        },
                    },
                },
            },
        }
    },
)
async def ingest(request: Request, client: Client):
    """Read one bounded JSON or multipart event and persist it off the event loop."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    image_data = None
    if content_type == "application/json":
        event = parse_event(await request.body())
    elif content_type == "multipart/form-data":
        async with request.form(max_files=1, max_fields=1, max_part_size=64 * 1024) as form:
            if sorted(form.keys()) != ["event", "image"] or any(
                len(form.getlist(key)) != 1 for key in form
            ):
                raise HTTPException(422, "Expected event JSON field and one image")
            if not isinstance(form["event"], str) or not isinstance(form["image"], UploadFile):
                raise HTTPException(422, "Expected event JSON field and one image")
            event = parse_event(form["event"])
            image_data = await form["image"].read(request.app.state.settings.max_image_bytes + 1)
    else:
        raise HTTPException(415, "Use application/json or multipart/form-data")

    return await run_in_threadpool(store_event, request, client, event, image_data)


def store_event(request: Request, client, event: EventInput, image_data: bytes | None):
    """Commit an authorized event once, or return its existing ID for an identical retry."""
    # Decode before acquiring SQLite's writer lock; identity uses original attachment bytes.
    settings = request.app.state.settings
    image = prepare_image(image_data, settings) if image_data is not None else None
    payload = event.model_dump(mode="json")
    digest = hashlib.sha256(
        canonical_json(
            {"event": payload, "image_sha256": image.source_digest if image else None}
        ).encode("utf-8")
    ).hexdigest()

    created_path = None
    try:
        with request.app.state.database.transaction() as connection:
            # Re-read revocation/scopes while holding the write lock.
            current = connection.execute(
                "SELECT * FROM clients WHERE id = ?", (client["id"],)
            ).fetchone()
            if (
                current is None
                or not current["enabled"]
                or current["token_hash"] != client["token_hash"]
            ):
                raise HTTPException(401, "Invalid client credentials")
            prefixes = json.loads(current["allowed_prefixes_json"])
            if prefixes and not any(channel_matches(prefix, event.channel) for prefix in prefixes):
                raise HTTPException(403, "Client cannot publish to this channel")

            # Compare immutable payloads before allocating a new image or event identifier.
            existing = connection.execute(
                "SELECT id, payload_digest FROM events WHERE client_id = ? AND event_key = ?",
                (client["id"], event.event_key),
            ).fetchone()
            if existing and existing["payload_digest"] != digest:
                logger.info(
                    "Conflicting event retry client_id=%s event_id=%s", client["id"], existing["id"]
                )
                raise HTTPException(409, "Event key already used with a different payload")

            received_at = utc_now()
            connection.execute(
                "UPDATE clients SET last_seen_at = ? WHERE id = ?", (received_at, client["id"])
            )
            if existing:
                return JSONResponse({"id": existing["id"], "status": "accepted", "duplicate": True})

            # Rename the sanitized image before inserting its reference in the transaction.
            event_id, image_id = str(uuid4()), None
            if image:
                image_id, disk_name, created_path = write_image(
                    image, settings.data_dir, received_at
                )
                connection.execute(
                    """INSERT INTO images(id, disk_name, content_type, size_bytes, sha256,
                        width, height, created_at) VALUES (?, ?, 'image/jpeg', ?, ?, ?, ?, ?)""",
                    (
                        image_id,
                        disk_name,
                        len(image.data),
                        hashlib.sha256(image.data).hexdigest(),
                        image.width,
                        image.height,
                        received_at,
                    ),
                )

            connection.execute(
                """INSERT INTO events(id, client_id, event_key, channel, severity, title, message,
                    occurred_at, received_at, tags_json, metadata_json, image_id, notify_hint,
                    payload_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    event_id,
                    client["id"],
                    event.event_key,
                    event.channel,
                    event.severity,
                    event.title,
                    event.message,
                    event.occurred_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
                    if event.occurred_at
                    else None,
                    received_at,
                    canonical_json(event.tags),
                    canonical_json(event.metadata),
                    image_id,
                    event.notify,
                    digest,
                ),
            )
            enqueue_notification(connection, event_id, client["id"], event, received_at, settings)
    except BaseException:
        # A rolled-back event must not leave a file behind during ordinary error recovery.
        if created_path is not None:
            created_path.unlink(missing_ok=True)
        raise

    logger.info(
        "Event accepted client_id=%s event_id=%s channel=%s", client["id"], event_id, event.channel
    )
    return JSONResponse({"id": event_id, "status": "accepted", "duplicate": False}, status_code=201)
