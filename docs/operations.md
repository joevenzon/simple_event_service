# Operations

Phase 3 provides provisioning, health checks, event/image ingestion, human login,
private query APIs, a responsive status page, client heartbeats, and durable text
notifications through ntfy. Cleanup and backups remain in later phases.

## Local development

Install Python 3.12+ and uv, then follow [the README](../README.md). Run commands from
the repository root so the local `.env` and workspace are discovered. `SES_DATA_DIR`
defaults to `./data`; the example `.env` enables development mode and loopback HTTP.
The `.env` file and runtime data are ignored by Git.

If Python is missing, uv can install it. In a restricted Windows environment, keep the
interpreter and cache inside this workspace before syncing:

```powershell
$env:UV_CACHE_DIR = Join-Path (Get-Location) '.uv-cache'
$env:UV_PYTHON_INSTALL_DIR = Join-Path (Get-Location) '.python'
uv python install 3.12 --no-bin
uv sync --python 3.12 --frozen --all-packages
```

### TestClient hangs in a restricted Linux sandbox

If pytest hangs while entering Starlette's `TestClient`, check whether the execution
sandbox permits writes to local socketpairs. TestClient uses an AnyIO portal in another
thread; Python's asyncio loop wakes that thread by writing to a socketpair. In the
restricted environment tested here, that write raises `PermissionError` with errno 1
(`Operation not permitted`). Asyncio normally suppresses the error, so the caller waits
indefinitely. An empty Starlette application and a standard-library cross-thread asyncio
callback reproduce the hang independently of SES startup.

Run the existing environment's tests from a normal local terminal, or use an approved
test command outside the restricted sandbox:

```sh
.venv/bin/python -m pytest -q
```

The same environment passes the test suite outside the sandbox. No dependency
change or application workaround is needed for this failure. For a bounded diagnostic
on Linux:

```sh
timeout 20s .venv/bin/python -m pytest \
  tests/test_bootstrap.py::test_liveness_readiness_and_restart_preserve_database \
  -vv -o faulthandler_timeout=10
```

A stack waiting in `anyio.from_thread` / `TestClient.__enter__` is a reason to investigate the portal;
confirm socket permissions before attributing the hang to this environment issue.

## Settings

| Variable | Default / requirement |
| --- | --- |
| `SES_ENV` | `production`; use `development` only for local access. |
| `SES_DATA_DIR` | `./data` locally, forced to `/data` by Compose. |
| `SES_BASE_URL` | `https://localhost`; an origin without credentials, path, query or fragment; production must use HTTPS. |
| `SESSION_SECRET` | Required in production: at least 32 random characters; placeholders rejected. Signs CSRF tokens. Keep stable across restarts. |
| `SES_SESSION_HOURS` | `24`; absolute session lifetime, 1–720 hours. |
| `SES_LOGIN_ATTEMPTS` | `10`; all login attempts per IP and separately per username during each window. |
| `SES_LOGIN_WINDOW_SECONDS` | `300`; persistent login throttle window in seconds. |
| `SES_INGEST_PER_MINUTE` | `60`; event/heartbeat attempts per client per 60-second window, including retries. |
| `SES_MAX_IMAGE_BYTES` | `5242880` (5 MiB); source and sanitized image limit, adjustable downward. |
| `SES_MAX_IMAGE_PIXELS` | `20000000`; decoded image pixel cap, adjustable downward. |
| `EVENT_RETENTION_DAYS` | `7`, positive integer; cleanup is not implemented yet. |
| `SES_LOG_LEVEL` | `info`; accepts debug, info, warning, error, critical. |
| `NTFY_URL` | `https://ntfy.sh`; an origin without credentials, path, query or fragment; production requires HTTPS. |
| `NTFY_TOPIC` | Empty/unset disables publishing. Otherwise use a random topic of 32–128 ASCII letters, digits, `_` or `-`; placeholders rejected. |
| `NTFY_SEND_IMAGES` | Must remain `false`; enabling an unimplemented image publishing path is rejected. |
| `SES_NOTIFICATION_COOLDOWN_SECONDS` | `60`; per client/channel, 0 disables, maximum 86400. Suppressed pushes remain in history. |
| `SES_NOTIFICATION_MAX_ATTEMPTS` | `8`; delivery attempt budget, 1–100, including interrupted attempts. |

