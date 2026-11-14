"""C4-C — storyboard generation state on the read model.

The board carries a per-shot `generation` block derived from durable
rows (generation_jobs.request_json + assets.provenance_json.request) —
backend-authoritative associations, never transient UI state.
"""
import io

import pytest
from PIL import Image

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import (
    GEN, _board, _char, _commit, _import, _preview, _refset, _rev,
    _scene, _shot)


def test_generation_block_present_empty(client, tmp_path):
    p = make_project(client)
    pid = p["id"]
    scene = _scene(client, pid)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    board = _board(client, pid)
    entry = next(x for s in board for x in s["shots"]
                 if x["shot"]["id"] == sh["shot"]["id"])
    gen = entry["generation"]
    assert gen["output_count"] == 0
    assert gen["latest_asset_id"] is None
    assert gen["latest_job_status"] is None


def test_generation_block_after_shot_generate(client, tmp_path):
    p = make_project(client)
    pid = p["id"]
    scene = _scene(client, pid)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="misty dawn")
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"]})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    board = _board(client, pid)
    entry = next(x for s in board for x in s["shots"]
                 if x["shot"]["id"] == sh["shot"]["id"])
    gen = entry["generation"]
    assert gen["latest_job_id"] == job["id"]
    assert gen["latest_job_status"] == "succeeded"
    assert gen["output_count"] >= 1
    assert gen["latest_asset_id"]
    # The reported asset is real and belongs to this project.
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    found = next(a for a in items if a["id"] == gen["latest_asset_id"])
    assert found["kind"] == "image"


def test_generation_block_failed_job(client, tmp_path):
    """A failed generation is surfaced as `failed` — the board must
    never present a dead job as pending or succeeded."""
    from app.db.models import GenerationJob
    p = make_project(client)
    pid = p["id"]
    scene = _scene(client, pid)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    with client.app.state.session_factory() as s:
        s.add(GenerationJob(
            id="deadbeef-0000-4000-8000-0000000000f1",
            project_id=pid, adapter_id="cloud.image.sim",
            kind="image", status="failed",
            idempotency_key="c4c-fake-failed-job",
            error="simulated provider rejected the request",
            request_json={"shot_generate": {"shot_id": sh["shot"]["id"]},
                          "shot_id": sh["shot"]["id"]}))
        s.commit()
    board = _board(client, pid)
    entry = next(x for s in board for x in s["shots"]
                 if x["shot"]["id"] == sh["shot"]["id"])
    assert entry["generation"]["latest_job_status"] == "failed"
    assert entry["generation"]["output_count"] == 0
    assert entry["generation"]["latest_asset_id"] is None


def test_generation_block_scoped_to_own_shot(client, tmp_path):
    """Shot A's outputs must not leak onto shot B's card."""
    p = make_project(client)
    pid = p["id"]
    scene = _scene(client, pid)
    a = _shot(client, pid, scene["scene"]["id"], "wide", prompt="alpha")
    b = _shot(client, pid, scene["scene"]["id"], "close", prompt="beta")
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": a["shot"]["id"]})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    board = _board(client, pid)
    ea = next(x for s in board for x in s["shots"]
              if x["shot"]["id"] == a["shot"]["id"])
    eb = next(x for s in board for x in s["shots"]
              if x["shot"]["id"] == b["shot"]["id"])
    assert ea["generation"]["output_count"] >= 1
    assert eb["generation"]["output_count"] == 0
    assert eb["generation"]["latest_job_status"] is None


def test_cross_project_generation_isolation(client, tmp_path):
    """Generation state is project-scoped: another project's jobs and
    assets never appear on this board."""
    p = make_project(client)
    p2 = make_project(client)
    scene = _scene(client, p["id"])
    sh = _shot(client, p["id"], scene["scene"]["id"], prompt="x")
    scene2 = _scene(client, p2["id"])
    sh2 = _shot(client, p2["id"], scene2["scene"]["id"], prompt="y")
    r = _commit(client, p2["id"], "shot.generate",
                {**GEN, "shot_id": sh2["shot"]["id"]})
    job = wait_for_job(client, p2["id"], r["job_id"])
    assert job["status"] == "succeeded", job
    board = _board(client, p["id"])
    entry = next(x for s in board for x in s["shots"]
                 if x["shot"]["id"] == sh["shot"]["id"])
    assert entry["generation"]["output_count"] == 0
    assert entry["generation"]["latest_job_status"] is None
