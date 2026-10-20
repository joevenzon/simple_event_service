"""Parse review notifications and commit qualifying alerts before MQTT acknowledgment."""

import logging
import threading
from datetime import UTC, datetime
from typing import Annotated, Literal

import paho.mqtt.client as mqtt
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from frigate_monitor.config import Settings, secret

logger = logging.getLogger(__name__)


class ReviewData(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    objects: list[Annotated[str, Field(max_length=100)]] = Field(max_length=100)


class Review(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
    camera: str = Field(max_length=100)
    start_time: float = Field(ge=0, allow_inf_nan=False)
    severity: Literal["alert", "detection"]
    data: ReviewData

    @field_validator("start_time")
    @classmethod
    def supported_time(cls, value):
        datetime.fromtimestamp(value, UTC)
        return value


class ReviewMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    type: Literal["new", "update", "end"]
    after: Review


def parse_review(payload: bytes, settings: Settings) -> dict | None:
    """Produce an immutable generic event for the first qualifying person alert."""
    if len(payload) > 65536:
        raise ValueError("MQTT review exceeds 64 KiB")
    review = ReviewMessage.model_validate_json(payload).after
    if review.severity != "alert" or "person" not in review.data.objects:
        return None
    prefix = settings.frigate.camera_channels.get(review.camera)
    if prefix is None:
        return None

    return {
        "event_key": f"frigate-review-{review.id}",
        "channel": f"{prefix}.{settings.alerts.channel_suffix}",
        "severity": "warning",
        "title": f"Person detected — {review.camera}",
        "message": f"Frigate observed a person at {review.camera}.",
        "occurred_at": datetime.fromtimestamp(review.start_time, UTC).isoformat(),
        "tags": ["frigate", "person"],
        "metadata": {
            "camera": review.camera,
            "review_id": review.id,
            "object_types": sorted(set(review.data.objects)),
        },
    }


class MQTTMonitor:
    """Subscribe without publishing, with broker reconnect and durable local handoff."""

    def __init__(self, settings, outbox, stop: threading.Event):
        self.settings, self.outbox, self.stop = settings, outbox, stop
        self.connected = False
        self.last_message_at = None
        self.fatal = False
        config = settings.frigate
        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=config.mqtt_client_id,
            clean_session=False,
            protocol=mqtt.MQTTv311,
            manual_ack=True,
        )
        if config.mqtt_username_env:
            self.client.username_pw_set(
                secret(config.mqtt_username_env), secret(config.mqtt_password_env)
            )
        if config.mqtt_tls:
            self.client.tls_set(ca_certs=str(config.mqtt_ca_file) if config.mqtt_ca_file else None)
        self.client.reconnect_delay_set(min_delay=1, max_delay=60)
        self.client.on_connect = self.on_connect
        self.client.on_subscribe = self.on_subscribe
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    def on_connect(self, client, userdata, flags, reason_code, properties):
        """Restore the exact subscription on every successful broker connection."""
        self.connected = False
        if reason_code.is_failure:
            logger.warning("MQTT connection refused")
            return
        result, _ = client.subscribe(self.settings.frigate.mqtt_topic, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self.fatal = True
            self.stop.set()

    def on_subscribe(self, client, userdata, mid, reason_codes, properties):
        """Report connectivity only after the broker accepts the subscription."""
        self.connected = bool(reason_codes) and not any(code.is_failure for code in reason_codes)
        if not self.connected:
            logger.error("MQTT review subscription refused")
            self.fatal = True
            self.stop.set()

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False

    def on_message(self, client, userdata, message):
        """Acknowledge valid handoffs and ignored input; leave storage failures unacknowledged."""
        if self.fatal or message.topic != self.settings.frigate.mqtt_topic:
            return
        self.last_message_at = datetime.now(UTC).isoformat()
        try:
            event = parse_review(message.payload, self.settings)
        except (ValueError, ValidationError, OverflowError, OSError):
            logger.warning("Ignored invalid MQTT review")
            event = None

        # Storage exceptions stop the process so the supervisor can restart and redeliver.
        try:
            if event is not None:
                self.outbox.enqueue(event, self.settings.alerts)
        except Exception:
            logger.error("Cannot persist MQTT review; stopping before acknowledgment")
            self.fatal = True
            self.stop.set()
            return

        client.ack(message.mid, message.qos)

    def start(self):
        """Start asynchronous connection attempts so SES replay works while MQTT is offline."""
        config = self.settings.frigate
        self.client.connect_async(config.mqtt_host, config.mqtt_port, keepalive=60)
        self.client.loop_start()

    def close(self):
        self.client.disconnect()
        self.client.loop_stop()
