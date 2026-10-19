# Simple Event Service

A private event archive for independently running clients, with hierarchical channels,
a mobile status page, and selected notifications through ntfy.sh.

Implementation follows [the project plan](simple_event_service_plan.md). **Current milestone:
Phase 3 durable ntfy delivery.** The host accepts JSON/image events,
enforces scoped client tokens, deduplicates retries, and serves a responsive dashboard,
filtered event feed, full snapshots, private thumbnails, and client health screens through
human login sessions. Host-approved notifications use a durable worker with retries,
cooldowns, and text plus a link to the authenticated event page. Retention jobs and the
Frigate client remain in later phases; real iPhone push acceptance remains a manual check.

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

For notifications, configure a random `NTFY_TOPIC`, add an explicit notification rule,
and run `uv run python -m app.worker` in another terminal. Compose starts the worker
automatically. Publishing stays off while the topic is empty; no rules select events by
default. See [notification setup](docs/operations.md#notifications-and-iphone-verification).

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

For setup, credential rotation, and the VPS deployment scaffold, see
[operations](docs/operations.md). See also [architecture](docs/architecture.md) and
[current API availability](docs/api.md).
