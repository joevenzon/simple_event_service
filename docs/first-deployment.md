# First deployment: Ubuntu VPS and Docker Frigate

This walkthrough assumes Ubuntu 24.04 on both machines, an SSH account with sudo,
and an existing Docker Frigate installation. Run each block on the machine named
in its section. Replace example domains, IP addresses, camera names, and passwords.
Start with one camera and leave phone notifications off until events work.

The traffic flow is:

```text
Home: Frigate → MQTT broker → SES monitor → HTTPS → VPS: Caddy → SES host
                                 ↓                              ↓
                        Frigate API for images            database + images
```

An MQTT broker is a message relay. Frigate publishes review notices to a topic
(normally `frigate/reviews`); the monitor subscribes to it. Mosquitto is one broker
you can run. The broker carries notices, while the monitor fetches pictures directly
from Frigate. The VPS does not need access to your cameras, broker, or Frigate.

## 1. Prepare the VPS and DNS

Create an Ubuntu 24.04 VPS and use the provider's SSH instructions to connect:

```sh
ssh ubuntu@YOUR_VPS_IP
```

Use your provider's actual account name if it is not `ubuntu`. On the VPS:

```sh
sudo apt-get update
sudo apt-get install -y git curl ca-certificates openssl nano dnsutils ufw
```

Choose a hostname such as `events.example.com`. In your DNS provider, add an A
record pointing that hostname at the VPS's public IPv4 address. Add an AAAA record
only if the VPS has working public IPv6. For the first deployment, use direct DNS
rather than a DNS provider's HTTP proxy.

In the VPS provider's firewall, allow inbound TCP 80 and your chosen HTTPS port
(443 by default) from the internet, and your SSH port from your administration
address. For a custom HTTPS port such as 8443, allow 8443 instead of 443 in the
firewall commands below. Port 80 remains necessary for automatic certificate
issuance/renewal with this configuration. Nothing in this deployment needs a
public port 8000, 1883, 5000, or 8971 unless you explicitly choose one for HTTPS.
Allow outbound DNS and HTTPS for certificate issuance, image builds, and notifications.

If using Ubuntu's UFW on this fresh VPS, first allow your actual SSH port. For
standard port 22:

```sh
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status
dig +short A events.example.com
dig +short AAAA events.example.com
```

