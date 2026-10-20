# Architecture

The generic host is an installable Python package in `host/`; no Frigate code is imported
or required by the host. The root uv workspace provides a shared dependency lockfile
and lint/test configuration.

The host provides:

- Environment settings with separate development and production modes.
- FastAPI startup initialization, liveness, and database write-readiness checks.
- SQLite WAL, foreign keys, a five-second busy timeout, and explicit `BEGIN IMMEDIATE`
  write transactions. Connections are short lived and closed after use.
- Versioned migrations applied inside one transaction, including schema history.
  Failed DDL rolls back; unknown/newer migration histories reject startup. Existing
  migrations must remain unchanged; append a version for each schema change.
- Tables for clients, events, images, notification rules/deliveries, users, and sessions.
- Operator provisioning with Argon2id password hashes, 256-bit random client tokens,
  SHA-256 token digests, and channel scope validation. Tokens are displayed only after
  a successful commit. Rotation re-enables a revoked client and replaces its token.
- A non-root host image and a persistent named volume, behind Caddy. `/readyz` is
  blocked at Caddy; only `/healthz` is publicly reachable as a health endpoint.

Compose runs the host, a separately supervised notification worker, and Caddy. Host and
worker share SQLite and private files; only Caddy publishes ports. The worker initializes
the schema independently so startup does not depend on host readiness. `/readyz` checks
database writes and, when a topic is configured, a worker heartbeat within 120 seconds.
Readiness does not require ntfy availability; outages remain queued for retry.
The worker runs retention cleanup at startup and hourly, including with an empty ntfy
topic. `EVENT_RETENTION_DAYS` defaults to seven days; expiry uses host `received_at`,
independent of the producer's clock. Events strictly older than the cutoff are deleted
in batches of 100, cascading to every delivery state. Unreferenced image metadata is
then deleted, and both private file trees are reconciled against current image references.
This removes expired originals/previews, abandoned uploads, orphan files, and empty date
directories without following directory symlinks. Filesystem pruning, ingest writes,
and thumbnail generation share SQLite's writer lock to protect in-progress writes.

Database deletions commit before file reconciliation. A crash or failed unlink leaves
an orphan that a later pass can retry without deleting files for retained events.
The worker advances one batch between delivery attempts, preserving notification and
heartbeat progress. Delivery claims skip expired history even while cleanup is in progress.
Storage failures retry after a minute; cleanup totals and generic
errors are logged without event contents or private filenames. An immediate pass is
available through `python -m app.cleanup` using the same settings and storage.

The independently packaged `clients/frigate_monitor/` producer runs through its own
home Compose configuration. Host and client images install only their own workspace package.
The monitor has no host imports: it implements the documented JSON/multipart producer
and heartbeat contracts over HTTP. See [monitor operations](frigate-monitor.md).

MQTT callbacks validate review messages and durably insert the first qualifying person
alert before acknowledging it. A separate loop resolves a review thumbnail or associated
person snapshot, freezes its JSON and JPEG in SQLite, and uploads with a stable review
event key. Transient failures remain queued across restarts; permanent HTTP errors keep
failed payloads for explicit requeue. Successful jobs retain compact deduplication
receipts. A local process lock serializes delivery. Broker persistent sessions and source
QoS determine whether messages emitted while disconnected can be replayed.

The monitor periodically reports MQTT subscription status, API connectivity/version,
outbox counts, and upload time through the heartbeat API. It changes no NVR
settings and publishes no MQTT commands. Deployment verification includes manual checks
of version/camera compatibility and physical camera/phone behavior.

Event ingestion uses strict validation, scoped bearer-token authentication, JSON and
multipart requests, immutable records, content digests and private sanitized JPEG storage.
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
see [the API guide](api.md).

Server-rendered Jinja2 screens use the same session checks at `/`, `/events`,
`/events/{id}`, and `/clients`. Signed-out page requests redirect to login and return to
a validated internal destination after authentication. Signed-out JSON/image requests return
401. Event text, client names, and metadata are escaped; logout uses session-bound CSRF
protection. Public static routes contain only application CSS and time-formatting JavaScript.
The screens work without JavaScript; an optional script shows local times and relative ages.
Feed filters use inclusive UTC calendar dates converted to the query API’s half-open range.

Thumbnails are generated on first authenticated access from normalized private images,
bounded to 640×480, and atomically cached under `/data/thumbnails` with private permissions.
Images do not need a precomputed preview or a database backfill. Both
originals and previews require a current human session on every request, even after the
preview exists, and use private/no-store response headers.

`POST /api/v1/heartbeat` authenticates a client token and records host receipt time and a
bounded health snapshot without creating an event or delivery. Heartbeats share the
per-client ingest throttle and recheck revocation/rotation under the writer lock.
`GET /api/v1/clients` exposes only safe fields to human sessions. Healthy/stale states
reflect explicit heartbeat age using each client’s `stale_timeout_seconds` (default 180);
events update last activity separately and do not make a missing heartbeat healthy.
Never-seen clients have no heartbeat, and disabled clients always display disabled.

Channel matching uses equality or `prefix + '.'`, so `backup.status` matches
`backup.status.watchdog` but excludes `backup.statuses`. Empty scope lists mean unrestricted
publishing; explicit scopes must be valid channels. `*` is reserved for notification rule
configuration, not client scopes or stored event channels.

The host evaluates notification policy inside the event's write transaction. The most
specific matching prefix wins, followed by descending `rule_priority`, ascending
`sort_order`, and ascending rule ID (insertion order). Disabled rules suppress matching
events; a failed minimum-severity check does not fall back to a broader rule. No match
means no notification. `notify=false` opts out; `notify=true` cannot override policy.

Selected events create one delivery row in the same commit. Priority is frozen at ingest:
debug/info/warning/error/critical map to 1/3/4/5/5 unless the rule supplies an override.
A persisted cooldown per `(client_id, channel)` reserves a slot when enqueued, including
during outages. Suppressed events remain in history with `suppressed_cooldown`; they are
never sent later. Rules are managed through the CLI and affect future events only.

The worker atomically claims a due job for 120 seconds and increments its attempt count
before releasing SQLite's writer lock for HTTP. Claim tokens prevent an old worker from
overwriting a newer claim. Crashed attempts become eligible after the lease; an interrupted
final attempt becomes failed. Network failures, HTTP 429 and 5xx retry with exponential
jitter and `Retry-After`; other HTTP errors fail immediately. The default budget is eight
attempts. Operators can explicitly requeue failed jobs.

Publishing uses the [ntfy JSON API](https://docs.ntfy.sh/publish/#publish-as-json) at the
configured HTTPS origin. Only title, bounded UTF-8 message with channel, a severity tag,
priority, topic, and authenticated event-page click URL are sent. No image, thumbnail,
client metadata, client tags, session credentials, or producer credentials are included.
Redirects are refused, socket operations time out after ten seconds, and response bodies
are bounded. Stored errors contain only HTTP status or a generic network error.

`sent_to_ntfy` records relay acceptance, not handset receipt. A crash after relay acceptance
but before the SQLite result commit may duplicate a push: delivery is at least once.
Event ingestion remains idempotent. Private query responses and event details expose
attempt count, next retry, sent time, and sanitized errors; internal claim tokens stay private.
An empty topic disables the publisher while preserving already queued jobs.

The lifecycle initialization uses FastAPI's
[lifespan API](https://fastapi.tiangolo.com/advanced/events/), settings use
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/), and
Caddy blocks internal health requests with its
[respond directive](https://caddyserver.com/docs/caddyfile/directives/respond).
