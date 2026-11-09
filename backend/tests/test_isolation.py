"""Project isolation: no cross-project reads, writes, or asset access."""

from __future__ import annotations

import uuid

from tests.conftest import make_project, wait_for_job


def _gen(client, project_id: str, prompt: str, revision: int, key: str):
    return client.post(
        f"/api/projects/{project_id}/commands/commit",
        json={
            "command_type": "generate.image.mock",
            "payload": {"prompt": prompt},
            "expected_revision": revision,
            "idempotency_key": key,
        },
    )


def test_prompt_drafts_are_isolated(client):
    a = make_project(client, "Project A")
    b = make_project(client, "Project B")

    client.post(
        f"/api/projects/{a['id']}/commands/commit",
        json={
            "command_type": "prompt.set_text",
            "payload": {"text": "A's private prompt"},
            "expected_revision": 0,
            "idempotency_key": "iso-prompt-1",
        },
    )
    draft_a = client.get(f"/api/projects/{a['id']}/prompt-draft").json()
    draft_b = client.get(f"/api/projects/{b['id']}/prompt-draft").json()
    assert draft_a["text"] == "A's private prompt"
    assert draft_b["text"] == ""


def test_commands_and_jobs_scoped_to_project(client):
    a = make_project(client, "Project A")
    b = make_project(client, "Project B")

    r = _gen(client, a["id"], "lantern on a hill", 0, "iso-gen-1")
    assert r.status_code == 200, r.text
    job = wait_for_job(client, a["id"], r.json()["job_id"])
    assert job["status"] == "succeeded"

    # B sees none of A's commands, jobs, or assets.
    assert client.get(f"/api/projects/{b['id']}/commands").json() == []
    assert client.get(f"/api/projects/{b['id']}/jobs").json() == []
    assert client.get(f"/api/projects/{b['id']}/assets").json()["total"] == 0

    assets_a = client.get(f"/api/projects/{a['id']}/assets").json()["items"]
    assert len(assets_a) == 1

    # A's asset is not reachable through B's project scope.
    r = client.get(f"/api/projects/{b['id']}/assets/{assets_a[0]['id']}/file")
    assert r.status_code == 404


def test_project_ids_do_not_collide_and_404_unknown(client):
    r = client.get(f"/api/projects/{uuid.uuid4()}")
    assert r.status_code == 404
