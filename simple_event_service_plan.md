# Simple Event Service (SES) — Implementation Plan

**Status:** Implementation specification / MVP plan  
**Version:** 0.2  
**Architecture:** General-purpose clients → VPS host → web status page and ntfy.sh  
**Chosen stack:** Python 3.12+, FastAPI, SQLite, Jinja2, Docker Compose, Caddy, public ntfy.sh  
**Default retention:** 7 days

## 1. Purpose and scope

Simple Event Service (SES) collects events from independently running **clients**, archives recent history on a central VPS **host**, displays a mobile-friendly status page, and forwards selected event notifications to an existing public **ntfy.sh** topic for delivery to iOS.

The first client is a **Frigate monitor**, running alongside a local Frigate NVR. It watches Frigate review events, filters person alerts, fetches a snapshot, and uploads the event to the host. Future clients may monitor backups, free disk space, watchdogs, jobs, or other systems.

### Goals

- No inbound ports opened on the home router: clients only initiate outbound HTTPS to the VPS.
- Simple, generic, versioned event API; clients never need Frigate-specific server logic.
- Hierarchical, dot-separated **channels**, with whole-subtree filtering and routing.
- Text events and optional attached JPEG/PNG/WebP snapshot.
- Per-client revocable bearer-token authentication.
- Durable events, deduplicated submissions, 7-day retention, and best-effort reliable delivery to ntfy.sh.
- Authenticated mobile-friendly status page for reviewing events and images when away from home.
- Simple Docker Compose deployment with Caddy providing HTTPS.
- Client health/last-seen status and basic host health checks.

### Non-goals (MVP)

- Streaming or proxying Frigate live video or recording clips.
- Native iOS app, proprietary APNs integration, or custom Web Push implementation.
- Multiple human users/teams, complex ACLs, or complex notification workflows.
- General media library, indefinite archive, or full observability platform.
- An SES MQTT broker on the VPS; clients submit over HTTPS.

## 2. Terminology

| Term | Meaning |
|---|---|
| **Host** | Central SES application deployed on VPS. Persists events, serves UI/API, publishes notifications. |
| **Client** | An independently running producer of SES events, authenticated with its own token. |
| **Frigate monitor** | A specific client on the home N95 Ubuntu machine; subscribes to local Frigate MQTT and submits events with snapshots. |
| **Event** | Immutable user-visible record with a channel, title, message, severity, timestamps, metadata, and optional image. |
| **Channel** | Hierarchical dot-separated classification/routing path, independent of the client identity. |
| **Notification rule** | Host policy deciding whether an event triggers ntfy delivery and at what priority. |
| **Delivery** | A host attempt to submit an event notification to ntfy.sh; distinct from actual iPhone receipt. |
| **Heartbeat** | Explicit client liveness report, separate from user-visible events. |

## 3. Architecture and data flow

```text
Home Ubuntu server                        Public cloud VPS                       iPhone
┌──────────────────────────┐              ┌────────────────────────────────┐
│ Frigate NVR              │              │ SES host                       │
│   └─ MQTT frigate/reviews│              │                                │
│              ↓           │ HTTPS POST   │ Caddy (TLS + routing)          │
│ Frigate monitor client   ├─────────────►│ FastAPI                        │
│ - filter/dedupe          │              │ - client API/token auth       │
│ - fetch snapshot         │              │ - input validation            │
│ - persistent outbox      │              │ - SQLite + private image files│
└──────────────────────────┘              │ - ntfy outbox/worker          ├──────► ntfy.sh → iOS app
                                          │ - HTML status dashboard       │
Other clients (backup, disk, ...) ────────►│ - retention/heartbeat jobs    │
                                          └───────────────┬────────────────┘
                                                          │ HTTPS authenticated
                                                          └──────────────────► mobile browser
```

**Trust boundaries:** Every producer authenticates with its own API token. The public status page/API requires separate *human* authentication. ntfy.sh is an external, publicly accessible relay; only data intentionally approved for public-topic exposure is sent to it.

**Delivery sequence:** client generates event → host validates and commits event/image + a pending notification job transactionally → HTTP 201 → background worker posts to ntfy.sh → record delivery outcome. A duplicate event returns the existing event without another notification.

## 4. Repository layout

