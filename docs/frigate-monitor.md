# Frigate monitor

The independent `ses-frigate-monitor` package subscribes to one local Frigate review
topic and uploads person alerts to SES. It publishes no MQTT messages and changes no
Frigate settings. No Frigate installation is required for the automated tests.

The implementation follows the documented
[review MQTT payload](https://docs.frigate.video/integrations/mqtt/#frigatereviews),
[review API](https://docs.frigate.video/integrations/api/review-review-get/), and
[credential login](https://docs.frigate.video/integrations/api/login-login-post/).
Endpoint behavior is checked against the
[Frigate 0.16.2 source](https://github.com/blakeblackshear/frigate/tree/v0.16.2/frigate/api).
Verify your running version, certificates, broker configuration, camera IDs, and real
images before deployment using the [deployment checks](#deployment-verification).

## Local development

From the repository root:

```sh
uv sync --frozen --all-packages
cp clients/frigate_monitor/config.example.yaml clients/frigate_monitor/config.yaml
uv run ses-frigate-monitor --config clients/frigate_monitor/config.yaml validate
uv run pytest tests/test_frigate_monitor.py
```

The optional container check uses a disposable Mosquitto broker, an internal Docker
network, and two successive monitor containers with synthetic HTTP fixtures:

```sh
docker build -f clients/frigate_monitor/Dockerfile -t ses-frigate-monitor:container-check .
python tests/check_frigate_container.py
```

It publishes no ports and removes its temporary containers, network, and data volume.
It checks QoS 1 broker-session replay, restart with a pending SES retry, identical frozen
JSON/image bytes, heartbeat upload, non-root execution, and runtime package separation.

`validate` parses YAML without reading secrets, creating an outbox, or connecting to
anything. Before running, edit `config.yaml` with the actual camera IDs, broker, SES
origin, and Frigate origin. The sample camera names are placeholders. Start with one
camera mapping and add the other cameras after verification. The suffix `.person` is
appended to each mapped channel prefix. Unmapped cameras are ignored.

Provision an SES client scoped to `frigate` using the existing host CLI. Its bearer
token identifies the client; `ses.client_id` is an operator label, not a way to choose
the authenticated identity. Export `SES_CLIENT_TOKEN` in the monitor process environment.
The monitor does not automatically load `.env` when run directly; Compose does.

For Frigate's authenticated port (normally HTTPS on 8971), set `api_auth: login` and
provide `FRIGATE_USERNAME` and `FRIGATE_PASSWORD` through the environment. Use an
existing viewer account where available. The client logs in with JSON `user`/`password`,
keeps its cookie in memory, and logs in again once after a 401. Only the login endpoint
receives POST requests; media, review, tracked-object, and version calls use GET.
It never updates review-viewed state, camera settings, or users.

TLS verification is always enabled. For a private CA, mount its certificate and set
`frigate.ca_file`. The URL hostname or IP must match the certificate. An already
available internal Docker endpoint such as `http://frigate:5000` can instead use
`api_auth: none` without supplying Frigate login secrets. Do not expose that internal
port publicly or open a home router port. SES requires HTTPS; isolated loopback tests
may explicitly set `ses.allow_loopback_http: true` with a localhost HTTP origin.

For an authenticated MQTT broker, set both username/password environment names and
provide those variables. Omit both names for an existing anonymous development broker.
For broker TLS, set `mqtt_tls: true` and optionally `mqtt_ca_file`. Broker permissions
need only allow subscription to the configured review topic. Give each independent
monitor a stable, unique `mqtt_client_id`.

```sh
uv run ses-frigate-monitor --config clients/frigate_monitor/config.yaml run
```

No Mosquitto or Frigate services are installed or configured by this command.
If MQTT is not already enabled, configuring the broker and Frigate is a separate
operator task. The monitor does not perform that step on an existing NVR.

## Home Docker deployment

The home Compose file starts only the monitor. From `clients/frigate_monitor/`:

```sh
cp .env.example .env
# Edit .env privately and config.yaml with the actual origins, broker, and cameras.
docker compose config --quiet
docker compose build
docker compose up -d
docker compose logs --tail 50 frigate-monitor
```

Use a broker hostname or LAN address reachable from this separate Compose project;
`mosquitto` resolves only on a shared Docker network with that service. Attach the
monitor to an existing external network if using internal Frigate/broker service names.
The sample Compose publishes no ports and mounts a persistent named outbox volume.
The process runs as UID/GID 10001 with a read-only root filesystem. If you replace
the volume with a bind mount, create its directory with that ownership and mode 0700.
Restrict `.env` and any private certificate/key files to the operator account.

The image installs only the monitor package and its dependencies. The VPS host image
installs only the host package; neither runtime imports the other.

## Delivery and recovery

The first qualifying `new`, `update`, or `end` message must have severity `alert`,
contain the `person` label, and come from a mapped camera. Detection-to-alert upgrades
are supported. The event key is `frigate-review-<review_id>`, scoped by the SES token.
The initial review is stored in SQLite before acknowledging a QoS 1 MQTT message.
Duplicates and later/out-of-order updates cannot change that event.

The monitor requests subscription QoS 1 and uses a persistent broker session with
manual acknowledgments. Frigate must also publish reviews at QoS 1, and the broker
must retain persistent sessions/messages, for broker-side outage replay. Subscribing
at QoS 1 cannot upgrade messages published at QoS 0. Messages the broker never received
or did not retain cannot be recovered by this client. There is no review-history
backfill. Storage failure stops the monitor without acknowledging the alert; its
supervisor restarts it. Invalid input is ignored and acknowledged without logging
payloads. A refused subscription also stops the process.

The image stage waits two seconds, then retries missing images within a 15-second
window. It reads `GET /api/review/{review_id}` and prefers the `thumb_path` returned by
Frigate. Only a validated path under `/media/frigate/clips/review/` is translated to
Frigate's authenticated `/clips/review/` media route, as configured in its
[0.16.2 nginx source](https://github.com/blakeblackshear/frigate/blob/v0.16.2/docker/main/rootfs/usr/local/nginx/conf/nginx.conf).
It never fetches arbitrary URLs or filesystem paths supplied by MQTT.

If the thumbnail is unavailable, it examines up to five associated `data.detections`
object IDs with `GET /api/events/{id}`, selects a person on the same camera, and reads
`GET /api/events/{id}/snapshot.jpg`. It never sends a review ID to a tracked-object
snapshot endpoint. Images are byte/pixel bounded, decoded, and converted to a JPEG.
An expired review or exhausted retry window produces a text event with
`metadata.snapshot_available=false`. A restart after that window sends the text fallback.
The retry window controls scheduling; an already running HTTP operation may finish later.

The final JSON and image bytes are committed together before the first SES request.
Retries reuse those exact fields and image bytes, even if Frigate later changes its
thumbnail. JSON uploads use `application/json`; image uploads use multipart `event`
and `image` fields. Only a valid accepted/duplicate receipt on HTTP 200/201 clears the
payload. A crash after host acceptance safely repeats the stable key and frozen content.

Network errors, HTTP 408/429/5xx, and ambiguous responses retry indefinitely with
exponential jitter, a one-hour backoff cap, and `Retry-After`. Other HTTP errors,
including authentication, scope, validation, and 409 conflicts, preserve the payload
in a failed state. Failed jobs do not block subsequent events. No automatic policy
changes or token rotation occur. Stop the monitor, fix the cause, requeue unchanged
failed payloads, then restart:

```sh
uv run ses-frigate-monitor --config clients/frigate_monitor/config.yaml status
uv run ses-frigate-monitor --config clients/frigate_monitor/config.yaml retry-failed
```

With Docker, use `docker compose stop frigate-monitor`, then
`docker compose run --rm frigate-monitor ses-frigate-monitor --config /config/config.yaml retry-failed`,
then `docker compose up -d`. `status` can run while the monitor runs; delivery and
requeue operations require an exclusive local process lock. Do not run two monitor
instances against the same broker client ID or outbox volume.

Successful jobs keep compact review-ID/host-ID receipts after removing payload and
image columns. Keep the volume and the SES client identity when restarting/rebuilding.
Deleting the outbox loses those receipts; a later replay with changed content can then
produce a 409. SQLite pages and WAL files are not a secure media-erasure mechanism.
The outbox has no size limit or automatic pruning: prolonged SES outages, unresolved
failed jobs, and accumulated receipts require disk monitoring.

Heartbeats report broker subscription status, API connectivity/version, queue size,
failed count, last MQTT message, uptime, and last successful upload time. A version
read refreshes API connectivity even during quiet periods. Queue replay continues
when MQTT is disconnected; health reports continue while SES is unavailable and
are attempted again at the next interval. The SES status page displays this metadata.
Do not equate broker connectivity with working camera detection.

SES notification rules still determine push eligibility and cooldown. This client
never publishes directly to ntfy. One stored event per review does not change the
host's documented at-least-once relay delivery behavior.

## Deployment verification

Run these manual checks against your deployment:

1. Confirm the installed Frigate version, HTTPS trust, existing MQTT settings/QoS,
   broker persistence, and camera IDs without changing the NVR during inspection.
2. Configure the monitor for one camera and an SES client scoped to `frigate`.
   Choose an explicit host notification rule before testing push delivery.
3. Walk in front of that camera. Verify one event, the correct review thumbnail or
   person snapshot, and its authenticated SES detail/image page.
4. Observe repeated review updates and confirm one stored event. Test SES outage
   and monitor restart, then verify its existing outbox drains on recovery.
5. Check disconnect/reconnect with the chosen broker's QoS/persistence configuration.
6. Add the remaining camera mappings and repeat. Verify the real phone push over
   cellular and confirm no Frigate port is exposed on the public internet.
