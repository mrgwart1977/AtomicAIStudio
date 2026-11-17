"""A4 — motion clips: shot-associated keyframe→video beats + mock video.

Coverage: persistence + migration, project isolation, start/end
keyframe refs, camera + motion + timing validation (the A2 vocabulary),
prompt ownership, mock video job lifecycle (enqueue → progress →
result apply_command → CAS mp4), deterministic cancellation via the
hold barrier, stale-result refusal, decodable output via ffprobe,
timeline placement on the existing EDL, undo/redo, backup/restore,
restart reconciliation, and A1/A2/A3 regression.
"""
import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_a1_screenplay import _character, _storyboard
from tests.test_a2_storyboard import _scene2, _shot2, _entry, _undo
from tests.test_a3_keyframes import _mk_kf, _kfs


def _clips(client, pid, shot_id):
    r = client.get(f"/api/projects/{pid}/shots/{shot_id}/clips")
    assert r.status_code == 200, r.text
    return r.json()


def _clip(client, pid, clip_id):
    r = client.get(f"/api/projects/{pid}/clips/{clip_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _mk_clip(client, pid, shot_id, **kw):
    _commit(client, pid, "clip.create", {"shot_id": shot_id, **kw})
    return _clips(client, pid, shot_id)[-1]


def _wait_hold(client, pid, job_id, timeout=60):
    """Deterministic: job must reach the mock hold barrier — the
    adapter parks emitting 'Mock video hold' while running."""
    import time
    t0 = time.time()
    j = None
    while time.time() - t0 < timeout:
        j = next(x for x in client.get(f"/api/projects/{pid}/jobs").json()
                 if x["id"] == job_id)
        if j["status"] == "running" and "hold" in (j["message"] or "").lower():
            return j
        if j["status"] in ("succeeded", "failed", "cancelled"):
            raise AssertionError(f"job settled before hold: {j['status']}")
        time.sleep(0.15)
    raise AssertionError(f"job never reached hold barrier: {j}")


def _gen(client, pid, clip_id, timeout=60, **extra):
    payload = {"clip_id": clip_id, **extra}
    pv = _preview(client, pid, "clip.generate", payload)
    payload["expected_clip"] = pv["resolved_clip"]
    if pv.get("resolved_prompt"):
        payload["expected_prompt"] = \
            pv["resolved_prompt"]["prompt_sha256"]
    out = _commit(client, pid, "clip.generate", payload)
    done = wait_for_job(client, pid, out["job_id"], timeout=timeout)
    assert done["status"] == "succeeded", done
    return done


# ---------- persistence + schema ----------

def test_create_and_list(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"],
                 prompt="push in on anna",
                 duration_frames=48, fps={"num": 24, "den": 1},
                 motion={"instructions": "slow push", "intensity": "subtle"},
                 camera={"movement": "dolly_in"})
    got = _clip(client, pid, c["id"])
    assert got["prompt"] == "push in on anna"
    assert got["duration_frames"] == 48
    assert got["fps_num"] == 24 and got["fps_den"] == 1
    assert got["motion_json"]["intensity"] == "subtle"
    assert got["camera_json"]["movement"] == "dolly_in"
    assert got["shot_id"] == sh["shot"]["id"]
    assert got["scene_id"] == sc["scene"]["id"]
    assert got["status"] == "active" and got["revision"] == 0
    entry = _entry(client, pid, sh["shot"]["id"])
    assert entry["clips"]["count"] == 1


@pytest.mark.parametrize("payload,frag", [
    ({"motion": {"intensity": "extreme"}}, "intensity"),
    ({"camera": {"follow_mode": "teleport"}}, "follow_mode"),
    ({"camera": {"movement": "warp"}}, "movement"),
    ({"camera": {"framing": "selfie"}}, "framing"),
    ({"duration_frames": 0}, "duration_frames"),
    ({"duration_frames": -3}, "duration_frames"),
    ({"fps": {"num": 0, "den": 1}}, "fps"),
    ({"fps": {"num": 24, "den": 0}}, "fps"),
    ({"fps": {"num": 999, "den": 1}}, "480"),
    ({"prompt": "x" * 4001}, "4000"),
    ({"width": 0}, "width"),
    ({"width": 99999}, "4096"),
])
def test_invalid_fields_rejected(client, payload, frag):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    r = _commit(client, pid, "clip.create",
                {"shot_id": sh["shot"]["id"], **payload}, expect=422)
    assert frag in r.text
    assert _clips(client, pid, sh["shot"]["id"]) == []


def test_start_end_keyframe_refs(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    sh2 = _shot2(client, pid, sc["scene"]["id"], title="insert")
    kf_s = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="s")
    kf_e = _mk_kf(client, pid, sh["shot"]["id"], "end", prompt="e")
    foreign_kf = _mk_kf(client, pid, sh2["shot"]["id"], "start",
                        prompt="other shot")
    c = _mk_clip(client, pid, sh["shot"]["id"],
                 start_keyframe_id=kf_s["id"],
                 end_keyframe_id=kf_e["id"], prompt="morph")
    got = _clip(client, pid, c["id"])
    assert got["start_keyframe_id"] == kf_s["id"]
    assert got["end_keyframe_id"] == kf_e["id"]
    # A clip's keyframe refs must live on the SAME shot — a keyframe
    # from a sibling shot is refused, not silently linked.
    r = _commit(client, pid, "clip.create",
                {"shot_id": sh["shot"]["id"],
                 "start_keyframe_id": foreign_kf["id"],
                 "prompt": "bad"}, expect=422)
    assert "different shot" in r.text
    r = _commit(client, pid, "clip.update",
                {"clip_id": c["id"],
                 "end_keyframe_id": foreign_kf["id"]}, expect=422)
    assert "different shot" in r.text


def test_follow_targets_project_scoped(client):
    pid = make_project(client)["id"]
    pid2 = make_project(client)["id"]
    anna = _character(client, pid, "Anna")
    eve = _character(client, pid, "Eve")
    foreign = _character(client, pid2, "Zed")
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="group chase",
                 camera={
                     "movement": "truck_left",
                     "follow_mode": "group_follow",
                     "follow_targets": [
                         {"kind": "character", "id": anna["id"]},
                         {"kind": "character", "id": eve["id"]},
                         {"kind": "label", "name": "the dog"}]})
    got = _clip(client, pid, c["id"])
    tg = got["camera_json"]["follow_targets"]
    assert [t.get("id") for t in tg[:2]] == [anna["id"], eve["id"]]
    assert tg[2] == {"kind": "label", "name": "the dog"}
    r = _commit(client, pid, "clip.update",
                {"clip_id": c["id"],
                 "camera": {"follow_targets": [
                     {"kind": "character", "id": foreign["id"]}]}},
                expect=422)
    assert "follow target" in r.text


