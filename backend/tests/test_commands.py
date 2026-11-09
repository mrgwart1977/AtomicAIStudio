"""Unified Creative Command System: preview/commit/undo, concurrency,
idempotency — and proof that preview never launches work."""

from __future__ import annotations

from tests.conftest import make_project, wait_for_job


def test_preview_has_no_side_effects(client):
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/commands/preview",
        json={"command_type": "generate.image.mock", "payload": {"prompt": "a castle"}},
    )
    assert r.status_code == 200, r.text
    proposal = r.json()
    assert proposal["launches_generation"] is True
    assert "mock adapter" in proposal["warnings"][0]
    assert proposal["current_revision"] == 0
    assert proposal["estimated_cost"]["requires_approval"] is False

    # Preview created nothing.
    assert client.get(f"/api/projects/{p['id']}/jobs").json() == []
    assert client.get(f"/api/projects/{p['id']}/commands").json() == []
    assert client.get(f"/api/projects/{p['id']}").json()["revision"] == 0


def test_commit_generation_then_job_succeeds(client):
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={
            "command_type": "generate.image.mock",
            "payload": {"prompt": "mist over the harbour"},
            "expected_revision": 0,
            "idempotency_key": "cmd-gen-1",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["revision"] == 1
    job = wait_for_job(client, p["id"], body["job_id"])
    assert job["status"] == "succeeded"
    assert job["output_asset_id"]

    assets = client.get(f"/api/projects/{p['id']}/assets").json()
    assert len(assets) == 1
    assert assets[0]["kind"] == "image"

    # The produced file is real and served back.
    f = client.get(f"/api/projects/{p['id']}/assets/{assets[0]['id']}/file")
    assert f.status_code == 200
    assert f.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_idempotent_commit_replays_without_duplicates(client):
    p = make_project(client)
    req = {
        "command_type": "prompt.set_text",
        "payload": {"text": "same"},
        "expected_revision": 0,
        "idempotency_key": "cmd-idem-1",
    }
    r1 = client.post(f"/api/projects/{p['id']}/commands/commit", json=req)
    r2 = client.post(f"/api/projects/{p['id']}/commands/commit", json=req)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["command_id"] == r2.json()["command_id"]
    assert r2.json()["idempotent_replay"] is True
    assert len(client.get(f"/api/projects/{p['id']}/commands").json()) == 1


def test_revision_conflict_rejected(client):
    p = make_project(client)
    client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={
            "command_type": "prompt.set_text",
            "payload": {"text": "first"},
            "expected_revision": 0,
            "idempotency_key": "cmd-rev-1",
        },
    )
    r = client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={
            "command_type": "prompt.set_text",
            "payload": {"text": "stale write"},
            "expected_revision": 0,  # now stale; project is at 1
            "idempotency_key": "cmd-rev-2",
        },
    )
    assert r.status_code == 409
    assert client.get(f"/api/projects/{p['id']}/prompt-draft").json()["text"] == "first"


def test_unsupported_and_invalid_commands_rejected(client):
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/commands/preview",
        json={"command_type": "lora.train", "payload": {}},
    )
    assert r.status_code == 422
    r = client.post(
        f"/api/projects/{p['id']}/commands/preview",
        json={"command_type": "generate.image.mock", "payload": {"prompt": "  "}},
    )
    assert r.status_code == 422


def test_undo_prompt_text_and_no_undo_for_generation(client):
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={
            "command_type": "prompt.set_text",
            "payload": {"text": "first text"},
            "expected_revision": 0,
            "idempotency_key": "cmd-undo-1",
        },
    )
    cmd_id = r.json()["command_id"]
    client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={
            "command_type": "prompt.set_text",
            "payload": {"text": "second text"},
            "expected_revision": 1,
            "idempotency_key": "cmd-undo-2",
        },
    )
    r = client.post(f"/api/projects/{p['id']}/commands/{cmd_id}/undo")
    assert r.status_code == 200
    # Undo restored the before value captured in the transaction audit.
    assert client.get(f"/api/projects/{p['id']}/prompt-draft").json()["text"] == ""

    r = client.post(
        f"/api/projects/{p['id']}/commands/commit",
        json={
            "command_type": "generate.image.mock",
            "payload": {"prompt": "cannot unspend"},
            "expected_revision": 2,
            "idempotency_key": "cmd-undo-gen",
        },
    )
    gen_cmd = r.json()["command_id"]
    wait_for_job(client, p["id"], r.json()["job_id"])
    r = client.post(f"/api/projects/{p['id']}/commands/{gen_cmd}/undo")
    assert r.status_code == 409  # compute cannot be unspent