```text
simple-event-service/
  README.md
  docs/
    architecture.md
    api.md
    operations.md
  host/
    Dockerfile
    pyproject.toml
    app/
      main.py
      config.py
      db.py
      models.py
      schemas.py
      auth.py
      channels.py
      ingest.py
      images.py
      notifications.py
      cleanup.py
      templates/
        base.html
        login.html
        events.html
        event_detail.html
        clients.html
      static/
        app.css
  clients/
    frigate_monitor/
      Dockerfile
      pyproject.toml
      config.example.yaml
      frigate_monitor/
        main.py
        mqtt_handler.py
        frigate_api.py
        outbox.py
        ses_api.py
  deploy/
    compose.yaml
    Caddyfile
    .env.example
  tests/
    test_channels.py
    test_ingest.py
    test_delivery.py
    test_retention.py
    test_frigate_monitor.py
```

Keep shared API shape documented in OpenAPI; do not create runtime coupling from the generic host to the Frigate client.

## 5. Hierarchical channels

### 5.1 Syntax

Use a **dot-separated, lowercase ASCII** convention: choose *lowercase ASCII only* for MVP, so comparisons are unambiguous and channel names are effectively case-insensitive by validation (reject uppercase). Examples:

```text
frigate.front.person
frigate.back.person
frigate.garage.person
frigate.front.motion
backup.status.freespace
backup.status.watchdog
backup.health
server.n95.disk.warning
```

Formal validation:

- 1–12 segments, each 1–40 characters; total length ≤ 255.
- Each segment must match `[a-z][a-z0-9_-]*`.
- No empty segments, leading/trailing dots, consecutive dots, whitespace, or wildcards in stored event channel names.
- Channel is event data, **not a filesystem path** or a URL path.
- Client identity is separate from channel, e.g. `client_id=frigate-home`, `channel=frigate.front.person`.

### 5.2 Prefix/subtree matching

The rule `backup.status` matches:

```text
backup.status
backup.status.freespace
backup.status.watchdog
backup.status.watchdog.restart
```

It **does not** match `backup.health` or `backup.statuses`. Matching formula:

```python
def channel_matches(prefix: str, channel: str) -> bool:
    return channel == prefix or channel.startswith(prefix + ".")
```

Represent all-depth selection as `*` in **rule configuration only** (not event channel names). Use exact/prefix semantics instead of a complex glob language for MVP. A UI filter for `frigate` shows the entire Frigate subtree. An exact-match option is useful for debugging; default is subtree.

### 5.3 Host notification routing

Rules are evaluated **most specific matching prefix first**; tie-break by explicit numeric priority and then file order. First matching rule wins. Default is **no push** unless a rule matches; every accepted event is still stored.

Example host configuration (conceptual YAML; may instead be seeded into SQLite):

```yaml
notification_rules:
  - channel_prefix: frigate
    min_severity: warning
    enabled: false           # suppress generic Frigate messages
  - channel_prefix: frigate.front.person
    min_severity: info
    enabled: true
    ntfy_priority: 4
  - channel_prefix: frigate.back.person
    min_severity: info
    enabled: true
    ntfy_priority: 4
  - channel_prefix: backup.status
    min_severity: warning
    enabled: true
    ntfy_priority: 3
  - channel_prefix: backup.health
    min_severity: error
    enabled: true
    ntfy_priority: 5
```

This implies more-specific rules must take precedence over `frigate` (even if the list is rearranged). A no-match rule can be added for `*` with `enabled: false` as a fallback. Validate severity and ntfy priority separately.

### 5.4 Channel governance

For MVP clients can publish any valid channel by default, but add an optional set of **allowed channel prefixes** to each client for least privilege, e.g. client `frigate-home` allowed `frigate`; `backup-agent` allowed `backup`. Enforce boundary-aware prefix matching. Return HTTP 403 for unauthorized channels. Do not confuse this with notification rules; publishing permission and notification preference are separate.

## 6. Event data contract

Events are **append-only/immutable after creation** for MVP. If a source has an ongoing condition, emit a new event with a separate ID rather than mutating event history (future: explicit incident/group tracking).

### 6.1 Required fields

- `event_key`: client-generated stable string or UUID, unique **within the client**; enables idempotent retries.
- `channel`: hierarchical path defined above.
- `severity`: `debug | info | warning | error | critical`.
- `title`: short display title, max 200 characters.
- `message`: plain-text body, max 4,000 characters.

### 6.2 Optional fields

- `occurred_at`: ISO-8601 UTC timestamp from client; host also sets authoritative `received_at`.
- `metadata`: JSON object, capped (e.g. 16 KB serialized), with flat or structured source-specific fields.
- `tags`: list of short lowercase strings, optional.
- `image`: single uploaded image for MVP; max 5 MiB after validating content. Host assigns its own filename and storage path.
- `notify`: optional boolean **hint**, not an override of host policy. Client cannot force public publishing unless a host rule allows it.

