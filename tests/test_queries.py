from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


def populate(client, credentials, event):
    channels = [
        "backup.status",
        "backup.status.freespace",
        "backup.statuses",
        "backup.health",
        "backup.disk_warning",
        "backup.disk_warning.full",
        "backup.disk-warning.full",
    ]
    ids = []
    for index, channel in enumerate(channels):
        response = client.post(
            "/api/v1/events",
            headers=credentials,
            json=event
            | {
                "event_key": str(index),
                "channel": channel,
                "severity": "warning" if index == 1 else "info",
                "title": "Needle" if index == 1 else "Disk status",
            },
        )
        assert response.status_code == 201
        ids.append(response.json()["id"])
    return ids


def test_filters_respect_boundaries_and_underscores(human, credentials, event):
    populate(human, credentials, event)

    def channels(**params):
        response = human.get("/api/v1/events", params=params)
        assert response.status_code == 200
        return {item["channel"] for item in response.json()["items"]}

    assert channels(channel="backup.status") == {"backup.status", "backup.status.freespace"}
    assert channels(channel="backup.status", subtree="false") == {"backup.status"}
    assert channels(channel="backup.disk_warning") == {
        "backup.disk_warning",
        "backup.disk_warning.full",
    }
    assert channels(severity="warning") == {"backup.status.freespace"}
    assert channels(search="needle") == {"backup.status.freespace"}
    assert channels(client_id="missing") == set()
    assert len(channels(client_id="producer")) == 7
    assert channels(received_before="2000-01-01T00:00:00Z") == set()
    assert len(channels(received_from="2000-01-01T00:00:00Z")) == 7


def test_cursor_pagination_same_timestamp_and_insert_between_pages(
    human, credentials, event, database
):
    ids = populate(human, credentials, event)
    with database.transaction() as connection:
        connection.execute("UPDATE events SET received_at = '2026-10-18T00:00:00.000000Z'")
    first = human.get("/api/v1/events", params={"limit": 2}).json()
    assert [row["id"] for row in first["items"]] == sorted(ids, reverse=True)[:2]
    seen = [row["id"] for row in first["items"]]
    newer = human.post("/api/v1/events", json=event | {"event_key": "newer"}, headers=credentials)
    cursor = first["next_cursor"]
    while cursor:
        page = human.get("/api/v1/events", params={"limit": 2, "cursor": cursor}).json()
        seen.extend(row["id"] for row in page["items"])
        cursor = page["next_cursor"]
    assert seen == sorted(ids, reverse=True)
    assert newer.json()["id"] not in seen


@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"cursor": "bogus"},
        {"cursor": "e30"},
        {"channel": "Backup"},
        {"subtree": "maybe"},
        {"severity": "alert"},
        {"received_from": "2026-10-18"},
        {"received_from": "2026-10-19T00:00:00Z", "received_before": "2026-10-18T00:00:00Z"},
    ],
)
def test_invalid_query(human, params):
    assert human.get("/api/v1/events", params=params).status_code == 422


def test_unknown_and_imageless_events(human, credentials, event):
    event_id = human.post("/api/v1/events", json=event, headers=credentials).json()["id"]
    assert human.get(f"/api/v1/events/{event_id}/image").status_code == 404
    assert human.get(f"/api/v1/events/{uuid4()}").status_code == 404


def test_history_and_session_persist_across_restart(human, credentials, event, settings):
    response = human.post("/api/v1/events", json=event, headers=credentials)
    with TestClient(create_app(settings), base_url="https://testserver") as restarted:
        restarted.cookies.update(human.cookies)
        assert restarted.get("/api/v1/events/" + response.json()["id"]).status_code == 200
        assert restarted.post("/api/v1/events", json=event, headers=credentials).status_code == 200


def test_openapi_contract_and_all_local_references(human):
    response = human.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    ingest = schema["paths"]["/api/v1/events"]["post"]
    assert ingest["security"] == [{"ClientBearer": []}]
    assert set(ingest["requestBody"]["content"]) == {"application/json", "multipart/form-data"}
    assert schema["paths"]["/api/v1/events"]["get"]["security"] == [{"HumanSession": []}]

    def check_refs(value):
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                assert ref.startswith("#/")
                target = schema
                for key in ref[2:].split("/"):
                    target = target[key]
            for child in value.values():
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
                check_refs(child)

    check_refs(schema)
    human.cookies.clear()
    assert human.get("/api/v1/openapi.json").status_code == 401