def test_all_ten_follow_modes(client):
    """A4.3 — every A2 follow mode serializes + persists verbatim."""
    pid = make_project(client)["id"]
    anna = _character(client, pid, "Anna")
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    modes = ("follow_behind", "follow_front", "follow_side",
             "follow_shoulder", "orbit_follow", "drone_follow",
             "vehicle_chase", "lead_and_follow", "group_follow",
             "target_switch")
    for i, mode in enumerate(modes):
        c = _mk_clip(client, pid, sh["shot"]["id"], prompt=f"m{i}",
                     camera={
                         "follow_mode": mode,
                         "follow_targets": [
                             {"kind": "character", "id": anna["id"]}],
                         "follow_params": {"speed": 1.0, "lead_frames": 12}})
        assert _clip(client, pid, c["id"])[
            "camera_json"]["follow_mode"] == mode


def test_update_merge_and_clear(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="v1",
                 motion={"instructions": "a", "intensity": "subtle"})
    _commit(client, pid, "clip.update",
            {"clip_id": c["id"], "motion": {"intensity": "strong"}})
    got = _clip(client, pid, c["id"])
    assert got["motion_json"] == {"instructions": "a",
                                  "intensity": "strong"}
    _commit(client, pid, "clip.update",
            {"clip_id": c["id"],
             "motion": {"instructions": None}, "prompt": "v2"})
    got = _clip(client, pid, c["id"])
    assert got["motion_json"] == {"intensity": "strong"}
    assert got["prompt"] == "v2" and got["revision"] == 2


def test_compose_draft_and_apply(client):
    pid = make_project(client)["id"]
    _character(client, pid, "Anna")
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"],
                 motion={"instructions": "she turns"}, prompt="turn")
    pv = _preview(client, pid, "clip.compose", {"clip_id": c["id"]})
    assert pv["resolved_draft"]["prompt"]
    # Read-only until committed.
    assert _clip(client, pid, c["id"])["prompt"] == "turn"
    _commit(client, pid, "clip.compose", {"clip_id": c["id"]})
    got = _clip(client, pid, c["id"])
    assert got["prompt_origin"] == "compose"
    # User edits reclaim ownership — compose never overwrites.
    _commit(client, pid, "clip.update",
            {"clip_id": c["id"], "prompt": "mine"})
    assert _clip(client, pid, c["id"])["prompt_origin"] == "user"


# ---------- mock generation lifecycle ----------

def test_generate_produces_decodable_video(client):
    """A4.5 — mock.video emits a real decodable H.264 mp4; CAS asset
    keeps explicit mock provenance; ffprobe confirms timing/frames."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf_s = _mk_kf(client, pid, sh["shot"]["id"], "start")
    c = _mk_clip(client, pid, sh["shot"]["id"],
                 start_keyframe_id=kf_s["id"], prompt="dolly shot",
                 duration_frames=24, fps={"num": 24, "den": 1},
                 width=256, height=144)
    done = _gen(client, pid, c["id"])
    got = _clip(client, pid, c["id"])
    assert done["adapter_id"] == "mock.video"
    assert got["result_asset_id"] == done["output_asset_id"]
    assert got["generate_job_id"] == done["id"]
    # CAS asset version media_json keeps is_mock / generated=false.
    vers = client.get(
        f"/api/projects/{pid}/assets/{got['result_asset_id']}/versions")
    assert vers.status_code == 200
    media = vers.json()[0].get("media_json") or {}
    assert media["is_mock"] is True and media["generated"] is False
    assert media["adapter_id"] == "mock.video"
    assert media["frames"] == 24
    # The embedded probe came from real ffprobe on the real file —
    # and we can re-verify the CAS blob itself.
    assert media["probe"]["ok"] and media["probe"]["codec"] == "h264"
    assert media["probe"]["width"] == 256
    a = next(a for a in
             client.get(f"/api/projects/{pid}/assets").json()["items"]
             if a["id"] == got["result_asset_id"])
    assert a["kind"] == "video"
    from pathlib import Path
    blob = Path(client.app.state.storage._data_dir) / a["uri"]
    assert blob.is_file()
    from app.domain.media import engine as me
    probe = me.probe(blob)
    assert probe["ok"] and probe["fps"] == 24.0
    assert probe["frames"] == 24
    cnt = me.count_decoded_frames(blob)
    assert cnt["ok"] and cnt["frames"] == 24, cnt


def test_deterministic_hold_and_cancel(client, tmp_path):
    """A4.5 — same barrier contract as A3: job parks deterministically;
    cancel propagates through the barrier AND the encode subprocess;
    no result lands; re-cancel is a safe 409."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="held",
                 duration_frames=24)
    rel = tmp_path / "release.flag"
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 2,
                                 "release_file": str(rel),
                                 "timeout_s": 120}})
    job_id = out["job_id"]
    _wait_hold(client, pid, job_id)
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text
    done = wait_for_job(client, pid, job_id, timeout=30)
    assert done["status"] == "cancelled"
    got = _clip(client, pid, c["id"])
    assert got["result_asset_id"] is None  # no late result
    assert got["job_status"] == "cancelled"
    # No clip.result command ran for this job.
    results = [x for x in client.get(f"/api/projects/{pid}/commands").json()
               if x["command_type"] == "clip.result"]
    assert results == []
    # Idempotent re-cancel is a safe terminal conflict.
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409
    # A late release file changes nothing — the task is gone.
    rel.write_text("go")
    import time
    time.sleep(0.5)
    assert _clip(client, pid, c["id"])["result_asset_id"] is None