Settings load from the process environment and then `.env` in the current directory.
Environment variables take precedence. Invalid settings abort startup. Error messages
avoid printing the full validation error because it may contain environment secrets.

## Provisioning

```text
uv run ses init-db
uv run ses create-admin admin
uv run ses revoke-sessions admin
uv run ses create-client frigate-home --name "Home Frigate" --allow-prefix frigate
uv run ses create-client backup-agent --allow-prefix backup
uv run ses rotate-client frigate-home
uv run ses revoke-client frigate-home
uv run ses set-client-prefixes backup-agent --allow-prefix backup.status --allow-prefix backup.health
uv run ses set-client-prefixes backup-agent --unrestricted
```

Passwords require at least 12 characters and a usable terminal for hidden input. Do not
put passwords on the command line or in source files. Client IDs use 1-80 lowercase ASCII
characters, start with a letter, and otherwise allow letters, digits, `_`, and `-`.
Omitting `--allow-prefix` during creation grants all channels; specify it for scoped clients.

Each create/rotate command prints its token once after committing. Save it in the client's
private environment or secret store. The database stores only its digest. Revoke disables
the client; rotate replaces the token and re-enables it. No token is recoverable from the
database. Revocation and rotation take effect on the next event/heartbeat request, including
duplicate submissions. `revoke-sessions` immediately invalidates all browser sessions
for a human user without changing their password or client tokens.

## Authentication and request limits

Open `/` to sign in and view the dashboard. `/events` provides filters and pagination;
`/events/{id}` shows full snapshots and metadata; `/clients` displays heartbeat status.
The navigation includes a CSRF-protected Sign out button and `/account` links to the API.
Sessions are stored in SQLite and survive restarts. Session expiry is absolute; browsing
does not extend it. Password verification uses Argon2id. Login attempts are throttled per
source IP and per exact username, including successful attempts. Both login and per-client
ingest throttles persist across processes and restarts; responses include `Retry-After`.
Expired throttle rows are removed on the next throttled request.

Production requires HTTPS as reported by the ASGI server and uses Secure session/CSRF
cookies. Only Caddy should reach Uvicorn, with trusted proxy headers as configured below.
Development mode allows HTTP cookies for loopback testing and generates a temporary CSRF
signing key when no `SESSION_SECRET` is configured. After a development restart, reload
forms or fetch `/api/v1/session` for a fresh CSRF token; persisted sessions still work.
Changing `SESSION_SECRET` rotates CSRF signing; use `revoke-sessions` to revoke sessions.

Request byte caps apply while streaming, even without Content-Length. Images are decoded
and normalized off the request event loop. Original images and EXIF are not retained.
Accepted event metadata is preserved as submitted; publish only intended private event data.
See [API details and curl examples](api.md).

## VPS deployment

Requires Docker Engine with Compose on the VPS, a hostname pointing at it, and inbound
ports 80/443 for Caddy. From the repository root on the VPS:

```sh
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
# Generate SESSION_SECRET and paste it privately into deploy/.env:
uv run python -c 'import secrets; print(secrets.token_urlsafe(48))'
# Edit deploy/.env: set SES_DOMAIN, SES_BASE_URL, SESSION_SECRET.
docker compose --project-directory deploy config --quiet
docker compose --project-directory deploy up -d --build
docker compose --project-directory deploy exec host ses create-admin admin
docker compose --project-directory deploy exec host ses create-client frigate-home --allow-prefix frigate
```

If uv is not installed on the VPS, generate the secret with any available Python 3
interpreter or a local trusted password manager. Keep `.env` privately owned by the deployer.
Configure DNS A/AAAA records and the VPS firewall before requesting certificates.
Local implementation and tests do not deploy to a VPS.

The Docker build context is the repository root; the lockfile is used with `uv sync --frozen`.
The host and worker run as UID/GID 10001 with read-only root filesystems and a shared writable `/data` volume.
For a bind mount instead of the named volume, provision its ownership for that UID first.
Caddy receives only the hostname, not the application's session secret.

