"""Exercise the independent monitor with synthetic MQTT, Frigate, and SES transports."""

import io
import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import paho.mqtt.client as mqtt
import pytest
import yaml
from PIL import Image
from pydantic import SecretStr, ValidationError
from test_notifications import command

from app.schemas import EventInput
from app.worker import process_once as deliver_once
from frigate_monitor.config import Settings, load_settings
from frigate_monitor.frigate_api import FrigateAPI, jpeg_image
from frigate_monitor.http import HTTPFailure
from frigate_monitor.main import main, process_once
from frigate_monitor.mqtt_handler import MQTTMonitor, parse_review
from frigate_monitor.outbox import Outbox
from frigate_monitor.ses_api import SESAPI, UploadResult, retry_delay


@pytest.fixture
def monitor_settings(tmp_path):
    return Settings.model_validate(
        {
            "frigate": {
                "api_base_url": "https://frigate.test:8971",
                "mqtt_host": "mqtt.test",
                "camera_channels": {
                    "wyze_front": "frigate.front",
                    "wyze_back": "frigate.back",
                    "wyze_garage": "frigate.garage",
                },
            },
            "ses": {
                "host_url": "https://ses.test",
                "outbox_path": str(tmp_path / "outbox.sqlite3"),
            },
        }
    )


@pytest.fixture
def review():
    # Shape follows Frigate's MQTT review contract; unknown release fields are tolerated.
    return {
        "type": "new",
        "before": {},
        "after": {
            "id": "1792350000.123456-review",
            "camera": "wyze_front",
            "severity": "alert",
            "start_time": 1792350000.123456,
            "end_time": None,
            "thumb_path": "/media/frigate/clips/review/thumb-wyze_front-review.webp",
            "data": {"objects": ["person", "car"], "detections": ["object-car", "object-person"]},
        },
    }


def parsed(review, settings):
    return parse_review(json.dumps(review).encode(), settings)


def image_bytes(format="JPEG"):
    output = io.BytesIO()
    Image.new("RGB", (100, 60), "blue").save(output, format=format)
    return output.getvalue()


def row_for(outbox, review_id):
    with outbox.connect() as db:
        return db.execute("SELECT * FROM outbox WHERE review_id = ?", (review_id,)).fetchone()


@pytest.mark.parametrize("kind", ["new", "update", "end"])
@pytest.mark.parametrize(
    "camera,channel",
    [
        ("wyze_front", "frigate.front.person"),
        ("wyze_back", "frigate.back.person"),
        ("wyze_garage", "frigate.garage.person"),
    ],
)
def test_review_contract(review, monitor_settings, kind, camera, channel):
    review["type"] = kind
    review["after"]["camera"] = camera
    event = parsed(review, monitor_settings)
    assert event["channel"] == channel
    assert event["event_key"] == "frigate-review-" + review["after"]["id"]
    assert event["severity"] == "warning"
    assert EventInput.model_validate_json(json.dumps(event)).channel == channel


@pytest.mark.parametrize(
    "change",
    [
        {"severity": "detection"},
        {"data": {"objects": ["car"]}},
        {"camera": "unknown"},
        {"camera": "wyze_front.person"},
    ],
)
def test_nonqualifying_review_ignored(review, monitor_settings, change):
    review["after"].update(change)
    assert parsed(review, monitor_settings) is None


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"{}",
        b"[]",
        b"x" * 65537,
        b'{"type":"unknown","after":{}}',
    ],
)
def test_invalid_payload_rejected(monitor_settings, payload):
    with pytest.raises(ValueError):
        parse_review(payload, monitor_settings)


@pytest.mark.parametrize(
    "change",
    [
        {"start_time": -1},
        {"start_time": float("nan")},
        {"start_time": 1e100},
        {"id": "../outside"},
        {"data": {"objects": "person"}},
    ],
)
def test_invalid_review_fields(review, monitor_settings, change):
    review["after"].update(change)
    with pytest.raises((ValueError, OverflowError, OSError)):
        parsed(review, monitor_settings)