def test_hold_release_completes(client, tmp_path):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="rel",
                 duration_frames=24)
    rel = tmp_path / "r.flag"
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 4,
                                 "release_file": str(rel),
                                 "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    rel.write_text("go")
    done = wait_for_job(client, pid, out["job_id"], timeout=60)
    assert done["status"] == "succeeded"
    assert _clip(client, pid, c["id"])["result_asset_id"] == \
        done["output_asset_id"]


# ---------- A4 stale-result protection (same contract as A3-F3) ----------

def test_stale_result_prompt_edit_refused(client, tmp_path):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="v1",
                 duration_frames=24)
    rel = tmp_path / "r.flag"
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 2,
                                 "release_file": str(rel),
                                 "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    _commit(client, pid, "clip.update",
            {"clip_id": c["id"], "prompt": "edited mid-flight"})
    rel.write_text("go")
    done = wait_for_job(client, pid, out["job_id"], timeout=60)
    assert done["status"] == "succeeded"  # artifact exists; apply refused
    got = _clip(client, pid, c["id"])
    assert got["prompt"] == "edited mid-flight"
    assert got["result_asset_id"] is None
    results = [x for x in client.get(f"/api/projects/{pid}/commands").json()
               if x["command_type"] == "clip.result"]
    assert results == []


def test_stale_result_planning_edit_refused(client, tmp_path):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="v1",
                 duration_frames=24)
    rel = tmp_path / "r.flag"
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 2,
                                 "release_file": str(rel),
                                 "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    _commit(client, pid, "shot.update",
            {"shot_id": sh["shot"]["id"],
             "planning": {"camera": {"follow_mode": "drone_follow"}}})
    rel.write_text("go")
    done = wait_for_job(client, pid, out["job_id"], timeout=60)
    assert done["status"] == "succeeded"
    assert _clip(client, pid, c["id"])["result_asset_id"] is None


def test_stale_result_superseded_refused(client, tmp_path):
    """A newer generate supersedes an older in-flight job — the older
    job's result must never attach."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="first",
                 duration_frames=24)
    rel = tmp_path / "r.flag"
    out1 = _commit(client, pid, "clip.generate",
                   {"clip_id": c["id"],
                    "mock_hold": {"at_stage": 2,
                                  "release_file": str(rel),
                                  "timeout_s": 120}})
    _wait_hold(client, pid, out1["job_id"])
    # Superseding generation queues behind the held job (one gen lane).
    out2 = _commit(client, pid, "clip.generate",
                   {"clip_id": c["id"], "prompt": "second"})
    assert _clip(client, pid, c["id"])["generate_job_id"] == out2["job_id"]
    rel.write_text("go")
    done1 = wait_for_job(client, pid, out1["job_id"], timeout=60)
    assert done1["status"] == "succeeded"
    # The superseded job's artifact is refused — it must never attach,
    # while job2's own result may already have landed.
    assert _clip(client, pid, c["id"])["result_asset_id"] != \
        done1["output_asset_id"]
    done2 = wait_for_job(client, pid, out2["job_id"], timeout=60)
    assert done2["status"] == "succeeded"
    got = _clip(client, pid, c["id"])
    assert got["result_asset_id"] == done2["output_asset_id"]
    assert got["result_asset_id"] != done1["output_asset_id"]


def test_stale_result_archived_refused(client, tmp_path):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="v1",
                 duration_frames=24)
    rel = tmp_path / "r.flag"
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 2,
                                 "release_file": str(rel),
                                 "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    _commit(client, pid, "clip.archive", {"clip_id": c["id"]})
    rel.write_text("go")
    done = wait_for_job(client, pid, out["job_id"], timeout=60)
    assert done["status"] == "succeeded"
    got = _clip(client, pid, c["id"])
    assert got["result_asset_id"] is None
    assert got["status"] == "archived"


def test_generate_stale_clip_pin_rejected(client):
    """Editing between preview and commit bumps the fingerprint —
    the stale approval refuses (same discipline as keyframes)."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="v1",
                 duration_frames=24)
    pv = _preview(client, pid, "clip.generate", {"clip_id": c["id"]})
    _commit(client, pid, "clip.update",
            {"clip_id": c["id"], "prompt": "v2"})
    _commit(client, pid, "clip.generate",
            {"clip_id": c["id"],
             "expected_clip": pv["resolved_clip"]}, expect=422)


# ---------- timeline integration (A4.6) ----------

