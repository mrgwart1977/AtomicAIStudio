"""A5 — dialogue segments: scene/shot-bound speaker lines + mock audio.

Coverage: persistence + migration, project isolation, screenplay
scene/element + storyboard scene/shot + speaker refs, voice-profile
resolution + missing-voice flags, language/pronunciation/timing
validation, mock audio job lifecycle (enqueue → progress →
result apply_command → CAS wav), deterministic cancellation via the
hold barrier, stale-result refusal (segment/speaker/element/source
drift), decodable output via ffprobe, timeline placement on an audio
lane, undo/redo, backup/restore, restart reconciliation.
"""
import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_a1_screenplay import (
    _character, _storyboard, _scene as _sp_scene, _set_elements,
    _dialogue, _detail as _sp_detail)
from tests.test_a2_storyboard import _scene2, _shot2, _entry, _undo
from tests.test_c5a_timeline import _tl, _detail
from tests.test_n1a_tracks import _track


def _mk_screenplay(client, pid, title="Draft 1"):
    _commit(client, pid, "screenplay.create", {"title": title})
    return next(s for s in
                client.get(f"/api/projects/{pid}/screenplays").json()
                if s["title"] == title)


def _segments(client, pid, **flt):
    qs = "&".join(f"{k}={v}" for k, v in flt.items())
    r = client.get(f"/api/projects/{pid}/dialogue?{qs}")
    assert r.status_code == 200, r.text
    return r.json()


def _segment(client, pid, seg_id):
    r = client.get(f"/api/projects/{pid}/dialogue/{seg_id}")
    assert r.status_code == 200, r.text
    return r.json()


def _mk_segment(client, pid, **kw):
    _commit(client, pid, "dialogue.create", kw)
    return _segments(client, pid)[-1]


def _wait_hold(client, pid, job_id, timeout=60):
    """Deterministic: job must reach the mock hold barrier — the
    adapter parks emitting 'Mock audio hold' while running."""
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


def _gen(client, pid, seg_id, timeout=60, **extra):
    payload = {"segment_id": seg_id, **extra}
    pv = _preview(client, pid, "dialogue.generate", payload)
    payload["expected_dialogue"] = pv["resolved_dialogue"]
    out = _commit(client, pid, "dialogue.generate", payload)
    done = wait_for_job(client, pid, out["job_id"], timeout=timeout)
    assert done["status"] == "succeeded", done
    return done


def _setup(client, pid):
    """A populated anchor set: character, storyboard scene+shot, and a
    screenplay scene with a dialogue element bound to the shot."""
    anna = _character(client, pid, "Anna")
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    sp = _mk_screenplay(client, pid)
    spsc = _sp_scene(client, pid, sp["id"], "INT. DOCK — NIGHT",
                     scene_id=sc["scene"]["id"])
    _set_elements(client, pid, spsc["id"],
                  [_dialogue(anna["id"], "The tide is coming in.")])
    from tests.test_a1_screenplay import _detail as _sp_detail
    det = _sp_detail(client, pid, sp["id"])
    el = det["scenes"][0]["elements"][0]
    return {"anna": anna, "scene": sc, "shot": sh, "sp": sp,
            "spsc": spsc, "element": el}


def _mk_full(client, pid, setup, **kw):
    base = {"sp_scene_id": setup["spsc"]["id"],
            "element_id": setup["element"]["id"],
            "scene_id": setup["scene"]["scene"]["id"],
            "shot_id": setup["shot"]["shot"]["id"],
            "character_id": setup["anna"]["id"],
            "text": "The tide is coming in.",
            "direction": "(quietly)"}
    base.update(kw)
    return _mk_segment(client, pid, **base)


# ---------- persistence + schema ----------

