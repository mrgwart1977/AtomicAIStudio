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