Only Caddy publishes TCP ports. Uvicorn trusts forwarded headers because this setup exposes
the host only on the private Compose network behind Caddy. Do not publish its port directly
or attach untrusted containers to that network. Local development uses Uvicorn's normal
loopback proxy trust defaults.

Verify liveness and persistence:

```sh
curl --fail https://events.example.com/healthz
docker compose --project-directory deploy exec host python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz').read().decode())"
docker compose --project-directory deploy restart host
docker compose --project-directory deploy exec host ses init-db
```

`init-db` is idempotent. Restart and rebuild preserve the named volume. `docker compose down`
also preserves it; adding `--volumes` destroys persisted data. A deployment verification
must still exercise the image and volume on a Docker-capable machine.

## Notifications and iPhone verification

Publishing requires both an explicit rule and a nonempty topic. Generate the topic with
`uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'`, save it privately
as `NTFY_TOPIC` in the host/worker environment, and subscribe to that exact topic using
the official ntfy iPhone app on `ntfy.sh`. A random topic is obscurity, not authentication;
send only text you intend to share with the public relay. Keep `NTFY_SEND_IMAGES=false`.
SES never uploads the image or makes its private image URL an ntfy attachment.

Add rules using the local CLI, or prefix `ses ...` with
`docker compose --project-directory deploy exec host` on the VPS:

```sh
uv run ses add-notification-rule frigate --disabled
uv run ses add-notification-rule frigate.front.person --min-severity info --ntfy-priority 4
uv run ses add-notification-rule frigate.back.person --min-severity info --ntfy-priority 4
uv run ses add-notification-rule backup.status --min-severity warning
uv run ses list-notification-rules
```

The most specific matching subtree wins. Equal prefixes use higher `--rule-priority`,
then lower `--sort-order`, then insertion order. Quote `'*'` for an all-channel rule.
No match means no push. Disabled rules and failed severity checks suppress without
falling back to a broader rule. With no explicit `--ntfy-priority`, severities
debug/info/warning/error/critical map to 1/3/4/5/5. `notify=false` opts out; true never
overrides the rule. `ses remove-notification-rule RULE_ID` affects future events;
already queued deliveries retain their selection and priority.

In local development, start `uv run python -m app.worker` in another terminal using the
same `.env` and data directory as Uvicorn. Compose starts the worker automatically;
after changing its environment, recreate both services:

```sh
docker compose --project-directory deploy up -d --build host worker
docker compose --project-directory deploy exec worker python -m app.worker --check
docker compose --project-directory deploy logs --tail=50 worker
```

An empty topic stops publishing while preserving pending jobs; setting it later resumes
those jobs. Ingestion does not contact ntfy. The default cooldown reserves one push per
client/channel per 60 seconds when selected, including during an outage. Additional
events remain visible as `suppressed_cooldown` and are never queued for later push.
Cooldown state and duplicate protection survive restarts. Set cooldown to 0 to disable it.

Network errors, HTTP 429, and 5xx retry with exponential jitter (starting at 2.5–5 seconds,
capped at 30–60 minutes) and respect valid `Retry-After` seconds or HTTP dates. Other
HTTP errors fail immediately; redirects are refused. After eight attempts by default,
the delivery fails. The private event page and API show state, attempt count, next retry,
sent time, and a sanitized error. After fixing the underlying issue, use
`ses requeue-delivery EVENT_UUID` to reset a failed delivery's attempt budget. Pending,
sent, and cooldown-suppressed jobs cannot be requeued by that command.

The worker claims a job for 120 seconds before publishing. A crash leaves it pending
until that claim expires; interrupted attempts count toward the budget. A crash after
ntfy accepts but before SES commits success can duplicate the push. `sent_to_ntfy`
means relay acceptance, not proof of phone receipt. `/readyz` requires a worker heartbeat
within 120 seconds when a topic is configured, in addition to its database write probe.
Relay outages do not make readiness fail while the worker remains active.

For real-device acceptance:

1. Deploy with public HTTPS, configure a random topic, subscribe on the iPhone, and add
   an enabled rule. Submit a matching event using the [API examples](api.md).