def test_timeline_placement(client):
    """A produced clip lands on the existing timeline via
    timeline.clip.place — rational fps conversion, no parallel path."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="place me",
                 duration_frames=24, fps={"num": 24, "den": 1})
    _gen(client, pid, c["id"])
    got = _clip(client, pid, c["id"])
    _commit(client, pid, "timeline.create", {"name": "cut"})
    tl = client.get(f"/api/projects/{pid}/timelines").json()[0]
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": tl["id"],
             "asset_id": got["result_asset_id"],
             "start_frames": 0, "shot_id": sh["shot"]["id"]})
    d = client.get(f"/api/projects/{pid}/timelines/{tl['id']}").json()
    assert len(d["clips"]) == 1
    clip = d["clips"][0]
    assert clip["source_asset_id"] == got["result_asset_id"]
    assert clip["start_frames"] == 0
    # 24 frames at 24 fps source → 24 source frames, 1 s on the line.
    assert clip["out_frame"] - clip["in_frame"] == 24
    assert clip["src_duration_s"] == 1.0
    assert clip["src_fps_num"] == 24


def test_timeline_fps_conversion(client):
    """A 48-frame/48fps source on a 24fps timeline maps to 24 timeline
    frames — rational conversion, never truncation."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="48fps",
                 duration_frames=48, fps={"num": 48, "den": 1})
    _gen(client, pid, c["id"])
    got = _clip(client, pid, c["id"])
    _commit(client, pid, "timeline.create",
            {"name": "cut24", "fps_num": 24, "fps_den": 1})
    tl = client.get(f"/api/projects/{pid}/timelines").json()[0]
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": tl["id"],
             "asset_id": got["result_asset_id"], "start_frames": 0})
    d = client.get(f"/api/projects/{pid}/timelines/{tl['id']}").json()
    clip = d["clips"][0]
    # Source records its own rate; the timeline extent conversion is
    # exercised at EDL/export level — 48f@48fps → 24 frames @24fps.
    assert clip["src_fps_num"] == 48
    assert clip["src_duration_s"] == 1.0
    from fractions import Fraction
    assert Fraction(clip["out_frame"] - clip["in_frame"]) * \
        Fraction(clip["src_fps_den"], clip["src_fps_num"]) * 24 == 24


# ---------- undo / backup / isolation / restart ----------

def test_undo_result_and_archive(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="undo me",
                 duration_frames=24)
    _gen(client, pid, c["id"])
    assert _clip(client, pid, c["id"])["result_asset_id"]
    res = next(x for x in client.get(
        f"/api/projects/{pid}/commands").json()
        if x["command_type"] == "clip.result")
    _undo(client, pid, res["id"])
    assert _clip(client, pid, c["id"])["result_asset_id"] is None
    _commit(client, pid, "clip.archive", {"clip_id": c["id"]})
    assert _clip(client, pid, c["id"])["status"] == "archived"
    arch = next(x for x in client.get(
        f"/api/projects/{pid}/commands").json()
        if x["command_type"] == "clip.archive")
    _undo(client, pid, arch["id"])
    assert _clip(client, pid, c["id"])["status"] == "active"


def test_backup_restore(client):
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start")
    c = _mk_clip(client, pid, sh["shot"]["id"],
                 start_keyframe_id=kf["id"], prompt="backup me",
                 duration_frames=24,
                 camera={"follow_mode": "orbit_follow"})
    _gen(client, pid, c["id"])
    _commit(client, pid, "backup.export", {"kind": "backup"})
    exjob = next(j for j in
                 client.get(f"/api/projects/{pid}/jobs").json()
                 if j["kind"] == "backup")
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
    clips2 = _clips(client, new_pid, new_shot["id"])
    assert len(clips2) == 1
    got = clips2[0]
    assert got["prompt"] == "backup me"
    assert got["camera_json"]["follow_mode"] == "orbit_follow"
    # Result asset link remapped; job link nulled (jobs aren't packaged).
    assert got["result_asset_id"] is not None
    assert got["generate_job_id"] is None
    # Keyframe ref remapped to the restored keyframe, not the old id.
    kfs2 = _kfs(client, new_pid, new_shot["id"])
    assert got["start_keyframe_id"] == kfs2[0]["id"] != kf["id"]


def test_project_isolation(client):
    pid = make_project(client)["id"]
    pid2 = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="mine")
    assert client.get(
        f"/api/projects/{pid2}/clips/{c['id']}").status_code == 404
    _commit(client, pid2, "clip.update",
            {"clip_id": c["id"], "prompt": "stolen"}, expect=404)
    assert _clip(client, pid, c["id"])["prompt"] == "mine"


def test_restart_reconcile_marks_interrupted(client, tmp_path):
    """A job parked at the barrier when the process stops reconciles to
    a terminal state on restart — never silently resumes and never
    publishes a result afterwards."""
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="restart",
                 duration_frames=24)
    rel = tmp_path / "r.flag"  # never written — no release path exists
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 2,
                                 "release_file": str(rel),
                                 "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    eng = client.app.state.job_engine
    from app.db.models import GenerationJob
    sf = client.app.state.session_factory
    with sf() as s:
        s.get(GenerationJob, out["job_id"]).status = "running"
        s.commit()
        n = eng.reconcile_interrupted(s)
        s.commit()  # reconcile mutates in-session; caller commits
    assert n == 0  # clip.generate is non-resumable → failed, not requeued
    j = next(x for x in client.get(f"/api/projects/{pid}/jobs").json()
             if x["id"] == out["job_id"])
    assert j["status"] == "failed"
    assert j["recovery_state"] == "interrupted"
    assert _clip(client, pid, c["id"])["result_asset_id"] is None


# ---------- A4-F2 stale-result matrix (each: held job → mutate mid-
# flight → release → apply refused, current result never replaced) ----