Verify the addresses before starting Caddy. Keep the existing SSH connection open
until you have verified a second connection. Docker-published ports can bypass UFW;
the provider firewall and the Compose port mappings also matter. See
[Docker's firewall notes](https://docs.docker.com/engine/install/ubuntu/#firewall-limitations).

## 2. Install Docker on the VPS

On a fresh VPS, install Engine and the Compose plugin using
[Docker's Ubuntu repository instructions](https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository):

```sh
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo docker compose version
```

These commands target a fresh Ubuntu 24.04 installation. If Docker is already
installed, verify it instead; resolve conflicting packages using the linked guide.
The commands below use `sudo docker`, so Docker group membership is unnecessary.
Python, uv, SQLite tools, and Caddy are included in the application containers;
you do not install them separately on either machine.

## 3. Clone, configure, and start the VPS host

On the VPS, as your normal SSH user:

```sh
mkdir -p ~/services
cd ~/services
git clone https://github.com/joevenzon/simple_event_service.git
cd simple_event_service
git rev-parse HEAD
cp deploy/.env.example deploy/.env
chmod 600 deploy/.env
openssl rand -hex 48
nano deploy/.env
```

If the repository is private, authenticate with an authorized GitHub credential
or use a read-only SSH deploy key and the SSH clone URL. Keep credentials out of
clone URLs. Save the commit ID so you can deploy the same version at home.

Copy the generated random string into `SESSION_SECRET`. Set these entries in
`deploy/.env`, keeping the other defaults:

```dotenv
SES_DOMAIN=events.example.com
SES_HTTPS_PORT=443
SES_BASE_URL=https://events.example.com
SESSION_SECRET=PASTE_THE_GENERATED_RANDOM_STRING_HERE
NTFY_TOPIC=
NTFY_SEND_IMAGES=false
```

`SES_DOMAIN` is a hostname without a scheme or port. `SES_HTTPS_PORT` chooses the
public HTTPS port; omit it to use 443. `SES_BASE_URL` is the HTTPS origin with no
additional path. For example, to serve on port **8443**, use:

```dotenv
SES_DOMAIN=events.example.com
SES_HTTPS_PORT=8443
SES_BASE_URL=https://events.example.com:8443
```

Allow TCP 8443 in both firewalls (`sudo ufw allow 8443/tcp` for UFW). Include
`:8443` in the browser URL, every public curl URL below, and the home monitor's
`ses.host_url`, for example `https://events.example.com:8443/healthz` for the
health check. Compose publishes the chosen port and Caddy serves HTTPS on it;
the application's internal port stays 8000.

Port 80 is still used for HTTP redirects and automatic certificate validation.
If another web server already owns port 80, this Compose setup needs integration
with that server before it can start; changing only the HTTPS port will not free
port 80. See [Caddy's certificate challenge requirements](https://caddyserver.com/docs/automatic-https#acme-challenges).

Keep the secret stable when updating. In nano, save with Ctrl+O, Enter, then exit
with Ctrl+X.

From `~/services/simple_event_service`:

```sh
sudo docker compose --project-directory deploy config --quiet
sudo docker compose --project-directory deploy up -d --build
sudo docker compose --project-directory deploy ps
sudo docker compose --project-directory deploy logs --tail=80 host worker caddy
curl --fail-with-body https://events.example.com/healthz
sudo docker compose --project-directory deploy exec host python -c "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=6).read().decode())"
```

The first build takes time. Expect three services: `host`, `worker`, and `caddy`.
The host and worker should become healthy. Caddy obtains the public certificate
automatically. The public health check should return `{"status":"ok"}`. `/readyz`
is deliberately accessible only internally; a public 404 there is expected.
`config --quiet` checks Compose configuration without printing environment secrets.

Create your dashboard login and the home monitor's credentials:

```sh
sudo docker compose --project-directory deploy exec host ses create-admin admin
sudo docker compose --project-directory deploy exec host ses create-client frigate-home --name "Home Frigate" --allow-prefix frigate
```

The admin password prompt requires at least 12 characters. The client command prints
a token once: save it privately for the home machine. The admin password is for
browser login; the client token is for uploads. Open `https://events.example.com/`
in your browser and sign in. An empty event list is expected.

Startup initializes the database automatically. Keep the worker running even with
notifications off: it removes events and images older than seven days by default.

## 4. Inspect Frigate and check for a broker at home

SSH into the Ubuntu Frigate machine. Install the basic tools and check its existing
Docker installation; do not reinstall Docker on a working NVR:

```sh
sudo apt-get update
sudo apt-get install -y git curl ca-certificates openssl nano
sudo docker version
sudo docker compose version
sudo docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
sudo systemctl status mosquitto --no-pager
```

The last command may say the service does not exist; that is fine. Look for an
existing broker such as a `mosquitto` container or a Home Assistant MQTT integration.
In Frigate's configuration editor, inspect the `mqtt:` section. A configured `host`
identifies the broker Frigate already uses. Reuse a working broker instead of adding
another; obtain its reachable address, port, and subscription credentials.

Find your Frigate Compose directory and mounted configuration if needed. Substitute
the actual container name from `docker ps`:

```sh
sudo docker inspect frigate --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
sudo docker inspect frigate --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
```

Record the Frigate version shown in its UI and the exact camera key, for example
`front_door`, from `cameras:` in its configuration. This client follows Frigate
0.16.2 API behavior; other versions need the real-image verification below.

If Docker works but `docker compose` is missing, install `docker-compose-plugin`
from the already configured Docker repository. Use your existing deployment method
for Frigate if it is managed by another tool.

### If you do not have a broker: install Mosquitto on the Frigate machine

This example installs Mosquitto as an Ubuntu service, separate from Frigate's Docker
deployment. First confirm there is no broker already listening on port 1883:

```sh
sudo ss -ltnp 'sport = :1883'
ip -br -4 address
```

Choose the home machine's LAN IPv4 address, for example `192.168.1.50`. Give it a
DHCP reservation in your router so it stays stable. Containers must be able to reach
that LAN address. The following commands belong on the home machine:

```sh
sudo apt-get install -y mosquitto mosquitto-clients
sudo mosquitto_passwd -c /etc/mosquitto/ses-passwords frigate
sudo mosquitto_passwd /etc/mosquitto/ses-passwords ses-monitor
sudo chown root:mosquitto /etc/mosquitto/ses-passwords
sudo chmod 640 /etc/mosquitto/ses-passwords
sudo nano /etc/mosquitto/conf.d/ses.conf
```

Choose separate passwords at the prompts. Use `-c` only for the initial password
file creation; repeating it erases existing users. Put this in `ses.conf`, replacing
the LAN IP:

```text
listener 1883 192.168.1.50
allow_anonymous false
password_file /etc/mosquitto/ses-passwords
acl_file /etc/mosquitto/ses-acl
```

Create the ACL:

```sh
sudo nano /etc/mosquitto/ses-acl
```

```text
user frigate
topic readwrite frigate/#

user ses-monitor
topic read frigate/reviews
```

The example uses topic prefix `frigate`; adjust both ACLs if yours differs. In
`/etc/mosquitto/mosquitto.conf`, verify the existing `persistence true` setting
and writable persistence directory; Ubuntu's packaged configuration normally has
these. Enable persistence there if absent, without duplicating an existing setting.
Then start and inspect the broker:

```sh
sudo chown root:mosquitto /etc/mosquitto/ses-acl
sudo chmod 640 /etc/mosquitto/ses-acl
sudo systemctl enable mosquitto
sudo systemctl restart mosquitto
sudo systemctl status mosquitto --no-pager
sudo journalctl -u mosquitto -n 40 --no-pager
```

If the home firewall blocks access, allow TCP 1883 to this LAN IP from Frigate's
and the monitor's actual source networks (including their Docker bridge subnets).
Do not open it on the router or VPS. Port 1883 is unencrypted; this example assumes
a trusted local network. For an untrusted network, configure broker TLS and the
monitor's `mqtt_tls`/`mqtt_ca_file` settings. See the official
[Mosquitto configuration](https://mosquitto.org/man/mosquitto-conf-5.html) and
[password tool](https://mosquitto.org/man/mosquitto_passwd-1.html) documentation.

### Connect Frigate to the broker

If MQTT is already working, preserve its host and credentials. Otherwise, merge
this into your existing Frigate configuration; do not replace your camera config:

```yaml
mqtt:
  enabled: true
  host: 192.168.1.50
  port: 1883
  topic_prefix: frigate
  client_id: frigate
  user: "{FRIGATE_MQTT_USER}"
  password: "{FRIGATE_MQTT_PASSWORD}"
  qos: 1
```

In your existing **Frigate Compose file**, add these to the Frigate service's
`environment` mapping, merging with any existing entries:

```yaml
environment:
  FRIGATE_MQTT_USER: frigate
  FRIGATE_MQTT_PASSWORD: ${FRIGATE_MQTT_PASSWORD}
```

In that Compose directory's `.env`, set the password chosen for the broker's
`frigate` user. Quote literal values with single quotes if they contain `$` or `#`:

```dotenv
FRIGATE_MQTT_PASSWORD='YOUR_FRIGATE_BROKER_PASSWORD'
```

Protect that file with `chmod 600 .env`. Back up Frigate's existing config first,
validate it using Frigate's editor, then recreate its service using its existing
Compose project, for example `sudo docker compose up -d frigate` from its directory.
This briefly interrupts Frigate. Use the actual service name if different. Check
its logs for MQTT connection errors and confirm the cameras still work.

Frigate 0.16.2 defaults to MQTT QoS 0; `qos: 1` allows broker replay while the
monitor is offline, when combined with broker persistence and an established
monitor subscription. Verify the option on your installed version. There is no
history backfill for notices the broker never received or retained. See
[Frigate's MQTT configuration source](https://github.com/blakeblackshear/frigate/blob/v0.16.2/frigate/config/mqtt.py)
and [review message documentation](https://docs.frigate.video/integrations/mqtt/#frigatereviews).

The monitor selects reviews with severity `alert` containing `person`. Verify in
Frigate that the first camera detects people and produces person alerts under its
existing zone rules. See [Frigate review settings](https://docs.frigate.video/configuration/review/).

## 5. Clone and configure the monitor on the home machine

On the home machine, as your normal SSH user:

```sh
mkdir -p ~/services
cd ~/services
git clone https://github.com/joevenzon/simple_event_service.git
cd simple_event_service
git rev-parse HEAD
cd clients/frigate_monitor
cp .env.example .env
chmod 600 .env
cp config.example.yaml config.yaml
chmod 644 config.yaml
nano .env
```

Compare the commit with the VPS checkout. If they differ, check out the VPS commit
with `git checkout COMMIT_ID` before building. That creates a detached checkout;
return to `main` before using the update commands later.

Set `SES_CLIENT_TOKEN` to the token printed by the VPS. For the Mosquitto example,
set `MQTT_USERNAME=ses-monitor` and `MQTT_PASSWORD` to that user's password. These
are different from the credentials Frigate uses to publish. Single-quote values
containing `$` or `#` in Compose `.env` files. Do not put actual secrets in YAML.

Choose one of the two Frigate API connection methods below.

### Option A: use Frigate's existing HTTPS endpoint

Use this if the monitor can reach a trusted HTTPS Frigate origin and you have a
Frigate viewer username/password. Set `FRIGATE_USERNAME` and `FRIGATE_PASSWORD`
in the monitor's `.env`. Edit `config.yaml` so it contains, for example:

```yaml
frigate:
  api_base_url: https://frigate.example.lan:8971
  api_auth: login
  username_env: FRIGATE_USERNAME
  password_env: FRIGATE_PASSWORD
  mqtt_host: 192.168.1.50
  mqtt_port: 1883
  mqtt_client_id: ses-frigate-home
  mqtt_topic: frigate/reviews
  mqtt_username_env: MQTT_USERNAME
  mqtt_password_env: MQTT_PASSWORD
  mqtt_tls: false
  camera_channels:
    front_door: frigate.front
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
  snapshot_retry_seconds: 2
```

If you chose a custom VPS HTTPS port in step 3, include it in `ses.host_url`, for
example `https://events.example.com:8443`.

Replace `front_door` with your exact Frigate camera key. This maps it to
`frigate.front.person`; `.person` is appended automatically. URLs must be origins,
with no `/api`, username, password, or additional path. Broker names such as
`mosquitto` resolve only if both containers share the relevant Docker network;
the LAN IP works for the Ubuntu service installed above. `localhost` inside the
monitor would mean the monitor container itself.

Frigate commonly uses a self-signed certificate. Accepting it in your browser does
not make the monitor trust it. TLS verification is mandatory. For a private CA,
mount the public CA certificate read-only in a local `compose.override.yaml`:

```yaml
services:
  frigate-monitor:
    volumes:
      - /home/YOUR_USER/certs/frigate-ca.pem:/config/frigate-ca.pem:ro
```

Add `ca_file: /config/frigate-ca.pem` under `frigate:` in `config.yaml`. Use a
certificate whose DNS name/IP matches `api_base_url`; adding a CA does not fix a
name mismatch. The certificate file must be readable by container UID 10001. Keep
private CA keys out of the container. If the current self-signed setup cannot meet
these requirements, use Option B. See [Frigate authentication](https://docs.frigate.video/configuration/authentication/).

### Option B: use Frigate's internal API on its existing Docker network

This avoids certificate setup when both applications run on this same machine.
Inspect the Frigate container's networks:

```sh
sudo docker inspect frigate --format '{{range $name, $network := .NetworkSettings.Networks}}{{println $name}}{{end}}'
```

Choose its existing private user-defined network, for example `frigate_default`.
If the container uses host networking or has no user-defined bridge network, this
option does not apply as written; use Option A with a trusted HTTPS endpoint.
In `clients/frigate_monitor/compose.override.yaml`, put:

```yaml
services:
  frigate-monitor:
    networks:
      - frigate_existing
networks:
  frigate_existing:
    external: true
    name: frigate_default
```

Use the real network name. Starting from Option A's full `config.yaml`, change:

```yaml
  api_base_url: http://frigate:5000
  api_auth: none
```

`frigate` must be the Frigate service's network alias/container name on that
network. Leave MQTT pointing at the broker's reachable LAN IP. You can remove
`FRIGATE_USERNAME` and `FRIGATE_PASSWORD` from `.env`; this method does not use them.
Only attach trusted containers to this network: the internal API is unauthenticated.
No host port 5000 mapping or router forwarding is needed.

For either option, `config.yaml` contains no secrets and is readable by the
container's non-root user; `.env` remains mode 600 and is read by Compose. Keep
local overrides/certificates out of commits. If you use an existing anonymous
broker, remove both `mqtt_username_env` and `mqtt_password_env` from the YAML.

## 6. Validate and start the home monitor

From `~/services/simple_event_service/clients/frigate_monitor`:

```sh
curl --fail-with-body https://events.example.com/healthz
sudo docker compose config --quiet
sudo docker compose build
sudo docker compose run --rm frigate-monitor ses-frigate-monitor --config /config/config.yaml validate
sudo docker compose up -d
sudo docker compose ps
sudo docker compose logs --tail=80 frigate-monitor
sudo docker compose exec frigate-monitor ses-frigate-monitor --config /config/config.yaml status
```

`Configuration valid` checks YAML only; it does not prove connections or credentials.
Within roughly a minute, open the VPS `/clients` page and refresh it. Expect a
fresh heartbeat for `frigate-home`, `mqtt_connected: true`, and
`frigate_api_connected: true` in its health metadata. The local `status` command
reports outbox counts; the VPS heartbeat contains connection details.

## 7. Run the first real camera test

1. Walk in front of the mapped camera and check that Frigate itself creates a
   **person alert**, not just a motion detection. Wait for a new review if activity
   is already part of an existing review.
2. Open the SES event list. Expect one `frigate.front.person` event per review,
   typically within seconds. Open its detail page and check the camera/image.
3. Repeated Frigate updates to that review should leave one SES event. Multiple
   separate reviews legitimately create multiple events.
4. Restart the monitor with `sudo docker compose restart frigate-monitor` on the
   home machine. Verify the heartbeat returns and the event remains in SES.
5. During a convenient test window, stop the VPS `host` service using
   `sudo docker compose --project-directory deploy stop host` from the VPS repo.
   Trigger a new review; verify the home outbox `queue_size` increases. Restart
   the monitor once while the event is pending. Then restore the VPS host with
   `sudo docker compose --project-directory deploy up -d host`. The queued event
   should eventually arrive and the queue drain; retry backoff may delay this.
6. Restart the VPS host and confirm your login, client, and stored events persist.
   Then add other camera mappings and repeat the image test.

An event with `metadata.snapshot_available=false` means the monitor uploaded a text
fallback after failing to retrieve an image. Check API trust/login, review media,
and snapshot availability before expanding the deployment. One event per review
means later images do not replace an already frozen event.

## 8. Optional phone notifications

On the VPS, generate another random value with `openssl rand -hex 32`, set it as
`NTFY_TOPIC` in `deploy/.env`, and subscribe to that exact topic on `ntfy.sh` in
the ntfy phone app. A random public topic is not authentication; SES sends text
and a link to the private event page, never camera images to the relay.

From the VPS repository root:

```sh
sudo docker compose --project-directory deploy exec host ses add-notification-rule frigate.front.person --min-severity warning --ntfy-priority 4
sudo docker compose --project-directory deploy exec host ses list-notification-rules
sudo docker compose --project-directory deploy up -d host worker
sudo docker compose --project-directory deploy exec worker python -m app.worker --check
```

Add the rule once; use `list-notification-rules` before repeating setup. Trigger a
new person review, confirm `sent_to_ntfy` on its SES detail page, and verify receipt
on your phone. Tap it while signed out, sign in, and confirm the private image
opens. Repeat over cellular. The default cooldown allows one push per client/channel
per 60 seconds; other events remain in history. See the
[notification verification guide](operations.md#notifications-and-iphone-verification).

## 9. Updates, storage, and troubleshooting

Updating requires pulling source **and rebuilding**. On the VPS, from the repo root:

```sh
git status --short
git pull --ff-only origin main
sudo docker compose --project-directory deploy up -d --build
sudo docker compose --project-directory deploy ps
```

On the home machine:

```sh
cd ~/services/simple_event_service
git status --short
git pull --ff-only origin main
cd clients/frigate_monitor
sudo docker compose up -d --build
sudo docker compose logs --tail=50 frigate-monitor
```

If you pinned a detached commit earlier, use `git switch main` before pulling.
Resolve intentional tracked-file edits before pulling; do not overwrite them.
Local `.env`, `config.yaml`, and named volumes survive rebuilds. Keep any
`compose.override.yaml` local too. Do not re-create the admin/client on updates.

The VPS named volume stores SQLite and images; the home's named volume stores the
durable outbox and duplicate receipts. Keep the Compose project names/directories
stable so they continue using the same volumes. Never use `down --volumes` or prune
these volumes if you want to keep that data. Before updates, back up the VPS's
database **and** images together; a stopped-service volume backup is one option.
Do not copy only a live SQLite main file and omit its WAL. Save private configuration
and credentials separately. See [recovery notes](operations.md#migration-and-recovery-notes).

Watch disk space on both machines with `df -h` and `sudo docker system df`. Host
history expires after seven days by default; the home outbox has no automatic
size cap/pruning. Check logs and delivery failures during the first few days.

| Symptom | First checks |
| --- | --- |
| Public HTTPS does not work | DNS A/AAAA, provider firewall, port 80 or the chosen HTTPS port already occupied, Caddy logs. |
| Host or worker unhealthy | VPS logs, actual generated session secret, writable data volume, disk space. |
| No home heartbeat | Monitor logs, SES URL/token, outbound HTTPS, container restarting. |
| MQTT disconnected | Broker address, listener/firewall, credentials, ACL, custom topic prefix, duplicate MQTT client ID. |
| Frigate API disconnected | Container DNS/network, API port/auth mode, viewer credentials, TLS CA/name match. |
| Connected but no events | Exact camera key, `alert` severity, `person` label, correct `/reviews` topic, Frigate zone rules. |
| Event has no image | Frigate API access, available review thumbnail/person snapshot, actual Frigate version. |
| Failed outbox jobs | Fix token/scope/configuration first; then stop, requeue, and restart as below. |
| No phone push | Topic subscription, explicit rule, worker running, event notification state, cooldown. |

To retry failed uploads after fixing the underlying problem, run at home from
`clients/frigate_monitor`:

```sh
sudo docker compose stop frigate-monitor
sudo docker compose run --rm frigate-monitor ses-frigate-monitor --config /config/config.yaml retry-failed
sudo docker compose up -d
```

Network failures retry automatically. Authentication and other permanent HTTP errors
retain payloads as failed jobs and need this explicit retry. Further details are in
[monitor operations](frigate-monitor.md#delivery-and-recovery) and
[host operations](operations.md).
