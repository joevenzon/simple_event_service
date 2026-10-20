# API v1

The service provides generic ingestion, browser authentication, private history, status
pages, client heartbeats, and asynchronous host-approved ntfy delivery.
All production requests require HTTPS. Client bearer tokens authorize event/heartbeat submissions only;
history and images require a separate human session cookie.

| Endpoint | Authentication | Behavior |
| --- | --- | --- |
| `GET /healthz` | None | Process liveness; `200 {"status":"ok"}`. |
| `GET /readyz` | Internal | Database write probe and worker heartbeat when a topic is configured; 503 on failure. Caddy blocks this route. |
| `GET /login` | None | Browser form with signed, ten-minute CSRF cookie and hidden token. |
| `POST /login` | Login CSRF token | URL-encoded `username`, `password`, `csrf_token`; sets session cookie and redirects to `/` or a validated internal `next` destination. |
| `GET /` | Human session | Dashboard, severity counts, recent events, and heartbeat summary. |
| `GET /events` | Human session | Filtered, paginated HTML event feed with thumbnails. |
| `GET /events/{id}` | Human session | Event details and full private snapshot. |
| `GET /clients` | Human session | Client heartbeat status and latest health reports. |
| `GET /account` | Human session | Account, dashboard/API links, and logout form. |
| `GET /api/v1/session` | Human session | Username and session-bound `csrf_token`. |
| `POST /logout` | Human session + CSRF | Revokes current session; clears cookie; redirects to `/login`. |
| `POST /api/v1/events` | Client bearer | JSON or multipart ingestion. |
| `POST /api/v1/heartbeat` | Client bearer | Record liveness and optional health snapshot. |
| `GET /api/v1/clients` | Human session | List registered clients, scopes, and heartbeat health. |
| `GET /api/v1/events` | Human session | Filtered, cursor-paginated history. |
| `GET /api/v1/events/{id}` | Human session | Immutable event details. |
| `GET /api/v1/events/{id}/image` | Human session | Private normalized JPEG; 404 if absent. |
| `GET /api/v1/events/{id}/thumbnail` | Human session | Private JPEG preview, bounded to 640×480; 404 if no image. |
| `GET /api/v1/openapi.json` | Human session | OpenAPI contract, including JSON/multipart request schemas and authentication. |

Interactive OpenAPI documentation remains disabled. Notification selection and durable
enqueue happen in the event transaction; HTTP acceptance never waits for ntfy.
Signed-out HTML screen requests redirect to login, while private
API/image/thumbnail requests return 401. Client bearer tokens do not authorize those reads.

## Submit events

`Authorization: Bearer <token>` is mandatory even if a browser session is present.
Provision a client with `ses create-client` and appropriate `--allow-prefix` arguments.
An empty scope list permits any valid channel. Prefix checks respect segment boundaries.

Required JSON fields: `event_key` (1–200 characters), `channel`, `severity`, `title`
(1–200 characters, nonblank), and `message` (up to 4,000 characters, may be empty).
Keys and titles cannot contain ASCII control characters. Severity is one of `debug`,
`info`, `warning`, `error`, `critical`. Channels contain 1–12 dot-separated segments,
each matching `[a-z][a-z0-9_-]{0,39}`, with a maximum total length of 255 characters.
Empty segments and wildcards are invalid. For example, `frigate.front.person` is valid.

Optional fields:

- `occurred_at`: ISO-8601 timestamp with timezone, normalized to UTC; naive dates rejected.
- `metadata`: JSON object, at most 16 KiB when compactly serialized as UTF-8; no NaN/infinity.
- `tags`: at most 32 strings matching `[a-z][a-z0-9_-]{0,39}`.
- `notify`: boolean hint; false opts out, while true cannot override host notification policy.

Unknown fields and implicit type conversions are rejected. JSON bodies are capped at
64 KiB. Multipart uses exactly one text field named `event` containing the same JSON,
and one file named `image`. The event field is capped at 64 KiB; the total multipart
body at the configured image limit plus 80 KiB of overhead. Limits count streamed bytes,
including requests without Content-Length.

