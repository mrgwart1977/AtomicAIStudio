"""A6 — first-cut assembly: deterministic plan → review → apply.

Coverage: persistence + migration, deterministic scene/shot ordering,
preview resolved_assembly, missing-media flags, lane resolution +
creation, apply placing clips with shot/scene provenance, idempotent
re-apply, stale-source refusal, manual-edit preservation + explicit
replace approval, per-item review gating, collapsed timeline undo,
dialogue overflow lanes, shot-linked audio, backup/restore (v7),
revert + versions, project isolation.
"""
from pathlib import Path

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
    plan = _plan(client, pid, t["id"])
    conflicted = [i["key"] for i in plan["items"]
                  if i["flags"]["conflicts"]]
    assert conflicted
    r = _apply(client, pid, t["id"], resolve_conflicts="bogus",
               expect=422)
    assert "resolve_conflicts" in r.text
    # Bare 'replace' is refused — approval must name the items.
    r = _apply(client, pid, t["id"], resolve_conflicts="replace",
               expect=422)
    assert "replace_keys" in r.text
    _apply(client, pid, t["id"], resolve_conflicts="replace",
           replace_keys=conflicted)
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


# ---------- F2: atomic apply ----------

def test_apply_failure_rolls_back_everything(client, monkeypatch):
    """A catastrophic mid-apply failure commits nothing: no partial
    clips, no applied records, no undo entry, plan stays draft — and a
    clean retry applies fully + idempotently."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    from app.domain import timeline as tld
    orig = tld.insert_clip
    calls = {"n": 0}

    def boom(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected mid-apply failure")
        return orig(*a, **kw)

    monkeypatch.setattr(tld, "insert_clip", boom)
    with pytest.raises(RuntimeError):
        _apply(client, pid, t["id"])
    monkeypatch.setattr(tld, "insert_clip", orig)

    det = _detail(client, pid, t["id"])
    assert det["clips"] == []
    assert det["undo_label"] is None
    with client.app.state.session_factory() as s:
        from app.db.models import AssemblyPlan, Timeline
        row = s.get(AssemblyPlan, plan["id"])
        assert row.status == "draft"
        assert not (row.applied_json or {}).get("items")
        assert (s.get(Timeline, t["id"]).undo_stack_json or []) == []

    # Clean retry: full apply, then an idempotent no-op replay.
    _apply(client, pid, t["id"])
    n = len(_detail(client, pid, t["id"])["clips"])
    assert n >= 3
    _apply(client, pid, t["id"])
    assert len(_detail(client, pid, t["id"])["clips"]) == n


def test_single_insert_failure_records_skip(client, monkeypatch):
    """A per-item TimelineError is the designed graceful outcome: that
    item is skipped with a recorded reason, the rest of the apply
    still commits, and the plan records the partial result."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    from app.domain import timeline as tld
    orig = tld.insert_clip
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise tld.TimelineError("injected placement failure")
        return orig(*a, **kw)

    monkeypatch.setattr(tld, "insert_clip", flaky)
    _apply(client, pid, t["id"])
    got = _plan_detail(client, pid, plan["id"])
    assert got["status"] == "applied"
    with client.app.state.session_factory() as s:
        from app.db.models import AssemblyPlan
        row = s.get(AssemblyPlan, plan["id"])
        skipped = (row.applied_json or {}).get("skipped") or []
    assert any("placement failure" in (n.get("reason") or "")
               for n in skipped)
    det = _detail(client, pid, t["id"])
    # One item skipped, the remaining items still placed.
    assert len(det["clips"]) == len(
        [i for i in plan["items"]
         if not i["flags"].get("missing")]) - 1


# ---------- F3: conflict approval scope ----------

