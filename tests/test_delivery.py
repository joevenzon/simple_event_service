"""Verify delivery reliability and privacy without contacting the public ntfy relay."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_ingest import submit_image
from test_notifications import command, deliveries

from app.db import Database, utc_now
from app.worker import (
    CLAIM_SECONDS,
    check_worker,
    claim_delivery,
    heartbeat,
    process_once,
    retry_delay,
    timestamp,
)


@pytest.fixture
def queued(client, credentials, event, database, settings):
    command(database, "add-notification-rule", "backup")
    settings.ntfy_topic = SecretStr("test-topic-" + "x" * 40)
    response = submit_image(
        client,
        credentials,
        event | {"metadata": {"private": "never-publish"}, "tags": ["private-tag"]},
    )
    assert response.status_code == 201
    return response.json()["id"]


def provider(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def make_due(database):
    with database.transaction() as connection:
        connection.execute(
            """UPDATE notification_deliveries SET next_attempt_at = '2000-01-01T00:00:00.000000Z'
                WHERE state = 'pending'"""
        )


def test_success_text_only_click_url_and_duplicate_does_not_republish(
    queued, database, settings, client, credentials, event, human
):
    requests = []

    def send(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "provider-id"})

    with provider(send) as relay:
        assert process_once(database, settings, relay)
        assert not process_once(database, settings, relay)
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST" and str(request.url) == "https://ntfy.sh/"
    body = json.loads(request.content)
    assert body == {
        "topic": settings.ntfy_topic.get_secret_value(),
        "title": event["title"],
        "message": event["channel"] + ": " + event["message"],
        "priority": 3,
        "tags": ["info"],
        "click": "https://localhost/events/" + queued,
    }
    assert "Authorization" not in request.headers and "Cookie" not in request.headers
    assert "never-publish" not in request.content.decode()
    assert "private-tag" not in request.content.decode()
    row = deliveries(database)[0]
    assert row["state"] == "sent_to_ntfy" and row["attempts"] == 1
    assert row["provider_response_id"] == "provider-id" and row["sent_at"]
    assert row["claim_token"] is None

    detail = human.get("/api/v1/events/" + queued).json()
    assert detail["notification_state"] == "sent_to_ntfy"
    assert detail["notification_delivery"]["attempts"] == 1
    assert detail["notification_delivery"]["next_attempt_at"] is None
    assert "claim_token" not in detail
    assert "Sent to ntfy" in human.get("/events/" + queued).text

    # The push click retains the existing login-and-return behavior for private images.
    client.cookies.clear()
    redirect = client.get("/events/" + queued, follow_redirects=False)
    assert redirect.status_code == 303 and queued in redirect.headers["location"]
    assert client.get("/api/v1/events/" + queued + "/image").status_code == 401
    repeated = submit_image(
        client,
        credentials,
        event | {"metadata": {"private": "never-publish"}, "tags": ["private-tag"]},
    )
    assert repeated.status_code == 200 and repeated.json()["id"] == queued
    assert deliveries(database)[0]["attempts"] == 1


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_http_failure_respects_retry_after_and_recovers_after_restart(
    queued, database, settings, status
):
    before = datetime.now(UTC)
    with provider(
        lambda _: httpx.Response(status, headers={"Retry-After": "90"}, text="private error")
    ) as relay:
        assert process_once(database, settings, relay)
        assert not process_once(database, settings, relay)
    row = deliveries(database)[0]
    assert row["state"] == "pending" and row["attempts"] == 1
    assert datetime.fromisoformat(row["next_attempt_at"]) >= before + timedelta(seconds=90)
    assert row["last_error"] == f"ntfy HTTP {status}"
    restarted = Database(database.path)
    restarted.initialize()
    make_due(restarted)
    with provider(lambda _: httpx.Response(200, json={"id": "recovered"})) as relay:
        assert process_once(restarted, settings, relay)
    assert deliveries(database)[0]["state"] == "sent_to_ntfy"
    assert deliveries(database)[0]["attempts"] == 2


@pytest.mark.parametrize("exception", [httpx.ConnectError, httpx.ReadTimeout])
def test_network_failure_is_sanitized_and_retried(queued, database, settings, exception, caplog):
    def fail(request):
        raise exception(
            "private credential " + settings.ntfy_topic.get_secret_value(), request=request
        )

    with provider(fail) as relay:
        assert process_once(database, settings, relay)
    row = deliveries(database)[0]
    assert row["state"] == "pending"
    assert row["last_error"] == "ntfy network request failed"
    assert settings.ntfy_topic.get_secret_value() not in caplog.text
    assert "private credential" not in caplog.text


@pytest.mark.parametrize("status", [301, 400, 401, 403, 404])
def test_permanent_errors_fail_without_following_redirects(queued, database, settings, status):
    calls = []

    def send(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"Location": "https://other.example/private"}, text="secret topic"
        )

    with provider(send) as relay:
        assert process_once(database, settings, relay)
        assert not process_once(database, settings, relay)
    assert len(calls) == 1
    row = deliveries(database)[0]
    assert row["state"] == "failed" and row["last_error"] == f"ntfy HTTP {status}"


def test_retry_limit_and_operator_requeue(queued, database, settings):
    settings.notification_max_attempts = 2
    with provider(lambda _: httpx.Response(503)) as relay:
        assert process_once(database, settings, relay)
        make_due(database)
        assert process_once(database, settings, relay)
        assert not process_once(database, settings, relay)
    assert deliveries(database)[0]["state"] == "failed"
    command(database, "requeue-delivery", queued)
    assert deliveries(database)[0]["attempts"] == 0
    with provider(lambda _: httpx.Response(200)) as relay:
        assert process_once(database, settings, relay)
    assert deliveries(database)[0]["state"] == "sent_to_ntfy"
    with pytest.raises(ValueError, match="not found"):
        command(database, "requeue-delivery", queued)
    with pytest.raises(ValueError, match="not found"):
        command(database, "requeue-delivery", str(uuid4()))


def test_concurrent_claims_reserve_one_attempt_and_recover_expired_claim(
    queued, database, settings
):
    now = utc_now()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: claim_delivery(database, settings, now), range(2)))
    job = next(claim for claim in claims if claim is not None)
    assert len([claim for claim in claims if claim is not None]) == 1
    assert deliveries(database)[0]["attempts"] == 1
    assert claim_delivery(Database(database.path), settings, now) is None
    later = timestamp(datetime.fromisoformat(now) + timedelta(seconds=CLAIM_SECONDS + 1))
    recovered = claim_delivery(Database(database.path), settings, later)
    assert recovered["delivery_id"] == job["delivery_id"]
    assert recovered["claim_token"] != job["claim_token"] and recovered["attempts"] == 2


def test_crash_during_final_attempt_does_not_exceed_budget(queued, database, settings):
    settings.notification_max_attempts = 1
    now = utc_now()
    assert claim_delivery(database, settings, now)["attempts"] == 1
    later = timestamp(datetime.fromisoformat(now) + timedelta(seconds=CLAIM_SECONDS + 1))
    assert claim_delivery(Database(database.path), settings, later) is None
    row = deliveries(database)[0]
    assert row["state"] == "failed" and row["attempts"] == 1


def test_crash_after_provider_acceptance_can_retry_same_event(
    queued, database, settings, monkeypatch
):
    original_transaction = database.transaction
    accepted = []

    def send(request):
        accepted.append(json.loads(request.content)["click"])

        # The next write simulates losing the process before the acceptance commit.
        def unavailable():
            raise sqlite3.OperationalError("commit unavailable")

        monkeypatch.setattr(database, "transaction", unavailable)
        return httpx.Response(200, json={"id": "accepted"})

    with provider(send) as relay, pytest.raises(sqlite3.OperationalError):
        process_once(database, settings, relay)
    monkeypatch.setattr(database, "transaction", original_transaction)
    assert deliveries(database)[0]["state"] == "pending"
    make_due(database)
    with provider(
        lambda request: accepted.append(json.loads(request.content)["click"]) or httpx.Response(200)
    ) as relay:
        assert process_once(Database(database.path), settings, relay)
    assert len(accepted) == 2 and len(set(accepted)) == 1
    assert deliveries(database)[0]["attempts"] == 2


def test_disabled_topic_leaves_jobs_pending_without_attempts(queued, database, settings):
    settings.ntfy_topic = None

    def unexpected(_):
        pytest.fail("disabled publishing contacted the relay")

    with provider(unexpected) as relay:
        assert not process_once(database, settings, relay)
    assert deliveries(database)[0]["attempts"] == 0
    check_worker(database)


def test_cooldown_suppression_never_contacts_relay(
    queued, client, credentials, event, database, settings
):
    client.post("/api/v1/events", headers=credentials, json=event | {"event_key": "cooldown"})
    calls = []
    with provider(lambda request: calls.append(request) or httpx.Response(200)) as relay:
        assert process_once(database, settings, relay)
        assert not process_once(database, settings, relay)
    assert len(calls) == 1
    assert {row["state"] for row in deliveries(database)} == {"sent_to_ntfy", "suppressed_cooldown"}


def test_rule_changes_do_not_rewrite_queued_policy(queued, database, settings):
    command(database, "remove-notification-rule", "1")
    command(database, "add-notification-rule", "*", "--disabled")
    with provider(
        lambda request: httpx.Response(
            200, json={"priority": json.loads(request.content)["priority"]}
        )
    ) as relay:
        assert process_once(database, settings, relay)
    assert deliveries(database)[0]["state"] == "sent_to_ntfy"
    assert deliveries(database)[0]["ntfy_priority"] == 3


def test_retry_after_date_and_invalid_values():
    now = datetime.now(UTC).replace(microsecond=0)
    assert retry_delay(1, format_datetime(now + timedelta(seconds=120), usegmt=True), now) == 120
    for invalid in (None, "bad value", "-1", "nan", "inf", "1e999"):
        assert 2.5 <= retry_delay(1, invalid, now) <= 5
    assert 1800 <= retry_delay(99, None, now) <= 3600


def test_worker_readiness_tracks_heartbeat_age(client, settings, database):
    settings.ntfy_topic = SecretStr("x" * 48)
    assert client.get("/readyz").status_code == 503
    assert client.get("/healthz").status_code == 200
    heartbeat(database, utc_now())
    assert client.get("/readyz").status_code == 200
    heartbeat(database, timestamp(datetime.now(UTC) - timedelta(seconds=CLAIM_SECONDS + 1)))
    assert client.get("/readyz").status_code == 503
    settings.ntfy_topic = None
    assert client.get("/readyz").status_code == 200


def test_utf8_message_is_bounded_and_only_selected_text_is_sent(
    client, credentials, event, database, settings
):
    command(database, "add-notification-rule", "backup")
    settings.ntfy_topic = SecretStr("x" * 48)
    assert (
        client.post(
            "/api/v1/events",
            headers=credentials,
            json=event | {"message": "😀" * 4000, "title": "é" * 200},
        ).status_code
        == 201
    )
    bodies = []
    with provider(
        lambda request: bodies.append(json.loads(request.content)) or httpx.Response(200)
    ) as relay:
        assert process_once(database, settings, relay)
    assert len(bodies[0]["message"].encode("utf-8")) <= 4000
    assert "�" not in bodies[0]["message"]
    assert bodies[0]["title"] == "é" * 200


def test_failed_delivery_is_visible_only_to_human_sessions(
    queued, human, database, settings, credentials
):
    with provider(lambda _: httpx.Response(403, text="private provider response")) as relay:
        assert process_once(database, settings, relay)
    detail = human.get("/api/v1/events/" + queued).json()
    assert detail["notification_state"] == "failed"
    assert detail["notification_delivery"]["last_error"] == "ntfy HTTP 403"
    assert detail["notification_delivery"]["next_attempt_at"] is None
    page = human.get("/events/" + queued)
    assert "ntfy HTTP 403" in page.text
    assert "private provider response" not in page.text
    assert human.get("/api/v1/events").json()["items"][0]["notification_state"] == "failed"
    human.cookies.clear()
    assert human.get("/api/v1/events/" + queued, headers=credentials).status_code == 401


def test_old_claimant_cannot_overwrite_a_reclaimed_job(queued, database, settings):
    def send(_):
        # Model an unusually delayed response arriving after a replacement worker claimed it.
        make_due(database)
        replacement = claim_delivery(Database(database.path), settings, utc_now())
        assert replacement is not None and replacement["attempts"] == 2
        return httpx.Response(200, json={"id": "late-acceptance"})

    with provider(send) as relay:
        assert process_once(database, settings, relay)
    row = deliveries(database)[0]
    assert row["state"] == "pending" and row["attempts"] == 2
    assert row["claim_token"] is not None and row["provider_response_id"] is None