Snapshots may be JPEG, PNG or static WebP. Actual decoded content determines acceptance;
filenames and declared MIME types are not trusted. Source and normalized images are
capped at 5 MiB and decoded images at 20 million pixels (configurable downward).
Images are oriented, flattened against white, stripped of source metadata, and saved as
JPEGs under host-generated names. Animated and unsupported formats are rejected.

Idempotency is scoped to `(client_id, event_key)`:

- New event: `201 {"id":"…","status":"accepted","duplicate":false}`.
- Identical retry: `200` with the same ID and `duplicate:true`; no extra image or event.
- Changed payload under the same key: `409`, without exposing the existing content.

Digests use normalized JSON with sorted object keys, explicit defaults and UTC timestamps,
plus the SHA-256 of original uploaded image bytes. JSON whitespace, key order, attachment
filenames and MIME headers do not affect identity. Changing image bytes does affect it,
even if the images render identically. Keep the original attachment for retries.

Other errors: 401 invalid/disabled credentials, 403 forbidden channel, 413 oversized
request/image, 415 unsupported request content type, 422 invalid event/image, 429 rate
limit with `Retry-After`, 503 unavailable storage. Malformed multipart/form encoding
returns 400. Clients should retry timeouts/429/5xx with the same key and payload.

Example commands below use a POSIX shell. In PowerShell use `curl.exe`, and put each
command on one line instead of using backslash continuation. Set `SES_BASE_URL` and
`SES_CLIENT_TOKEN` privately. Save the following as `event.json`:

```json
{
  "event_key": "backup-free-space-001",
  "channel": "backup.status.freespace",
  "severity": "info",
  "title": "Free space",
  "message": "Backup disk has sufficient space."
}
```

```sh
curl --fail-with-body -H "Authorization: Bearer $SES_CLIENT_TOKEN" \
  -H 'Content-Type: application/json' --data-binary @event.json "$SES_BASE_URL/api/v1/events"
```

For an image event, change the key and channel (for example, `frigate.front.person`)
and save as `image-event.json`. Use a token allowed to publish to that subtree:

```sh
curl --fail-with-body -H "Authorization: Bearer $SES_CLIENT_TOKEN" \
  -F 'event=<image-event.json' -F 'image=@snapshot.jpg' "$SES_BASE_URL/api/v1/events"
```

## Human sessions

Open `/login` in a browser and use the admin provisioned by `ses create-admin`.
The session cookie is HttpOnly, Secure in production, SameSite=Lax, and valid for
24 hours by default. Session tokens are random and stored only as SHA-256 digests.
Each request checks expiry, revocation and user disablement. Logging in replaces the
session presented by that browser; other devices remain signed in. Logout revokes
the current session. `ses revoke-sessions <username>` revokes all of a user's sessions.

The login POST requires the signed token from `/login` both as its cookie and form
field, preventing login CSRF. Logout requires `csrf_token` in a form or `X-CSRF-Token`
header; obtain it from `/account` or `GET /api/v1/session`. Tokens are bound to the
current session. Mutating browser actions use POST; GET logout is not supported.

For curl login, save cookies and the form first:

```sh
curl -c cookies.txt "$SES_BASE_URL/login" -o login.html
# Read the hidden csrf_token value from login.html and set LOGIN_CSRF privately.
# /private/admin-password contains the exact password, without a trailing newline.
curl -b cookies.txt -c cookies.txt --data-urlencode 'username=admin' \
  --data-urlencode 'password@/private/admin-password' \
  --data-urlencode "csrf_token=$LOGIN_CSRF" "$SES_BASE_URL/login"
curl -b cookies.txt "$SES_BASE_URL/api/v1/events?channel=backup.status"
```

Protect cookie/password files and remove them after use. Never put credentials in URLs.
Development cookies permit loopback HTTP; development mode must remain local.

## Query events

`GET /api/v1/events` returns `{"items":[…],"next_cursor":"…"}`. Each item includes the
event fields, host ID, client ID, UTC received time, image ID/private relative image URL
(or null), and notification state. Digests, credentials and disk paths are never returned.