def test_upgrade_to_alert_duplicates_and_out_of_order_survive_restart(review, monitor_settings):
    outbox = Outbox(monitor_settings.ses.outbox_path)
    review["after"]["severity"] = "detection"
    assert parsed(review, monitor_settings) is None
    review["after"]["severity"] = "alert"
    event = parsed(review, monitor_settings)
    assert outbox.enqueue(event, monitor_settings.alerts, now=100)

    # Later review changes cannot change the already queued event's idempotency digest.
    review["type"] = "end"
    review["after"]["data"]["objects"].append("dog")
    restarted = Outbox(outbox.path)
    assert not restarted.enqueue(parsed(review, monitor_settings), monitor_settings.alerts, now=101)
    assert json.loads(restarted.due(102)["event_json"]) == event
    assert not restarted.enqueue(event, monitor_settings.alerts, now=99)


def test_outbox_lifetime_lock_and_schema(monitor_settings):
    outbox = Outbox(monitor_settings.ses.outbox_path)
    with (
        outbox.process_lock(),
        pytest.raises(RuntimeError, match="Another monitor"),
        Outbox(outbox.path).process_lock(),
    ):
        pytest.fail("Lock was not exclusive")
    with outbox.process_lock():
        pass
    with outbox.connect() as db:
        db.execute("PRAGMA user_version = 99")
    with pytest.raises(RuntimeError, match="schema"):
        Outbox(outbox.path)


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retry_preserves_frozen_image_and_recovers_after_restart(review, monitor_settings, status):
    outbox = Outbox(monitor_settings.ses.outbox_path)
    event = parsed(review, monitor_settings)
    outbox.enqueue(event, monitor_settings.alerts, now=100)
    frigate = Mock()
    frigate.snapshot.return_value = image_bytes()
    requests = []
    event_id = str(uuid4())

    def provider(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(status, headers={"Retry-After": "90"}, text="private error")
        return httpx.Response(200, json={"status": "accepted", "duplicate": True, "id": event_id})

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        ses = SESAPI(monitor_settings.ses, "test-secret", http)
        assert not process_once(outbox, monitor_settings, frigate, ses, now=101)
        assert process_once(outbox, monitor_settings, frigate, ses, now=102)
        frozen = outbox.due(102)
        assert process_once(outbox, monitor_settings, frigate, ses, now=102)
        failed = row_for(outbox, event["metadata"]["review_id"])
        assert failed["next_attempt_at"] >= 192
        assert failed["image"] == frozen["image"]
        assert failed["event_json"] == frozen["event_json"]
        assert "private error" not in failed["last_error"]

        restarted = Outbox(outbox.path)
        assert process_once(restarted, monitor_settings, frigate, ses, now=200)
        stored = row_for(restarted, event["metadata"]["review_id"])
        assert stored["state"] == "sent" and stored["host_event_id"] == event_id
        assert stored["image"] is None and stored["event_json"] is None
        assert not restarted.enqueue(event, monitor_settings.alerts, now=300)
        assert restarted.health()["queue_size"] == 0
        assert restarted.health()["last_successful_upload_at"]
    assert frigate.snapshot.call_count == 1
    for request in requests:
        assert request.url == "https://ses.test/api/v1/events"
        assert request.headers["Authorization"] == "Bearer test-secret"
        assert b'name="event"' in request.content and b'name="image"' in request.content
        assert frozen["event_json"].encode() in request.content
        assert frozen["image"] in request.content


def test_missing_snapshot_retries_then_sends_text(review, monitor_settings):
    outbox = Outbox(monitor_settings.ses.outbox_path)
    event = parsed(review, monitor_settings)
    outbox.enqueue(event, monitor_settings.alerts, now=100)
    frigate, ses = Mock(), Mock()
    frigate.snapshot.return_value = None
    ses.upload.return_value = UploadResult(accepted_id=str(uuid4()))
    assert process_once(outbox, monitor_settings, frigate, ses, now=102)
    assert outbox.due(103) is None
    assert process_once(outbox, monitor_settings, frigate, ses, now=115)
    frozen = outbox.due(115)
    assert frozen["image"] is None
    assert "Snapshot unavailable" in json.loads(frozen["event_json"])["message"]
    assert not json.loads(frozen["event_json"])["metadata"]["snapshot_available"]
    process_once(outbox, monitor_settings, frigate, ses, now=115)
    assert ses.upload.call_count == 1


def test_restart_after_expired_snapshot_window_sends_text_without_fetch(review, monitor_settings):
    event = parsed(review, monitor_settings)
    outbox = Outbox(monitor_settings.ses.outbox_path)
    outbox.enqueue(event, monitor_settings.alerts, now=100)
    frigate = Mock()
    process_once(Outbox(outbox.path), monitor_settings, frigate, Mock(), now=200)
    frigate.snapshot.assert_not_called()
    assert outbox.due(200)["state"] == "pending"


@pytest.mark.parametrize("status", [401, 403, 409, 413, 422, 302])
def test_permanent_ses_errors_preserve_payload_for_operator(review, monitor_settings, status):
    outbox = Outbox(monitor_settings.ses.outbox_path)
    event = parsed(review, monitor_settings)
    outbox.enqueue(event, monitor_settings.alerts, now=100)
    outbox.freeze(outbox.due(102), image_bytes(), 102)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as http:
        ses = SESAPI(monitor_settings.ses, "test-secret", http)
        process_once(outbox, monitor_settings, Mock(), ses, now=102)
    assert outbox.health()["failed_count"] == 1
    stored = row_for(outbox, event["metadata"]["review_id"])
    assert stored["image"] and stored["event_json"]
    assert outbox.due(1000) is None
    assert outbox.retry_failed() == 1
    assert outbox.due(1000)["state"] == "pending"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, json={}),
        httpx.Response(201, text="not JSON"),
        httpx.Response(201, json={"id": "bad", "status": "accepted", "duplicate": False}),
        httpx.Response(201, json={"id": str(uuid4()), "status": "accepted", "duplicate": 0}),
        httpx.Response(202, json={"id": str(uuid4()), "status": "accepted", "duplicate": False}),
        httpx.Response(200, content=b"x" * 65537),
    ],
)
def test_ambiguous_success_does_not_remove_outbox(review, monitor_settings, response):
    row = {"event_json": json.dumps(parsed(review, monitor_settings)), "image": None, "attempts": 0}
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as http:
        result = SESAPI(monitor_settings.ses, "test-secret", http).upload(row)
    assert not result.accepted_id and not result.permanent and result.delay > 0