def test_replace_keys_scope_to_previewed_conflicts(client):
    """Two flagged conflicts; approving only one replaces only that
    item's clips — the unapproved manual edit is preserved."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    items = {i["key"]: i for i in
             _plan_detail(client, pid, plan["id"])["items"]}
    shot_item = next(i for i in items.values() if i["kind"] == "shot")
    dlg_item = next(i for i in items.values() if i["kind"] == "dialogue")
    v_clip_id = shot_item["timeline_clip_id"]
    d_clip_id = dlg_item["timeline_clip_id"]
    # Two manual edits — one on video, one on dialogue audio.
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"], "clip_id": v_clip_id,
             "start_frames": 300})
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"], "clip_id": d_clip_id,
             "start_frames": 320})
    plan2 = _plan(client, pid, t["id"])  # re-plan re-flags both
    items2 = {i["key"]: i for i in plan2["items"]}
    assert items2[shot_item["key"]]["flags"]["conflicts"]
    assert items2[dlg_item["key"]]["flags"]["conflicts"]

    # Preview the scoped replacement — approval sees exact scope.
    pv = _preview(client, pid, "assembly.apply",
                  {"timeline_id": t["id"],
                   "resolve_conflicts": "replace",
                   "replace_keys": [shot_item["key"]]})
    disp = {d["key"]: d for d in pv["resolved_assembly"]["items"]}
    assert disp[shot_item["key"]]["action"] == "replace"
    assert disp[dlg_item["key"]]["action"] == "skip"

    _apply(client, pid, t["id"], resolve_conflicts="replace",
           replace_keys=[shot_item["key"]])
    det = _detail(client, pid, t["id"])
    # The approved conflict was replaced at planned geometry (the old
    # moved clip is gone; the new clip sits at the planned start).
    assert not [c for c in det["clips"] if c["id"] == v_clip_id]
    v_new = next(c for c in det["clips"]
                 if c["shot_id"] == shot_item["shot_id"]
                 and det["tracks"] and any(
                     tr["id"] == c["track_id"] and tr["kind"] == "video"
                     for tr in det["tracks"]))
    assert v_new["start_frames"] == shot_item["start_frames"]
    # The unapproved manual edit is untouched.
    d_still = next(c for c in det["clips"] if c["id"] == d_clip_id)
    assert d_still["start_frames"] == 320


def test_replace_keys_rejects_unflagged_key(client):
    """Approval can't reach items the preview didn't flag — an
    unflagged key (or a bogus key) refuses the whole apply."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    shot_key = next(i["key"] for i in plan["items"]
                    if i["kind"] == "shot")
    r = _apply(client, pid, t["id"], resolve_conflicts="replace",
               replace_keys=[shot_key], expect=422)
    assert "conflict" in r.text or "replace_keys" in r.text
    _apply(client, pid, t["id"], resolve_conflicts="replace",
           replace_keys=["shot:bogus"], expect=422)


def test_post_plan_timeline_edit_refuses_apply(client):
    """A manual timeline edit between plan and apply stales the plan —
    even a validly-scoped replace approval refuses on drift."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    # Manual clip occupying the dialogue lane BEFORE the plan —
    # previewed conflict is flagged on the dialogue item.
    au = _audio(client, pid, name="manual.wav")
    dx = _track(client, pid, t["id"], kind="audio", name="DX")
    _place(client, pid, t["id"], au, dx["id"], at=0)
    plan = _plan(client, pid, t["id"])
    conflicted = [i["key"] for i in plan["items"]
                  if i["flags"]["conflicts"]]
    assert conflicted
    # A second manual edit AFTER the plan was computed → stale.
    _place(client, pid, t["id"], _audio(client, pid, name="m2.wav"),
           dx["id"], at=4000)
    _apply(client, pid, t["id"], expect=422)
    _apply(client, pid, t["id"], resolve_conflicts="replace",
           replace_keys=conflicted, expect=422)


def test_duplicate_apply_is_clean_noop(client):
    """Re-applying an unchanged plan/timeline is an audited no-op —
    no duplicate clips, no extra undo frame, no revision bump."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    p1 = _plan_detail(client, pid, plan["id"])
    _apply(client, pid, t["id"])
    p2 = _plan_detail(client, pid, plan["id"])
    assert p2["revision"] == p1["revision"]
    assert _detail(client, pid, t["id"])["undo_label"] == \
        "Applied first-cut assembly"


