# Architecture and implementation status

The [implementation plan](../simple_event_service_plan.md) remains the full specification.
The generic host is an installable Python package in `host/`; no Frigate code is imported
or required by the host. The root uv workspace provides a shared dependency lockfile
and lint/test configuration.

Phase 0 provides:

- Environment settings with separate development and production modes.
- FastAPI startup initialization, liveness, and database write-readiness checks.
- SQLite WAL, foreign keys, a five-second busy timeout, and explicit `BEGIN IMMEDIATE`
  write transactions. Connections are short lived and closed after use.
- Versioned migrations applied inside one transaction, including schema history.
  Failed DDL rolls back; unknown/newer migration histories reject startup. Existing
  migrations must remain unchanged; add a new version for future schema changes.
- Tables for clients, events, images, notification rules/deliveries, users, and sessions.
  Ingestion and session APIs use these tables in Phase 1; the worker arrives in Phase 3.
- Operator provisioning with Argon2id password hashes, 256-bit random client tokens,
  SHA-256 token digests, and channel scope validation. Tokens are displayed only after
  a successful commit. Rotation re-enables a revoked client and replaces its token.
- A non-root host image and a persistent named volume, behind Caddy. `/readyz` is
  blocked at Caddy; only `/healthz` is publicly reachable as a health endpoint.

The Compose scaffold deliberately contains only the host and Caddy at this stage.
A separately supervised worker and worker readiness checks will be added with durable
notification processing in Phase 3. The current readiness check covers the database only.
The configured seven-day retention is reserved for Phase 5; no cleanup job exists yet.
Similarly, `NTFY_*` settings are reserved for Phase 3 and no external requests are sent.

Phase 1 adds strict event validation, scoped bearer-token authentication, JSON and
multipart ingest, immutable records, content digests and private sanitized JPEG storage.
A serialized write transaction decides duplicate/conflict behavior and inserts events
and image metadata together. Identical concurrent retries create one event/image.
JSON object key order and equivalent UTC timestamps normalize before hashing; attachment
identity uses the original uploaded bytes. Ordinary database failures remove image files.

Machine and human authentication remain separate. Query APIs and image endpoints require
human sessions; client tokens grant ingestion only. Login uses Argon2id, signed login
CSRF tokens and random, digest-only sessions in SQLite. Logout revokes sessions and checks
a session-bound CSRF token. The CLI can revoke all sessions for a user. Cookies are Secure
in production, HttpOnly and SameSite=Lax. Durable SQLite throttles limit login attempts
per IP/account and ingest attempts per client. Streamed request limits precede parsing.
Private image directories are never mounted as static content.

Queries use boundary-aware channel equality/ranges and cursor pagination by receipt time
and ID. A session-protected OpenAPI document describes the producer/query contracts;
see [the API guide](api.md). The minimal login/account forms are the only HTML screens in
Phase 1. Event dashboard rendering and heartbeats remain in Phase 2.

Channel matching uses equality or `prefix + '.'`, so `backup.status` matches
`backup.status.watchdog` but excludes `backup.statuses`. Empty scope lists mean unrestricted
publishing; explicit scopes must be valid channels. `*` is reserved for notification rule
configuration, not client scopes or stored event channels.

The lifecycle initialization uses FastAPI's
[lifespan API](https://fastapi.tiangolo.com/advanced/events/), settings use
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/), and
Caddy blocks internal health requests with its
[respond directive](https://caddyserver.com/docs/caddyfile/directives/respond).