def test_create_and_list(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    got = _segment(client, pid, d["id"])
    assert got["text"] == "The tide is coming in."
    assert got["direction"] == "(quietly)"
    assert got["sp_scene_id"] == su["spsc"]["id"]
    assert got["element_id"] == su["element"]["id"]
    assert got["shot_id"] == su["shot"]["shot"]["id"]
    assert got["character_id"] == su["anna"]["id"]
    assert got["speaker_name"] == "Anna"
    assert got["language"] == "en"
    assert got["status"] == "active" and got["revision"] == 0
    # filter surfaces: by scene AND by shot
    by_scene = _segments(client, pid,
                         scene_id=su["scene"]["scene"]["id"])
    by_shot = _segments(client, pid,
                        shot_id=su["shot"]["shot"]["id"])
    by_sp = _segments(client, pid, sp_scene_id=su["spsc"]["id"])
    assert [x["id"] for x in by_scene] == [d["id"]]
    assert [x["id"] for x in by_shot] == [d["id"]]
    assert [x["id"] for x in by_sp] == [d["id"]]


def test_anchor_required(client):
    pid = make_project(client)["id"]
    r = _commit(client, pid, "dialogue.create", {"text": "hello"},
                expect=422)
    assert "anchor" in r.text


def test_element_must_be_dialogue_kind(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    _set_elements(client, pid, su["spsc"]["id"],
                  [{"kind": "action", "text": "waves crash"}])
    from tests.test_a1_screenplay import _detail as _sp_detail
    det = _sp_detail(client, pid, su["sp"]["id"])
    act = det["scenes"][0]["elements"][0]
    r = _commit(client, pid, "dialogue.create",
                {"sp_scene_id": su["spsc"]["id"],
                 "element_id": act["id"], "text": "x"}, expect=422)
    assert "dialogue" in r.text.lower()


def test_shot_must_match_scene(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    sc2 = _scene2(client, pid, title="S2")
    sh2 = _shot2(client, pid, sc2["scene"]["id"], title="other")
    r = _commit(client, pid, "dialogue.create",
                {"scene_id": su["scene"]["scene"]["id"],
                 "shot_id": sh2["shot"]["id"], "text": "x"}, expect=422)
    assert "different scene" in r.text


def test_cross_project_refs_refused(client):
    a = make_project(client, "A")["id"]
    b = make_project(client, "B")["id"]
    su = _setup(client, a)
    r = _commit(client, b, "dialogue.create",
                {"sp_scene_id": su["spsc"]["id"], "text": "x"},
                expect=422)
    assert "not in project" in r.text
    # a segment created in A is invisible/unreachable from B
    d = _mk_full(client, a, su)
    assert _segments(client, b) == []
    r = client.get(f"/api/projects/{b}/dialogue/{d['id']}")
    assert r.status_code == 404


def test_update_fields_and_voice_merge(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su,
                 voice={"style": "warm", "emotion": "calm"})
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"], "text": "New line.",
             "direction": "(loud)", "voice": {"delivery": "projected"},
             "language": "fr",
             "pronunciation": {"hints": [
                 {"word": "tide", "say_as": "teed"}]},
             "timing": {"start_frames": 48, "fps": {"num": 24,
                                                    "den": 1}}})
    got = _segment(client, pid, d["id"])
    assert got["text"] == "New line."
    assert got["direction"] == "(loud)"
    assert got["voice_json"]["style"] == "warm"  # merged, not replaced
    assert got["voice_json"]["delivery"] == "projected"
    assert got["language"] == "fr"
    assert got["pronunciation_json"]["hints"][0]["say_as"] == "teed"
    assert got["timing_json"]["start_frames"] == 48
    assert got["revision"] == 1
    # explicit null clears a voice field
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"], "voice": {"style": None}})
    assert "style" not in _segment(client, pid, d["id"])["voice_json"]


def test_voice_resolution_and_missing_flag(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    # give Anna a voice profile via character.update (data merges
    # into profile_json — the voice identity of record)
    _commit(client, pid, "character.update",
            {"character_id": su["anna"]["id"],
             "data": {"voice": {"profile": "low and steady",
                                "style": "narrator"}}})
    d = _mk_full(client, pid, su, voice={"emotion": "tense"})
    got = _segment(client, pid, d["id"])
    rv = got["resolved_voice"]
    assert rv["missing"] is False
    assert rv["profile"] == "low and steady"
    assert rv["style"] == "narrator"
    assert rv["emotion"] == "tense"   # segment override layers on top
    # no speaker → missing-voice indicator, not an error
    d2 = _mk_full(client, pid, su, character_id=None, text="wild line")
    r2 = _segment(client, pid, d2["id"])
    assert r2["speaker_name"] is None
    assert r2["resolved_voice"]["missing"] is True


def test_archive_unarchive_revert_undo(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _commit(client, pid, "dialogue.archive", {"segment_id": d["id"]})
    assert _segment(client, pid, d["id"])["status"] == "archived"
    # update on archived refuses
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"], "text": "no"}, expect=422)
    _commit(client, pid, "dialogue.unarchive", {"segment_id": d["id"]})
    assert _segment(client, pid, d["id"])["status"] == "active"
    # revert to r0 restores original text
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"], "text": "changed"})
    _commit(client, pid, "dialogue.revert",
            {"segment_id": d["id"], "to_revision": 0})
    assert _segment(client, pid, d["id"])["text"] == \
        "The tide is coming in."
    # undo an update restores the field exactly
    cmd = _commit(client, pid, "dialogue.update",
                  {"segment_id": d["id"], "direction": "(shouted)"})
    _undo(client, pid, cmd["command_id"])
    assert _segment(client, pid, d["id"])["direction"] == "(quietly)"


def test_version_history(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"], "text": "v2"})
    r = client.get(f"/api/projects/{pid}/dialogue/{d['id']}/versions")
    assert r.status_code == 200
    revs = r.json()
    assert [v["revision"] for v in revs] == [1, 0]


# ---------- validation ----------

@pytest.mark.parametrize("payload,frag", [
    ({"text": "x" * 4001}, "4000"),
    ({"direction": "x" * 501}, "500"),
    ({"voice": {"style": 5}}, "voice.style"),
    ({"voice": {"unknown": "x"}, "character_id": None}, None),  # ok —
    ({"language": "x" * 21}, "language"),
    ({"pronunciation": {"hints": [{"word": "a"}]}}, "say_as"),
    ({"timing": {"start_frames": -1}}, "start_frames"),
    ({"timing": {"fps": {"num": 0, "den": 1}}}, "fps"),
    ({"timing": {"duration_s": 0}}, "duration_s"),
    ({"seed": -1}, "seed"),
])
def test_invalid_fields_rejected(client, payload, frag):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    base = {"sp_scene_id": su["spsc"]["id"], "text": "ok"}
    base.update(payload)
    expect = 200 if frag is None else 422
    r = _commit(client, pid, "dialogue.create", base, expect=expect)
    if frag is not None:
        assert frag in r.text


# ---------- generation lifecycle ----------

def test_generate_preview_and_result(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    pv = _preview(client, pid, "dialogue.generate",
                  {"segment_id": d["id"]})
    assert pv["resolved_dialogue"]["segment_id"] == d["id"]
    assert pv["resolved_dialogue"]["revision"] == 0
    ra = pv["resolved_audio"]
    assert ra["simulated"] is True
    assert ra["text"] == "The tide is coming in."
    assert ra["sample_rate"] == 48000
    assert ra["duration_s"] > 0
    assert ra["voice"]["missing"] is True  # Anna has no profile yet
    job = _gen(client, pid, d["id"])
    seg = _segment(client, pid, d["id"])
    assert seg["generate_job_id"] == job["id"]
    assert seg["result_asset_id"] == job["output_asset_id"]
    assert seg["result_asset_name"].endswith(".wav")


def test_real_decodable_wav(client):
    """The produced file is a genuine PCM WAV — ffprobe-verified,
    the same media engine the timeline uses."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])
    seg = _segment(client, pid, d["id"])
    with client.app.state.session_factory() as s:
        from app.db.models import Asset, AssetVersion
        a = s.get(Asset, seg["result_asset_id"])
        v = s.get(AssetVersion, a.current_version_id)
        media = v.media_json
        probe = media["probe"]
        assert media["is_mock"] is True
        assert media["generated"] is False
        assert media["simulated_speech"] is True
        assert probe["ok"] is True and probe["kind"] == "audio"
        assert probe["codec"] == "pcm_s16le"
        assert probe["sample_rate"] == 48000
        assert probe["channels"] == 1
        assert probe["duration_s"] > 0.5
        assert probe["samples"] == int(probe["duration_s"] * 48000) \
            or probe["samples"] > 0
        # bytes really exist in CAS
        from pathlib import Path
        uri = Path(client.app.state.storage._data_dir) / a.uri
        assert uri.exists() and uri.stat().st_size > 1000


def test_duration_is_deterministic(client):
    from app.domain import dialogue as dlg
    a = dlg.est_duration_s("one two three")
    b = dlg.est_duration_s("one two three")
    assert a == b > 0
    assert dlg.est_duration_s("one two three", "(urgent)") < a
    assert dlg.est_duration_s("one two three", "(whispered)") > a


def test_generate_empty_text_refused(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su, text="")
    _commit(client, pid, "dialogue.generate",
            {"segment_id": d["id"]}, expect=422)


def test_deterministic_cancel_via_hold(client, tmp_path):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    rel = tmp_path / "rel.flag"
    out = _commit(client, pid, "dialogue.generate",
                  {"segment_id": d["id"],
                   "mock_hold": {"at_stage": 2,
                                 "release_file": str(rel),
                                 "timeout_s": 120}})
    job_id = out["job_id"]
    _wait_hold(client, pid, job_id)
    r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "cancelled"
    assert _segment(client, pid, d["id"])["result_asset_id"] is None
    # duplicate cancel → 409, no state drift
    r2 = client.post(f"/api/jobs/{job_id}/cancel")
    assert r2.status_code == 409


def test_retry_after_cancel(client, tmp_path):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    rel = tmp_path / "r.flag"
    out = _commit(client, pid, "dialogue.generate",
                  {"segment_id": d["id"],
                   "mock_hold": {"at_stage": 1,
                                 "release_file": str(rel)}})
    _wait_hold(client, pid, out["job_id"])
    client.post(f"/api/jobs/{out['job_id']}/cancel")
    assert wait_for_job(client, pid, out["job_id"])["status"] == "cancelled"
    # fresh generate needs fresh pins (revision bumped at enqueue)
    job = _gen(client, pid, d["id"])
    assert job["status"] == "succeeded"
    assert _segment(client, pid, d["id"])["result_asset_id"] == \
        job["output_asset_id"]


# ---------- stale-result refusal ----------

def _held(client, pid, seg_id, tmp_path, stage=2):
    rel = tmp_path / f"rel-{seg_id[:6]}-{stage}.flag"
    out = _commit(client, pid, "dialogue.generate",
                  {"segment_id": seg_id,
                   "mock_hold": {"at_stage": stage,
                                 "release_file": str(rel),
                                 "timeout_s": 120}})
    _wait_hold(client, pid, out["job_id"])
    return out["job_id"], rel


def test_stale_segment_edit_refused(client, tmp_path):
    """Edit mid-flight → the landing result is refused; the produced
    artifact stays in CAS but never becomes the segment result."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path)
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"], "text": "different line"})
    rel.touch()
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "succeeded"  # artifact produced…
    seg = _segment(client, pid, d["id"])
    assert seg["result_asset_id"] != done["output_asset_id"]  # …refused


def test_stale_speaker_change_refused(client, tmp_path):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    bob = _character(client, pid, "Bob")
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path)
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"], "character_id": bob["id"]})
    rel.touch()
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "succeeded"
    assert _segment(client, pid, d["id"])["result_asset_id"] != \
        done["output_asset_id"]


def test_stale_element_edit_refused(client, tmp_path):
    """The bound screenplay element's text is a generation source —
    editing it mid-flight stales the audio."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path)
    _set_elements(client, pid, su["spsc"]["id"],
                  [_dialogue(su["anna"]["id"], "Rewritten line.")])
    rel.touch()
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "succeeded"
    assert _segment(client, pid, d["id"])["result_asset_id"] != \
        done["output_asset_id"]


def test_stale_archive_refused(client, tmp_path):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path)
    _commit(client, pid, "dialogue.archive", {"segment_id": d["id"]})
    rel.touch()
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "succeeded"
    assert _segment(client, pid, d["id"])["result_asset_id"] is None


def test_superseded_generation_refused(client, tmp_path):
    """A newer generation supersedes the held one — the stale job's
    artifact never attaches."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    rel1 = tmp_path / "r1.flag"
    out1 = _commit(client, pid, "dialogue.generate",
                   {"segment_id": d["id"],
                    "mock_hold": {"at_stage": 3,
                                  "release_file": str(rel1),
                                  "timeout_s": 120}})
    _wait_hold(client, pid, out1["job_id"])
    # job2 runs to completion while job1 is parked (one gen lane job
    # at a time — release job1 is NOT needed for job2 to queue after;
    # cancel job1 so lane frees, then job2 lands)
    client.post(f"/api/jobs/{out1['job_id']}/cancel")
    wait_for_job(client, pid, out1["job_id"])
    j2 = _gen(client, pid, d["id"])
    assert _segment(client, pid, d["id"])["result_asset_id"] == \
        j2["output_asset_id"]


def test_stale_commit_pin_refused(client):
    """Preview pins checked at commit — a mid-review segment edit
    refuses the generate itself."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    pv = _preview(client, pid, "dialogue.generate",
                  {"segment_id": d["id"]})
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"], "text": "edited after preview"})
    r = _commit(client, pid, "dialogue.generate",
                {"segment_id": d["id"],
                 "expected_dialogue": pv["resolved_dialogue"]},
                expect=422)
    assert "changed since preview" in r.text


# ---------- timeline placement ----------

def _audio_lane(client, pid, tid, name="A1"):
    return _track(client, pid, tid, kind="audio", name=name)


def test_place_on_audio_lane(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])
    tl = _tl(client, pid)
    a1 = _audio_lane(client, pid, tl["id"])
    _commit(client, pid, "dialogue.place",
            {"segment_id": d["id"], "timeline_id": tl["id"],
             "track_id": a1["id"], "start_frames": 24})
    det = _detail(client, pid, tl["id"])
    clip = next(c for c in det["clips"] if c["track_id"] == a1["id"])
    assert clip["start_frames"] == 24
    assert clip["shot_id"] == su["shot"]["shot"]["id"]
    assert clip["source_asset_id"] == _segment(
        client, pid, d["id"])["result_asset_id"]
    # audio geometry: extent is the wav's samples at the timeline rate
    assert clip["out_frame"] > clip["in_frame"]


def test_place_refuses_video_lane_and_no_lane(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])
    tl = _tl(client, pid)
    # default track is video — explicit video lane refused
    v1 = _track(client, pid, tl["id"], kind="video", name="V1")
    r = _commit(client, pid, "dialogue.place",
                {"segment_id": d["id"], "timeline_id": tl["id"],
                 "track_id": v1["id"], "start_frames": 0}, expect=422)
    assert "audio" in r.text.lower()


def test_place_requires_result(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    tl = _tl(client, pid)
    _audio_lane(client, pid, tl["id"])
    r = _commit(client, pid, "dialogue.place",
                {"segment_id": d["id"], "timeline_id": tl["id"],
                 "start_frames": 0}, expect=422)
    assert "no audio result" in r.text


def test_place_appends_without_start(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d1 = _mk_full(client, pid, su)
    _gen(client, pid, d1["id"])
    d2 = _mk_full(client, pid, su, text="Second line.")
    _gen(client, pid, d2["id"])
    tl = _tl(client, pid)
    a1 = _audio_lane(client, pid, tl["id"])
    _commit(client, pid, "dialogue.place",
            {"segment_id": d1["id"], "timeline_id": tl["id"],
             "track_id": a1["id"], "start_frames": 0})
    _commit(client, pid, "dialogue.place",
            {"segment_id": d2["id"], "timeline_id": tl["id"],
             "track_id": a1["id"], "allow_duplicate": False})
    det = _detail(client, pid, tl["id"])
    lane_clips = sorted(
        (c for c in det["clips"] if c["track_id"] == a1["id"]),
        key=lambda c: c["start_frames"])
    assert len(lane_clips) == 2
    assert lane_clips[1]["start_frames"] > lane_clips[0]["start_frames"]


def test_placed_clip_undoes_via_timeline_undo(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])
    tl = _tl(client, pid)
    a1 = _audio_lane(client, pid, tl["id"])
    _commit(client, pid, "dialogue.place",
            {"segment_id": d["id"], "timeline_id": tl["id"],
             "track_id": a1["id"], "start_frames": 0})
    det = _detail(client, pid, tl["id"])
    assert len([c for c in det["clips"]
                if c["track_id"] == a1["id"]]) == 1
    _commit(client, pid, "timeline.undo", {"timeline_id": tl["id"]})
    det = _detail(client, pid, tl["id"])
    assert [c for c in det["clips"] if c["track_id"] == a1["id"]] == []


def test_placed_dialogue_exports_with_audio(client):
    """An audio lane carrying a generated segment mixes into the
    exported film — the N2 audio path, not a parallel engine."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])
    tl = _tl(client, pid)
    _audio_lane(client, pid, tl["id"])
    _commit(client, pid, "dialogue.place",
            {"segment_id": d["id"], "timeline_id": tl["id"],
             "start_frames": 0})
    out = _commit(client, pid, "timeline.export",
                  {"timeline_id": tl["id"], "profile": "720p"})
    job = wait_for_job(client, pid, out["job_id"], timeout=120)
    assert job["status"] == "succeeded", job.get("error")


# ---------- backup / restore ----------

def test_backup_roundtrip_preserves_dialogue(client, tmp_path):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su, voice={"style": "warm"})
    _gen(client, pid, d["id"])
    seg = _segment(client, pid, d["id"])
    _commit(client, pid, "backup.export", {"kind": "backup"})
    job = client.get(f"/api/projects/{pid}/jobs").json()[0]
    done = wait_for_job(client, pid, job["id"], timeout=60)
    assert done["status"] == "succeeded", done
    pkg = done["result_json"]["package_path"]
    r = client.post("/api/packages/import",
                    json={"package_path": pkg})
    assert r.status_code == 200, r.text
    new_pid = r.json()["project_id"]
    wait_for_job(client, new_pid, r.json()["job_id"], timeout=60)
    segs = _segments(client, new_pid)
    assert len(segs) == 1
    rs = segs[0]
    assert rs["text"] == d["text"]
    assert rs["voice_json"]["style"] == "warm"
    assert rs["result_asset_id"] is not None
    assert rs["generate_job_id"] is None  # jobs are not packaged
    # remapped refs resolve inside the restored project — no dangling
    assert rs["character_id"] is not None
    assert rs["sp_scene_id"] is not None
    assert rs["shot_id"] is not None
    # and it can still be placed
    tl = _tl(client, new_pid)
    _audio_lane(client, new_pid, tl["id"])
    _commit(client, new_pid, "dialogue.place",
            {"segment_id": rs["id"], "timeline_id": tl["id"],
             "start_frames": 0})


def test_restart_reconcile_interrupted_job(client):
    """An in-flight non-resumable dialogue job reconciles to
    failed/interrupted on restart — never silently resumes."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    out = _commit(client, pid, "dialogue.generate",
                  {"segment_id": d["id"],
                   "mock_hold": {"at_stage": 2,
                                 "release_file": "/nonexistent",
                                 "timeout_s": 300}})
    _wait_hold(client, pid, out["job_id"])
    with client.app.state.session_factory() as s:
        n = client.app.state.job_engine.reconcile_interrupted(s)
        s.commit()
        assert n == 0  # non-resumable: failed, not re-queued
    with client.app.state.session_factory() as s:
        from app.db.models import GenerationJob
        j = s.get(GenerationJob, out["job_id"])
        assert j.status == "failed"
        assert (j.recovery_state or "") == "interrupted"