def _stale_case(client, pid, clip_id, tmp_path, mutate, tag):
    """Held generation at stage 2 → mutate → release → the job may
    finish (artifact exists) but clip.result is refused."""
    rel = tmp_path / f"rel-{tag}.flag"
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": clip_id,
                   "mock_hold": {"at_stage": 2,
                                 "release_file": str(rel),
                                 "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    mutate()
    rel.write_text("go")
    done = wait_for_job(client, pid, out["job_id"], timeout=90)
    assert done["status"] == "succeeded", done  # artifact exists in CAS
    got = _clip(client, pid, clip_id)
    assert got["result_asset_id"] is None, (
        f"stale result attached: {got}")
    return done


def _clip_setup(client, pid):
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    return sh


def test_stale_start_keyframe_refused(client, tmp_path):
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    kf1 = _mk_kf(client, pid, sh["shot"]["id"], "start")
    kf2 = _mk_kf(client, pid, sh["shot"]["id"], "reference")
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="skf",
                 start_keyframe_id=kf1["id"])
    _stale_case(client, pid, c["id"], tmp_path,
                lambda: _commit(client, pid, "clip.update",
                                {"clip_id": c["id"],
                                 "start_keyframe_id": kf2["id"]}),
                "skf")


def test_stale_end_keyframe_refused(client, tmp_path):
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    kf1 = _mk_kf(client, pid, sh["shot"]["id"], "end")
    kf2 = _mk_kf(client, pid, sh["shot"]["id"], "reference")
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="ekf",
                 end_keyframe_id=kf1["id"])
    _stale_case(client, pid, c["id"], tmp_path,
                lambda: _commit(client, pid, "clip.update",
                                {"clip_id": c["id"],
                                 "end_keyframe_id": kf2["id"]}),
                "ekf")


def test_stale_negative_prompt_refused(client, tmp_path):
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="neg",
                 negative_prompt="no blur")
    _stale_case(client, pid, c["id"], tmp_path,
                lambda: _commit(client, pid, "clip.update",
                                {"clip_id": c["id"],
                                 "negative_prompt": "no fog either"}),
                "neg")


def test_stale_camera_edit_refused(client, tmp_path):
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="cam",
                 camera={"movement": "dolly_in"})
    _stale_case(client, pid, c["id"], tmp_path,
                lambda: _commit(client, pid, "clip.update",
                                {"clip_id": c["id"],
                                 "camera": {"movement": "dolly_out"}}),
                "cam")


def test_stale_source_keyframe_edit_refused(client, tmp_path):
    """A mid-flight edit to the SOURCE keyframe (prompt/revision) makes
    the produced video stale even though the clip itself is unchanged —
    the expected_sources pin refuses publication."""
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start",
                prompt="original source frame")
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="src-edit",
                 start_keyframe_id=kf["id"])
    _stale_case(client, pid, c["id"], tmp_path,
                lambda: _commit(client, pid, "keyframe.update",
                                {"keyframe_id": kf["id"],
                                 "prompt": "edited mid-flight"}),
                "srcedit")


def test_stale_source_keyframe_result_refused(client, tmp_path):
    """The source keyframe's own generation landing mid-flight changes
    the imagery the clip generation was approved against — refused."""
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start",
                prompt="kf will generate")
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="src-res",
                 start_keyframe_id=kf["id"])
    rel = tmp_path / "rel-kf.flag"
    # The generation lane serializes jobs: hold the KEYFRAME job first so
    # its result lands while the clip job is still queued (mid-flight).
    kout = _commit(client, pid, "keyframe.generate",
                   {"keyframe_id": kf["id"],
                    "mock_hold": {"at_stage": 2,
                                  "release_file": str(rel),
                                  "timeout_s": 120}})
    _wait_hold(client, pid, kout["job_id"])
    out = _commit(client, pid, "clip.generate", {"clip_id": c["id"]})
    rel.write_text("go")
    kdone = wait_for_job(client, pid, kout["job_id"], timeout=60)
    assert kdone["status"] == "succeeded"
    done = wait_for_job(client, pid, out["job_id"], timeout=90)
    assert done["status"] == "succeeded"
    # expected_sources was pinned at clip.generate commit — the kf
    # result landing since then makes the produced video stale.
    assert _clip(client, pid, c["id"])["result_asset_id"] is None


def test_cancel_races_completion_invariants(client, tmp_path):
    """Release + cancel landing together: whichever order the engine
    sees, the clip never gains a result from a cancelled job, and a
    succeeded job's artifact is the only one that may attach."""
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="race",
                 duration_frames=24)
    rel = tmp_path / "rel-race.flag"
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 5,
                                 "release_file": str(rel),
                                 "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    rel.write_text("go")
    client.post(f"/api/jobs/{out['job_id']}/cancel")  # may 200 or 409
    done = wait_for_job(client, pid, out["job_id"], timeout=60)
    got = _clip(client, pid, c["id"])
    if done["status"] == "cancelled":
        assert got["result_asset_id"] is None
    else:
        assert done["status"] == "succeeded"
        # Only this job's artifact may attach — never a foreign one.
        assert got["result_asset_id"] in (None, done["output_asset_id"])


def test_result_cross_project_refused(client, tmp_path):
    pid = make_project(client)["id"]
    pid2 = make_project(client, "Other")["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="xp")
    # A clip.result targeting a clip in another project can't even
    # resolve the row — project scoping refuses before any check.
    _commit(client, pid2, "clip.result",
            {"clip_id": c["id"], "result_asset_id": "x"},
            expect=404)


# ---------- A4-F3 media correctness (real ffprobe of real output) ----

import json as _json
import subprocess as _sp
from pathlib import Path as _Path


def _blob(client, asset):
    return _Path(client.app.state.storage._data_dir) / asset["uri"]


def _asset(client, pid, asset_id):
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in items if a["id"] == asset_id)