# ---------- F4: source freshness ----------

def test_deleted_result_asset_stales_plan(client):
    """Result-asset liveness is fingerprinted — archiving the take's
    result asset stales the plan even with the clip row untouched."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    _plan(client, pid, t["id"])
    with client.app.state.session_factory() as s:
        from app.db.models import Asset, MotionClip
        mc = s.get(MotionClip, su["clip1"]["id"])
        a = s.get(Asset, mc.result_asset_id)
        assert a is not None
        a.status = "archived"
        s.commit()
    pv = _preview(client, pid, "assembly.apply",
                  {"timeline_id": t["id"]})
    assert pv["resolved_assembly"]["sources_stale"] is True
    _apply(client, pid, t["id"], expect=422)


def test_manual_edit_after_apply_stales_plan(client):
    """The timeline_after exemption covers only the exact post-apply
    state — a later manual edit stales the plan again."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    shot_item = next(i for i in
                     _plan_detail(client, pid, plan["id"])["items"]
                     if i["kind"] == "shot")
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"],
             "clip_id": shot_item["timeline_clip_id"],
             "start_frames": 500})
    pv = _preview(client, pid, "assembly.apply",
                  {"timeline_id": t["id"]})
    assert pv["resolved_assembly"]["sources_stale"] is True
    _apply(client, pid, t["id"], expect=422)


# ---------- F5: review + undo preservation ----------

def test_review_votes_and_notes_survive_replan(client):
    """Re-plan preserves review state on stable item keys — votes and
    notes carry across recomputed proposals."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    item = plan["items"][0]
    _review(client, pid, plan["id"], item_ids=[item["id"]],
            status="approved", note="looks right")
    # Force a re-plan that changes the proposal — archive shot 2.
    _commit(client, pid, "shot.archive",
            {"shot_id": su["s2"]["shot"]["shot"]["id"]})
    plan2 = _plan(client, pid, t["id"])
    kept = next(i for i in plan2["items"]
                if i["key"] == item["key"])
    assert kept["review_status"] == "approved"
    assert kept["notes"] == "looks right"
    # The removed source is a flagged row, not silently dropped.
    removed = plan2["proposal"]["removed"]
    assert any(r.get("kind") == "shot" for r in removed)


def test_undo_restores_exact_pre_apply_state(client):
    """Undo of the apply restores the EXACT previous timeline —
    clip set, track set, positions — not an approximation."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    _plan(client, pid, t["id"])
    before = _detail(client, pid, t["id"])
    _apply(client, pid, t["id"])
    _commit(client, pid, "timeline.undo", {"timeline_id": t["id"]})
    after = _detail(client, pid, t["id"])
    assert [dict(c, id=None, created_at=None)
            for c in after["clips"]] == \
           [dict(c, id=None, created_at=None)
            for c in before["clips"]]
    assert {tr["name"] for tr in after["tracks"]} == \
        {tr["name"] for tr in before["tracks"]}


# ---------- F6: export A/V integrity ----------

@pytest.fixture()
def media_tools():
    from app.domain.media import binaries
    if binaries.ffmpeg() is None or binaries.ffprobe() is None:
        pytest.skip(
            "ffmpeg/ffprobe not resolvable — run scripts/fetch-ffmpeg.ps1")


@pytest.fixture()
def dev_ffmpeg() -> str:
    import shutil
    from app.domain.media import binaries
    if (which := shutil.which("ffmpeg")):
        return which
    tool = binaries.ffmpeg()
    if tool is not None:
        return tool["path"]
    pytest.skip("no ffmpeg available to synthesize fixtures")

