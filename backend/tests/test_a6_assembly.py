"""A6 — first-cut assembly: deterministic plan → review → apply.

Coverage: persistence + migration, deterministic scene/shot ordering,
preview resolved_assembly, missing-media flags, lane resolution +
creation, apply placing clips with shot/scene provenance, idempotent
re-apply, stale-source refusal, manual-edit preservation + explicit
replace approval, per-item review gating, collapsed timeline undo,
dialogue overflow lanes, shot-linked audio, backup/restore (v7),
revert + versions, project isolation.
"""
import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_a1_screenplay import (
    _character, _scene as _sp_scene, _set_elements,
    _dialogue, _detail as _sp_detail)
from tests.test_a2_storyboard import _scene2, _shot2, _undo
from tests.test_a4_motion_clips import _mk_clip, _gen as _gen_clip
from tests.test_a5_dialogue import (
    _mk_screenplay, _mk_full, _gen as _gen_dlg, _segments)
from tests.test_c5a_timeline import _tl, _detail
from tests.test_n1a_tracks import _track
from tests.test_n2a_audio import _audio, _place


def _plans(client, pid, **flt):
    qs = "&".join(f"{k}={v}" for k, v in flt.items())
    r = client.get(f"/api/projects/{pid}/assemblies?{qs}")
    assert r.status_code == 200, r.text
    return r.json()