`notification_state` is `not_selected`, `pending`, `sent_to_ntfy`, `failed`, or
`suppressed_cooldown`. A nullable `notification_delivery` object supplies `attempts`,
`last_attempt_at`, `next_attempt_at` (pending only), `sent_at`, and sanitized `last_error`.
Delivery fields may change as the worker runs; the submitted event remains immutable.
Relay acceptance does not establish iPhone receipt. Identical event retries do not create
another job. A worker crash after relay acceptance can duplicate a push on recovery.

| Parameter | Semantics |
| --- | --- |
| `channel` | Valid channel; matches the exact channel and descendants by default. |
| `subtree` | Default true; false selects the exact channel only. |
| `severity` | Exact severity. |
| `client_id` | Exact client ID. |
| `received_from` | Inclusive lower bound on host receipt time; timezone required. |
| `received_before` | Exclusive upper bound; timezone required, later than lower bound. |
| `search` | Literal substring in title/message; ASCII case-insensitive, at most 200 characters. |
| `limit` | Default 50; 1–100. |
| `cursor` | Opaque next-page cursor; reuse with the same filters. |

Ordering is `(received_at DESC, id DESC)`. Cursors handle tied timestamps without
repeating entries; new events appear on a fresh first page. History and image responses
use `Cache-Control: private, no-store` and `X-Content-Type-Options: nosniff`.
Image directories have no static or anonymous route.

## Client heartbeats and health

`POST /api/v1/heartbeat` requires a client bearer token. Send a JSON object containing an
optional `health` object; unknown top-level fields and nonfinite numbers are rejected.
Each successful call replaces the previous health snapshot (default `{}`), sets
`last_heartbeat_at` and `last_seen_at` to host receipt time, and returns
`200 {"status":"accepted","received_at":"…Z"}`. It creates no event or notification.

```sh
curl --fail-with-body -H "Authorization: Bearer $SES_CLIENT_TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"health":{"mqtt_connected":true,"queue_size":0}}' "$SES_BASE_URL/api/v1/heartbeat"
```

The health object is capped at 8 KiB in compact UTF-8 JSON; the entire streamed request
is capped at 16 KiB. Health need not contain Frigate-specific fields. Heartbeats share
the event ingestion per-client rate limit and return 429 with `Retry-After` when full.
Malformed bodies return 422; oversized requests return 413. Revoked/rotated/disabled client
credentials return 401 immediately, including a recheck inside the write transaction.

`GET /api/v1/clients` requires a human session and returns an array containing `id`, `name`,
`enabled`, `allowed_prefixes`, `stale_timeout_seconds`, `last_seen_at`, `last_heartbeat_at`,
`last_event_at`, `health`, and `status`. Credential hashes are excluded. An empty scope list
means all valid channels. Status is `disabled` for disabled clients, `never_seen` before
the first heartbeat, `healthy` when the heartbeat age is less than the client’s timeout
(default 180 seconds), and `stale` thereafter. An event submission updates last activity
and last event time but does not substitute for an explicit heartbeat.

## Browser filters and images

The HTML feed at `/events` accepts `channel`, `subtree`, `client_id`, `severity`, `search`,
`limit`, and `cursor` with the API’s semantics. Browser date controls use `date_from` and
`date_through` as inclusive UTC calendar dates; these map to `received_from` at midnight
and `received_before` at midnight after the through date. Empty controls mean no filter.
The page shows escaped validation errors and returns 422 for invalid filters. Pagination
retains all filters; submitting the filter form starts at the newest matching event.
Channel breadcrumb links select the corresponding subtree.

Image events use `/api/v1/events/{id}/thumbnail` in the feed and the full private image
endpoint on the detail page. Previews are generated from sanitized originals on first
authenticated access and cached privately on disk. Every subsequent request still checks
the human session, including expiry, revocation and account disablement. Both full images
and previews use `Cache-Control: private, no-store`, inline JPEG content disposition, and
`X-Content-Type-Options: nosniff`. Source files and cached previews have no static routes.

HTML renders UTC timestamps without JavaScript. The optional script formats them in the
browser’s local timezone with relative ages; filtering continues to use the labeled UTC
dates. No JavaScript is required for authentication, filtering, pagination, or image views.