def _pcm_windows(ffmpeg: str, media: Path, *, rate=48000,
                 window_s=0.25) -> list[float]:
    """Decode the audio stream to mono s16le PCM and return per-window
    RMS (0..1) — placement/silence verified on real decoded samples."""
    import subprocess, struct
    r = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(media), "-map", "0:a:0",
         "-f", "s16le", "-ac", "1", "-ar", str(rate), "-"],
        capture_output=True, timeout=120)
    assert r.returncode == 0 and r.stdout, r.stderr[:400]
    n = len(r.stdout) // 2
    smp = struct.unpack(f"<{n}h", r.stdout[: n * 2])
    w = int(rate * window_s)
    return [
        (sum(x * x for x in smp[i:i + w]) / max(1, len(smp[i:i + w])))
        ** 0.5 / 32768.0
        for i in range(0, n - (n % w), w)]


def _ffprobe_streams(ffprobe: str, media: Path) -> dict:
    import json, subprocess
    r = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json",
         "-count_frames", "-show_streams", "-show_format",
         str(media)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr[:300]
    return json.loads(r.stdout)


def _cas_wav(client, pid, path: Path, name="sfx.wav"):
    """Real audio bytes into CAS + asset/version rows (mirrors
    test_c5b_export._cas_video for audio)."""
    import hashlib, shutil
    from pathlib import Path as _P
    from app.domain.media import engine
    from app.db.models import (Asset, AssetFile, AssetVersion,
                               StorageLocation, new_id)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    data_dir = _P(client.app.state.settings.data_dir)
    rel = _P("blobs") / sha[:2] / sha
    dest = data_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    meta = engine.probe_audio(dest)
    assert meta["ok"], meta.get("reason")
    with client.app.state.session_factory() as s:
        loc = s.query(StorageLocation).first()
        if s.get(AssetFile, sha) is None:
            s.add(AssetFile(
                sha256=sha,
                storage_location_id=loc.id if loc else None,
                rel_path=str(rel).replace("\\", "/"),
                byte_size=path.stat().st_size, status="ready"))
        a = Asset(id=new_id(), project_id=pid, name=name, kind="audio",
                  origin="imported", uri=str(rel).replace("\\", "/"),
                  sha256=sha, byte_size=path.stat().st_size,
                  status="ready")
        s.add(a)
        s.flush()
        v = AssetVersion(id=new_id(), asset_id=a.id, version_no=1,
                         sha256=sha, media_json={"probe": meta})
        s.add(v)
        s.flush()
        a.current_version_id = v.id
        s.commit()
        return {"id": a.id, "version_id": v.id, "probe": meta}


