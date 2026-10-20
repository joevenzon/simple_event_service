"""Bound HTTP response sizes and expose errors without private URLs or response bodies."""

import httpx


class HTTPFailure(Exception):
    """Describe a safe network outcome for local retry decisions."""

    def __init__(self, status: int | None = None, retry_after: str | None = None):
        super().__init__(f"HTTP {status}" if status else "Network or invalid response")
        self.status, self.retry_after = status, retry_after


def request_bytes(client, method, url, *, limit=65536, statuses=None, **kwargs) -> bytes:
    """Read a bounded response, refusing redirects and keeping sockets time limited."""
    try:
        with client.stream(method, url, timeout=5, follow_redirects=False, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise HTTPFailure(response.status_code, response.headers.get("Retry-After"))
            if statuses is not None and response.status_code not in statuses:
                raise HTTPFailure()
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=16384):
                body.extend(chunk)
                if len(body) > limit:
                    raise HTTPFailure()
            return bytes(body)
    except httpx.RequestError:
        raise HTTPFailure() from None
