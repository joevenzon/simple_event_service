# Simple Event Service

A private event archive for independently running clients, with hierarchical channels,
a mobile status page, and selected notifications through ntfy.sh.

The host accepts JSON/image events, enforces scoped client tokens, deduplicates retries,
and serves a responsive dashboard,
filtered event feed, full snapshots, private thumbnails, and client health screens through
human login sessions. Host-approved notifications use a durable worker with retries,
cooldowns, and text plus a link to the authenticated event page. A separate Frigate
client filters review MQTT alerts, retrieves private images, and uploads through a
durable local outbox. The worker automatically removes events older than seven days
and prunes orphan images and thumbnails. Deployment verification includes manual camera
and iPhone push checks described in the operations guides.

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). From the repository root:

```powershell
uv sync --frozen --all-packages
Copy-Item .env.example .env
uv run ses init-db
uv run ses create-admin admin
uv run ses create-client frigate-home --allow-prefix frigate
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000
```

On Linux/macOS use `cp .env.example .env` instead of `Copy-Item`. The admin command
prompts for a password without echoing it. The client command prints a new token once;
save it privately. No real credentials are included in the repository.

Check `http://127.0.0.1:8000/healthz` and `http://127.0.0.1:8000/readyz`.
Open `http://127.0.0.1:8000/` to sign in and view the dashboard. Use `/events` for
channel/client/severity/date/search filters and `/clients` for heartbeat status.
See [API examples](docs/api.md) to submit `backup.status.freespace` or image events to
`frigate.front.person`, query history, and use cursor pagination.
Local development uses `./data`; stop and restart the process to reuse the database.
Development mode is for loopback access. Production requires HTTPS and a session secret.

Run `uv run python -m app.worker` in another terminal for automatic retention cleanup,
including when notifications are disabled. Compose starts the worker automatically.
Cleanup runs at startup and hourly; `EVENT_RETENTION_DAYS` defaults to `7`.
For notifications, configure a random `NTFY_TOPIC` and add an explicit notification rule.
Publishing stays off while the topic is empty; no rules select events by default.
See [notification setup](docs/operations.md#notifications-and-iphone-verification) and
[retention operations](docs/operations.md#retention-and-orphan-cleanup).

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

For setup, credential rotation, and VPS deployment, see
[operations](docs/operations.md). See also [architecture](docs/architecture.md) and
[the API guide](docs/api.md).

For the independent home client, see [Frigate monitor setup](docs/frigate-monitor.md).
Its configuration can be checked without credentials or a running Frigate:

```sh
uv run ses-frigate-monitor --config clients/frigate_monitor/config.example.yaml validate
```

The home Compose configuration starts only the monitor; it does not install a broker or
change an existing Frigate deployment.
