"""Check real MQTT replay and monitor container restart on an isolated Docker network.

Run from the repository root after building ses-frigate-monitor:container-check:
    python tests/check_frigate_container.py
No ports are published; all HTTP fixtures and MQTT messages are synthetic.
"""

import argparse
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4


def inside(stage):
    """Exercise the built client with a real broker and loopback Frigate/SES fixtures."""
    import httpx
    import paho.mqtt.client as mqtt
    from PIL import Image

    from frigate_monitor.config import Settings
    from frigate_monitor.frigate_api import FrigateAPI
    from frigate_monitor.main import process_once
    from frigate_monitor.mqtt_handler import MQTTMonitor
    from frigate_monitor.outbox import Outbox
    from frigate_monitor.ses_api import SESAPI

    assert os.getuid() == 10001
    assert importlib.util.find_spec("app") is None
    os.umask(0o077)
    image = io.BytesIO()
    Image.new("RGB", (160, 100), "green").save(image, format="WEBP")
    stored, attempts, heartbeats = {}, [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body, headers=None):
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/api/version":
                self.reply(200, b"0.16.2-fixture")
            elif self.path.startswith("/api/review/"):
                review_id = self.path.rsplit("/", 1)[-1]
                self.reply(
                    200,
                    json.dumps(
                        {
                            "id": review_id,
                            "camera": "front",
                            "thumb_path": (
                                f"/media/frigate/clips/review/thumb-front-{review_id}.webp"
                            ),
                        }
                    ).encode(),
                )
            else:
                assert self.path.startswith("/clips/review/")
                self.reply(200, image.getvalue())

        def do_POST(self):
            assert self.headers["Authorization"] == "Bearer synthetic-token"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/api/v1/heartbeat":
                heartbeats.append(json.loads(body))
                self.reply(200, b'{"status":"accepted"}')
                return
            assert self.path == "/api/v1/events"
            parts = BytesParser(policy=policy.default).parsebytes(
                b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body
            )
            fields = {
                part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                for part in parts.iter_parts()
            }
            attempts.append(fields)
            if stage == "enqueue":
                Path("/data/frozen.json").write_bytes(fields["event"])
                Path("/data/frozen.jpg").write_bytes(fields["image"])
                self.reply(503, b"temporary failure", {"Retry-After": "90"})
                return

            event = json.loads(fields["event"])
            if event["metadata"]["review_id"] == "review-1":
                assert fields["event"] == Path("/data/frozen.json").read_bytes()
                assert fields["image"] == Path("/data/frozen.jpg").read_bytes()
            event_key = event["event_key"]
            duplicate = event_key in stored
            stored.setdefault(event_key, str(uuid4()))
            self.reply(
                200 if duplicate else 201,
                json.dumps(
                    {
                        "status": "accepted",
                        "duplicate": duplicate,
                        "id": stored[event_key],
                    }
                ).encode(),
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{server.server_port}"
    settings = Settings.model_validate(
        {
            "frigate": {
                "api_base_url": origin,
                "api_auth": "none",
                "mqtt_host": "broker",
                "mqtt_client_id": "isolated-monitor",
                "camera_channels": {"front": "frigate.front"},
            },
            "ses": {
                "host_url": origin,
                "allow_loopback_http": True,
                "outbox_path": "/data/outbox.sqlite3",
            },
        }
    )
    outbox = Outbox(settings.ses.outbox_path)
    monitor = MQTTMonitor(settings, outbox, threading.Event())
    publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)

    def wait_until(condition):
        deadline = time.monotonic() + 15
        while not condition():
            assert time.monotonic() < deadline, "Timed out waiting for isolated MQTT fixture"
            time.sleep(0.05)

    def publish(review_id, kind="new"):
        message = {
            "type": kind,
            "after": {
                "id": review_id,
                "camera": "front",
                "severity": "alert",
                "start_time": time.time(),
                "data": {"objects": ["person"]},
            },
        }
        info = publisher.publish("frigate/reviews", json.dumps(message), qos=1)
        info.wait_for_publish(timeout=5)
        assert info.is_published()

    with outbox.process_lock(), httpx.Client(trust_env=False) as http:
        try:
            monitor.start()
            wait_until(lambda: monitor.connected)
            publisher.connect("broker", 1883)
            publisher.loop_start()
            if stage == "enqueue":
                for kind in ("new", "update", "end"):
                    publish("review-1", kind)
                wait_until(lambda: outbox.health()["queue_size"] == 1)

                # Freeze the thumbnail and persist a 503 retry before this container exits.
                frigate = FrigateAPI(settings.frigate, http)
                ses = SESAPI(settings.ses, "synthetic-token", http)
                assert process_once(outbox, settings, frigate, ses, now=time.time() + 3)
                assert process_once(outbox, settings, frigate, ses, now=time.time() + 3)
                assert len(attempts) == 1 and outbox.health()["queue_size"] == 1
                monitor.close()
                publish("review-2")
                print("PASS: QoS 1 handoff, review dedupe, thumbnail, durable HTTP 503 retry")
            else:
                # The broker retained review-2 while the first container was stopped.
                wait_until(lambda: outbox.health()["queue_size"] == 2)
                frigate = FrigateAPI(settings.frigate, http)
                ses = SESAPI(settings.ses, "synthetic-token", http)
                now = time.time() + 3
                while process_once(outbox, settings, frigate, ses, now=now):
                    pass
                assert outbox.health()["queue_size"] == 1
                assert process_once(outbox, settings, frigate, ses, now=now + 100)
                assert outbox.health()["queue_size"] == 0
                assert len(stored) == 2 and len(attempts) == 2
                publish("review-1", "end")
                time.sleep(0.2)
                assert outbox.health()["queue_size"] == 0
                frigate.probe()
                assert ses.heartbeat(
                    outbox.health()
                    | {"mqtt_connected": monitor.connected, "frigate_version": frigate.version}
                )
                assert heartbeats[0]["health"]["mqtt_connected"]
                assert heartbeats[0]["health"]["frigate_version"] == "0.16.2-fixture"
                print("PASS: container restart, broker session replay, frozen bytes, heartbeat")
        finally:
            monitor.close()
            publisher.disconnect()
            publisher.loop_stop()
            server.shutdown()


