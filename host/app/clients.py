"""Producer heartbeats and private client health summaries."""

import json
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from app.db import utc_now
from app.schemas import ClientView, HeartbeatAccepted, HeartbeatInput, canonical_json
from app.security import Client, require_human

router = APIRouter()


@router.post("/api/v1/heartbeat", response_model=HeartbeatAccepted)
def heartbeat(request: Request, client: Client, body: HeartbeatInput):
    """Record authenticated liveness without creating an event or notification."""
    with request.app.state.database.transaction() as connection:
        # Recheck credentials under the write lock so rotation and revocation apply immediately.
        current = connection.execute(
            "SELECT enabled, token_hash FROM clients WHERE id = ?", (client["id"],)
        ).fetchone()
        if (
            current is None
            or not current["enabled"]
            or current["token_hash"] != client["token_hash"]
        ):
            raise HTTPException(401, "Invalid client credentials")

        received_at = utc_now()
        connection.execute(
            """UPDATE clients SET last_seen_at = ?, last_heartbeat_at = ?, last_health_json = ?
            WHERE id = ?""",
            (received_at, received_at, canonical_json(body.health), client["id"]),
        )
    return {"status": "accepted", "received_at": received_at}


def client_summaries(database) -> list[dict]:
    """Return safe client fields, deriving heartbeat age separately from event activity."""
    with database.connect() as connection:
        rows = connection.execute(
            """SELECT clients.*, (SELECT MAX(received_at) FROM events
                WHERE events.client_id = clients.id) AS last_event_at
            FROM clients ORDER BY name, id"""
        ).fetchall()

    now = datetime.now(UTC)
    result = []
    for row in rows:
        status = "never_seen"
        if not row["enabled"]:
            status = "disabled"
        elif row["last_heartbeat_at"]:
            age = (now - datetime.fromisoformat(row["last_heartbeat_at"])).total_seconds()
            status = "healthy" if age < row["stale_timeout_seconds"] else "stale"

        result.append(
            {
                key: row[key]
                for key in (
                    "id",
                    "name",
                    "stale_timeout_seconds",
                    "last_seen_at",
                    "last_heartbeat_at",
                    "last_event_at",
                )
            }
            | {
                "enabled": bool(row["enabled"]),
                "allowed_prefixes": json.loads(row["allowed_prefixes_json"]),
                "health": json.loads(row["last_health_json"] or "{}"),
                "status": status,
            }
        )
    return result


@router.get(
    "/api/v1/clients", response_model=list[ClientView], dependencies=[Depends(require_human)]
)
def clients(request: Request):
    """Expose registered client health to human sessions without credential hashes."""
    return client_summaries(request.app.state.database)
