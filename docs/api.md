# API v1

Phase 1 implements generic ingestion, browser authentication and private history.
All production requests require HTTPS. Client bearer tokens authorize ingestion only;
history and images require a separate human session cookie.

| Endpoint | Authentication | Behavior |
| --- | --- | --- |
| `GET /healthz` | None | Process liveness; `200 {"status":"ok"}`. |
| `GET /readyz` | Internal | Database write-and-rollback probe; 503 on failure. Caddy blocks this route. |
| `GET /login` | None | Browser form with signed, ten-minute CSRF cookie and hidden token. |
| `POST /login` | Login CSRF token | URL-encoded `username`, `password`, `csrf_token`; sets session cookie and redirects to `/account`. |
| `GET /account` | Human session | Minimal account and logout form; dashboard arrives in Phase 2. |
| `GET /api/v1/session` | Human session | Username and session-bound `csrf_token`. |
| `POST /logout` | Human session + CSRF | Revokes current session; clears cookie; redirects to `/login`. |
| `POST /api/v1/events` | Client bearer | JSON or multipart ingestion. |
| `GET /api/v1/events` | Human session | Filtered, cursor-paginated history. |
| `GET /api/v1/events/{id}` | Human session | Immutable event details. |
| `GET /api/v1/events/{id}/image` | Human session | Private normalized JPEG; 404 if absent. |
| `GET /api/v1/openapi.json` | Human session | OpenAPI contract, including JSON/multipart request schemas and authentication. |

Interactive OpenAPI documentation remains disabled. Heartbeats, client status, event
dashboard pages and notification delivery are not implemented yet. All Phase 1 events
have `notification_state: "not_selected"`; no notification jobs are created.

## Submit events

`Authorization: Bearer <token>` is mandatory even if a browser session is present.
Provision a client with `ses create-client` and appropriate `--allow-prefix` arguments.
An empty scope list permits any valid channel. Prefix checks respect segment boundaries.

Required JSON fields: `event_key` (1–200 characters), `channel`, `severity`, `title`
(1–200 characters, nonblank), and `message` (up to 4,000 characters, may be empty).
Keys and titles cannot contain ASCII control characters. Severity is one of `debug`,
`info`, `warning`, `error`, `critical`. Channels follow the plan's lowercase ASCII syntax.

Optional fields:

- `occurred_at`: ISO-8601 timestamp with timezone, normalized to UTC; naive dates rejected.
- `metadata`: JSON object, at most 16 KiB when compactly serialized as UTF-8; no NaN/infinity.
- `tags`: at most 32 strings matching `[a-z][a-z0-9_-]{0,39}`.
- `notify`: boolean hint; notification policy arrives in Phase 3.

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
