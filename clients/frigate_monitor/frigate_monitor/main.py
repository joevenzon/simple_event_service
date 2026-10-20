"""Run the independent MQTT subscriber, durable image stage, SES sender, and health reports."""

import argparse
import json
import logging
import os
import signal
import sqlite3
import ssl
import threading
import time
from pathlib import Path

import httpx
import yaml
from pydantic import ValidationError

from frigate_monitor.config import load_settings, secret
from frigate_monitor.frigate_api import FrigateAPI
from frigate_monitor.mqtt_handler import MQTTMonitor
from frigate_monitor.outbox import Outbox
from frigate_monitor.ses_api import SESAPI

logger = logging.getLogger(__name__)


def process_once(outbox, settings, frigate, ses, now=None):
    """Advance one due job, freezing an image or text fallback before upload."""
    clock = time.time if now is None else lambda: now
    now = clock()
    row = outbox.due(now)
    if row is None:
        return False
    if row["state"] == "snapshot":
        image = None
        if now <= row["snapshot_deadline"]:
            image = frigate.snapshot(json.loads(row["event_json"]))
        if image is not None or now >= row["snapshot_deadline"]:
            outbox.freeze(row, image, now)
        else:
            outbox.snapshot_retry(
                row["review_id"],
                min(row["snapshot_deadline"], now + settings.alerts.snapshot_retry_seconds),
            )
        return True

    outbox.begin_upload(row["review_id"])
    result = ses.upload(row)
    outbox.finish(row["review_id"], result, clock())
    logger.info(
        "SES upload review_id=%s outcome=%s",
        row["review_id"],
        "accepted" if result.accepted_id else result.error,
    )
    return True


def run(settings, outbox):
    """Drain old work even during MQTT outages and shut down cleanly on SIGTERM/SIGINT."""
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    # Resolve required secrets before starting any network work.
    token = secret(settings.ses.token_env)
    if settings.frigate.api_auth == "login":
        secret(settings.frigate.username_env)
        secret(settings.frigate.password_env)
    mqtt = MQTTMonitor(settings, outbox, stop)
    context = ssl.create_default_context(cafile=settings.frigate.ca_file)
    with (
        httpx.Client(verify=context, trust_env=False) as frigate_http,
        httpx.Client(trust_env=False) as ses_http,
    ):
        frigate = FrigateAPI(settings.frigate, frigate_http)
        ses = SESAPI(settings.ses, token, ses_http)
        started, next_heartbeat = time.monotonic(), 0
        try:
            mqtt.start()
            while not stop.is_set():
                # Heartbeats run even when the queue is continuously busy.
                if time.monotonic() >= next_heartbeat:
                    frigate.probe()
                    health = outbox.health() | {
                        "mqtt_connected": mqtt.connected,
                        "frigate_api_connected": frigate.connected,
                        "frigate_version": frigate.version,
                        "last_mqtt_message_at": mqtt.last_message_at,
                        "uptime_seconds": int(time.monotonic() - started),
                    }
                    if not ses.heartbeat(health):
                        logger.warning("SES heartbeat failed")
                    next_heartbeat = time.monotonic() + settings.ses.heartbeat_seconds
                if not process_once(outbox, settings, frigate, ses):
                    stop.wait(0.5)
        finally:
            mqtt.close()
    if mqtt.fatal:
        raise RuntimeError("MQTT subscription or durable handoff failed")


def main(argv=None):
    """Validate config, inspect/requeue local jobs, or start one supervised monitor."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument(
        "command", choices=("run", "validate", "status", "retry-failed"), default="run", nargs="?"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    os.umask(0o077)
    try:
        settings = load_settings(args.config)
        if args.command == "validate":
            print("Configuration valid")
            return
        outbox = Outbox(settings.ses.outbox_path)
        if args.command == "status":
            print(json.dumps(outbox.health(), sort_keys=True))
            return
        with outbox.process_lock():
            if args.command == "retry-failed":
                print(f"Requeued {outbox.retry_failed()} failed uploads")
            else:
                run(settings, outbox)
    except ValidationError as error:
        # Pydantic/YAML errors can include input, so only print field locations and types.
        for item in error.errors(include_input=False, include_context=False, include_url=False):
            logger.error(
                "Invalid configuration: %s (%s)", ".".join(map(str, item["loc"])), item["type"]
            )
        raise SystemExit(2) from None
    except (ValueError, OSError, RuntimeError, sqlite3.Error, yaml.YAMLError):
        logger.error("Monitor stopped; check configuration, secret variables, and outbox storage")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