def test_timeout_retries_without_leaking_url_or_body(review, monitor_settings):
    def provider(request):
        raise httpx.ReadTimeout("sensitive-url-and-token", request=request)

    row = {"event_json": json.dumps(parsed(review, monitor_settings)), "image": None, "attempts": 0}
    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        api = SESAPI(monitor_settings.ses, "secret", http)
        result = api.upload(row)
        assert not api.heartbeat({"queue_size": 1})
    assert not result.permanent and result.delay > 0
    assert "sensitive" not in result.error


def test_retry_after_date_and_invalid_values():
    now = datetime.now(UTC)
    assert retry_delay(1, "90", now) >= 90
    assert retry_delay(1, format_datetime(now + timedelta(seconds=120)), now) >= 119
    for value in ("bogus", "nan", "inf", "-100", "1e100"):
        assert 0 < retry_delay(1, value, now) <= 5
    assert 0 < retry_delay(1000000) <= 3600


def test_authenticated_review_thumbnail_renews_session(review, monitor_settings, monkeypatch):
    monkeypatch.setenv("FRIGATE_USERNAME", "viewer")
    monkeypatch.setenv("FRIGATE_PASSWORD", "private-password")
    calls = []

    def provider(request):
        calls.append(request)
        if request.url.path == "/api/login":
            assert json.loads(request.content) == {"user": "viewer", "password": "private-password"}
            return httpx.Response(200, headers={"Set-Cookie": "frigate_token=session; Path=/"})
        if "session" not in request.headers.get("cookie", ""):
            return httpx.Response(401)
        if request.url.path.startswith("/api/review/"):
            return httpx.Response(200, json=review["after"])
        if request.url.path.startswith("/clips/review/"):
            return httpx.Response(200, content=image_bytes("WEBP"))
        return httpx.Response(200, text="0.16.2-test")

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        api = FrigateAPI(monitor_settings.frigate, http)
        image = api.snapshot(parsed(review, monitor_settings))
        assert Image.open(io.BytesIO(image)).format == "JPEG"
        api.probe()
        assert api.connected and api.version == "0.16.2-test"
    assert sum(request.method == "POST" for request in calls) == 1
    assert all(request.url.host == "frigate.test" for request in calls)
    assert not any("Authorization" in request.headers for request in calls)


