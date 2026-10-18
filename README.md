# Simple Event Service

A private event archive for independently running clients, with hierarchical channels,
a mobile status page, and selected notifications through ntfy.sh.

Implementation follows [the project plan](simple_event_service_plan.md). **Current milestone:
Phase 0 bootstrap.** The host runs, persists its schema, and provisions credentials.
Event ingestion, dashboard login, notifications, retention jobs, and the Frigate client
are not implemented yet. Do not deploy this milestone expecting monitoring or alert delivery.

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
Local development uses `./data`; stop and restart the process to reuse the database.
Development mode is for loopback access. Production requires HTTPS and a session secret.

```powershell
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

For setup, credential rotation, and the VPS deployment scaffold, see
[operations](docs/operations.md). See also [architecture](docs/architecture.md) and
[current API availability](docs/api.md).
