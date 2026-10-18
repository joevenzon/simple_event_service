# Operations

Phase 1 provides provisioning, health checks, event/image ingestion, human login and
private query APIs. The dashboard, notifications, cleanup, and backups remain in later phases.

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

## Settings

| Variable | Default / requirement |
| --- | --- |
| `SES_ENV` | `production`; use `development` only for local access. |
| `SES_DATA_DIR` | `./data` locally, forced to `/data` by Compose. |
| `SES_BASE_URL` | `https://localhost`; production must use HTTPS. |
| `SESSION_SECRET` | Required in production: at least 32 random characters; placeholders rejected. Signs CSRF tokens. Keep stable across restarts. |
| `SES_SESSION_HOURS` | `24`; absolute session lifetime, 1–720 hours. |
| `SES_LOGIN_ATTEMPTS` | `10`; all login attempts per IP and separately per username during each window. |
| `SES_LOGIN_WINDOW_SECONDS` | `300`; persistent login throttle window in seconds. |
| `SES_INGEST_PER_MINUTE` | `60`; ingestion attempts per client per 60-second window, including retries. |
| `SES_MAX_IMAGE_BYTES` | `5242880` (5 MiB); source and sanitized image limit, adjustable downward. |
| `SES_MAX_IMAGE_PIXELS` | `20000000`; decoded image pixel cap, adjustable downward. |
| `EVENT_RETENTION_DAYS` | `7`, positive integer; cleanup is not implemented yet. |
| `SES_LOG_LEVEL` | `info`; accepts debug, info, warning, error, critical. |
| `NTFY_URL`, `NTFY_TOPIC` | Reserved for Phase 3. No publish requests currently occur. |
| `NTFY_SEND_IMAGES` | Must remain `false`; enabling an unimplemented image publishing path is rejected. |

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
database. Revocation and rotation take effect on the next ingest request, including
duplicate submissions. `revoke-sessions` immediately invalidates all browser sessions
for a human user without changing their password or client tokens.

## Authentication and request limits

Use `/login` to sign in, then `/account` for logout or the private event API link.
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
The host runs as UID/GID 10001 with a read-only root filesystem and writable `/data` volume.
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

## Migration and recovery notes

All writes use explicit transactions and foreign keys. Migrations acquire an immediate
write lock before reading the applied history; re-running initialization does not reset
existing users or tokens. Failed migration DDL and version records roll back together.
A newer or incompatible schema stops initialization instead of attempting a downgrade.
Add a new numbered migration to `host/app/migrations.py` for schema changes.
Phase 1 appends migration 2 for persistent request throttles; Phase 0 databases upgrade
at startup without replacing existing users, tokens, or data.

Images use a temporary file, fsync and atomic rename before their event transaction commits.
Ordinary write/transaction failures remove staged files and roll back image/event rows.
A process crash between rename and database commit can leave an unreferenced file; orphan
reconciliation and scheduled retention arrive in Phase 5. This release has no automatic
seven-day deletion. Monitor disk use and use only data whose retention you can manage.

The current milestone has no scheduled retention or backup command. Until those are
implemented, any test data remains on disk. Future backups must use SQLite's online backup
API and an explicit expiry policy; copying a live SQLite main file without its WAL is unsafe.