@pytest.mark.parametrize(
    "thumb",
    [
        "",
        "/media/frigate/clips/review/../../private.webp",
        "https://evil.test/private",
    ],
)
def test_snapshot_fallback_resolves_person_object_id(review, monitor_settings, thumb):
    calls = []
    review["after"]["thumb_path"] = thumb

    def provider(request):
        path = request.url.path
        calls.append(path)
        if path.startswith("/api/review/"):
            return httpx.Response(200, json=review["after"])
        if path == "/api/events/object-car":
            return httpx.Response(200, json={"label": "car", "camera": "wyze_front"})
        if path == "/api/events/object-person":
            return httpx.Response(200, json={"label": "person", "camera": "wyze_front"})
        if path == "/api/events/object-person/snapshot.jpg":
            return httpx.Response(200, content=image_bytes())
        pytest.fail(f"Unexpected endpoint {path}")

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        api = FrigateAPI(monitor_settings.frigate, http)
        assert api.snapshot(parsed(review, monitor_settings))
    assert not any(review["after"]["id"] in path for path in calls if "events" in path)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(404),
        httpx.Response(500),
        httpx.Response(200, json=[]),
        httpx.Response(200, content=b"bad-json"),
        httpx.Response(200, json={"id": "other"}),
        httpx.Response(302, headers={"Location": "https://evil.test"}),
    ],
)
def test_expired_or_invalid_review_has_no_image(review, monitor_settings, response):
    with httpx.Client(transport=httpx.MockTransport(lambda _: response)) as http:
        assert (
            FrigateAPI(monitor_settings.frigate, http).snapshot(parsed(review, monitor_settings))
            is None
        )


def test_image_validation_and_bounded_response(review, monitor_settings):
    for payload in (b"<html>sign in</html>", image_bytes("GIF"), b"bad-image"):
        with pytest.raises(HTTPFailure):
            jpeg_image(payload)

    def provider(request):
        if request.url.path.startswith("/api/review/"):
            return httpx.Response(200, json=review["after"] | {"data": {"detections": []}})
        return httpx.Response(200, content=b"x" * (5 * 1024 * 1024 + 1))

    with httpx.Client(transport=httpx.MockTransport(provider)) as http:
        assert (
            FrigateAPI(monitor_settings.frigate, http).snapshot(parsed(review, monitor_settings))
            is None
        )


def test_mqtt_durable_ack_poison_message_and_storage_failure(review, monitor_settings):
    outbox = Outbox(monitor_settings.ses.outbox_path)
    stop = threading.Event()
    monitor = MQTTMonitor(monitor_settings, outbox, stop)
    client = Mock()
    message = SimpleNamespace(
        topic="frigate/reviews", payload=json.dumps(review).encode(), mid=1, qos=1
    )
    monitor.on_message(client, None, message)
    assert outbox.health()["queue_size"] == 1
    client.ack.assert_called_once_with(1, 1)

    client.reset_mock()
    message.payload = b"invalid-json"
    monitor.on_message(client, None, message)
    client.ack.assert_called_once()
    client.reset_mock()
    message.payload = json.dumps(review).encode()
    monitor.outbox = Mock()
    monitor.outbox.enqueue.side_effect = sqlite3.OperationalError("disk full")
    monitor.on_message(client, None, message)
    client.ack.assert_not_called()
    assert stop.is_set() and monitor.fatal


