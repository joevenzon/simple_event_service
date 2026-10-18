import io
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from app.auth import issue_token
from app.db import utc_now


def snapshot(format="PNG", color="red", exif=False):
    output = io.BytesIO()
    image = Image.new("RGB", (40, 30), color)
    metadata = Image.Exif()
    if exif:
        metadata[270] = "private location"
        metadata[274] = 6
    image.save(output, format=format, exif=metadata)
    return output.getvalue()


def submit_image(client, credentials, event, data=None, filename="../../snapshot.png"):
    return client.post(
        "/api/v1/events",
        headers=credentials,
        data={"event": json.dumps(event)},
        files={"image": (filename, snapshot() if data is None else data, "image/png")},
    )


def test_json_duplicate_conflict_and_persistent_data(client, credentials, event, database, human):
    first = client.post("/api/v1/events", json=event, headers=credentials)
    assert first.status_code == 201
    assert first.json()["duplicate"] is False
    repeat = client.post(
        "/api/v1/events", json=event | {"metadata": {}, "tags": []}, headers=credentials
    )
    assert repeat.status_code == 200
    assert repeat.json() == first.json() | {"duplicate": True}
    conflict = client.post(
        "/api/v1/events", json=event | {"message": "changed"}, headers=credentials
    )
    assert conflict.status_code == 409
    assert "Disk is healthy" not in conflict.text
    detail = human.get("/api/v1/events/" + first.json()["id"]).json()
    assert detail["channel"] == "backup.status.freespace"
    assert detail["notification_state"] == "not_selected"
    assert detail["image_url"] is None
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM notification_deliveries").fetchone()[0] == 0
        assert connection.execute("SELECT last_seen_at FROM clients").fetchone()[0] is not None


def test_concurrent_duplicates_create_one_event_and_image(client, credentials, event, database):
    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: submit_image(client, credentials, event), range(4)))
    assert sorted(response.status_code for response in responses) == [200, 200, 200, 201]
    assert len({response.json()["id"] for response in responses}) == 1
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM images").fetchone()[0] == 1


def test_key_is_scoped_to_client(client, credentials, event, database):
    token, digest = issue_token()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO clients(id, name, token_hash, created_at) VALUES ('other', 'Other', ?, ?)",
            (digest, utc_now()),
        )
    first = client.post("/api/v1/events", json=event, headers=credentials)
    other = client.post("/api/v1/events", json=event, headers={"Authorization": "Bearer " + token})
    assert first.status_code == other.status_code == 201
    assert first.json()["id"] != other.json()["id"]


