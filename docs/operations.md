# Operations

This is the Phase 0 scaffold. It provides provisioning and health checks; ingestion,
dashboard login, notifications, cleanup, and backups still require implementation.

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
| `SESSION_SECRET` | Required in production: at least 32 random characters; `replace-` placeholders rejected. Reserved for session implementation. |
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
database. Token acceptance at the ingestion endpoint will be implemented in Phase 1.

## VPS deployment scaffold

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
No deployment is performed by this repository's local bootstrap.

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

The current milestone has no scheduled retention or backup command. Until those are
implemented, any test data remains on disk. Future backups must use SQLite's online backup
API and an explicit expiry policy; copying a live SQLite main file without its WAL is unsafe.