def test_mqtt_reconnect_resubscribes_and_reports_subscription(monitor_settings):
    monitor = MQTTMonitor(monitor_settings, Mock(), threading.Event())
    client = Mock()
    client.subscribe.return_value = (mqtt.MQTT_ERR_SUCCESS, 1)
    ok, failure = SimpleNamespace(is_failure=False), SimpleNamespace(is_failure=True)
    for _ in range(2):
        monitor.on_connect(client, None, None, ok, None)
        assert not monitor.connected
        monitor.on_subscribe(client, None, 1, [ok], None)
        assert monitor.connected
        monitor.on_disconnect(client, None, None, failure, None)
        assert not monitor.connected
    assert client.subscribe.call_count == 2
    client.publish.assert_not_called()
    monitor.on_subscribe(client, None, 1, [failure], None)
    assert monitor.fatal and monitor.stop.is_set()


@pytest.mark.parametrize(
    "section,change",
    [
        ("ses", {"host_url": "http://public.test"}),
        ("ses", {"host_url": "https://token@ses.test"}),
        ("ses", {"host_url": "https://ses.test/prefix"}),
        ("frigate", {"api_base_url": "http://frigate.test"}),
        ("frigate", {"mqtt_topic": "frigate/#"}),
        ("frigate", {"camera_channels": {"wyze_front": "Frigate.front"}}),
        ("frigate", {"camera_channels": {"wyze_front": "a." * 11 + "a"}}),
        ("alerts", {"initial_snapshot_wait_seconds": 20, "max_snapshot_retry_seconds": 15}),
    ],
)
def test_invalid_configuration(monitor_settings, section, change):
    data = monitor_settings.model_dump(mode="json")
    data[section].update(change)
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


def test_cli_validate_offline_status_and_safe_errors(monitor_settings, tmp_path, capsys, caplog):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(monitor_settings.model_dump(mode="json")))
    assert load_settings(path) == monitor_settings
    main(["--config", str(path), "validate"])
    assert "Configuration valid" in capsys.readouterr().out
    main(["--config", str(path), "status"])
    assert json.loads(capsys.readouterr().out)["queue_size"] == 0
    path.write_text("ses: {host_url: 'https://private-token@ses.test'}")
    with pytest.raises(SystemExit):
        main(["--config", str(path), "validate"])
    assert "private-token" not in caplog.text


def test_client_upload_integrates_with_real_host_dedupe_and_heartbeat(
    review, monitor_settings, client, credentials, database, settings, human
):
    command(database, "add-notification-rule", "frigate.front.person")
    settings.ntfy_topic = SecretStr("synthetic-topic-" + "x" * 40)
    event = parsed(review, monitor_settings)
    outbox = Outbox(monitor_settings.ses.outbox_path)
    outbox.enqueue(event, monitor_settings.alerts, now=100)
    outbox.freeze(outbox.due(102), image_bytes(), 102)
    token = credentials["Authorization"].removeprefix("Bearer ")
    api = SESAPI(monitor_settings.ses, token, client)
    row = outbox.due(102)

    # Simulate a lost receipt: host stores the first request, then the client repeats it.
    first = api.upload(row)
    second = api.upload(row)
    assert first.accepted_id and second.accepted_id == first.accepted_id
    assert api.heartbeat(outbox.health() | {"mqtt_connected": False, "frigate_api_connected": True})
    with database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM images").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM notification_deliveries").fetchone()[0] == 1
        health = json.loads(db.execute("SELECT last_health_json FROM clients").fetchone()[0])
        assert health["mqtt_connected"] is False and health["queue_size"] == 1

    # Follow the frozen event through host delivery without sending a real notification.
    pushes = []

    def relay(request):
        pushes.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "synthetic-relay-id"})

    with httpx.Client(transport=httpx.MockTransport(relay)) as http:
        assert deliver_once(database, settings, http)
        assert not deliver_once(database, settings, http)
    assert len(pushes) == 1
    assert pushes[0]["click"].endswith("/events/" + first.accepted_id)
    assert "metadata" not in pushes[0] and "image" not in pushes[0]
    detail = human.get("/api/v1/events/" + first.accepted_id).json()
    assert detail["notification_state"] == "sent_to_ntfy"
    assert human.get(detail["image_url"]).status_code == 200