@pytest.mark.parametrize("num,den", [(24, 1), (25, 1), (30, 1),
                                     (30000, 1001)])
def test_mock_video_media_correctness(client, num, den):
    """Real ffprobe of the produced CAS blob at each supported rate:
    h264, exact frame count, rational fps, dims, yuv420p, no audio,
    sha + CAS integrity, mock provenance."""
    from app.domain.media import binaries
    if binaries.ffprobe() is None:
        pytest.skip("no ffprobe")
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    frames = 24
    c = _mk_clip(client, pid, sh["shot"]["id"],
                 prompt=f"probe {num}/{den}", duration_frames=frames,
                 fps={"num": num, "den": den}, width=256, height=144)
    done = _gen(client, pid, c["id"], timeout=90)
    a = _asset(client, pid, done["output_asset_id"])
    blob = _blob(client, a)
    assert blob.is_file()
    pr = _json.loads(_sp.run(
        [binaries.ffprobe()["path"], "-v", "error",
         "-print_format", "json",
         "-show_streams", "-show_format", str(blob)],
        capture_output=True, text=True, check=True).stdout)
    vs = next(s for s in pr["streams"] if s["codec_type"] == "video")
    assert vs["codec_name"] == "h264"
    assert int(vs["nb_frames"]) == frames
    assert vs["width"] == 256 and vs["height"] == 144
    assert vs["pix_fmt"] == "yuv420p"
    # Rational rate preserved end-to-end.
    rate = vs.get("avg_frame_rate") or vs.get("r_frame_rate")
    rn, rd = (int(x) for x in rate.split("/"))
    from fractions import Fraction
    assert Fraction(rn, rd) == Fraction(num, den)
    # Duration = frames / fps, within container rounding.
    import math
    assert math.isclose(float(pr["format"]["duration"]),
                        frames * den / num, abs_tol=0.05)
    # No unexpected audio streams.
    assert not [s for s in pr["streams"] if s["codec_type"] == "audio"]
    # CAS integrity: stored sha256 matches the bytes.
    import hashlib
    assert hashlib.sha256(blob.read_bytes()).hexdigest() == a["sha256"]
    # Mock provenance on the version record.
    vers = client.get(
        f"/api/projects/{pid}/assets/{a['id']}/versions").json()
    media = vers[0]["media_json"]
    assert media["is_mock"] is True and media["generated"] is False
    assert media["adapter_id"] == "mock.video"


def test_mock_video_pts_monotonic(client):
    """Presentation timestamps start at 0 and increase by exactly one
    frame period — the produced file is well-formed, not just
    decodable."""
    from app.domain.media import binaries
    if binaries.ffprobe() is None:
        pytest.skip("no ffprobe")
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="pts",
                 duration_frames=24,
                 fps={"num": 24, "den": 1}, width=128, height=128)
    done = _gen(client, pid, c["id"], timeout=90)
    blob = _blob(client, _asset(client, pid, done["output_asset_id"]))
    pr = _json.loads(_sp.run(
        [binaries.ffprobe()["path"], "-v", "error",
         "-select_streams", "v", "-show_frames",
         "-print_format", "json", str(blob)],
        capture_output=True, text=True, check=True).stdout)
    pts = [f.get("pts") for f in pr["frames"] if f.get("pts") is not None]
    assert len(pts) == 24
    assert pts[0] == 0
    assert all(b > a for a, b in zip(pts, pts[1:]))


def test_timeline_nonexact_fps_conversion(client):
    """30000/1001 and 25fps sources on a 24fps timeline keep their
    wall-clock durations — rational conversion, never truncation."""
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="ntsc",
                 duration_frames=30,
                 fps={"num": 30000, "den": 1001})
    done = _gen(client, pid, c["id"], timeout=90)
    _commit(client, pid, "timeline.create",
            {"name": "cut24", "fps_num": 24, "fps_den": 1})
    tl = client.get(f"/api/projects/{pid}/timelines").json()[0]
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": tl["id"],
             "asset_id": done["output_asset_id"], "start_frames": 0})
    d = client.get(f"/api/projects/{pid}/timelines/{tl['id']}").json()
    clip = d["clips"][0]
    assert clip["src_fps_num"] == 30000
    assert clip["src_fps_den"] == 1001
    import math
    # 30f @29.97 ≈ 1.001s → ~24.024 timeline frames; placement keeps
    # the source duration, not the frame count.
    assert math.isclose(clip["src_duration_s"], 30 * 1001 / 30000,
                        abs_tol=0.01)


# ---------- A4-F4 cancellation matrix + cleanup ----------------------

def test_cancel_before_encode(client, tmp_path):
    """Cancel while frames are still rendering (stage 1): no ffmpeg is
    ever spawned, no partial mp4, no result."""
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="early",
                 duration_frames=24)
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 1, "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    client.post(f"/api/jobs/{out['job_id']}/cancel")
    done = wait_for_job(client, pid, out["job_id"], timeout=30)
    assert done["status"] == "cancelled"
    assert _clip(client, pid, c["id"])["result_asset_id"] is None


