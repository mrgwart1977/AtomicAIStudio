"""C5-D — API-level First Film walkthrough.

Drives the whole guided workflow through real commands — exactly the
calls the wizard makes: project settings (wizard-state persistence),
character → scene → shot → import video → timeline → arrange/trim →
export. Layer-3 tests use real synthesized media + the packaged
ffmpeg, skipping cleanly when the binaries aren't staged.
"""
import shutil
from pathlib import Path

import pytest

from app.domain.media import binaries, engine
from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_c5a_timeline import _tl, _detail, _insert
from tests.test_c5b_export import _synth


@pytest.fixture()
def media_tools():
    if binaries.ffmpeg() is None or binaries.ffprobe() is None:
        pytest.skip("ffmpeg/ffprobe not resolvable — run scripts/fetch-ffmpeg.ps1")


@pytest.fixture()
def dev_ffmpeg() -> str:
    if (which := shutil.which("ffmpeg")):
        return which
    tool = binaries.ffmpeg()
    if tool is not None:
        return tool["path"]
    pytest.skip("no ffmpeg available to synthesize fixtures")


def _import_video(client, pid, path: Path, name: str):
    r = _commit(client, pid, "asset.import",
                {"source_path": str(path), "name": name, "kind": "video"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in items if a["name"] == name)


def test_wizard_state_persists_via_update_settings(client):
    """Wizard progress is project-scoped settings — survives a fresh
    read exactly like a restart."""
    pid = make_project(client)["id"]
    wizard = {"version": 1, "step": 3, "furthest": 4, "scene_id": "x"}
    _commit(client, pid, "project.update_settings",
            {"settings": {"first_film": wizard}})
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["settings_json"]["first_film"]["step"] == 3
    # merge semantics: an unrelated settings key is preserved
    _commit(client, pid, "project.update_settings",
            {"settings": {"unrelated": True}})
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["settings_json"]["first_film"]["step"] == 3
    assert proj["settings_json"]["unrelated"] is True


def test_entities_created_by_wizard_are_real(client):
    """Characters, scene, shots the wizard commits are ordinary rows —
    visible to the storyboard and entity endpoints, not a parallel model."""
    pid = make_project(client)["id"]
    _commit(client, pid, "character.create",
            {"name": "Ada", "summary": "red jacket", "data": {}})
    chars = client.get(f"/api/projects/{pid}/character").json()
    assert chars[0]["name"] == "Ada"
    _commit(client, pid, "scene.create", {"title": "Rooftop"})
    _commit(client, pid, "scene.create", {"title": "Kitchen"})
    board = client.get(f"/api/projects/{pid}/storyboard").json()
    titles = [s["scene"]["title"] for s in board]
    assert "Rooftop" in titles and "Kitchen" in titles
    scene_id = next(s["scene"]["id"] for s in board
                    if s["scene"]["title"] == "Rooftop")
    _commit(client, pid, "shot.create",
            {"scene_id": scene_id, "title": "wide", "prompt": "rain"})
    board = client.get(f"/api/projects/{pid}/storyboard").json()
    sc = next(s for s in board if s["scene"]["id"] == scene_id)
    assert sc["shots"][0]["shot"]["title"] == "wide"


@pytest.mark.usefixtures("media_tools")
def test_full_imported_media_first_film(client, dev_ffmpeg, tmp_path):
    """End-to-end first film on imported clips — no model needed:
    settings → scene → shots → import → timeline → trim → export →
    output asset, with wizard state persisting alongside."""
    pid = make_project(client)["id"]

    # wizard state persisted (what the UI writes)
    _commit(client, pid, "project.update_settings",
            {"settings": {"first_film": {"version": 1, "step": 0}}})

    # scene + two shots (wizard commits)
    _commit(client, pid, "scene.create", {"title": "Opening"})
    board = client.get(f"/api/projects/{pid}/storyboard").json()
    scene_id = board[0]["scene"]["id"]
    for i, title in enumerate(("wide", "close")):
        _commit(client, pid, "shot.create",
                {"scene_id": scene_id, "title": title,
                 "prompt": f"shot {i}"})

    # import two synthesized clips — the beginner path
    clips = [_synth(dev_ffmpeg, tmp_path / f"clip{i}.mp4",
                    dur=0.5, rate=24) for i in range(2)]
    assets = [_import_video(client, pid, c, c.name) for c in clips]
    assert all(a["kind"] == "video" for a in assets)

    # build timeline + insert in shot order, trim the second clip
    tl = _tl(client, pid, "My First Film", fps_num=24, fps_den=1)
    for a in assets:
        _insert(client, pid, tl["id"], a)
    d = _detail(client, pid, tl["id"])
    assert len(d["clips"]) == 2
    clip2 = d["clips"][1]
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tl["id"], "clip_id": clip2["id"],
             "in_frame": 0, "out_frame": clip2["src_frames"] // 2})

    # export: preview → pin → commit → durable job → output asset
    p = _preview(client, pid, "timeline.export",
                 {"timeline_id": tl["id"], "profile": "source"})
    rt = p["resolved_timeline"]
    assert rt["clip_count"] == 2
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": tl["id"], "profile": "source",
                 "expected_timeline": {"id": tl["id"],
                                       "revision": rt["revision"]},
                 "expected_edl_sha": rt["edl_sha256"]})
    job = wait_for_job(client, pid, r["job_id"], timeout=60)
    assert job["status"] == "succeeded", job.get("error")

    # output asset registered + probed; wizard state still intact
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    out = next(a for a in items if a["origin"] == "rendered")
    proj = client.get(f"/api/projects/{pid}").json()
    assert proj["settings_json"]["first_film"]["step"] == 0

    meta = engine.probe(
        Path(client.app.state.settings.data_dir) / out["uri"])
    assert meta["ok"] and meta["codec"] == "h264"


def test_stale_export_approval_refuses(client):
    """Editing the timeline after a preview refuses the stale commit —
    the same approval guard the wizard's export step relies on."""
    pid = make_project(client)["id"]
    tl = _tl(client, pid)
    p = _preview(client, pid, "timeline.export",
                 {"timeline_id": tl["id"], "profile": "720p"})
    rt = p["resolved_timeline"]
    _commit(client, pid, "timeline.update",
            {"timeline_id": tl["id"], "name": "renamed"})
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": tl["id"], "profile": "720p",
                 "expected_timeline": {"id": tl["id"],
                                       "revision": rt["revision"]},
                 "expected_edl_sha": rt["edl_sha256"]}, expect=422)
    assert "changed" in str(r.json()).lower() or "stale" in str(r.json()).lower()


def test_wizard_entities_stay_project_scoped(client):
    """Everything the wizard touches answers 404 from another project."""
    pid = make_project(client)["id"]
    other = make_project(client, name="Other")["id"]
    _commit(client, pid, "scene.create", {"title": "Mine"})
    board = client.get(f"/api/projects/{pid}/storyboard").json()
    sid = board[0]["scene"]["id"]
    _commit(client, other, "shot.create",
            {"scene_id": sid, "title": "inject"}, expect=404)
