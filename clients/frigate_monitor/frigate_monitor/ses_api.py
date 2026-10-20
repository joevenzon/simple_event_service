"""Upload frozen producer payloads and heartbeats without depending on the SES host package."""

import json
import math
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from uuid import UUID

from frigate_monitor.http import HTTPFailure, request_bytes


@dataclass(frozen=True)
class UploadResult:
    accepted_id: str | None = None
    permanent: bool = False
    error: str | None = None
    delay: float = 0


def retry_delay(attempts, retry_after=None, now=None):
    """Use capped jittered backoff and honor bounded Retry-After dates or seconds."""
    delay = random.uniform(0.5, 1) * min(3600, 5 * 2 ** min(max(0, attempts - 1), 10))
    if retry_after:
        try:
            seconds = float(retry_after)
        except ValueError:
            try:
                seconds = (
                    parsedate_to_datetime(retry_after) - (now or datetime.now(UTC))
                ).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 0
        if math.isfinite(seconds) and 0 <= seconds <= 365 * 86400:
            delay = max(delay, seconds)
    return delay


class SESAPI:
    """Send client credentials only to the explicitly configured SES origin."""

    def __init__(self, config, token, client):
        self.base = str(config.host_url).rstrip("/")
        self.headers = {"Authorization": "Bearer " + token}
        self.client = client

    def upload(self, row) -> UploadResult:
        """Accept only the documented stored-event receipt; keep all ambiguous outcomes queued."""
        try:
            kwargs = {"content": row["event_json"].encode("utf-8")}
            headers = self.headers | {"Content-Type": "application/json"}
            if row["image"] is not None:
                kwargs = {
                    "data": {"event": row["event_json"]},
                    "files": {"image": ("snapshot.jpg", row["image"], "image/jpeg")},
                }
                headers = self.headers
            body = request_bytes(
                self.client,
                "POST",
                self.base + "/api/v1/events",
                headers=headers,
                statuses=(200, 201),
                **kwargs,
            )
            data = json.loads(body)
            if (
                not isinstance(data, dict)
                or data.get("status") != "accepted"
                or type(data.get("duplicate")) is not bool
                or not isinstance(data.get("id"), str)
            ):
                raise ValueError("Invalid receipt")
            UUID(data["id"])
            return UploadResult(accepted_id=data["id"])
        except (ValueError, UnicodeError):
            error = HTTPFailure()
        except HTTPFailure as failure:
            error = failure

        permanent = (
            error.status is not None
            and error.status not in (408, 429)
            and not (500 <= error.status < 600)
        )
        return UploadResult(
            permanent=permanent,
            error=str(error),
            delay=retry_delay(row["attempts"] + 1, error.retry_after),
        )

    def heartbeat(self, health: dict) -> bool:
        """Report liveness separately from archived events; retry on the next heartbeat tick."""
        try:
            request_bytes(
                self.client,
                "POST",
                self.base + "/api/v1/heartbeat",
                headers=self.headers,
                json={"health": health},
            )
            return True
        except HTTPFailure:
            return False
