# API availability

Only the Phase 0 health endpoints currently exist:

| Endpoint | Result |
| --- | --- |
| `GET /healthz` | `200 {"status":"ok"}` while the process is serving requests. |
| `GET /readyz` | `200 {"status":"ready"}` after a database write-and-rollback probe; `503 {"status":"not_ready"}` on database failure. |

Neither response exposes credentials, file paths, event content, or database errors.
`/readyz` is internal: Caddy returns 404 for that path. The container healthcheck queries
it directly on loopback. Worker readiness will be included when the worker is implemented.

The versioned event and heartbeat APIs described in [the plan](../simple_event_service_plan.md)
are not implemented yet. The root, login, event pages, and OpenAPI documentation also
return 404 in this bootstrap. There is no anonymous event or image access.