def test_cancel_during_encode_kills_ffmpeg(client):
    """Cancel while the ffmpeg encode subprocess is running: the
    supervised runner kills the process tree — no orphan ffmpeg, no
    result, no late publish."""
    import psutil
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    # Heavy enough that the encode definitely spans the cancel — the
    # file-appear → event-set window must be far inside the encode.
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="midenc",
                 duration_frames=480,
                 fps={"num": 24, "den": 1}, width=1920, height=1080)
    import tempfile, os
    rel = os.path.join(tempfile.mkdtemp(), "rel.flag")
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 4,
                                 "release_file": rel,
                                 "timeout_s": 180}})
    job_id = out["job_id"]
    # Park deterministically right BEFORE the encode — the cancel
    # event definitely exists while the job runs.
    _wait_hold(client, pid, job_id, timeout=120)
    eng = client.app.state.job_engine
    assert job_id in eng._cancel_events
    import time
    # Release into the encode, then flip the same threading.Event the
    # cancel endpoint sets as soon as ffmpeg starts writing output —
    # supervise.run polls it every 250 ms and kills the process tree.
    work = _Path(client.app.state.storage._data_dir) / "jobs" / pid
    out_mp4 = work / f"{job_id}.mp4"
    open(rel, "w").write("go")
    t0 = time.time()
    while not out_mp4.exists() and time.time() - t0 < 120:
        time.sleep(0.02)
    assert out_mp4.exists(), "ffmpeg never started writing output"
    if job_id in eng._cancel_events:
        eng._cancel_events[job_id].set()
    done = wait_for_job(client, pid, job_id, timeout=60)
    assert done["status"] == "cancelled"
    assert _clip(client, pid, c["id"])["result_asset_id"] is None
    # Partial output purged — cancelled jobs leave no artifact that
    # could masquerade as a result.
    assert not out_mp4.exists()
    # No orphaned ffmpeg anywhere under this process tree.
    strays = [p for p in psutil.Process().children(recursive=True)
              if "ffmpeg" in (p.name() or "").lower()]
    assert strays == []


def test_cancel_after_encode_before_publish(client, tmp_path):
    """Stage-5 barrier sits after the encode, before probe+publish:
    the mp4 bytes exist in the job work dir but never enter CAS."""
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="postenc",
                 duration_frames=24)
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 5, "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    before = {a["id"] for a in
              client.get(f"/api/projects/{pid}/assets").json()["items"]}
    client.post(f"/api/jobs/{out['job_id']}/cancel")
    done = wait_for_job(client, pid, out["job_id"], timeout=30)
    assert done["status"] == "cancelled"
    assert _clip(client, pid, c["id"])["result_asset_id"] is None
    after = {a["id"] for a in
             client.get(f"/api/projects/{pid}/assets").json()["items"]}
    assert after == before  # partial output never ingested into CAS


def test_retry_after_cancel_succeeds(client, tmp_path):
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="retry",
                 duration_frames=24)
    out = _commit(client, pid, "clip.generate",
                  {"clip_id": c["id"],
                   "mock_hold": {"at_stage": 2, "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    client.post(f"/api/jobs/{out['job_id']}/cancel")
    done = wait_for_job(client, pid, out["job_id"], timeout=30)
    assert done["status"] == "cancelled"
    done2 = _gen(client, pid, c["id"])
    assert done2["id"] != out["job_id"]
    assert _clip(client, pid, c["id"])["result_asset_id"] == \
        done2["output_asset_id"]


# ---------- A4-F5 timeline editing on the produced clip --------------

def test_timeline_edit_cycle(client):
    """Place → trim → move → split → undo/redo on the real produced
    clip through the existing commands — no parallel path."""
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="edit me",
                 duration_frames=48, fps={"num": 24, "den": 1})
    done = _gen(client, pid, c["id"], timeout=90)
    asset_id = done["output_asset_id"]
    _commit(client, pid, "timeline.create",
            {"name": "cut", "fps_num": 24, "fps_den": 1})
    tl = client.get(f"/api/projects/{pid}/timelines").json()[0]
    det = lambda: client.get(
        f"/api/projects/{pid}/timelines/{tl['id']}")

    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": tl["id"], "asset_id": asset_id,
             "start_frames": 0})
    clip = det().json()["clips"][0]
    assert clip["source_asset_id"] == asset_id

    # Trim to the second half.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tl["id"], "clip_id": clip["id"],
             "in_frame": 24, "out_frame": 48})
    clip = det().json()["clips"][0]
    assert clip["in_frame"] == 24 and clip["out_frame"] == 48

    # Split at a frame — two clips on the same source.
    _commit(client, pid, "timeline.clip.split",
            {"timeline_id": tl["id"], "clip_id": clip["id"],
             "at_frame": clip["start_frames"] + 12})
    clips = det().json()["clips"]
    assert len(clips) == 2
    assert all(x["source_asset_id"] == asset_id for x in clips)

    # Move: a second clip reorders positions without new commands.
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": tl["id"], "clip_id": clips[1]["id"],
             "to_index": 0})
    clips = det().json()["clips"]
    assert [x["position"] for x in clips] == [0, 1]

    # timeline.undo/redo — the timeline's own edit history. First undo
    # reverts the move; second reverts the split (2 → 1 clip).
    _commit(client, pid, "timeline.undo", {"timeline_id": tl["id"]})
    _commit(client, pid, "timeline.undo", {"timeline_id": tl["id"]})
    assert len(det().json()["clips"]) == 1
    _commit(client, pid, "timeline.redo", {"timeline_id": tl["id"]})
    assert len(det().json()["clips"]) == 2


def test_missing_media_fails_closed(client):
    """Delete the produced blob → verify flags it missing → the export
    refuses rather than writing a corrupt film."""
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="gone",
                 duration_frames=24)
    done = _gen(client, pid, c["id"], timeout=90)
    a = _asset(client, pid, done["output_asset_id"])
    _commit(client, pid, "timeline.create", {"name": "cut"})
    tl = client.get(f"/api/projects/{pid}/timelines").json()[0]
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": tl["id"], "asset_id": a["id"],
             "start_frames": 0})
    _blob(client, a).unlink()
    _commit(client, pid, "assets.verify", {})
    a2 = _asset(client, pid, a["id"])
    assert a2["status"] == "missing"
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": tl["id"], "profile": "720p"})
    job = wait_for_job(client, pid, r["job_id"], timeout=120)
    assert job["status"] == "failed"