@pytest.mark.usefixtures("media_tools")
def test_multiscene_export_av_integrity(client, dev_ffmpeg, tmp_path):
    """F6 — a two-scene first cut exports through the real ffmpeg
    pipeline: h264 + aac streams, expected frame count at the timeline
    rate, shot-relative dialogue offsets, an overflow DX2 lane, a music
    lane clip — and a decoded-PCM silence gap proving placement is
    exact with no bleed or duplication."""
    from app.domain.media import binaries
    from app.db.models import AssetFile
    from tests.test_c5b_export import _export, _out_asset

    pid = make_project(client)["id"]
    su = _full_setup(client, pid)   # 2 scenes × shot + 48f take + line
    # A second, overlapping line on shot 1 → deterministic DX2 lane.
    _mk_line(client, pid, su["s1"], su["s1"]["shot"],
             text="Overlapping whisper.",
             timing={"start_frames": 0})
    # A late line on shot 2 at 12 s — leaves a real silence gap after
    # scene-1/scene-2 audio (≈0–4.3 s).
    _mk_line(client, pid, su["s2"], su["s2"]["shot"],
             text="Late answer.", timing={"start_frames": 240})
    # Real music asset linked to shot 1 → MX lane.
    import subprocess as _sp
    wav = tmp_path / "mx.wav"
    _sp.run([dev_ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
             "sine=frequency=330:duration=2.0", str(wav)],
            check=True, capture_output=True)
    mx = _cas_wav(client, pid, wav, name="score.wav")
    _commit(client, pid, "entity.assets.link",
            {"entity_type": "shot",
             "entity_id": su["s1"]["shot"]["shot"]["id"],
             "asset_id": mx["id"], "role": "voice",
             "label": "score"})

    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    lanes = plan["proposal"]["lanes"]
    assert "dialogue2" in lanes, f"no DX2 lane: {list(lanes)}"
    _apply(client, pid, t["id"])
    det = _detail(client, pid, t["id"])
    names = {tr["name"] for tr in det["tracks"]}
    assert {"V1", "DX", "DX2", "MX"} <= names

    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    a, v, j = _out_asset(client, pid, job)
    assert a and a.kind == "video"
    with client.app.state.session_factory() as s:
        af = s.get(AssetFile, v.sha256)
        out = client.app.state.storage.file_path(s, af)

    fp = binaries.ffprobe()
    assert fp is not None
    pr = _ffprobe_streams(fp["path"], out)
    vs = next(s for s in pr["streams"] if s["codec_type"] == "video")
    aud = next(s for s in pr["streams"] if s["codec_type"] == "audio")
    assert vs["codec_name"] == "h264" and aud["codec_name"] == "aac"

    # Program duration covers the last clip end; clip geometry is in
    # SOURCE units — convert to timeline frames via each clip's src fps.
    dur = float(pr["format"]["duration"])
    tl_fps = 24.0

    def end_s(c):
        ext_tl = (c["out_frame"] - c["in_frame"]) \
            * tl_fps * c["src_fps_den"] / c["src_fps_num"]
        return (c["start_frames"] + ext_tl) / tl_fps

    expected_end = max(end_s(c) for c in det["clips"])
    assert abs(dur - expected_end) < 0.6, (dur, expected_end)
    nframes = int(vs.get("nb_read_frames") or vs.get("nb_frames") or 0)
    assert abs(nframes - round(dur * 24)) <= 4, (nframes, dur)

    # Decoded-PCM placement: loud where dialogue/music sit, silent in
    # the gap — no bleed, no duplication, no unintended clipping.
    wins = _pcm_windows(dev_ffmpeg, out)

    def rms(t0, t1):
        lo, hi = int(t0 / 0.25), min(len(wins), int(t1 / 0.25))
        return max(wins[lo:hi]) if hi > lo else 0.0

    assert rms(0.3, 1.8) > 0.01, "scene-1 audio missing/quiet"
    assert rms(5.0, 11.5) < 0.002, "audio bleed into the gap"
    assert rms(12.2, 13.8) > 0.01, "late scene-2 line missing/misplaced"


def test_skip_then_scoped_replace_apply_not_noop(client):
    """Regression: a skip-apply must not poison the idempotent no-op.
    After apply → manual edit → re-plan → skip-apply, a later apply
    with different options (approved replace) re-walks the decisions
    instead of no-op'ing on the stale kept-only applied_json."""
    pid = make_project(client)["id"]
    su = _full_setup(client, pid)
    t = _tl(client, pid)
    plan = _plan(client, pid, t["id"])
    _apply(client, pid, t["id"])
    items = {i["key"]: i for i in
             _plan_detail(client, pid, plan["id"])["items"]}
    shot_item = next(i for i in items.values() if i["kind"] == "shot")
    v_clip_id = shot_item["timeline_clip_id"]
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"], "clip_id": v_clip_id,
             "start_frames": 300})
    _plan(client, pid, t["id"])  # re-plan flags the edited clip
    # The poison step: a skip-apply records kept-only applied items.
    _apply(client, pid, t["id"], resolve_conflicts="skip")
    det = _detail(client, pid, t["id"])
    assert next(c for c in det["clips"]
                if c["id"] == v_clip_id)["start_frames"] == 300
    # Now the approved replace must actually run.
    _apply(client, pid, t["id"], resolve_conflicts="replace",
           replace_keys=[shot_item["key"]])
    det = _detail(client, pid, t["id"])
    assert not [c for c in det["clips"] if c["id"] == v_clip_id]
    v_new = next(c for c in det["clips"]
                 if c["shot_id"] == shot_item["shot_id"]
                 and any(tr["id"] == c["track_id"]
                         and tr["kind"] == "video"
                         for tr in det["tracks"]))
    assert v_new["start_frames"] == shot_item["start_frames"]