def _plan_detail(client, pid, plan_id):
    r = client.get(f"/api/projects/{pid}/assemblies/{plan_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _plan(client, pid, tid, name=None, expect=200):
    payload = {"timeline_id": tid}
    if name:
        payload["name"] = name
    r = _commit(client, pid, "assembly.plan", payload, expect=expect)
    if expect != 200:
        return r
    return _plan_detail(
        client, pid, _plans(client, pid, timeline_id=tid)[0]["id"])


def _apply(client, pid, tid, expect=200, **kw):
    return _commit(client, pid, "assembly.apply",
                   {"timeline_id": tid, **kw}, expect=expect)


def _review(client, pid, plan_id, expect=200, **kw):
    return _commit(client, pid, "assembly.review",
                   {"plan_id": plan_id, **kw}, expect=expect)


def _mk_take(client, pid, shot_id, **kw):
    """A motion clip with a generated result asset on the shot."""
    c = _mk_clip(client, pid, shot_id,
                 prompt=kw.pop("prompt", "push in slowly"),
                 duration_frames=kw.pop("duration_frames", 48),
                 fps=kw.pop("fps", {"num": 24, "den": 1}), **kw)
    _gen_clip(client, pid, c["id"])
    return c


def _mk_line(client, pid, su, shot, **kw):
    """A shot-bound dialogue segment with a generated audio result."""
    d = _mk_full(client, pid, su, shot_id=shot["shot"]["id"], **kw)
    _gen_dlg(client, pid, d["id"])
    return d


def _setup_scene(client, pid, title="S1", heading="INT. A — DAY"):
    """Storyboard scene + screenplay scene bound to it + one shot."""
    sc = _scene2(client, pid, title=title)
    sh = _shot2(client, pid, sc["scene"]["id"])
    sp = _mk_screenplay(client, pid, title=f"SP {title}")
    spsc = _sp_scene(client, pid, sp["id"], heading,
                     scene_id=sc["scene"]["id"])
    return {"scene": sc, "shot": sh, "sp": sp, "spsc": spsc}


def _full_setup(client, pid):
    """Two screenplay-ordered scenes, each with a shot + generated
    take + a dialogue line with a generated result."""
    su1 = _setup_scene(client, pid, "S1", "INT. DOCK — NIGHT")
    su2 = _setup_scene(client, pid, "S2", "EXT. PIER — DAWN")
    anna = _character(client, pid, "Anna")
    for su, text in ((su1, "The tide is coming in."),
                     (su2, "Hold the rope.")):
        _set_elements(client, pid, su["spsc"]["id"],
                      [_dialogue(anna["id"], text)])
        su["element"] = _sp_detail(
            client, pid, su["sp"]["id"])["scenes"][0]["elements"][0]
        su["anna"] = anna
    c1 = _mk_take(client, pid, su1["shot"]["shot"]["id"])
    c2 = _mk_take(client, pid, su2["shot"]["shot"]["id"])
    d1 = _mk_line(client, pid, su1, su1["shot"])
    d2 = _mk_line(client, pid, su2, su2["shot"], text="Hold the rope.")
    return {"s1": su1, "s2": su2, "clip1": c1, "clip2": c2,
            "d1": d1, "d2": d2}


# ---------- persistence + schema ----------

def test_plan_persists_and_versions(client):
    pid = make_project(client)["id"]
    su = _setup_scene(client, pid)
    _mk_take(client, pid, su["shot"]["shot"]["id"])
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"], name="First cut")
    assert plan["name"] == "First cut"
    assert plan["status"] == "draft" and plan["revision"] == 1
    assert len(plan["items"]) == 1
    it = plan["items"][0]
    assert it["kind"] == "shot" and it["shot_id"] == su["shot"]["shot"]["id"]
    assert it["start_frames"] == 0 and it["extent_frames"] == 48
    assert it["review_status"] == "pending"
    r = client.get(f"/api/projects/{pid}/assemblies/{plan['id']}/versions")
    assert r.status_code == 200 and len(r.json()) >= 1


def test_plan_is_idempotent_per_timeline(client):
    pid = make_project(client)["id"]
    su = _setup_scene(client, pid)
    t = _tl(client, pid)
    p1 = _plan(client, pid, t["id"])
    p2 = _plan(client, pid, t["id"])
    assert p1["id"] == p2["id"]  # one plan per timeline, recomputed
    assert p2["revision"] == p1["revision"] + 1


def test_project_isolation(client):
    p1 = make_project(client)["id"]
    p2 = make_project(client, name="Other")["id"]
    su = _setup_scene(client, p1)
    t = _tl(client, p1)
    plan = _plan(client, p1, t["id"])
    r = client.get(f"/api/projects/{p2}/assemblies/{plan['id']}")
    assert r.status_code == 404
    r = _commit(client, p2, "assembly.apply",
                {"plan_id": plan["id"]}, expect=404)
    assert "plan" in r.text.lower()


# ---------- deterministic ordering + preview ----------

def test_deterministic_scene_and_shot_order(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    pv = _preview(client, pid, "assembly.plan", {"timeline_id": t["id"]})
    ra = pv["resolved_assembly"]
    assert ra["counts"]["items"] == 4  # 2 shots + 2 dialogue
    shot_items = [i for i in ra["items"] if i["kind"] == "shot"]
    assert shot_items[0]["shot_id"] == su["s1"]["shot"]["shot"]["id"]
    assert shot_items[1]["shot_id"] == su["s2"]["shot"]["shot"]["id"]
    assert shot_items[0]["start_frames"] == 0
    assert shot_items[1]["start_frames"] == shot_items[0]["extent_frames"]
    # Dialogue follows its shot's span start.
    dx = [i for i in ra["items"] if i["kind"] == "dialogue"]
    assert dx[0]["start_frames"] == 0
    assert dx[1]["start_frames"] == shot_items[1]["start_frames"]
    # Identical recompute — determinism.
    pv2 = _preview(client, pid, "assembly.plan", {"timeline_id": t["id"]})
    assert pv2["resolved_assembly"]["items"] == ra["items"]
    assert ra["program_frames"] == \
        shot_items[0]["extent_frames"] + shot_items[1]["extent_frames"]


def test_preview_shows_lanes_and_missing(client):
    pid = make_project(client)["id"]
    su = _setup_scene(client, pid)
    t = _tl(client, pid)
    pv = _preview(client, pid, "assembly.plan", {"timeline_id": t["id"]})
    lanes = pv["resolved_assembly"]["lanes"]
    assert lanes["video"]["track_id"] is not None  # default V1
    assert lanes["dialogue"]["create"] == {"kind": "audio", "name": "DX"}
    assert lanes["music"]["create"] == {"kind": "audio", "name": "MX"}
    # The shot has no generated clip → missing media flag.
    it = pv["resolved_assembly"]["items"][0]
    assert it["flags"]["missing"] == "no motion-clip result"
    assert pv["resolved_assembly"]["counts"]["missing"] == 1
    assert any("no motion-clip result" in w
               for w in pv["resolved_assembly"]["warnings"])


def test_missing_dialogue_result_flagged(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    d_noresult = _mk_full(client, pid, su["s1"],
                          shot_id=su["s1"]["shot"]["shot"]["id"],
                          text="ungenerated line")
    t = _tl(client, pid)
    pv = _preview(client, pid, "assembly.plan", {"timeline_id": t["id"]})
    items = {i["key"]: i for i in pv["resolved_assembly"]["items"]}
    di = items[f"dialogue:{d_noresult['id']}"]
    assert "no audio result" in di["flags"]["missing"]


# ---------- apply ----------

def test_apply_places_clips_with_provenance(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    _plan(client, pid, t["id"])
    r = _apply(client, pid, t["id"])
    det = _detail(client, pid, t["id"])
    kinds = {tr["kind"] for tr in det["tracks"]}
    assert {"video", "audio"} <= kinds
    names = {tr["name"] for tr in det["tracks"]}
    # DX + overflow DX2 (the lines run longer than one shot span);
    # MX is absent — no shot-linked audio in this setup.
    assert {"V1", "DX", "DX2"} <= names
    video_clips = [c for c in det["clips"]
                   if c["track_id"] == next(
                       tr["id"] for tr in det["tracks"]
                       if tr["kind"] == "video")]
    assert len(video_clips) == 2
    assert video_clips[0]["shot_id"] == su["s1"]["shot"]["shot"]["id"]
    assert video_clips[1]["shot_id"] == su["s2"]["shot"]["shot"]["id"]
    assert video_clips[0]["start_frames"] == 0
    audio_ids = {tr["id"] for tr in det["tracks"]
                 if tr["kind"] == "audio"}
    dx_clips = [c for c in det["clips"] if c["track_id"] in audio_ids]
    assert len(dx_clips) == 2
    plan = _plan_detail(client, pid,
                        _plans(client, pid, timeline_id=t["id"])[0]["id"])
    assert plan["status"] == "applied"
    applied_items = [i for i in plan["items"] if i["timeline_clip_id"]]
    assert len(applied_items) == 4
    assert all(i["on_timeline"] for i in applied_items)


def test_apply_is_idempotent(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    n_clips = len(_detail(client, pid, t["id"])["clips"])
    _apply(client, pid, t["id"])
    assert len(_detail(client, pid, t["id"])["clips"]) == n_clips


def test_apply_refuses_stale_sources(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    _plan(client, pid, t["id"])
    # Source drift after planning — shot edit bumps its revision.
    _commit(client, pid, "shot.update",
            {"shot_id": su["s1"]["shot"]["shot"]["id"],
             "title": "renamed"})
    r = _apply(client, pid, t["id"], expect=422)
    assert "re-run assembly.plan" in r.text
    # Re-plan + apply succeeds.
    _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    assert len(_detail(client, pid, t["id"])["clips"]) == 4


def test_apply_preview_parity(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    _plan(client, pid, t["id"])
    pv = _preview(client, pid, "assembly.apply",
                  {"timeline_id": t["id"]})
    ra = pv["resolved_assembly"]
    assert ra["sources_stale"] is False
    assert ra["counts"]["insert"] == 4 and ra["counts"]["skip"] == 0
    _apply(client, pid, t["id"])
    pv2 = _preview(client, pid, "assembly.apply",
                   {"timeline_id": t["id"]})
    assert pv2["resolved_assembly"]["counts"]["keep"] == 4
    # Source drift flags stale on the apply preview.
    _commit(client, pid, "shot.update",
            {"shot_id": su["s1"]["shot"]["shot"]["id"], "title": "x"})
    pv3 = _preview(client, pid, "assembly.apply",
                   {"timeline_id": t["id"]})
    assert pv3["resolved_assembly"]["sources_stale"] is True


# ---------- manual-edit preservation + conflicts ----------

def test_manual_clip_conflict_preserved(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    au = _audio(client, pid, name="manual.wav")
    dx = _track(client, pid, t["id"], kind="audio", name="DX")
    _place(client, pid, t["id"], au, dx["id"], at=0)
    plan = _plan(client, pid, t["id"])
    d_items = [i for i in plan["items"] if i["kind"] == "dialogue"]
    assert any(i["flags"]["conflicts"] for i in d_items)
    _apply(client, pid, t["id"])
    det = _detail(client, pid, t["id"])
    manual = [c for c in det["clips"] if c["source_asset_id"] == au["id"]]
    assert len(manual) == 1  # manual clip untouched
    # The conflicting dialogue item skipped, not overwritten.
    dx_items = [i for i in _plan_detail(
        client, pid, plan["id"])["items"]
        if i["kind"] == "dialogue"]
    assert not all(i["timeline_clip_id"] for i in dx_items)


def test_conflict_replace_requires_approval(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    au = _audio(client, pid, name="manual.wav")
    dx = _track(client, pid, t["id"], kind="audio", name="DX")
    _place(client, pid, t["id"], au, dx["id"], at=0)
    _plan(client, pid, t["id"])
    r = _apply(client, pid, t["id"], resolve_conflicts="bogus",
               expect=422)
    assert "resolve_conflicts" in r.text
    _apply(client, pid, t["id"], resolve_conflicts="replace")
    det = _detail(client, pid, t["id"])
    assert not [c for c in det["clips"]
                if c["source_asset_id"] == au["id"]]


def test_applied_clip_manual_edit_preserved(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    plan = _plan_detail(
        client, pid, _plans(client, pid, timeline_id=t["id"])[0]["id"])
    shot_item = next(i for i in plan["items"] if i["kind"] == "shot")
    clip_id = shot_item["timeline_clip_id"]
    # Manual move — the clip is now user-edited.
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"], "clip_id": clip_id,
             "start_frames": 500})
    plan2 = _plan(client, pid, t["id"])
    it2 = next(i for i in plan2["items"] if i["kind"] == "shot")
    assert any(c.get("clip_id") == clip_id
               for c in it2["flags"]["conflicts"])
    _apply(client, pid, t["id"])
    det = _detail(client, pid, t["id"])
    moved = next(c for c in det["clips"] if c["id"] == clip_id)
    assert moved["start_frames"] == 500  # user's move preserved


def test_removed_source_clip_kept_unless_replace(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    plan = _plan_detail(
        client, pid, _plans(client, pid, timeline_id=t["id"])[0]["id"])
    clip_ids = {i["timeline_clip_id"] for i in plan["items"]
                if i["timeline_clip_id"]}
    # Archive shot 2 → its item leaves the plan.
    _commit(client, pid, "shot.archive",
            {"shot_id": su["s2"]["shot"]["shot"]["id"]})
    plan2 = _plan(client, pid, t["id"])
    assert plan2["proposal"]["counts"]["removed"] >= 1
    _apply(client, pid, t["id"])
    # The applied clip stays (fail-safe default).
    remaining = {c["id"] for c in _detail(client, pid, t["id"])["clips"]}
    assert clip_ids <= remaining


# ---------- review ----------

def test_review_reject_skips_item(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    shot_item = next(i for i in plan["items"] if i["kind"] == "shot")
    _review(client, pid, plan["id"],
            item_ids=[shot_item["id"]], status="rejected",
            note="wrong take")
    got = _plan_detail(client, pid, plan["id"])
    assert next(i for i in got["items"]
                if i["id"] == shot_item["id"])["review_status"] == "rejected"
    assert next(i for i in got["items"]
                if i["id"] == shot_item["id"])["notes"] == "wrong take"
    _apply(client, pid, t["id"])
    det = _detail(client, pid, t["id"])
    video_clips = [c for c in det["clips"]
                   if c["track_id"] == next(
                       tr["id"] for tr in det["tracks"]
                       if tr["kind"] == "video")]
    assert len(video_clips) == 1
    assert video_clips[0]["shot_id"] == su["s2"]["shot"]["shot"]["id"]


def test_review_by_scene_and_note_roundtrip(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    _review(client, pid, plan["id"],
            scene_id=su["s1"]["scene"]["scene"]["id"],
            status="approved", note="scene 1 ok")
    got = _plan_detail(client, pid, plan["id"])
    s1_items = [i for i in got["items"]
                if i["scene_id"] == su["s1"]["scene"]["scene"]["id"]]
    assert all(i["review_status"] == "approved" for i in s1_items)
    r = _review(client, pid, plan["id"], status="approved", expect=422)
    assert "target" in r.text
    r = _review(client, pid, plan["id"],
                item_ids=["bogus"], status="approved", expect=422)
    assert "matched" in r.text


# ---------- undo ----------

def test_plan_undo_restores_prior(client):
    pid = make_project(client)["id"]
    su = _setup_scene(client, pid)
    _mk_take(client, pid, su["shot"]["shot"]["id"])
    t = _tl(client, pid)
    p1 = _plan(client, pid, t["id"])
    cmds = client.get(f"/api/projects/{pid}/commands").json()
    plan_cmd = next(c for c in reversed(cmds)
                    if c["command_type"] == "assembly.plan")
    _undo(client, pid, plan_cmd["id"])
    r = client.get(f"/api/projects/{pid}/assemblies/{p1['id']}")
    assert r.status_code == 404  # first plan undo deletes the row


def test_apply_undo_via_timeline_one_step(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    assert len(_detail(client, pid, t["id"])["clips"]) == 4
    # One timeline.undo removes the whole apply (collapsed entry).
    _commit(client, pid, "timeline.undo", {"timeline_id": t["id"]})
    det = _detail(client, pid, t["id"])
    assert det["clips"] == []
    assert {tr["name"] for tr in det["tracks"]} == {"V1"}
    _commit(client, pid, "timeline.redo", {"timeline_id": t["id"]})
    assert len(_detail(client, pid, t["id"])["clips"]) == 4


def test_review_undo_restores_status(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    shot_item = next(i for i in plan["items"] if i["kind"] == "shot")
    _review(client, pid, plan["id"], item_ids=[shot_item["id"]],
            status="rejected")
    cmds = client.get(f"/api/projects/{pid}/commands").json()
    rev_cmd = next(c for c in reversed(cmds)
                   if c["command_type"] == "assembly.review")
    _undo(client, pid, rev_cmd["id"])
    got = _plan_detail(client, pid, plan["id"])
    assert next(i for i in got["items"]
                if i["id"] == shot_item["id"])["review_status"] == "pending"


# ---------- dialogue overflow + audio lanes ----------

def test_dialogue_overflow_stacks_on_dx2(client):
    pid = make_project(client)["id"]
    su = _setup_scene(client, pid)
    anna = _character(client, pid, "Anna")
    _set_elements(client, pid, su["spsc"]["id"],
                  [_dialogue(anna["id"], "line one")])
    su["element"] = _sp_detail(
        client, pid, su["sp"]["id"])["scenes"][0]["elements"][0]
    su["anna"] = anna
    # Two long lines at the same shot start — the second overflows.
    d1 = _mk_line(client, pid, su, su["shot"], text="first")
    d2 = _mk_line(client, pid, su, su["shot"], text="second")
    t = _tl(client, pid)
    pv = _preview(client, pid, "assembly.plan", {"timeline_id": t["id"]})
    lanes = pv["resolved_assembly"]["lanes"]
    dx_items = [i for i in pv["resolved_assembly"]["items"]
                if i["kind"] == "dialogue"]
    lane_keys = {i["lane_key"] for i in dx_items}
    assert lane_keys == {"dialogue", "dialogue2"}
    assert "dialogue2" in lanes
    _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    det = _detail(client, pid, t["id"])
    audio_tracks = [tr for tr in det["tracks"] if tr["kind"] == "audio"]
    assert len(audio_tracks) >= 2
    names = {tr["name"] for tr in audio_tracks}
    assert {"DX", "DX2"} <= names


def test_shot_linked_audio_on_music_lane(client):
    pid = make_project(client)["id"]
    su = _setup_scene(client, pid)
    _mk_take(client, pid, su["shot"]["shot"]["id"])
    au = _audio(client, pid, name="ambience.wav", duration=2.0)
    _commit(client, pid, "entity.assets.link",
            {"entity_type": "shot",
             "entity_id": su["shot"]["shot"]["id"],
             "asset_id": au["id"], "role": "voice",
             "label": "ambience"})
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    audio_items = [i for i in plan["items"] if i["kind"] == "audio"]
    assert len(audio_items) == 1
    assert audio_items[0]["asset_id"] == au["id"]
    _apply(client, pid, t["id"])
    det = _detail(client, pid, t["id"])
    mx = next(tr for tr in det["tracks"] if tr["name"] == "MX")
    mx_clips = [c for c in det["clips"] if c["track_id"] == mx["id"]]
    assert len(mx_clips) == 1
    assert mx_clips[0]["source_asset_id"] == au["id"]
    # 2 s at 48 kHz → 48 timeline frames at 24 fps.
    assert mx_clips[0]["src_frames"] == 96000


def test_scene_level_dialogue_places_at_scene_start(client):
    pid = make_project(client)["id"]
    su = _setup_scene(client, pid)
    anna = _character(client, pid, "Anna")
    _set_elements(client, pid, su["spsc"]["id"],
                  [_dialogue(anna["id"], "scene vo")])
    su["element"] = _sp_detail(
        client, pid, su["sp"]["id"])["scenes"][0]["elements"][0]
    su["anna"] = anna
    _mk_take(client, pid, su["shot"]["shot"]["id"])
    d = _mk_full(client, pid, su,
                 scene_id=su["scene"]["scene"]["id"], shot_id=None,
                 text="scene voiceover")
    _gen_dlg(client, pid, d["id"])
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    dx = next(i for i in plan["items"] if i["kind"] == "dialogue")
    assert dx["start_frames"] == 0 and dx["shot_id"] is None


def test_rational_fps_motion_clip_extent(client):
    pid = make_project(client)["id"]
    su = _setup_scene(client, pid)
    # 48 frames at 30 fps on a 24 fps timeline → 38.4 TL frames →
    # grid footprint ceil = 39.
    c = _mk_take(client, pid, su["shot"]["shot"]["id"],
                 duration_frames=48, fps={"num": 30, "den": 1})
    t = _tl(client, pid)  # 24 fps timeline
    pv = _preview(client, pid, "assembly.plan", {"timeline_id": t["id"]})
    it = pv["resolved_assembly"]["items"][0]
    assert it["extent_frames"] == 39


# ---------- revert ----------

def test_revert_restores_prior_plan(client):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    p1 = _plan(client, pid, t["id"], name="cut A")
    _commit(client, pid, "shot.archive",
            {"shot_id": su["s2"]["shot"]["shot"]["id"]})
    p2 = _plan(client, pid, t["id"])
    # Orphaned rows stay with a removed flag so the next apply can
    # reconcile their managed clips — count active items instead.
    active2 = [i for i in p2["items"]
               if not i["flags"].get("removed")]
    assert len(active2) < len(p1["items"])
    _commit(client, pid, "assembly.revert",
            {"plan_id": p2["id"], "to_revision": p1["revision"]})
    p3 = _plan_detail(client, pid, p2["id"])
    active3 = [i for i in p3["items"]
               if not i["flags"].get("removed")]
    assert len(active3) == len(p1["items"])


# ---------- backup / restore (v7) ----------

def _export_import(client, pid):
    """Export the project, import as a fresh project, return new pid."""
    _commit(client, pid, "backup.export", {"kind": "backup"})
    job = client.get(f"/api/projects/{pid}/jobs").json()[0]
    done = wait_for_job(client, pid, job["id"], timeout=60)
    assert done["status"] == "succeeded", done
    r = client.post("/api/packages/import",
                    json={"package_path": done["result_json"]["package_path"]})
    assert r.status_code == 200, r.text
    done = wait_for_job(client, r.json()["project_id"],
                        r.json()["job_id"], timeout=60)
    assert done["status"] == "succeeded", done.get("error") or done
    return r.json()["project_id"]


def test_backup_restore_roundtrip(client, tmp_path):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    _review(client, pid, plan["id"],
            scene_id=su["s1"]["scene"]["scene"]["id"],
            status="approved", note="s1 reviewed")
    _apply(client, pid, t["id"])
    n_clips = len(_detail(client, pid, t["id"])["clips"])
    new_pid = _export_import(client, pid)
    plans = _plans(client, new_pid)
    assert len(plans) == 1
    new_plan = _plan_detail(client, new_pid, plans[0]["id"])
    assert new_plan["status"] == "applied"
    assert len(new_plan["items"]) == len(plan["items"])
    # Review state + applied clip refs survived the id remap.
    s1 = [i for i in new_plan["items"]
          if i["review_status"] == "approved"]
    assert s1 and all(i["on_timeline"] for i in s1)
    tls = client.get(f"/api/projects/{new_pid}/timelines").json()
    assert len(tls) == 1
    det = _detail(client, new_pid, tls[0]["id"])
    assert len(det["clips"]) == n_clips
    # Stable keys remapped — a re-plan re-keys items, not duplicates.
    _commit(client, new_pid, "assembly.plan",
            {"timeline_id": tls[0]["id"]})
    p2 = _plan_detail(client, new_pid, plans[0]["id"])
    assert len(p2["items"]) == len(plan["items"])


def test_restart_persistence(client, tmp_path):
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    # Re-open a session — the plan + applied records are durable rows.
    with client.app.state.session_factory() as s:
        from app.db.models import AssemblyPlan, AssemblyItem
        p = s.query(AssemblyPlan).filter_by(id=plan["id"]).one()
        assert p.status == "applied" and p.timeline_id == t["id"]
        assert p.applied_json["items"]
        n = s.query(AssemblyItem).filter_by(plan_id=p.id).count()
        assert n == len(plan["items"])
