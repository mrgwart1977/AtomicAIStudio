"""Multi-project foundations: independent projects, lifecycle, persistence."""

from __future__ import annotations

from tests.conftest import make_project


def test_create_three_independent_projects(client):
    p1 = make_project(client, "The Lantern", "short_film")
    p2 = make_project(client, "Orbit Nine", "series")
    p3 = make_project(client, "Morning Tide", "music_video")

    projects = client.get("/api/projects").json()
    ids = {p["id"] for p in projects}
    assert {p1["id"], p2["id"], p3["id"]} <= ids
    assert len(projects) >= 3

    assert p1["template"] == "short_film"
    assert p2["template"] == "series"
    assert p1["revision"] == 0


def test_rename_archive_restore_via_commands(client):
    p = make_project(client, "Old Name")

    r = client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={
            "command_type": "project.rename",
            "payload": {"name": "New Name"},
            "expected_revision": 0,
            "idempotency_key": "rename-1",
        },
    )
    assert r.status_code == 200, r.text
    assert client.get(f"/api/projects/{p['id']}").json()["name"] == "New Name"

    r = client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={
            "command_type": "project.archive",
            "payload": {},
            "expected_revision": 1,
            "idempotency_key": "archive-1",
        },
    )
    assert r.status_code == 200
    assert client.get(f"/api/projects/{p['id']}").json()["status"] == "archived"

    # Mutations are blocked while archived.
    r = client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={
            "command_type": "prompt.set_text",
            "payload": {"text": "x"},
            "expected_revision": 2,
            "idempotency_key": "blocked-1",
        },
    )
    assert r.status_code == 409

    r = client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={
            "command_type": "project.restore",
            "payload": {},
            "expected_revision": 2,
            "idempotency_key": "restore-1",
        },
    )
    assert r.status_code == 200
    assert client.get(f"/api/projects/{p['id']}").json()["status"] == "active"


def test_auth_required(client):
    r = client.get("/api/projects", headers={"X-Atomic-Token": "wrong"})
    assert r.status_code == 401
    r = client.get("/health", headers={})
    assert r.status_code == 200


def test_persistence_across_recreate(settings, client):
    p = make_project(client, "Persistent")
    client2 = None
    from fastapi.testclient import TestClient
    from app.main import create_app

    with TestClient(create_app(settings)) as client2:
        client2.headers["X-Atomic-Token"] = "test-token"
        got = client2.get(f"/api/projects/{p['id']}")
        assert got.status_code == 200
        assert got.json()["name"] == "Persistent"
