"""Exercise host routing, atomic enqueue, idempotency, and durable storm suppression."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_ingest import submit_image

import app.db as db_module
from app.cli import build_parser, run
from app.db import Database
from app.migrations import MIGRATIONS
from app.notifications import NotificationRule, select_rule


def command(database, *arguments):
    return run(build_parser().parse_args(arguments), database)


def deliveries(database):
    with database.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM notification_deliveries ORDER BY created_at, id"
            )
        ]


def test_rule_cli_validation_order_and_removal(database):
    assert command(database, "list-notification-rules") == "[]"
    command(database, "add-notification-rule", "*", "--disabled", "--rule-priority", "100")
    command(database, "add-notification-rule", "backup.status", "--ntfy-priority", "4")
    command(
        database,
        "add-notification-rule",
        "backup.status",
        "--rule-priority",
        "1",
        "--sort-order",
        "9",
    )
    command(
        database,
        "add-notification-rule",
        "backup.status",
        "--rule-priority",
        "1",
        "--sort-order",
        "2",
        "--disabled",
    )
    command(
        database,
        "add-notification-rule",
        "backup.status",
        "--rule-priority",
        "1",
        "--sort-order",
        "2",
    )
    rules = json.loads(command(database, "list-notification-rules"))
    assert [r["id"] for r in rules] == [1, 2, 3, 4, 5]
    with database.connect() as connection:
        assert select_rule(connection, "backup.status.disk")["id"] == 4
        assert select_rule(connection, "backup.statuses")["id"] == 1
    command(database, "remove-notification-rule", "4")
    with database.connect() as connection:
        assert select_rule(connection, "backup.status")["id"] == 5
    with pytest.raises(ValueError, match="not found"):
        command(database, "remove-notification-rule", "99")


@pytest.mark.parametrize(
    "values",
    [
        {"channel_prefix": "Backup"},
        {"channel_prefix": "backup.*"},
        {"channel_prefix": "backup", "min_severity": "alert"},
        {"channel_prefix": "backup", "ntfy_priority": 0},
        {"channel_prefix": "backup", "ntfy_priority": 6},
        {"channel_prefix": "backup", "enabled": "false"},
    ],
)
def test_invalid_policies_are_rejected(values):
    with pytest.raises(ValidationError):
        NotificationRule(**values)


@pytest.mark.parametrize(
    ("severity", "expected"),
    [("debug", 1), ("info", 3), ("warning", 4), ("error", 5), ("critical", 5)],
)
def test_severity_priority_defaults(client, credentials, event, database, severity, expected):
    command(database, "add-notification-rule", "backup", "--min-severity", "debug")
    response = client.post(
        "/api/v1/events", headers=credentials, json=event | {"severity": severity}
    )
    assert response.status_code == 201
    assert deliveries(database)[0]["ntfy_priority"] == expected


def test_specific_rules_win_and_failed_threshold_does_not_fall_back(
    human, credentials, event, database
):
    command(database, "add-notification-rule", "*")
    command(database, "add-notification-rule", "backup", "--disabled")
    command(
        database,
        "add-notification-rule",
        "backup.status",
        "--min-severity",
        "warning",
        "--ntfy-priority",
        "2",
    )
    for index, (channel, severity, expected) in enumerate(
        [
            ("backup.status", "warning", "pending"),
            ("backup.status.freespace", "info", "not_selected"),
            ("backup.statuses", "critical", "not_selected"),
            ("frigate.front.person", "info", "pending"),
        ]
    ):
        response = human.post(
            "/api/v1/events",
            headers=credentials,
            json=event | {"event_key": str(index), "channel": channel, "severity": severity},
        )
        detail = human.get("/api/v1/events/" + response.json()["id"]).json()
        assert detail["notification_state"] == expected
    assert {row["ntfy_priority"] for row in deliveries(database)} == {2, 3}


def test_notify_hint_cannot_force_policy_and_false_opts_out(human, credentials, event, database):
    first = human.post("/api/v1/events", headers=credentials, json=event | {"notify": True})
    assert (
        human.get("/api/v1/events/" + first.json()["id"]).json()["notification_state"]
        == "not_selected"
    )
    command(database, "add-notification-rule", "backup")
    human.post(
        "/api/v1/events",
        headers=credentials,
        json=event | {"event_key": "opt-out", "notify": False},
    )
    assert deliveries(database) == []
    human.post("/api/v1/events", headers=credentials, json=event | {"event_key": "opt-in"})
    assert len(deliveries(database)) == 1


def test_concurrent_duplicate_submissions_queue_one_delivery(client, credentials, event, database):
    command(database, "add-notification-rule", "backup")
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda _: client.post("/api/v1/events", json=event, headers=credentials), range(4)
            )
        )
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
    assert len(deliveries(database)) == 1
    assert (
        client.post(
            "/api/v1/events", json=event | {"title": "changed"}, headers=credentials
        ).status_code
        == 409
    )
    assert len(deliveries(database)) == 1


def test_outbox_failure_rolls_back_event_image_and_cooldown(
    client, credentials, event, database, settings
):
    command(database, "add-notification-rule", "backup")
    with database.transaction() as connection:
        connection.execute("""CREATE TRIGGER reject_delivery
            BEFORE INSERT ON notification_deliveries
            BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
    assert submit_image(client, credentials, event).status_code == 503
    with database.connect() as connection:
        for table in ("events", "images", "notification_deliveries", "notification_cooldowns"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert not list((settings.data_dir / "images").rglob("*.jpg"))


def test_cooldown_survives_restart_and_is_scoped_to_client_channel(
    client, credentials, event, database, monkeypatch
):
    command(database, "add-notification-rule", "backup")
    base = datetime(2026, 10, 18, tzinfo=UTC)

    def submit(key, seconds, headers=credentials, channel=event["channel"]):
        now = (
            (base + timedelta(seconds=seconds))
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        monkeypatch.setattr("app.ingest.utc_now", lambda: now)
        assert (
            client.post(
                "/api/v1/events",
                headers=headers,
                json=event | {"event_key": key, "channel": channel},
            ).status_code
            == 201
        )

    submit("first", 0)
    # A separate Database instance observes the committed cooldown after restart.
    Database(database.path).initialize()
    submit("suppressed", 59)
    submit("boundary", 60)
    submit("another-channel", 61, channel="backup.health")
    token = command(database, "create-client", "second", "--allow-prefix", "backup").splitlines()[
        -1
    ]
    submit("another-client", 62, headers={"Authorization": "Bearer " + token})
    assert [row["state"] for row in deliveries(database)] == [
        "pending",
        "suppressed_cooldown",
        "pending",
        "pending",
        "pending",
    ]


def test_zero_cooldown_allows_distinct_events(client, credentials, event, database, settings):
    settings.notification_cooldown_seconds = 0
    command(database, "add-notification-rule", "backup")
    for index in range(2):
        assert (
            client.post(
                "/api/v1/events", headers=credentials, json=event | {"event_key": str(index)}
            ).status_code
            == 201
        )
    assert [row["state"] for row in deliveries(database)] == ["pending", "pending"]


def test_phase_two_upgrade_preserves_existing_events_and_explicit_rule_priority(
    settings, monkeypatch
):
    database = Database(settings.database_path)
    with monkeypatch.context() as patch:
        patch.setattr(db_module, "MIGRATIONS", MIGRATIONS[:2])
        database.initialize()
        with database.transaction() as connection:
            connection.execute(
                """INSERT INTO clients(id, name, token_hash, created_at)
                    VALUES ('old', 'Old', 'hash', 'now')"""
            )
            connection.execute("""INSERT INTO events(id, client_id, event_key, channel, severity,
                title, message, received_at, payload_digest)
                VALUES ('old-event', 'old', 'key', 'backup',
                'info', 'Title', 'Message', 'now', 'digest')""")
            connection.execute("""INSERT INTO notification_rules(id, channel_prefix, enabled,
                min_severity, ntfy_priority) VALUES (9, 'backup', 1, 'info', 4)""")
    database.initialize()
    with database.connect() as connection:
        assert connection.execute("SELECT id FROM events").fetchone()[0] == "old-event"
        assert connection.execute("SELECT token_hash FROM clients").fetchone()[0] == "hash"
        assert select_rule(connection, "backup")["ntfy_priority"] == 4
        assert connection.execute("SELECT count(*) FROM notification_deliveries").fetchone()[0] == 0
    command(database, "add-notification-rule", "frigate")