@pytest.mark.parametrize(
    "headers", [{}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic x"}]
)
def test_invalid_or_missing_token(client, event, headers):
    assert client.post("/api/v1/events", json=event, headers=headers).status_code == 401


def test_revoked_and_rotated_token_denied(client, credentials, event, database):
    with database.transaction() as connection:
        connection.execute("UPDATE clients SET enabled = 0")
    assert client.post("/api/v1/events", json=event, headers=credentials).status_code == 401
    with database.transaction() as connection:
        connection.execute("UPDATE clients SET enabled = 1, token_hash = 'new-hash'")
    assert client.post("/api/v1/events", json=event, headers=credentials).status_code == 401


@pytest.mark.parametrize(
    "channel", ["Backup.status", "backup..status", "backup.*", "backup/status", "backup."]
)
def test_invalid_channel(client, credentials, event, channel):
    assert (
        client.post(
            "/api/v1/events", json=event | {"channel": channel}, headers=credentials
        ).status_code
        == 422
    )


@pytest.mark.parametrize("channel", ["backups.status", "server.disk", "frigates.front.person"])
def test_scope_boundaries(client, credentials, event, channel):
    assert (
        client.post(
            "/api/v1/events", json=event | {"channel": channel}, headers=credentials
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "change",
    [
        {"event_key": ""},
        {"title": " "},
        {"title": "x" * 201},
        {"message": "x" * 4001},
        {"severity": "alert"},
        {"notify": "true"},
        {"extra": "secret event text"},
        {"metadata": {"large": "x" * (16 * 1024)}},
        {"metadata": []},
        {"tags": ["UPPER"]},
        {"occurred_at": "2026-10-18T12:00:00"},
        {"occurred_at": 123},
    ],
)
def test_schema_validation(client, credentials, event, change):
    response = client.post("/api/v1/events", json=event | change, headers=credentials)
    assert response.status_code == 422
    assert "secret event text" not in response.text


def test_timezone_and_json_order_normalize_for_duplicates(client, credentials, event):
    first = event | {"occurred_at": "2026-10-18T15:00:00-07:00", "metadata": {"b": 2, "a": 1}}
    equivalent = event | {"occurred_at": "2026-10-18T22:00:00Z", "metadata": {"a": 1, "b": 2}}
    assert client.post("/api/v1/events", json=first, headers=credentials).status_code == 201
    assert client.post("/api/v1/events", json=equivalent, headers=credentials).status_code == 200


@pytest.mark.parametrize("format", ["JPEG", "PNG", "WEBP"])
def test_images_sanitized_and_private(human, credentials, event, settings, format):
    event = event | {"channel": "frigate.front.person"}
    response = submit_image(human, credentials, event, snapshot(format, exif=True))
    assert response.status_code == 201, response.text
    event_id = response.json()["id"]
    detail = human.get("/api/v1/events/" + event_id)
    image = human.get(detail.json()["image_url"])
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert image.headers["cache-control"] == "private, no-store"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert "inline" in image.headers["content-disposition"]
    with Image.open(io.BytesIO(image.content)) as decoded:
        assert not decoded.getexif()
        assert "private location" not in str(decoded.info)
        assert decoded.size == (30, 40)
    assert "disk_name" not in detail.text and str(settings.data_dir) not in detail.text
    assert len(list((settings.data_dir / "images").rglob("*.jpg"))) == 1
    assert human.get("/data/images/test.jpg").status_code == 404


def test_image_duplicate_ignores_filename_and_conflicts_on_content(
    client, credentials, event, settings
):
    assert submit_image(client, credentials, event).status_code == 201
    assert submit_image(client, credentials, event, filename="different.png").status_code == 200
    assert submit_image(client, credentials, event, snapshot(color="blue")).status_code == 409
    assert client.post("/api/v1/events", json=event, headers=credentials).status_code == 409
    assert len(list((settings.data_dir / "images").rglob("*.jpg"))) == 1


@pytest.mark.parametrize(
    "data", [b"", b"not an image", b"<svg onload='alert(1)'></svg>", b"GIF89a", snapshot()[:40]]
)
def test_malicious_or_truncated_image(client, credentials, event, data):
    assert submit_image(client, credentials, event, data).status_code == 422


def test_oversized_image_and_pixel_bomb(client, credentials, event, settings):
    assert (
        submit_image(client, credentials, event, b"x" * (settings.max_image_bytes + 1)).status_code
        == 413
    )
    settings.max_image_pixels = 100
    assert submit_image(client, credentials, event).status_code == 422


def test_database_failure_removes_image(client, credentials, event, database, settings):
    with database.transaction() as connection:
        connection.execute("""CREATE TRIGGER fail_event BEFORE INSERT ON events
            BEGIN SELECT RAISE(ABORT, 'private database error'); END""")
    response = submit_image(client, credentials, event)
    assert response.status_code == 503
    assert "private database error" not in response.text
    assert list((settings.data_dir / "images").rglob("*.jpg")) == []
    with database.connect() as connection:
        assert connection.execute("SELECT count(*) FROM images").fetchone()[0] == 0


def test_atomic_rename_failure_removes_temporary_image(
    client, credentials, event, settings, monkeypatch
):
    def fail(*args):
        raise OSError("private filesystem detail")

    monkeypatch.setattr("app.images.os.replace", fail)
    response = submit_image(client, credentials, event)
    assert response.status_code == 503
    assert "private filesystem detail" not in response.text
    assert not [path for path in (settings.data_dir / "images").rglob("*") if path.is_file()]


def test_rate_limit_survives_database_connections(client, credentials, event, settings, database):
    settings.ingest_per_minute = 1
    assert client.post("/api/v1/events", json=event, headers=credentials).status_code == 201
    limited = client.post("/api/v1/events", json=event, headers=credentials)
    assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
    with database.transaction() as connection:
        connection.execute("UPDATE rate_limits SET expires_at = 0")
    assert client.post("/api/v1/events", json=event, headers=credentials).status_code == 200


def test_request_limits_include_chunked_uploads(client, credentials):
    large = b"x" * (64 * 1024 + 1)
    for content in (large, iter([large[:40000], large[40000:]])):
        response = client.post(
            "/api/v1/events",
            content=content,
            headers=credentials | {"Content-Type": "application/json"},
        )
        assert response.status_code == 413


def test_invalid_json_and_wrong_content_type(client, credentials):
    assert (
        client.post(
            "/api/v1/events",
            content="{",
            headers=credentials | {"Content-Type": "application/json"},
        ).status_code
        == 422
    )
    assert client.post("/api/v1/events", content="data", headers=credentials).status_code == 415


def test_wrong_multipart_field(client, credentials, event):
    response = client.post(
        "/api/v1/events",
        headers=credentials,
        data={"metadata": json.dumps(event)},
        files={"image": ("snapshot.png", snapshot(), "image/png")},
    )
    assert response.status_code == 422