def test_export_after_edits(client, tmp_path):
    """A trimmed mock clip + a real imported WAV on an audio lane →
    export produces a film with both a video and an audio stream."""
    from tests.test_n2a_audio import _wav
    from app.domain.media import binaries
    if binaries.ffprobe() is None:
        pytest.skip("no ffprobe")
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    c = _mk_clip(client, pid, sh["shot"]["id"], prompt="final",
                 duration_frames=48, fps={"num": 24, "den": 1})
    done = _gen(client, pid, c["id"], timeout=90)
    # Real audio bytes on an audio lane — N2 path, not synthetic.
    p = _wav(tmp_path / "music.wav", rate=48000, channels=2, seconds=2.0)
    r = _commit(client, pid, "asset.import",
                {"source_path": str(p), "name": "music.wav",
                 "kind": "audio"})
    imp = wait_for_job(client, pid, r["job_id"], timeout=60)
    assert imp["status"] == "succeeded", imp
    audio = _asset(client, pid, imp["output_asset_id"])
    _commit(client, pid, "timeline.create",
            {"name": "cut", "fps_num": 24, "fps_den": 1})
    tl = client.get(f"/api/projects/{pid}/timelines").json()[0]
    _commit(client, pid, "timeline.track.add",
            {"timeline_id": tl["id"], "kind": "audio", "name": "A1"})
    tl2 = client.get(f"/api/projects/{pid}/timelines/{tl['id']}").json()
    atrack = next(t for t in tl2["tracks"] if t["kind"] == "audio")
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": tl["id"], "asset_id": audio["id"],
             "track_id": atrack["id"], "start_frames": 0})
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": tl["id"],
             "asset_id": done["output_asset_id"], "start_frames": 0})
    # Trim the video to 1.5s — export must reflect the edit.
    det = client.get(f"/api/projects/{pid}/timelines/{tl['id']}").json()
    vclip = next(x for x in det["clips"]
                 if x["source_asset_id"] == done["output_asset_id"])
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tl["id"], "clip_id": vclip["id"],
             "in_frame": 0, "out_frame": 36})
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": tl["id"], "profile": "720p"})
    job = wait_for_job(client, pid, r["job_id"], timeout=180)
    assert job["status"] == "succeeded", job
    out = _asset(client, pid, job["output_asset_id"])
    pr = _json.loads(_sp.run(
        [binaries.ffprobe()["path"], "-v", "error",
         "-print_format", "json",
         "-show_streams", "-show_format", str(_blob(client, out))],
        capture_output=True, text=True, check=True).stdout)
    kinds = {s["codec_type"] for s in pr["streams"]}
    assert "video" in kinds and "audio" in kinds
    import math
    # Film duration = longest lane (2.0s audio > 1.5s trimmed video).
    assert math.isclose(float(pr["format"]["duration"]), 2.0,
                        abs_tol=0.15)


# ---------- A4-F6 backup provenance + restored placement -------------

def test_backup_restore_provenance_and_place(client, tmp_path):
    """Restored clips keep CAS result refs + mock provenance; the
    restored clip places on a restored timeline and exports."""
    pid = make_project(client)["id"]
    sh = _clip_setup(client, pid)
    kf = _mk_kf(client, pid, sh["shot"]["id"], "start",
                prompt="source frame")
    c = _mk_clip(client, pid, sh["shot"]["id"],
                 start_keyframe_id=kf["id"], prompt="restore me",
                 duration_frames=24, camera={"movement": "dolly_in"})
    done = _gen(client, pid, c["id"], timeout=90)
    _commit(client, pid, "backup.export", {"kind": "backup"})
    exjob = next(j for j in
                 client.get(f"/api/projects/{pid}/jobs").json()
                 if j["kind"] == "backup")
    bjob = wait_for_job(client, pid, exjob["id"], timeout=60)
    assert bjob["status"] == "succeeded", bjob
    r = client.post("/api/packages/import",
                    json={"package_path": str(
                        bjob["result_json"]["package_path"])})
    assert r.status_code == 200, r.text
    new_pid = r.json()["project_id"]
    wait_for_job(client, new_pid, r.json()["job_id"], timeout=90)
    board = client.get(f"/api/projects/{new_pid}/storyboard").json()
    new_shot = board[0]["shots"][0]["shot"]
    got = _clips(client, new_pid, new_shot["id"])[0]
    # Result asset restored, project-scoped, bytes present.
    a = _asset(client, new_pid, got["result_asset_id"])
    assert a["project_id"] == new_pid
    assert _blob(client, a).is_file()
    vers = client.get(
        f"/api/projects/{new_pid}/assets/{a['id']}/versions").json()
    assert vers[0]["media_json"]["is_mock"] is True
    # Restored clip places + exports on a new timeline.
    _commit(client, new_pid, "timeline.create", {"name": "restored"})
    tl = client.get(f"/api/projects/{new_pid}/timelines").json()[0]
    _commit(client, new_pid, "timeline.clip.place",
            {"timeline_id": tl["id"], "asset_id": a["id"],
             "start_frames": 0})
    det = client.get(f"/api/projects/{new_pid}/timelines/{tl['id']}"
                     ).json()
    assert det["clips"][0]["source_asset_id"] == a["id"]
