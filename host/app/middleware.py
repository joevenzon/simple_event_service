"""Bound bytes as they arrive, including chunked requests, before parsers buffer them."""

from fastapi import HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse


class RequestGuards:
    """Enforce HTTPS, streaming byte caps and private response headers for HTTP routes."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        settings = scope["app"].state.settings
        path = scope["path"]
        if (
            settings.environment == "production"
            and scope["scheme"] != "https"
            and path not in ("/healthz", "/readyz")
        ):
            return await JSONResponse({"detail": "HTTPS required"}, status_code=400)(
                scope, receive, send
            )
        headers = Headers(scope=scope)
        # Reject known oversized bodies before consuming bytes, then count actual bytes too.
        limit = 16 * 1024
        if path == "/api/v1/events" and scope["method"] == "POST":
            limit = (
                settings.max_image_bytes + 80 * 1024
                if headers.get("content-type", "").split(";", 1)[0].strip().lower()
                == "multipart/form-data"
                else 64 * 1024
            )
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > limit:
            return await JSONResponse({"detail": "Request body too large"}, status_code=413)(
                scope, receive, send
            )
        total = 0

        async def bounded_receive():
            """Bound bytes delivered to downstream body parsers, including chunked uploads."""
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > limit:
                    raise HTTPException(413, "Request body too large")
            return message

        async def private_send(message):
            """Prevent browsers and proxies from retaining private API or form responses."""
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (b"cache-control", b"private, no-store"),
                        (b"x-content-type-options", b"nosniff"),
                    ]
                )
            await send(message)

        await self.app(scope, bounded_receive, private_send)
