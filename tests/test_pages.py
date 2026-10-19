import io
import json
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from PIL import Image

from app.security import LOGIN_COOKIE


class Links(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.links = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


def image_event(client, credentials, event):
    output = io.BytesIO()
    Image.new("RGB", (1600, 1200), "navy").save(output, format="PNG")
    response = client.post(
        "/api/v1/events",
        headers=credentials,
        data={"event": json.dumps(event)},
        files={"image": ("snapshot.png", output.getvalue(), "image/png")},
    )
    assert response.status_code == 201
    return response.json()["id"]


@pytest.mark.parametrize("bearer", [False, True])
def test_pages_redirect_to_login_without_disclosing_events(client, credentials, event, bearer):
    event_id = image_event(client, credentials, event)
    headers = credentials if bearer else {}
    for path in ("/", "/events", f"/events/{event_id}", "/clients"):
        response = client.get(path, headers=headers, follow_redirects=False)
        assert response.status_code == 303
        destination = urlsplit(response.headers["location"])
        assert destination.path == "/login"
        assert parse_qs(destination.query)["next"] == [path]
        assert event["title"] not in response.text
        assert "Producer" not in response.text
        assert response.headers["cache-control"] == "private, no-store"
    for path in (f"/api/v1/events/{event_id}/image", f"/api/v1/events/{event_id}/thumbnail"):
        assert client.get(path, headers=headers).status_code == 401


def test_login_returns_to_original_filtered_page(client, credentials):
    target = "/events?channel=backup.status&severity=warning"
    response = client.get(target, follow_redirects=False)
    login_url = response.headers["location"]
    form = client.get(login_url)
    assert 'action="/login?next=' in form.text
    signed_in = client.post(
        login_url,
        data={
            "username": "admin",
            "password": "a sufficiently long password",
            "csrf_token": client.cookies[LOGIN_COOKIE],
        },
        follow_redirects=False,
    )
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == target
    assert client.get(target).status_code == 200


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/",
        "//evil.example/",
        "/\\evil.example",
        "/%2f%2fevil.example",
        "javascript:alert(1)",
        "/api/v1/events",
        "/events\r\nLocation: https://evil.example",
        "/events/" + "a" * 2050,
    ],
)
def test_login_rejects_external_or_unsafe_destinations(client, target):
    query = {"next": target}
    client.get("/login", params=query)
    response = client.post(
        "/login",
        params=query,
        data={
            "username": "admin",
            "password": "a sufficiently long password",
            "csrf_token": client.cookies[LOGIN_COOKIE],
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


@pytest.mark.parametrize("mode", ["expired", "revoked", "disabled", "logged_out"])
def test_invalid_sessions_lose_page_and_thumbnail_access(human, credentials, event, database, mode):
    event_id = image_event(human, credentials, event)
    assert human.get(f"/api/v1/events/{event_id}/thumbnail").status_code == 200
    if mode == "logged_out":
        token = human.get("/api/v1/session").json()["csrf_token"]
        human.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    else:
        with database.transaction() as connection:
            if mode == "expired":
                connection.execute("UPDATE sessions SET expires_at = '2000-01-01T00:00:00Z'")
            elif mode == "revoked":
                connection.execute("DELETE FROM sessions")
            else:
                connection.execute("UPDATE human_users SET disabled = 1")
    for path in ("/", "/events", f"/events/{event_id}", "/clients"):
        assert human.get(path, follow_redirects=False).status_code == 303
    assert human.get(f"/api/v1/events/{event_id}/thumbnail").status_code == 401
    assert human.get("/api/v1/clients").status_code == 401


def test_dashboard_details_and_clients_escape_private_text(human, credentials, event, database):
    unsafe = '<script>alert("private")</script>'
    payload = event | {
        "title": unsafe,
        "message": unsafe,
        "metadata": {"note": unsafe},
        "tags": ["backup"],
        "occurred_at": "2026-10-18T19:00:00Z",
    }
    event_id = image_event(human, credentials, payload)
    with database.transaction() as connection:
        connection.execute("UPDATE clients SET name = ?", (unsafe,))
    human.post("/api/v1/heartbeat", headers=credentials, json={"health": {"note": unsafe}})
    for path in ("/", "/events", f"/events/{event_id}", "/clients", "/account"):
        response = human.get(path)
        assert response.status_code == 200, response.text
        assert "<script>alert(" not in response.text
        assert response.headers["cache-control"] == "private, no-store"
    detail = human.get(f"/events/{event_id}").text
    assert "&lt;script&gt;" in detail
    assert "\\u003cscript\\u003e" in detail
    assert f"/api/v1/events/{event_id}/image" in detail
    assert "not selected" in detail
    links = [link["href"] for link in Links(detail).links]
    assert "/events?channel=backup" in links
    assert "/events?channel=backup.status" in links
    assert "/events?channel=backup.status.freespace" in links
    clients = human.get("/clients").text
    assert "healthy" in clients and "Latest health report" in clients
    assert "token_hash" not in clients


def test_feed_filters_dates_pagination_and_breadcrumbs(human, credentials, event, database):
    records = []
    for index, (channel, severity) in enumerate(
        [
            ("backup.status", "warning"),
            ("backup.status.disk", "warning"),
            ("backup.statuses", "warning"),
            ("backup.status.disk", "info"),
        ]
    ):
        payload = event | {
            "event_key": str(index),
            "title": f"Row {index}",
            "channel": channel,
            "severity": severity,
        }
        result = human.post("/api/v1/events", headers=credentials, json=payload)
        assert result.status_code == 201
        records.append(result.json()["id"])
    with database.transaction() as connection:
        connection.execute("UPDATE events SET received_at = '2026-10-18T23:59:59.000000Z'")
    params = {
        "channel": "backup.status",
        "client_id": "producer",
        "severity": "warning",
        "date_from": "2026-10-18",
        "date_through": "2026-10-18",
        "search": "Row",
        "limit": 1,
    }
    first = human.get("/events", params=params)
    assert first.status_code == 200
    assert "Row 2" not in first.text and "Row 3" not in first.text
    next_url = next(link["href"] for link in Links(first.text).links if link.get("rel") == "next")
    next_query = parse_qs(urlsplit(next_url).query)
    assert {key: next_query[key][0] for key in params} == {k: str(v) for k, v in params.items()}
    second = human.get(next_url)
    assert second.status_code == 200
    assert sum(f"/events/{event_id}" in first.text for event_id in records[:2]) == 1
    assert sum(f"/events/{event_id}" in second.text for event_id in records[:2]) == 1
    assert all(
        (f"/events/{event_id}" in first.text) != (f"/events/{event_id}" in second.text)
        for event_id in records[:2]
    )
    assert not any(link.get("rel") == "next" for link in Links(second.text).links)
    assert "Newest matching events" in second.text

    exact = human.get("/events", params={"channel": "backup.status", "subtree": "false"})
    assert "Row 0" in exact.text and "Row 1" not in exact.text
    before = human.get("/events", params={"date_through": "2026-10-17"})
    assert "No events found" in before.text
    after = human.get("/events", params={"date_from": "2026-10-19"})
    assert "No events found" in after.text
    empty = human.get(
        "/events",
        params={
            "channel": "",
            "severity": "",
            "client_id": "",
            "date_from": "",
            "date_through": "",
            "search": "",
        },
    )
    assert empty.status_code == 200 and "Row 3" in empty.text


@pytest.mark.parametrize(
    "params",
    [
        {"channel": "Backup"},
        {"channel": "<script>alert(1)</script>"},
        {"severity": "bad"},
        {"date_from": "wrong"},
        {"date_from": "2026-10-19", "date_through": "2026-10-18"},
        {"date_through": "9999-12-31"},
        {"cursor": "invalid"},
        {"limit": 101},
        {"subtree": "maybe"},
    ],
)
def test_invalid_filters_return_escaped_html(human, params):
    response = human.get("/events", params=params)
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("text/html")
    assert 'role="alert"' in response.text
    assert "<script>alert(1)</script>" not in response.text


def test_thumbnail_is_bounded_private_and_works_for_existing_images(
    human, credentials, event, settings, database
):
    event_id = image_event(human, credentials, event)
    assert not list((settings.data_dir / "thumbnails").rglob("*.jpg"))
    path = f"/api/v1/events/{event_id}/thumbnail"
    with ThreadPoolExecutor(max_workers=3) as executor:
        responses = list(executor.map(lambda _: human.get(path), range(3)))
    assert all(response.status_code == 200 for response in responses)
    response = responses[0]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "image/jpeg"
    assert "inline" in response.headers["content-disposition"]
    assert len({response.content for response in responses}) == 1
    with Image.open(io.BytesIO(response.content)) as image:
        assert image.size == (640, 480)
        assert not image.getexif()
    assert human.get(path).content == response.content
    previews = list((settings.data_dir / "thumbnails").rglob("*.jpg"))
    assert len(previews) == 1
    assert previews[0].stat().st_mode & 0o077 == 0
    assert not list((settings.data_dir / "thumbnails").rglob(".upload-*"))
    # Data directories remain unreachable even when a session is present.
    with database.connect() as connection:
        disk_name = connection.execute("SELECT disk_name FROM images").fetchone()[0]
    for root in ("/images/", "/thumbnails/", "/data/images/", "/static/images/"):
        assert human.get(root + disk_name).status_code == 404


def test_unknown_missing_and_deleted_images(human, credentials, event, settings):
    imageless = human.post("/api/v1/events", headers=credentials, json=event).json()["id"]
    assert human.get(f"/api/v1/events/{imageless}/thumbnail").status_code == 404
    assert human.get(f"/events/{uuid4()}").status_code == 404
    event_id = image_event(human, credentials, event | {"event_key": "with-image"})
    assert human.get(f"/api/v1/events/{event_id}/thumbnail").status_code == 200
    for path in (settings.data_dir / "images").rglob("*.jpg"):
        path.unlink()
    assert human.get(f"/api/v1/events/{event_id}/thumbnail").status_code == 404


def test_static_assets_and_empty_dashboard_are_available(client, human):
    for path in ("/static/app.css", "/static/app.js"):
        assert client.get(path).status_code == 200
    page = human.get("/")
    assert page.status_code == 200
    assert "0 events in retained history" in page.text
    assert "No events found" in page.text
    assert "never seen" in page.text