Host-generated:

- `id`: UUID/ULID for URL and storage reference.
- `client_id`, `received_at`, `image_id`, and `notification_state`.

Example:

```json
{
  "event_key": "frigate-review-1731082145.89123",
  "channel": "frigate.front.person",
  "severity": "warning",
  "title": "Person detected at front camera",
  "message": "Frigate observed a person near the front entrance.",
  "occurred_at": "2026-10-18T20:30:00Z",
  "tags": ["frigate", "person"],
  "metadata": {
    "camera": "wyze_front",
    "review_id": "1731082145.89123",
    "object_types": ["person"]
  }
}
```

When uploaded with a JPEG, place the JSON in a `event` *multipart field* (distinct from the JSON object's `metadata` property) and attach `image` as another part. Without an image, clients can POST the JSON body directly.

## 7. Host REST API (v1)

Base URL: `https://events.example.com` (replace with deployed hostname); prefix all JSON API routes with `/api/v1`.

| Method/path | Auth | Purpose |
|---|---|---|
| `POST /api/v1/events` | Client bearer token | Ingest event JSON or multipart event+image. |
| `POST /api/v1/heartbeat` | Client bearer token | Record last-seen time and optional health metadata. |
| `GET /api/v1/events` | Human session | Paginated filtering by channel subtree, client, severity, date, search. |
| `GET /api/v1/events/{id}` | Human session | Event details and delivery state. |
| `GET /api/v1/events/{id}/image` | Human session | Serve associated image, private and non-cacheable. |
| `GET /api/v1/clients` | Human session | Client statuses and last-seen timestamps. |
| `GET /healthz` | Internal or limited public | Process liveness. No secrets or private data. |
| `GET /readyz` | Internal | DB/worker readiness, suitable for Docker healthcheck. |

### Ingest behavior

- `Authorization: Bearer <client_token>` over HTTPS only.
- `Content-Type: application/json` when no image; `multipart/form-data` with fields `event` (JSON string) and `image` for images. Use **`event`** consistently as field name.
- Successful new event: `201 Created` with `{id, status: "accepted", duplicate: false}`.
- Repeated `(client_id, event_key)` with identical payload: `200 OK` with existing ID and `duplicate: true`, no duplicate notification.
- Same key but different payload: `409 Conflict`; log a diagnostic without revealing old content.
- Invalid schema/channel/image: `422`; invalid/absent token: `401`; forbidden channel: `403`; oversize upload: `413`; rate limit: `429` with `Retry-After`.
- Persist image using temporary file + atomic rename. Avoid orphaned images after DB failures. Limit request body before full buffering; verify decoded image and real MIME type, not just filename or header.
- **Persist first, notify later**. HTTP success must never require ntfy.sh availability.
- Clients retry transient failures (timeouts/429/5xx) with exponential backoff + jitter and reuse the same `event_key`.

### Query/filter behavior

`GET /api/v1/events?channel=backup.status&subtree=true&severity=warning&client_id=backup-agent&limit=50&cursor=...`

- Default subtree filtering, opt out with `subtree=false`.
- Severity filter is exact in MVP; optionally add minimum severity in v2.
- Cursor pagination ordered by `(received_at DESC, id DESC)`; hard cap page size (e.g. 100).
- API responses never expose filesystem paths or bearer secrets.

### Image endpoint

- Serve under authenticated `/api/v1/events/{id}/image`, with `Cache-Control: private, no-store`.
- Safe content disposition and `X-Content-Type-Options: nosniff`.
- Never expose `/data/images` through a static-files mount or an anonymous image URL.
- Thumbnails may be generated and stored on ingest (bounded dimensions/size) for an efficient event list.

## 8. Database/storage design

SQLite is adequate for low-rate personal monitoring. Put SQLite DB and images on **persistent Docker volumes/bind mounts**. Enable WAL and busy timeout; use one transactional writer discipline for ingestion and worker updates.

### Tables

```text
clients
  id PK, name UNIQUE, token_hash, allowed_prefixes_json,
  enabled, created_at, last_seen_at, last_heartbeat_at,
  last_health_json

events
  id PK, client_id FK, event_key, channel, severity,
  title, message, occurred_at, received_at, tags_json,
  metadata_json, image_id NULL, payload_digest,
  UNIQUE(client_id, event_key)

images
  id PK, disk_name UNIQUE, content_type, size_bytes,
  sha256, width, height, created_at

notification_rules
  id PK, channel_prefix, enabled, min_severity,
  ntfy_priority, sort_order

notification_deliveries
  id PK, event_id FK, provider, state,
  attempts, next_attempt_at, last_attempt_at,
  provider_response_id NULL, last_error NULL,
  created_at, sent_at NULL,
  UNIQUE(event_id, provider)

human_users
  id PK, username UNIQUE, password_hash, created_at, disabled

sessions
  id PK, user_id FK, secret_hash, expires_at, created_at
```

Recommended indexes: events `(received_at, id)`, `(client_id, received_at)`, `(channel, received_at)`; delivery jobs `(state, next_attempt_at)`. SQLite can use channel equality/ranges or a simple indexed prefix strategy; start with a straightforward query and test boundary correctness. Avoid naive `LIKE 'backup.status%'` without segment-boundary handling.

### Filesystem

```text
/data/
  ses.sqlite3
  images/YYYY/MM/DD/<generated-id>.jpg
  thumbnails/YYYY/MM/DD/<generated-id>.jpg
```

Store generated IDs only; never trust client-provided filenames. Strip EXIF metadata when re-encoding accepted images to normalized JPEG to reduce private metadata leakage. Set max pixel count to reject decompression bombs.

### Seven-day retention

- Scheduled cleanup job hourly or daily deletes expired events, images, thumbnails, and delivery rows (cutoff based on host `received_at`).
- Run cleanup in batches with DB/filesystem failure recovery; orphan cleanup scans periodically.
- Default `EVENT_RETENTION_DAYS=7`, configurable.
- Snapshot deletion follows event deletion; avoid deleting a file still referenced by an active event.
- Mark explicit operational events (e.g. host delivery errors) separately if needed, but do not accidentally violate seven-day privacy retention.
- Backups: daily SQLite online backup plus optional image backup, with a conscious backup-expiry policy so deleted snapshots are not retained indefinitely in VPS backups.

## 9. Client authentication and dashboard login

### Client tokens

- Generate cryptographically random ≥256-bit tokens, display once on creation.
- Store only a password-style hash or HMAC digest on the server, never raw tokens in DB or logs. For high-entropy machine tokens, keyed SHA-256/HMAC or SHA-256 digests with constant-time comparisons are appropriate.
- Different token per client; support revocation and rotation.
- Client can be limited to specific channel prefixes.
- Require HTTPS and implement per-token rate limiting and body-size limits.

### Human authentication

- Implement login/session authentication in **Phase 1**, before exposing any event query or image endpoint; Phase 2 reuses it for the status page.
- Separate admin user for web dashboard (initially one user).
- Password hash using Argon2id, or a well-supported secure password library.
- Secure `HttpOnly`, `Secure`, `SameSite=Lax` session cookies; CSRF protection on mutating browser actions; login throttling.
- Avoid putting API tokens or session credentials in URLs.
- Caddy is the only service publishing ports 80/443 publicly; FastAPI bound to an internal Docker network.

## 10. ntfy.sh integration

### MVP delivery

- Publish to an existing **public ntfy.sh** service, not a self-hosted ntfy instance.
- Configure `NTFY_TOPIC` to a **long, unguessable random value**, never `frigate`, an email address, or a hostname.
- Install official ntfy iOS app and subscribe to that exact topic.
- Publish only host-approved events, using plain text with a **click URL** to the corresponding authenticated SES detail page.
- Map severity to ntfy priorities conservatively (e.g. info→3, warning→4, error/critical→5), unless overridden by rule.
- Use ntfy's documented HTTP JSON publish API, e.g. POST JSON to `https://ntfy.sh/` containing `topic`, `title`, `message`, `priority`, `tags`, and `click`.
- The host's own hierarchical channels are **not** mapped one-to-one to ntfy topics in MVP. Use a single private-by-obscurity random ntfy topic and show channel in the text/title. Multiple topics may be added later.
- Restrict title/message content sent to ntfy to approved fields. Never include access tokens or raw client metadata.

Example payload (not containing a real topic):

```json
{
  "topic": "replace-with-long-random-topic",
  "title": "Person detected — front",
  "message": "frigate.front.person: Person detected at front entrance",
  "priority": 4,
  "tags": ["warning", "camera"],
  "click": "https://events.example.com/events/01JEXAMPLE"
}
```

### Privacy decision: images in notifications

**Default:** Do *not* send the camera JPEG to public ntfy.sh. Deliver title/message plus authenticated link to SES, where the full image is displayed. ntfy.sh public topics are readable by anyone who knows the topic and publishing an attachment makes the image accessible to ntfy's infrastructure and potentially via its attachment URL. An unguessable topic is obscurity, not an access-control mechanism.

**Opt-in follow-up:** If the owner explicitly accepts public-relay exposure, allow image publishing on a separate configuration flag `NTFY_SEND_IMAGES=true`, using ntfy's documented binary attachment upload. Do not put SES session tokens or long-lived image access keys in the attachment URL. Clearly label the privacy consequences. External URL attachments may require publicly fetchable images and are not suitable for SES private image endpoints.

This choice preserves text + image in the **authenticated SES mobile status page** from day one while prioritizing confidentiality. The ntfy iOS app's attachment/preview behavior should be verified on-device before promising embedded image previews.

### Reliability semantics

- A durable worker processes `pending` jobs asynchronously.
- Retry network timeouts, 429 and 5xx using exponential backoff, jitter, bounded retry count, and respect `Retry-After`.
- Distinguish `sent_to_ntfy` from `received_on_iphone`; SES cannot verify handset receipt via the ntfy HTTP publish response.
- Mark permanent failure after the retry limit and expose it on dashboard.
- If worker crashes after ntfy accepts but before commit, a retry may duplicate a push: delivery is **at least once**, not exactly once. The host still stores only one event.
- Use an explicit cooldown/rate cap per `(client_id, channel)` or configurable channel prefix to prevent notification storms, preserving all events in history even if push is suppressed.

Reference: https://docs.ntfy.sh/publish/

## 11. Server-rendered status page

FastAPI with Jinja2, minimal JavaScript/CSS, responsive on desktop and iPhone. No SPA build system.

### Routes/screens

- `/login`: session login.
- `/`: dashboard with recent events, count by severity, latest client heartbeats.
- `/events`: newest-first event feed with thumbnails and compact cards.
- `/events/{id}`: title/message, timestamp, channel breadcrumb, client, tags/metadata, full image, delivery status.
- `/clients`: registered clients, allowed prefixes, last heartbeat/last event, healthy/stale status.
- Optional `/channels`: inferred channel tree from recent events, collapsible and selectable to filter subtree.

### Core interactions

- Filter by channel subtree (`frigate`, `backup.status`), severity, client, time range.
- Quick path breadcrumbs: `backup` → `backup.status` → `backup.status.freespace` where each click filters its subtree.
- Recent alerts show preview image without expanding; tap to view larger image.
- Display relative time and local timezone, keeping UTC in API/database.
- Infinite scrolling unnecessary initially; simple paginated list is sufficient.
- Accessible layout, clear colors/labels and mobile tap targets.
- Separate UI rendering from event ingestion, and escape all untrusted text in templates.
- Status page must require login, including image endpoints and any JSON search API.

### Client status

Client heartbeat request every 60 seconds (configurable). Show `last_seen`, `last_heartbeat`, and `healthy/stale` computed from each client's configured stale timeout (default 3 minutes). **Host startup/worker state must not falsely report connected clients as healthy based solely on previous boot.** Heartbeat metadata can contain uptime, Frigate connectivity, last MQTT event, pending local outbox length, etc.; clamp fields/sizes.

## 12. Frigate monitor client

### Runtime

Deploy as a separate small Python Docker container on the **home Ubuntu N95** (same LAN as Frigate and Mosquitto). The Frigate monitor is just one client of SES and is never imported by host source code.

Dependencies: `paho-mqtt`, `httpx`, `pydantic` and a small local SQLite outbox, plus ordinary Python stdlib. Avoid needing to mount Frigate media or NAS storage.

### Frigate integration

1. Subscribe to Frigate's `frigate/reviews` MQTT topic, after enabling Frigate MQTT and configuring a local Mosquitto broker.
2. Parse messages by their documented event type (`new`, `update`, `end`), review IDs, camera, severity, objects, and timestamps; account for differences between Frigate releases.
3. Filter for person-containing alerts (`severity=alert`, `objects` includes `person`).
4. Ensure one SES event per Frigate review ID (or a deliberately configured policy on completion). Decide whether to send on first qualifying `new/update` for immediacy, or on `end` for finalized snapshots. **MVP default:** first qualifying alert, with a brief retry window for image availability.
5. Fetch the review's thumbnail/snapshot from Frigate's **documented authenticated API**. Review snapshot endpoints and event snapshot endpoints are distinct: do not assume a review ID is a tracked-object event ID. Implement endpoint choice according to the installed Frigate version; for a specific object snapshot, first resolve its associated event ID. Use the Frigate review thumbnail where available.
6. Compose a generic SES event, e.g. channel `frigate.front.person`, severity `warning`, title `Person detected — front`, and a JPEG attachment.
7. Place payload/image into persistent local outbox before upload; upload to SES with retry/reconnect. Remove from outbox only on confirmed accepted/duplicate response.
8. Periodically send SES heartbeat with MQTT and Frigate API connectivity, queue size, and last successful upload time.

### Camera → channel mapping

Configure explicit mapping (do not trust arbitrary camera identifiers as channel segments):

```yaml
frigate:
  mqtt_host: mosquitto
  mqtt_port: 1883
  api_base_url: http://frigate:5000
  camera_channels:
    wyze_front: frigate.front
    wyze_back: frigate.back
    wyze_garage: frigate.garage
ses:
  host_url: https://events.example.com
  client_id: frigate-home
  token_env: SES_CLIENT_TOKEN
  heartbeat_seconds: 60
  outbox_path: /data/outbox.sqlite3
alerts:
  only_labels: [person]
  channel_suffix: person
  initial_snapshot_wait_seconds: 2
  max_snapshot_retry_seconds: 15
```

Treat Frigate API URL and MQTT auth as deployment-specific. Avoid exposing Frigate's unauthenticated HTTP port externally; it can be reached via internal Docker network or loopback LAN binding. If image retrieval fails after retries, **still submit a text event**, indicating no image, rather than dropping the alert.

### Testing

- Simulate MQTT event payloads as unit fixtures (`new`, `update`, `end`), including duplicate and out-of-order delivery.
- Test alert with snapshot, missing image, expired review, transient MQTT disconnect, SES outage, 429, restarted client and replayed outbox.
- Compare emitted event IDs/fields against actual Frigate version's `/api` and MQTT data.

Reference: https://docs.frigate.video/integrations/mqtt/

## 13. Deployment: VPS with Docker Compose and Caddy

### Host containers

- `ses-host`: FastAPI / Uvicorn, app + UI + API; no directly published TCP port.
- `ses-worker`: same image, worker command; reads DB notification outbox and runs retention cleanup. One worker for MVP.
- `caddy`: serves public HTTPS with automatic certificate provisioning; reverse-proxies SES host.
- Data bind mount or named volume to persist `/data` across rebuilds.

Potential initial `deploy/compose.yaml` skeleton:

```yaml
services:
  host:
    build: ../host
    restart: unless-stopped
    env_file: .env
    volumes:
      - ses_data:/data
    expose:
      - "8000"

  worker:
    build: ../host
    command: ["python", "-m", "app.worker"]
    restart: unless-stopped
    env_file: .env
    volumes:
      - ses_data:/data

  caddy:
    image: caddy:2
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - host

volumes:
  ses_data:
  caddy_data:
  caddy_config:
```

Caddyfile:

```caddyfile
{$SES_DOMAIN} {
    encode zstd gzip
    reverse_proxy host:8000
}
```

Environment example (populate secrets privately, no real tokens in source control):

```dotenv
SES_DOMAIN=events.example.com
SES_BASE_URL=https://events.example.com
SES_DATA_DIR=/data
EVENT_RETENTION_DAYS=7
NTFY_URL=https://ntfy.sh
NTFY_TOPIC=replace-with-random-unguessable-value
NTFY_SEND_IMAGES=false
SESSION_SECRET=replace-with-long-random-secret
SES_LOG_LEVEL=info
```

Use a root-owned `.env` file (or Docker secrets) with restricted permissions, exclude from Git, and avoid printing secrets to logs. Caddy must be the only externally reachable web port; configure a VPS firewall that allows only necessary management and web ports. Use DNS A/AAAA for the chosen SES hostname, which may be a subdomain of an existing owned domain. Provision certificates normally on the **VPS**, not on the home NVR.

### Home client deployment

- Separate Compose project for `frigate-monitor` with local Mosquitto/Frigate connectivity and a persistent volume for its retry outbox.
- Set its token via an env/secret and grant only `frigate` allowed prefix.
- Do not publish ports; only outbound HTTPS to SES and local MQTT/Frigate connections are needed.

## 14. Security/privacy checklist

- [ ] TLS for all public host access and SES client uploads.
- [ ] Per-client 256-bit API tokens, hash at rest, easy revocation, scoped prefixes.
- [ ] Separate session-based human login; secure cookies and CSRF protection.
- [ ] No unauthenticated image or event-list endpoints.
- [ ] Validate all inputs, metadata sizes, timestamps, channels, upload byte sizes and decoded image dimensions.
- [ ] Normalize uploaded images and remove EXIF; private filesystem permissions.
- [ ] No raw secrets or private image data in request logs.
- [ ] ntfy.sh topic is public-by-design. Send only approved text by default; **never treat topic secrecy as true authentication**.
- [ ] ntfy image attachment publishing off by default; explicit opt-in and documented public-access risk.
- [ ] Caddy and containers patched; non-root host/worker processes when practical; minimal Linux capabilities.
- [ ] DB/image backups appropriately protected and given an expiration policy.
- [ ] Rate limiting per token and login; payload caps and timeouts for outbound ntfy requests.
- [ ] Authenticated user-visible event links; avoid auth tokens in push deep links.
- [ ] Data never requires a public connection *to the home network*.

## 15. Observability and operations

- Structured logs: request ID, client ID, event ID, channel, outcome, duration; redact secrets and sensitive messages where possible.
- Host health: DB write check, worker heartbeat, outbox backlog count, last successful ntfy publish, disk-free threshold.
- `/healthz` lightweight; `/readyz` internal.
- Status page may show delivery state per event: `not_selected`, `pending`, `sent_to_ntfy`, `failed`, `suppressed_cooldown`.
- Client heartbeat states: `healthy`, `stale`, `never_seen`, `disabled`.
- Operator commands/management CLI: create client, issue/rotate/revoke token, set allowed prefixes, create first human admin, requeue failed delivery, prune expired data.
- Daily DB online backup, optional image archive, and periodic restore test.
- Strong logging around image retrieval failures in the Frigate monitor.

## 16. Implementation phases and acceptance criteria

**Sequencing:** Human login/session authentication belongs to Phase 1 because its query API requires human sessions. Implement that protection before exposing event history or images; Phase 2 builds the status page on the same authentication. Client bearer tokens authorize ingestion only and do not grant access to human query/image routes.

### Phase 0 — Bootstrap

- [ ] Initialize repo, Python package, formatting/lint/test configuration, Dockerfiles.
- [ ] Implement environment settings and example Compose/Caddy config.
- [ ] Bootstrap SQLite schema/migrations and admin-client provisioning CLI.

**Done when:** Host starts locally, DB persists across container restart, `healthz` works, and first user/token can be securely provisioned.

### Phase 1 — Generic event ingest and authenticated queries

- [x] Implement channel validation and boundary-aware prefix matcher.
- [x] Implement token auth, allowed prefixes, API request validation, duplicate semantics.
- [x] Implement JSON and multipart submission with image sanitization/storage.
- [x] Implement human login/logout and session management, including secure cookies, session expiry/revocation, CSRF protection on mutating browser actions, and login throttling.
- [x] Store events and images persistently; implement query API and pagination.
- [x] Require a valid human session for event list/detail and image endpoints; client bearer tokens must not authorize access to these routes.
- [x] Add tests for duplicate key, conflicting key, invalid channel, unauthorized channel, oversized/malicious image.
- [x] Add authentication tests for login/logout, expired/revoked sessions, CSRF protection, login throttling, and denial of event/image access without a human session (including requests with only a client bearer token).

**Validation (2026-10-18):** 116 tests pass; Ruff lint and formatting pass. Local Uvicorn + curl verification covers JSON/image submission, deduplication, denial of client-token reads, human login, filtered queries and private image access. Phase 0 database upgrade is tested. Docker/container verification remains pending because Docker is unavailable in the implementation environment.

**Done when:** CLI `curl` can submit `backup.status.freespace` and `frigate.front.person` with/without images, repeat submissions are deduped, and only authorized clients can publish. A human can log in and query events or view images using a valid session; unauthenticated requests, expired/revoked sessions, and client bearer tokens alone cannot read event history or images.

### Phase 2 — Status page

- [ ] Reuse Phase 1 login/session authentication to protect every status page and image/thumbnail view.
- [ ] Server-rendered event feed/details with thumbnails and images.
- [ ] Channel subtree filtering/breadcrumbs; client/severity/date filtering.
- [ ] Client heartbeat endpoint and client status screen.
- [ ] Responsive iPhone testing over public HTTPS.

**Done when:** Logged-in phone browser can review images remotely; unauthenticated visitors cannot view events/images.

### Phase 3 — ntfy integration

- [ ] Subscribe iPhone ntfy app to a random topic.
- [ ] Implement notification rules with most-specific-prefix match.
- [ ] Durable delivery worker, retries, cooldown, delivery status.
- [ ] Publish text + authenticated status-page click URL.
- [ ] Test outage/restart, rate-limits, and notification dedupe.

**Done when:** `curl` submitting an event that matches a notification rule leads to one iPhone push, and tapping it opens the SES event login/detail page. Image is visible **after login**; ntfy itself does not receive JPEG by default.

### Phase 4 — Frigate monitor client

- [ ] Deploy/configure local Mosquitto and Frigate MQTT if not already present.
- [ ] Implement review event parser, person filter and camera→channel map.
- [ ] Integrate correct Frigate snapshot/thumbnail API for running version.
- [ ] Implement persistent outbox and exactly-one-stored-event semantics via stable review ID.
- [ ] Heartbeat reports and restart/reconnect handling.
- [ ] Test with one Wyze camera, then all three.

**Done when:** A real person detection creates a single `frigate.front.person` SES event with snapshot and push; repeated review updates do not spam the phone; Frigate remains inaccessible from the public internet.

### Phase 5 — Hardening and maintenance

- [ ] Implement automated 7-day retention cleanup and orphan file pruning.
- [ ] Test backup/restore, token rotation and revocation, expired sessions.
- [ ] Add health/status failure metrics and disk monitoring.
- [ ] End-to-end VPS outage recovery test with queued events.
- [ ] Validate iOS behavior on cellular with web dashboard link.
- [ ] Decide whether direct ntfy attachments are worth the image privacy tradeoff.

**Done when:** System recovers automatically from short outages, retains only intended data, and is manageable from a short operations guide.

## 17. Manual end-to-end verification plan

1. Deploy host with HTTPS and create one admin login + one `test-client` token.
2. Submit an `info` event to `backup.health` and confirm it appears on status page.
3. Submit image event to `frigate.front.person`; ensure it shows up only when logged in.
   Verify that event list/detail and image requests without a human session, including requests with only a valid client bearer token, are denied; repeat after logout and session expiry/revocation.
4. Query `channel=frigate` subtree and confirm front person included; query `backup.status` and confirm `backup.health` excluded.
5. Resubmit same `(client_id, event_key)` and confirm one stored event and one push job.
6. Configure rule on `frigate.front.person`; subscribe iPhone to ntfy topic; verify text push over cellular.
7. Tap notification; authenticate if needed; view private snapshot.
8. Shut down ntfy networking temporarily and verify pending retry + eventual delivery.
9. Revoke client token and verify submissions fail immediately.
10. Simulate age >7 days in test DB and verify images and history are pruned.
11. Disconnect home internet temporarily and verify Frigate monitor outbox drains after reconnect.

## 18. Design decisions deferred until after MVP

- Real-time UI updates via SSE/WebSockets rather than page refresh.
- Multi-user accounts, roles, and per-user notification subscriptions.
- Channel-level retention and notification schedules/quiet hours.
- Open/resolve state, incident aggregation, acknowledgment and escalation.
- Multiple images/video clips per event.
- Direct image previews in ntfy (requires explicit data exposure decision).
- Self-hosted ntfy, private APNs/PWA delivery, or custom iOS app.
- External integrations beyond Frigate: CLI, backup agent, service health checks.

## 19. Key defaults / architectural principles

1. **Host never reaches into the home network:** clients push outbound only.
2. **Channels are hierarchical; filtering is segment-aware.** `backup.status` is not `backup.statuses`.
3. **Client identity is not encoded into channel.** Permission scopes and event categorization stay distinct.
4. **Storage before notification:** push delivery failures never lose the event.
5. **Idempotent submissions:** durable outboxes plus unique client event keys prevent duplicate stored events.
6. **Human history is private:** a valid session is required for text, full images and thumbnails.
7. **Public ntfy gets minimal information:** text+link by default, image exposed only after SES login.
8. **7-day retention:** documented and automated for both DB and image files.
9. **Simple first:** SQLite, server-rendered templates, one host process and one worker, no message broker in VPS.

---

### References

- ntfy publishing API, priorities, click actions, topics, attachments: https://docs.ntfy.sh/publish/
- ntfy getting started and iOS subscriptions: https://docs.ntfy.sh/
- Frigate MQTT integration: https://docs.frigate.video/integrations/mqtt/
- Frigate API reference: https://docs.frigate.video/integrations/api/
- FastAPI documentation: https://fastapi.tiangolo.com/
- Caddy documentation: https://caddyserver.com/docs/
