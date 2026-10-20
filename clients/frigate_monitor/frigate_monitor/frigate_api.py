"""Read authenticated review thumbnails, with associated person snapshots as a fallback."""

import io
import json
import logging
import re
import warnings

from PIL import Image, UnidentifiedImageError

from frigate_monitor.config import secret
from frigate_monitor.http import HTTPFailure, request_bytes

logger = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 5 * 1024 * 1024


def jpeg_image(data: bytes) -> bytes:
    """Decode a bounded JPEG/PNG/WebP and freeze a small JPEG without source metadata."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as original:
                if original.format not in ("JPEG", "PNG", "WEBP"):
                    raise ValueError("Unsupported image")
                if original.width * original.height > 20_000_000:
                    raise ValueError("Oversized image")
                original.load()
                image = original.convert("RGB")
                image.thumbnail((1920, 1080))
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=85)
                return output.getvalue()
    except (
        OSError,
        ValueError,
        UnidentifiedImageError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        raise HTTPFailure() from None


class FrigateAPI:
    """Use only reads and optional credential login; no Frigate configuration writes."""

    def __init__(self, config, client):
        self.config, self.client = config, client
        self.base = str(config.api_base_url).rstrip("/")
        self.connected = False
        self.version = None

    def get(self, path, *, limit=65536):
        """Retry an expired Frigate cookie once using the documented JSON login."""
        try:
            try:
                data = request_bytes(self.client, "GET", self.base + path, limit=limit)
            except HTTPFailure as error:
                if error.status != 401 or self.config.api_auth != "login":
                    raise
                request_bytes(
                    self.client,
                    "POST",
                    self.base + "/api/login",
                    json={
                        "user": secret(self.config.username_env),
                        "password": secret(self.config.password_env),
                    },
                )
                data = request_bytes(self.client, "GET", self.base + path, limit=limit)
        except HTTPFailure:
            self.connected = False
            raise
        self.connected = True
        return data

    def probe(self):
        """Refresh API connectivity even when there have been no camera alerts."""
        try:
            value = self.get("/api/version", limit=256).decode().strip().strip('"')
            if not re.fullmatch(r"[A-Za-z0-9_.+-]{1,128}", value):
                raise HTTPFailure()
            self.version = value
        except (HTTPFailure, UnicodeError):
            self.connected = False

    def snapshot(self, event: dict) -> bytes | None:
        """Prefer the review thumbnail; resolve object IDs before using the snapshot API."""
        review_id = event["metadata"]["review_id"]
        reason = "No usable thumbnail or associated person snapshot"
        try:
            review = json.loads(self.get(f"/api/review/{review_id}"))
            if (
                not isinstance(review, dict)
                or review.get("id") != review_id
                or review.get("camera") != event["metadata"]["camera"]
            ):
                raise HTTPFailure()

            # Frigate serves review.thumb_path under its authenticated /clips/ location.
            thumb = review.get("thumb_path", "")
            if isinstance(thumb, str) and re.fullmatch(
                r"/media/frigate/clips/review/thumb-[A-Za-z0-9_.-]+\.webp", thumb
            ):
                try:
                    return jpeg_image(
                        self.get(thumb.removeprefix("/media/frigate"), limit=MAX_IMAGE_BYTES)
                    )
                except HTTPFailure as error:
                    reason = str(error)

            # Reviews group multiple objects; never use the review ID as an object event ID.
            data = review.get("data")
            detections = data.get("detections", []) if isinstance(data, dict) else []
            if not isinstance(detections, list):
                raise HTTPFailure()
            for detection in detections[:5]:
                if not isinstance(detection, str) or not re.fullmatch(
                    r"[A-Za-z0-9][A-Za-z0-9_.-]{0,159}", detection
                ):
                    continue
                try:
                    tracked = json.loads(self.get(f"/api/events/{detection}"))
                    if (
                        isinstance(tracked, dict)
                        and tracked.get("label") == "person"
                        and tracked.get("camera") == event["metadata"]["camera"]
                    ):
                        return jpeg_image(
                            self.get(f"/api/events/{detection}/snapshot.jpg", limit=MAX_IMAGE_BYTES)
                        )
                except (HTTPFailure, ValueError) as error:
                    reason = (
                        str(error) if isinstance(error, HTTPFailure) else "Invalid object response"
                    )
                    continue
        except (HTTPFailure, ValueError) as error:
            reason = str(error) if isinstance(error, HTTPFailure) else "Invalid review response"

        logger.warning("Frigate snapshot unavailable review_id=%s reason=%s", review_id, reason)
        return None