2. Confirm one push and `sent_to_ntfy` in SES. Retry the identical submission and confirm
   it returns the existing event without another push.
3. Tap the notification while signed out. Confirm login returns to that event and its
   private image appears after login. Repeat over cellular.
4. Submit another event on the same client/channel within the cooldown and confirm it
   remains in history with `suppressed_cooldown` and produces no push.

Automated tests and the isolated HTTPS mock relay verify host behavior without sending
public notifications. Physical iPhone delivery, cellular access, public DNS/TLS, and the
notification tap behavior still need this manual acceptance check. API details follow
the [ntfy JSON publishing documentation](https://docs.ntfy.sh/publish/#publish-as-json).

## Migration and recovery notes

All writes use explicit transactions and foreign keys. Migrations acquire an immediate
write lock before reading the applied history; re-running initialization does not reset
existing users or tokens. Failed migration DDL and version records roll back together.
A newer or incompatible schema stops initialization instead of attempting a downgrade.
Add a new numbered migration to `host/app/migrations.py` for schema changes.
Phase 1 appends migration 2 for persistent request throttles; Phase 0 databases upgrade
at startup without replacing existing users, tokens, or data.
Phase 3 appends migrations 3 and 4 for durable claims, cooldowns, worker heartbeats, and
optional priority overrides. Existing explicit rules and event records are preserved;
old events are not automatically queued for push.

Images use a temporary file, fsync and atomic rename before their event transaction commits.
Ordinary write/transaction failures remove staged files and roll back image/event rows.
A process crash between rename and database commit can leave an unreferenced file; orphan
reconciliation and scheduled retention arrive in Phase 5. This release has no automatic
seven-day deletion. Monitor disk use and use only data whose retention you can manage.

The current milestone has no scheduled retention or backup command. Until those are
implemented, any test data remains on disk. Future backups must use SQLite's online backup
API and an explicit expiry policy; copying a live SQLite main file without its WAL is unsafe.

## Heartbeats and status page verification

Clients should send `POST /api/v1/heartbeat` every 60 seconds with their bearer token,
using `{"health":{"mqtt_connected":true,"queue_size":0}}` or `{}` for liveness alone.
The default stale threshold is 180 seconds; each client’s `stale_timeout_seconds` is stored
in SQLite. Heartbeats and event submissions share `SES_INGEST_PER_MINUTE`; include both in
rate budgeting. Health is an optional object of at most 8 KiB serialized as compact UTF-8;
the complete request is limited to 16 KiB. Only the latest report is retained on the client.
Do not include credentials in health reports. They are visible to the signed-in operator.

Healthy/stale reflects heartbeat freshness, rather than interpreting arbitrary health
fields. A client with event activity but no heartbeat remains `never_seen` in this heartbeat
status. The client screen shows last heartbeat, last event, and last activity separately.
Refresh the page to update times and status; there is no background polling.

Previews are created lazily on first authenticated view, including for existing events.
The host needs write access to `/data/thumbnails` in addition to `/data/images` and SQLite.
Previews use generated image paths and 0600 files. Deleting a preview causes it to be
regenerated on the next authorized view while the original exists. Neither directory is
served statically. The wheel includes the HTML templates, stylesheet, and time-formatting
script used by the Docker image; no frontend build is needed.

For the remaining real-device acceptance check, deploy through the existing Caddy HTTPS
configuration, then use Safari on an iPhone over cellular:

1. Open `/events/{id}` for an image event while signed out. Confirm it goes to login, then
   returns to that event after signing in and displays the full image.
2. Use `/events` in portrait and landscape. Filter the channel subtree, client, severity,
   and UTC calendar dates; follow Older events and open a thumbnail.
3. Open `/clients`, send a heartbeat, refresh, and confirm healthy. Stop heartbeats, wait
   past the client’s stale threshold, and refresh to confirm stale.
4. Sign out. Reopen the event, full image, and thumbnail URLs; confirm the pages require
   login and the direct image routes return 401. Repeat after session expiry or revocation.

Local automated checks cannot verify public DNS/certificate issuance, cellular routing,
or the behavior of a physical iPhone. Keep that Phase 2 acceptance item pending until
these checks pass on the deployed hostname.