def outside():
    """Create only disposable named resources and remove them even when a check fails."""
    name = "ses-frigate-check-" + uuid4().hex[:10]
    network, volume, broker = name + "-net", name + "-data", name + "-broker"

    def docker(*args, check=True):
        return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)

    with tempfile.TemporaryDirectory(prefix="ses-frigate-check-") as directory:
        config = Path(directory) / "mosquitto.conf"
        config.write_text("listener 1883\nallow_anonymous true\n")
        try:
            docker("network", "create", "--internal", network)
            docker("volume", "create", volume)
            docker(
                "run",
                "-d",
                "--name",
                broker,
                "--network",
                network,
                "--network-alias",
                "broker",
                "--mount",
                f"type=bind,src={config},dst=/mosquitto/config/mosquitto.conf,readonly",
                "eclipse-mosquitto:2",
            )
            for stage in ("enqueue", "replay"):
                result = docker(
                    "run",
                    "--rm",
                    "--name",
                    name + "-runner",
                    "--network",
                    network,
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    "--mount",
                    f"type=volume,src={volume},dst=/data",
                    "--mount",
                    f"type=bind,src={Path(__file__).resolve()},dst=/check.py,readonly",
                    "ses-frigate-monitor:container-check",
                    "python",
                    "/check.py",
                    "--inside",
                    stage,
                )
                print(result.stdout, end="")
        except subprocess.CalledProcessError as error:
            print(error.stdout, error.stderr)
            raise
        finally:
            docker("rm", "-f", name + "-runner", broker, check=False)
            docker("volume", "rm", volume, check=False)
            docker("network", "rm", network, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inside", choices=("enqueue", "replay"))
    args = parser.parse_args()
    inside(args.inside) if args.inside else outside()
