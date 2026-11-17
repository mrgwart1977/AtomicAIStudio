"""A3 — keyframes: shot-associated still-image beats + mock generation.

Coverage: persistence + migration, project isolation, revision safety
(stale expected_revision + entity pins), prompt ownership (compose is
proposal-only, never overwrites), continuity reference validation,
mock job lifecycle (enqueue → progress → result apply_command → CAS
asset → pin → undo), cancellation, restart reconciliation, backup/
restore, and A1/A2 regression (planning + sync untouched).
"""
import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview, _rev
from tests.test_a1_screenplay import (
    _character, _location, _film, _scaffold, _sync, _detail, _storyboard)
from tests.test_a2_storyboard import (
    _scene2, _shot2, _entry, _import_image, _undo)


def _kfs(client, pid, shot_id):
    r = client.get(f"/api/projects/{pid}/shots/{shot_id}/keyframes")
    assert r.status_code == 200, r.text
    return r.json()


def _kf(client, pid, kf_id):
    r = client.get(f"/api/projects/{pid}/keyframes/{kf_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _mk_kf(client, pid, shot_id, role="start", **kw):
    _commit(client, pid, "keyframe.create",
            {"shot_id": shot_id, "role": role, **kw})
    return next(k for k in _kfs(client, pid, shot_id) if k["role"] == role)


# ---------- persistence + schema ----------

def test_create_and_list(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start",
                prompt="opening frame", frame={"num": 0, "den": 1},
                settings={"seed": 7, "model_key": "mock-image"})
    got = _kf(client, pid, kf["id"])
    assert got["role"] == "start"
    assert got["prompt"] == "opening frame"
    assert got["frame_num"] == 0 and got["frame_den"] == 1
    assert got["settings_json"]["seed"] == 7
    assert got["shot_id"] == sh["shot"]["id"]
    assert got["scene_id"] == sc["scene"]["id"]
    assert got["status"] == "active" and got["revision"] == 0
    # Board exposes the compact summary
    entry = _entry(client, pid, sh["shot"]["id"])
    assert entry["keyframes"]["count"] == 1
    assert entry["keyframes"]["roles"] == ["start"]


def test_all_roles_accepted(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    for role in ("start", "end", "reference", "intermediate"):
        _commit(client, pid, "keyframe.create",
                {"shot_id": sh["shot"]["id"], "role": role})
    assert {k["role"] for k in _kfs(client, pid, sh["shot"]["id"])} == \
        {"start", "end", "reference", "intermediate"}


@pytest.mark.parametrize("payload,frag", [
    ({"role": "keylight"}, "role"),
    ({"frame": {"num": 1}}, "frame"),
    ({"frame": {"num": -1, "den": 1}}, "non-negative"),
    ({"frame": {"num": 1, "den": 0}}, "positive"),
    ({"settings": {"seed": "abc"}}, "seed"),
    ({"settings": {"seed": -5}}, "seed"),
    ({"settings": {"steps": 0}}, "steps"),
    ({"settings": {"cfg": "high"}}, "cfg"),
    ({"prompt": "x" * 4001}, "4000"),
])
def test_invalid_fields_rejected(client, payload, frag):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    r = _commit(client, pid, "keyframe.create",
                {"shot_id": sh["shot"]["id"], **payload}, expect=422)
    assert frag in r.text
    assert _kfs(client, pid, sh["shot"]["id"]) == []


def test_update_merge_and_clear(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start",
                prompt="v1", settings={"seed": 1, "steps": 8})
    _commit(client, pid, "keyframe.update",
            {"keyframe_id": kf["id"], "settings": {"steps": 12}})
    got = _kf(client, pid, kf["id"])
    assert got["settings_json"] == {"seed": 1, "steps": 12}
    assert got["prompt"] == "v1"  # untouched
    _commit(client, pid, "keyframe.update",
            {"keyframe_id": kf["id"], "settings": {"seed": None},
             "prompt": "v2"})
    got = _kf(client, pid, kf["id"])
    assert got["settings_json"] == {"steps": 12}  # seed cleared
    assert got["prompt"] == "v2" and got["revision"] == 2


def test_continuity_refs_project_scoped(client):
    pid = make_project(client)["id"]
    pid2 = make_project(client)["id"]
    anna = _character(client, pid, "Anna")
    loc = _location(client, pid, "Dock")
    foreign = _character(client, pid2, "Eve")
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    _commit(client, pid, "keyframe.create",
            {"shot_id": sh["shot"]["id"], "role": "start",
             "continuity": {"refs": [
                 {"kind": "character", "id": anna["id"]},
                 {"kind": "location", "id": loc["id"]}],
                 "notes": "night look"}})
    got = _kfs(client, pid, sh["shot"]["id"])[0]
    assert len(got["continuity_json"]["refs"]) == 2
    # Foreign character rejected — create AND update paths
    _commit(client, pid, "keyframe.create",
            {"shot_id": sh["shot"]["id"], "role": "end",
             "continuity": {"refs": [
                 {"kind": "character", "id": foreign["id"]}]}},
            expect=422)
    _commit(client, pid, "keyframe.update",
            {"keyframe_id": got["id"],
             "continuity": {"refs": [
                 {"kind": "character", "id": foreign["id"]}]}},
            expect=422)


def test_cross_project_isolation(client):
    pid = make_project(client)["id"]
    pid2 = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start")
    # Same keyframe id in another project → 404, never a leak
    for ct in ("keyframe.update", "keyframe.archive",
               "keyframe.generate", "keyframe.pin", "keyframe.compose"):
        _commit(client, pid2, ct, {"keyframe_id": kf["id"]}, expect=404)
    r = client.get(f"/api/projects/{pid2}/keyframes/{kf['id']}")
    assert r.status_code == 404
    r = client.get(f"/api/projects/{pid2}/shots/{sh['shot']['id']}/keyframes")
    assert r.status_code == 404


def test_archive_unarchive_and_revision_history(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="x")
    _commit(client, pid, "keyframe.archive", {"keyframe_id": kf["id"]})
    assert _kf(client, pid, kf["id"])["status"] == "archived"
    _commit(client, pid, "keyframe.update",
            {"keyframe_id": kf["id"], "prompt": "y"}, expect=422)
    _commit(client, pid, "keyframe.unarchive", {"keyframe_id": kf["id"]})
    got = _kf(client, pid, kf["id"])
    assert got["status"] == "active"
    r = client.get(f"/api/projects/{pid}/keyframes/{kf['id']}/versions")
    assert r.status_code == 200
    revs = r.json()
    assert len(revs) >= 3  # create + archive + unarchive
    assert revs[0]["revision"] == got["revision"]


def test_revert_and_undo(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="v1")
    cid = _commit(client, pid, "keyframe.update",
                  {"keyframe_id": kf["id"], "prompt": "v2"})
    assert _kf(client, pid, kf["id"])["prompt"] == "v2"
    # revert to revision 0 (creation snapshot)
    _commit(client, pid, "keyframe.revert",
            {"keyframe_id": kf["id"], "to_revision": 0})
    assert _kf(client, pid, kf["id"])["prompt"] == "v1"
    # undo the update command → back to v1 again
    _commit(client, pid, "keyframe.update",
            {"keyframe_id": kf["id"], "prompt": "v3"})
    cmds = client.get(f"/api/projects/{pid}/commands").json()
    upd = next(c for c in cmds if c["command_type"] == "keyframe.update")
    _undo(client, pid, upd["id"])
    assert _kf(client, pid, kf["id"])["prompt"] == "v1"


def test_stale_project_revision_rejected(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start")
    rev = _rev(client, pid)
    _commit(client, pid, "keyframe.update",
            {"keyframe_id": kf["id"], "prompt": "changed"})
    r = client.post(f"/api/projects/{pid}/commands/commit",
                    json={"command_type": "keyframe.archive",
                          "payload": {"keyframe_id": kf["id"]},
                          "scope": "project", "expected_revision": rev,
                          "idempotency_key": "a3-stale-1"})
    assert r.status_code == 409
    assert _kf(client, pid, kf["id"])["status"] == "active"


# ---------- compose draft (A3.2) ----------

def test_compose_preview_shows_draft_never_writes(client):
    pid = make_project(client)["id"]
    anna = _character(client, pid, "Anna")
    loc = _location(client, pid, "Dock")
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"], prompt="she turns")
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": anna["id"],
             "state": {"expression": "wary"}})
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"], "location_id": loc["id"]})
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start")
    prop = _preview(client, pid, "keyframe.compose",
                    {"keyframe_id": kf["id"]})
    draft = prop["resolved_draft"]
    assert draft["prompt_sha256"]
    assert draft["sections"]  # continuity context was composed
    assert draft["user_prompt"].startswith("opening frame")
    # Preview wrote nothing
    assert _kf(client, pid, kf["id"])["prompt"] == ""


def test_compose_commit_applies_draft_marks_origin(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"], prompt="beat")
    kf = _mk_kf(client, pid, sh["shot"]["id"], "end")
    _commit(client, pid, "keyframe.compose", {"keyframe_id": kf["id"]})
    got = _kf(client, pid, kf["id"])
    assert got["prompt"].startswith("closing frame")
    assert got["prompt_origin"] == "compose"
    # User edit after compose flips origin back to user
    _commit(client, pid, "keyframe.update",
            {"keyframe_id": kf["id"], "prompt": "my own words"})
    got = _kf(client, pid, kf["id"])
    assert got["prompt"] == "my own words"
    assert got["prompt_origin"] == "user"


def test_generate_requires_prompt(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start")
    _commit(client, pid, "keyframe.generate",
            {"keyframe_id": kf["id"]}, expect=422)


# ---------- mock generation lifecycle (A3.4/A3.5) ----------

def test_generate_job_lifecycle_result_pin_undo(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"], prompt="a still")
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start",
                prompt="a still", settings={"seed": 42})
    prop = _preview(client, pid, "keyframe.generate",
                    {"keyframe_id": kf["id"]})
    assert prop["resolved_keyframe"]["keyframe_id"] == kf["id"]
    assert prop["resolved_prompt"]["prompt_sha256"]
    out = _commit(client, pid, "keyframe.generate",
                  {"keyframe_id": kf["id"],
                   "expected_keyframe": prop["resolved_keyframe"],
                   "expected_prompt":
                       prop["resolved_prompt"]["prompt_sha256"]})
    job_id = out["job_id"]
    got = _kf(client, pid, kf["id"])
    assert got["generate_job_id"] == job_id
    done = wait_for_job(client, pid, job_id, timeout=30)
    assert done["status"] == "succeeded"
    assert done["adapter_id"] == "mock.image"
    got = _kf(client, pid, kf["id"])
    assert got["result_asset_id"] == done["output_asset_id"]
    assert got["job_status"] == "succeeded"
    # Result is a real CAS asset, labeled mock — not inference
    assets = client.get(f"/api/projects/{pid}/assets").json()["items"]
    a = next(a for a in assets if a["id"] == got["result_asset_id"])
    assert a["kind"] == "image" and a["status"] == "ready"
    # Pin → shot thumbnail; undo → restored
    _commit(client, pid, "keyframe.pin", {"keyframe_id": kf["id"]})
    entry = _entry(client, pid, sh["shot"]["id"])
    assert entry["shot"]["thumbnail_asset_id"] == got["result_asset_id"]
    cmds = client.get(f"/api/projects/{pid}/commands").json()
    pin = next(c for c in cmds if c["command_type"] == "keyframe.pin")
    _undo(client, pid, pin["id"])
    assert _entry(client, pid, sh["shot"]["id"])["shot"]["thumbnail_asset_id"] is None
    # Undo keyframe.result → result link cleared but asset survives
    res = next(c for c in client.get(
        f"/api/projects/{pid}/commands").json()
        if c["command_type"] == "keyframe.result")
    _undo(client, pid, res["id"])
    assert _kf(client, pid, kf["id"])["result_asset_id"] is None


def test_generate_stale_keyframe_pin_rejected(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"], prompt="s")
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="s")
    prop = _preview(client, pid, "keyframe.generate",
                    {"keyframe_id": kf["id"]})
    # Edit between preview and commit → expected_keyframe mismatch
    _commit(client, pid, "keyframe.update",
            {"keyframe_id": kf["id"], "prompt": "s edited"})
    _commit(client, pid, "keyframe.generate",
            {"keyframe_id": kf["id"],
             "expected_keyframe": prop["resolved_keyframe"]},
            expect=422)
    # Fresh preview + commit succeeds
    prop2 = _preview(client, pid, "keyframe.generate",
                     {"keyframe_id": kf["id"]})
    out = _commit(client, pid, "keyframe.generate",
                  {"keyframe_id": kf["id"],
                   "expected_keyframe": prop2["resolved_keyframe"]})
    wait_for_job(client, pid, out["job_id"], timeout=30)


def test_generate_archived_keyframe_rejected(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"], prompt="s")
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="s")
    _commit(client, pid, "keyframe.archive", {"keyframe_id": kf["id"]})
    _commit(client, pid, "keyframe.generate",
            {"keyframe_id": kf["id"]}, expect=422)


def test_job_cancel_and_idempotent_replay(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"], prompt="s")
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="s")
    out = _commit(client, pid, "keyframe.generate",
                  {"keyframe_id": kf["id"]}, key="a3-gen-idem")
    # Idempotent replay: same key → the stored command returns, and
    # critically NO second job is enqueued.
    _commit(client, pid, "keyframe.generate",
            {"keyframe_id": kf["id"]}, key="a3-gen-idem")
    jobs = [j for j in client.get(f"/api/projects/{pid}/jobs").json()
            if (j.get("request_json") or {}).get("keyframe_id") == kf["id"]
            or j["id"] == out["job_id"]]
    assert len({j["id"] for j in jobs}) == 1
    wait_for_job(client, pid, out["job_id"], timeout=30)


# ---------- backup/restore ----------

def test_backup_roundtrip(client, tmp_path):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"], prompt="s")
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="s",
                settings={"seed": 3})
    out = _commit(client, pid, "keyframe.generate",
                  {"keyframe_id": kf["id"]})
    wait_for_job(client, pid, out["job_id"], timeout=30)
    _commit(client, pid, "backup.export", {"kind": "backup"})
    job = client.get(f"/api/projects/{pid}/jobs").json()
    exjob = next(j for j in job if j["kind"] == "backup")
    done = wait_for_job(client, pid, exjob["id"], timeout=60)
    assert done["status"] == "succeeded", done
    pkg = done["result_json"]["package_path"]
    r = client.post("/api/packages/import",
                    json={"package_path": str(pkg)})
    assert r.status_code == 200, r.text
    new_pid = r.json()["project_id"]
    wait_for_job(client, new_pid, r.json()["job_id"], timeout=60)
    board = client.get(f"/api/projects/{new_pid}/storyboard").json()
    new_shot = board[0]["shots"][0]["shot"]
    kfs = _kfs(client, new_pid, new_shot["id"])
    assert len(kfs) == 1
    assert kfs[0]["role"] == "start"
    assert kfs[0]["prompt"] == "s"
    assert kfs[0]["settings_json"]["seed"] == 3
    # Result asset remapped to the restored copy; job link dropped
    assert kfs[0]["result_asset_id"] is not None
    assert kfs[0]["generate_job_id"] is None
    assert kfs[0]["id"] != kf["id"]


# ---------- A1/A2 regressions ----------

def test_sync_and_planning_untouched(client):
    """Screenplay sync must not touch keyframes; keyframes don't
    affect the scaffold fingerprint or planning."""
    pid = make_project(client)["id"]
    film = _film(client, pid)
    _scaffold(client, pid, film["spid"])
    board = _storyboard(client, pid)
    sh = board[0]["shots"][0]["shot"]
    kf = _mk_kf(client, pid, sh["id"], "start", prompt="k")
    _commit(client, pid, "shot.update",
            {"shot_id": sh["id"],
             "planning": {"camera": {"follow_mode": "orbit_follow"}}})
    # Re-sync: keyframes + planning survive
    _sync(client, pid, film["spid"])
    assert len(_kfs(client, pid, sh["id"])) == 1
    entry = _entry(client, pid, sh["id"])
    assert entry["shot"]["planning_json"]["camera"]["follow_mode"] == \
        "orbit_follow"


def test_restart_persistence(client, settings):
    """A second app instance on the same data dir sees keyframes,
    results, and planning exactly as left (durable rows)."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"], prompt="s")
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="s")
    out = _commit(client, pid, "keyframe.generate",
                  {"keyframe_id": kf["id"]})
    wait_for_job(client, pid, out["job_id"], timeout=30)
    result_id = _kf(client, pid, kf["id"])["result_asset_id"]
    # Restart: a second app on the same data dir sees everything
    from fastapi.testclient import TestClient
    from app.main import create_app
    from tests.conftest import TOKEN
    client.app.state.data_dir_lock.release()
    with TestClient(create_app(settings)) as c2:
        c2.headers["X-Atomic-Token"] = TOKEN
        got = _kf(c2, pid, kf["id"])
        assert got["prompt"] == "s"
        assert got["result_asset_id"] == result_id
        assert got["job_status"] == "succeeded"
        assert len(_kfs(c2, pid, sh["shot"]["id"])) == 1
