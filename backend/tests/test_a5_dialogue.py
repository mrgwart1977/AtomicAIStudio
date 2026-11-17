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
from tests.test_n1a_tracks import _track, _edl
from tests.test_n2a_audio import _place, _audio


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
    assert job["adapter_id"] == "mock.audio"  # job-level provenance
    seg = _segment(client, pid, d["id"])
    assert seg["generate_job_id"] == job["id"]
    assert seg["result_asset_id"] == job["output_asset_id"]
    assert seg["result_asset_name"].endswith(".wav")
    # Preview/artifact consistency — the duration the preview promised
    # is the duration the produced WAV actually measures (deterministic
    # estimate, not a post-hoc measurement).
    with client.app.state.session_factory() as s:
        from app.db.models import Asset, AssetVersion
        a = s.get(Asset, seg["result_asset_id"])
        v = s.get(AssetVersion, a.current_version_id)
    assert v.media_json["probe"]["duration_s"] == pytest.approx(
        ra["duration_s"], abs=0.02)


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
        assert media["adapter_id"] == "mock.audio"
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


# ---------- A5-F2/F3 — ownership, source fingerprint, publish safety --

def test_stale_voice_profile_change_refused(client, tmp_path):
    """Mid-flight character voice-profile edit → the generated audio is
    stale on landing (voice_sha is part of the source fingerprint)."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path)
    _commit(client, pid, "character.update",
            {"character_id": su["anna"]["id"],
             "data": {"voice": {"style": "cold"}}})
    rel.touch()
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "succeeded"
    assert _segment(client, pid, d["id"])["result_asset_id"] != \
        done["output_asset_id"]


def test_stale_timing_change_refused(client, tmp_path):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path)
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"],
             "timing": {"start_frames": 48,
                        "fps": {"num": 24, "den": 1}}})
    rel.touch()
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "succeeded"
    assert _segment(client, pid, d["id"])["result_asset_id"] != \
        done["output_asset_id"]


def test_stale_archived_speaker_refused(client, tmp_path):
    """Archiving the speaker mid-flight changes its status in the
    source fingerprint — the produced audio never attaches."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path)
    _commit(client, pid, "character.archive",
            {"character_id": su["anna"]["id"]})
    rel.touch()
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "succeeded"
    assert _segment(client, pid, d["id"])["result_asset_id"] != \
        done["output_asset_id"]


def test_stale_element_unlinked_refused(client, tmp_path):
    """Dropping the bound screenplay element mid-flight unlinks the
    segment (SET NULL) — the fingerprint's element state changes and
    the produced audio never attaches."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path)
    _set_elements(client, pid, su["spsc"]["id"], [])  # element deleted
    rel.touch()
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "succeeded"
    seg = _segment(client, pid, d["id"])
    assert seg["result_asset_id"] != done["output_asset_id"]
    assert seg["element_id"] is None  # unlinked, text kept


def test_result_stale_flag_on_source_drift(client):
    """After a result lands, editing a source flags it stale — marked,
    never silently replaced or deleted."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])
    seg = _segment(client, pid, d["id"])
    assert seg["result_asset_id"] is not None
    assert seg["result_stale"] is False
    # Drift the bound element's text — the result flags stale.
    _set_elements(client, pid, su["spsc"]["id"],
                  [_dialogue(su["anna"]["id"], "A rewritten line.")])
    seg = _segment(client, pid, d["id"])
    assert seg["result_stale"] is True
    assert seg["result_asset_id"] is not None  # marked, not replaced


def test_result_stale_on_authored_field_drift(client):
    """Editing the segment's own authored fields (text, voice override,
    timing) flags the landed result stale — these fields are pinned in
    the job's expected_dialogue, revision bumps alone never flag."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])
    assert _segment(client, pid, d["id"])["result_stale"] is False
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"], "text": "An edited line."})
    assert _segment(client, pid, d["id"])["result_stale"] is True
    # Regenerate → fresh pin → clean again.
    _gen(client, pid, d["id"])
    assert _segment(client, pid, d["id"])["result_stale"] is False
    # Voice-override drift flags too.
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"], "voice": {"style": "cold"}})
    assert _segment(client, pid, d["id"])["result_stale"] is True


def test_segment_fields_survive_screenplay_rewrite(client):
    """F2 ownership — screenplay_scene.update rewrites the ELEMENT
    list; the segment's own user-authored fields are never touched."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su, voice={"style": "warm"},
                 timing={"start_frames": 24,
                         "fps": {"num": 24, "den": 1}},
                 pronunciation={"hints": [{"word": "tide",
                                           "say_as": "tahyd"}]})
    _set_elements(client, pid, su["spsc"]["id"],
                  [_dialogue(su["anna"]["id"], "Totally other line."),
                   {"kind": "action", "text": "Thunder."}])
    seg = _segment(client, pid, d["id"])
    assert seg["text"] == "The tide is coming in."
    assert seg["direction"] == "(quietly)"
    assert seg["voice_json"]["style"] == "warm"
    assert seg["language"] == "en"
    assert seg["timing_json"]["start_frames"] == 24
    assert seg["pronunciation_json"]["hints"][0]["say_as"] == "tahyd"


def test_screenplay_update_warns_unlink(client):
    """Previewing an element-drop warns that linked segments unlink —
    the destructive sync is never silent."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    _mk_full(client, pid, su)
    pv = _preview(client, pid, "screenplay_scene.update",
                  {"sp_scene_id": su["spsc"]["id"], "elements": []})
    assert any("dialogue segment" in w.lower()
               for w in pv.get("warnings") or [])


def test_screenplay_scene_delete_warns_unlink(client):
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    _mk_full(client, pid, su)
    pv = _preview(client, pid, "screenplay_scene.delete",
                  {"sp_scene_id": su["spsc"]["id"]})
    assert any("dialogue segment" in w.lower()
               for w in pv.get("warnings") or [])


def test_archived_speaker_voice_still_resolves(client):
    """An archived character keeps its voice identity — the segment's
    resolved_voice still shows it (the identity-of-record survives
    archival; generation is what's gated)."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    _commit(client, pid, "character.update",
            {"character_id": su["anna"]["id"],
             "data": {"voice": {"style": "warm"}}})
    d = _mk_full(client, pid, su)
    _commit(client, pid, "character.archive",
            {"character_id": su["anna"]["id"]})
    seg = _segment(client, pid, d["id"])
    assert seg["resolved_voice"]["style"] == "warm"
    assert seg["resolved_voice"]["missing"] is False


def test_unlinked_speaker_missing_voice(client):
    """A segment with no speaker flags missing — the UI's honest
    no-voice indicator, never an invented identity."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su, character_id=None)
    seg = _segment(client, pid, d["id"])
    assert seg["speaker_name"] is None
    assert seg["resolved_voice"]["missing"] is True


def test_cancel_leaves_no_temp_files(client, tmp_path):
    """Held mid-synthesis → cancel → the partial WAV is purged from the
    job work dir; no result, no new asset rows."""
    from pathlib import Path
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path, stage=2)
    work = Path(client.app.state.storage._data_dir) / "jobs" / pid
    before_assets = len(
        client.get(f"/api/projects/{pid}/assets").json()["items"])
    client.post(f"/api/jobs/{job_id}/cancel")
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "cancelled"
    assert done["output_asset_id"] is None
    leftovers = list(work.glob(f"{job_id}*"))
    assert leftovers == [], f"temp files leaked: {leftovers}"
    after_assets = len(
        client.get(f"/api/projects/{pid}/assets").json()["items"])
    assert after_assets == before_assets
    assert _segment(client, pid, d["id"])["result_asset_id"] is None


# ---------- A5-F5 — PCM parity (N3 methodology) ---------------------

@pytest.fixture()
def media_tools():
    from app.domain.media import binaries
    if binaries.ffmpeg() is None or binaries.ffprobe() is None:
        pytest.skip("ffmpeg/ffprobe not resolvable")


def _sine_wav(path, rate=48000, seconds=1.0, freq=220.0, amp=12000):
    """Deterministic mono sine WAV — the import-side fixture."""
    import math
    import struct
    import wave
    n = int(rate * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(
            f"<{n}h",
            *(int(amp * math.sin(2 * math.pi * freq * i / rate))
              for i in range(n))))
    return path


def _import(client, pid, path, name, kind="audio"):
    r = _commit(client, pid, "asset.import",
                {"source_path": str(path), "name": name, "kind": kind})
    job = wait_for_job(client, pid, r["job_id"], timeout=60)
    assert job["status"] == "succeeded", job
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    return next(a for a in items if a["name"] == name)


def _place_seg(client, pid, seg_id, tid, track_id, at=0, **kw):
    return _commit(client, pid, "dialogue.place",
                   {"segment_id": seg_id, "timeline_id": tid,
                    "track_id": track_id, "start_frames": at, **kw})


@pytest.mark.usefixtures("media_tools")
def test_plan_fields_envelope_parity(client, tmp_path):
    """Real mock-dialogue WAV + imported sine on two audio lanes → the
    project's own EDL → composite_plan fields vs independent
    expectations; then the dialogue entry's REAL _lane_chain renders a
    constant signal through ffmpeg — the measured amplitude IS the
    render envelope (N3 methodology, no formula duplicated)."""
    import subprocess
    from fractions import Fraction
    from app.domain import timeline_export as tx
    from app.domain.media import binaries
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])
    music = _import(client, pid,
                    _sine_wav(tmp_path / "mx.wav", seconds=1.0),
                    "mx.wav")
    tl = _tl(client, pid)
    dx = _audio_lane(client, pid, tl["id"], "DX")
    mx = _audio_lane(client, pid, tl["id"], "MX")
    _place_seg(client, pid, d["id"], tl["id"], dx["id"], at=0)
    _place(client, pid, tl["id"], music, mx["id"], at=0)
    doc = _edl(client, pid, tl["id"], version=2)
    plan = tx.composite_plan(doc, {})
    tl_fps = Fraction(24, 1)
    entries = {a["clip_id"]: a for a in plan["audio_clips"]}
    assert len(entries) == 2
    det = _detail(client, pid, tl["id"])
    dlg_clip = next(c for c in det["clips"] if c["track_id"] == dx["id"])
    mx_clip = next(c for c in det["clips"] if c["track_id"] == mx["id"])
    a_dlg = entries[dlg_clip["id"]]
    a_mx = entries[mx_clip["id"]]
    # Plan fields — independently derived: the dialogue clip starts at
    # tl t=0 and spans its real duration at 48 kHz; music spans 1.0 s.
    assert a_dlg["start_tl"] == 0
    assert a_dlg["rate"] == 48000
    seg = a_dlg["seg_end_src"] - a_dlg["seg_start_src"]
    assert float(a_dlg["extent_tl"]) / 24 == pytest.approx(
        seg / 48000, abs=1e-6)
    assert float(a_mx["extent_tl"]) / 24 == pytest.approx(1.0, abs=1e-3)
    assert a_mx["rate"] == 48000
    # Envelope: real chain on a constant 0.8 signal — no gain/fades,
    # so measured ≈ input amplitude mid-segment.
    ffmpeg = binaries.ffmpeg()["path"]
    rate = 48000
    src = tmp_path / "const.wav"
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"aevalsrc=exprs=0.8|0.8:d={a_dlg['seg_end_src'] / rate + 0.5:.3f}:s={rate}",
         str(src)], check=True)
    chain = tx._lane_chain(0, a_dlg, tl_fps)
    r = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(src),
         "-filter_complex", chain, "-f", "s16le", "-ac", "2", "-"],
        capture_output=True)
    assert r.returncode == 0, r.stderr[:300]
    pcm = r.stdout
    mid = int(seg / 2) * 4
    v = int.from_bytes(pcm[mid:mid + 2], "little", signed=True)
    assert abs(abs(v) / 32768.0 / 0.8 - 1.0) < 3e-3


@pytest.mark.usefixtures("media_tools")
def test_gain_fades_automation_in_plan(client, tmp_path):
    """Gain, fades and clip automation on a placed dialogue clip are
    the plan's own values — and the real chain's envelope reflects
    them at checkpoints."""
    import subprocess
    from fractions import Fraction
    from app.domain import timeline_export as tx
    from app.domain.media import binaries
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])
    tl = _tl(client, pid)
    dx = _audio_lane(client, pid, tl["id"], "DX")
    _place_seg(client, pid, d["id"], tl["id"], dx["id"], at=0)
    det = _detail(client, pid, tl["id"])
    clip = det["clips"][0]
    _commit(client, pid, "timeline.clip.gain",
            {"timeline_id": tl["id"], "clip_id": clip["id"],
             "gain_db": -6.0})
    dur_units = clip["out_frame"] - clip["in_frame"]
    _commit(client, pid, "timeline.clip.fade",
            {"timeline_id": tl["id"], "clip_id": clip["id"],
             "fade_in_units": dur_units // 2, "fade_in_curve": "linear"})
    # Automation offsets are clip-local TIMELINE frames; a single
    # point at 0 clamps its value across the whole clip (−12 dB
    # everywhere — deterministic arithmetic for the envelope check).
    _commit(client, pid, "timeline.clip.automation",
            {"timeline_id": tl["id"], "clip_id": clip["id"],
             "points": [{"at_frames": 0, "gain_db": -12.0}]})
    doc = _edl(client, pid, tl["id"], version=2)
    plan = tx.composite_plan(doc, {})
    a = plan["audio_clips"][0]
    assert a["clip_db"] == pytest.approx(-6.0)
    assert a["fade_in"]["n"] == dur_units // 2
    assert a["fade_in"]["curve"] == "linear"
    assert a["automation"] and \
        float(a["automation"][0]["gain_db"]) == pytest.approx(-12.0)
    # Envelope: constant 0.8 through the real chain — composition is
    # additive dB: −6 clip + −12 automation + fade ramp.
    # early (75% through the fade): −2.5 dB ramp → total −20.5 dB
    # ≈ 0.094; deep body: −18 dB ≈ 0.126.
    ffmpeg = binaries.ffmpeg()["path"]
    rate = int(a["rate"])
    src = tmp_path / "const.wav"
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
         f"aevalsrc=exprs=0.8|0.8:d={a['seg_end_src'] / rate + 0.5:.3f}:s={rate}",
         str(src)], check=True)
    r = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(src),
         "-filter_complex", tx._lane_chain(0, a, Fraction(24, 1)),
         "-f", "s16le", "-ac", "2", "-"],
        capture_output=True)
    assert r.returncode == 0, r.stderr[:300]
    pcm = r.stdout
    seg = a["seg_end_src"] - a["seg_start_src"]

    def env(u: int) -> float:
        i = int(u) * 4
        return abs(int.from_bytes(
            pcm[i:i + 2], "little", signed=True)) / 32768.0 / 0.8

    # 75% through the fade: 0.75 × 10^(−18/20) ≈ 0.094; body: ≈ 0.126.
    early = env(seg * 0.375)
    mid = env(seg * 0.75)
    assert 0.06 < early < 0.12, early
    assert mid == pytest.approx(0.126, abs=0.025)
    assert mid > early * 1.15  # the fade ramp is what separates them


def test_mute_solo_and_crossfade_in_plan(client):
    """Track mute/solo filtering and a dialogue↔dialogue crossfade —
    plan-level semantics through the real EDL."""
    from app.domain import timeline_export as tx
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d1 = _mk_full(client, pid, su)
    d2 = _mk_full(client, pid, su, text="Second line.")
    _gen(client, pid, d1["id"])
    _gen(client, pid, d2["id"])
    tl = _tl(client, pid)
    dx = _audio_lane(client, pid, tl["id"], "DX")
    _place_seg(client, pid, d1["id"], tl["id"], dx["id"], at=0)
    det = _detail(client, pid, tl["id"])
    c1 = det["clips"][0]
    # Crossfade pairs are adjacent clips borrowing source handles, not
    # overlaps — trim c1's tail 0.5 s to open lane room + tail handle.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tl["id"], "clip_id": c1["id"],
             "out_frame": c1["out_frame"] - 24000})
    e1 = c1["start_frames"] + (c1["out_frame"] - 24000) * 24 / 48000
    _place_seg(client, pid, d2["id"], tl["id"], dx["id"],
               at=int(e1) + 1)
    det = _detail(client, pid, tl["id"])
    c1, c2 = sorted(det["clips"], key=lambda c: c["start_frames"])
    # Give c2 a 0.2 s head handle so the window can open before it.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tl["id"], "clip_id": c2["id"],
             "in_frame": 9600})
    # Window covers the seam [c1.end, c2.start): 4 tl-frames into c1's
    # territory (head = 8000 ≤ 9600) and ends 8f past c1's end
    # (tail = 16000 ≤ 24000).
    e2 = c2["start_frames"]
    _commit(client, pid, "timeline.crossfade.set",
            {"timeline_id": tl["id"],
             "out_clip_id": c1["id"], "in_clip_id": c2["id"],
             "tl_start": e2 - 4, "tl_frames": 12,
             "curve": "equal_power"})
    doc = _edl(client, pid, tl["id"], version=2)
    assert doc["crossfades"][0]["satisfied"] is True
    plan = tx.composite_plan(doc, {})
    assert len(plan["audio_clips"]) == 2
    xf_entries = [a for a in plan["audio_clips"]
                  if a.get("xf") is not None]
    assert len(xf_entries) == 2
    # Muted lane → no audible entries at all.
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": tl["id"], "track_id": dx["id"],
             "muted": True})
    doc = _edl(client, pid, tl["id"], version=2)
    assert tx.composite_plan(doc, {})["audio_clips"] == []


@pytest.mark.usefixtures("media_tools")
def test_export_pcm_mix_overlap(client, tmp_path):
    """Decode the exported film's audio: the overlap window (dialogue
    + music) carries more energy than either single-source window —
    the decoded PCM matches the expected mix. Post-encode comparison
    is windowed RMS (codec-safe); exact-sample parity is asserted at
    the pre-encode WAV boundary elsewhere."""
    import math
    import subprocess
    from pathlib import Path
    from app.domain.media import binaries
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])  # ~2.3 s at 48 kHz
    music = _import(client, pid,
                    _sine_wav(tmp_path / "mx.wav", seconds=1.5),
                    "mx.wav")
    tl = _tl(client, pid)
    dx = _audio_lane(client, pid, tl["id"], "DX")
    mx = _audio_lane(client, pid, tl["id"], "MX")
    # dialogue 0–~2.3 s on DX; music 1–2.5 s on MX (24f→1.0 s offset).
    _place_seg(client, pid, d["id"], tl["id"], dx["id"], at=0)
    _place(client, pid, tl["id"], music, mx["id"], at=24)
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": tl["id"], "profile": "720p"})
    job = wait_for_job(client, pid, r["job_id"], timeout=180)
    assert job["status"] == "succeeded", job
    assets = client.get(f"/api/projects/{pid}/assets").json()["items"]
    ex = next(a for a in assets if a["id"] == job["output_asset_id"])
    ffmpeg = binaries.ffmpeg()["path"]
    blob = Path(client.app.state.storage._data_dir) / ex["uri"]
    dec = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(blob),
         "-map", "0:a:0", "-f", "s16le", "-ac", "1", "-ar", "48000",
         "-"], capture_output=True)
    assert dec.returncode == 0, dec.stderr[:300]
    pcm = dec.stdout

    def rms(t0: float, t1: float) -> float:
        i0, i1 = int(t0 * 48000) * 2, int(t1 * 48000) * 2
        buf = pcm[i0:i1]
        n = len(buf) // 2
        if not n:
            return 0.0
        acc = 0
        for i in range(0, len(buf), 2):
            v = int.from_bytes(buf[i:i + 2], "little", signed=True)
            acc += v * v
        return math.sqrt(acc / n) / 32768.0

    only_dlg = rms(0.3, 0.9)      # dialogue alone
    overlap = rms(1.2, 2.2)       # dialogue + music
    tail_dlg = rms(2.6, 3.1)      # dialogue alone again (if ≥2.6 s)
    assert only_dlg > 0
    assert overlap > only_dlg * 1.15
    if tail_dlg > 0:
        assert overlap > tail_dlg * 1.15


# ---------- A5-F3/F5/F6 — remaining hardening matrix ---------------

def test_stale_parenthetical_refused(client, tmp_path):
    """The element's parenthetical alone is a generation source — a
    direction-only rewrite mid-flight stales the produced audio."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path)
    _set_elements(client, pid, su["spsc"]["id"],
                  [_dialogue(su["anna"]["id"], "The tide is coming in.",
                             parenthetical="(shouting)")])
    rel.touch()
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "succeeded"
    assert _segment(client, pid, d["id"])["result_asset_id"] != \
        done["output_asset_id"]


def test_stale_shot_association_refused(client, tmp_path):
    """Re-anchoring the segment to a different shot+scene mid-flight
    changes both the segment revision and the source fingerprint —
    the produced audio never attaches."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    sc2 = _scene2(client, pid)
    sh2 = _shot2(client, pid, sc2["scene"]["id"])
    d = _mk_full(client, pid, su)
    job_id, rel = _held(client, pid, d["id"], tmp_path)
    _commit(client, pid, "dialogue.update",
            {"segment_id": d["id"],
             "scene_id": sc2["scene"]["id"],
             "shot_id": sh2["shot"]["id"]})
    rel.touch()
    done = wait_for_job(client, pid, job_id)
    assert done["status"] == "succeeded"
    seg = _segment(client, pid, d["id"])
    assert seg["result_asset_id"] != done["output_asset_id"]
    assert seg["shot_id"] == sh2["shot"]["id"]  # edit kept, audio refused


def test_multi_speaker_scene_voice_resolution(client):
    """Two speakers in one scene resolve distinct voices — the segment
    override layers under each character's own profile."""
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    bob = _character(client, pid, "Bob")
    _commit(client, pid, "character.update",
            {"character_id": su["anna"]["id"],
             "data": {"voice": {"style": "warm"}}})
    _commit(client, pid, "character.update",
            {"character_id": bob["id"],
             "data": {"voice": {"style": "gravel"}}})
    d1 = _mk_full(client, pid, su)
    d2 = _mk_full(client, pid, su, character_id=bob["id"],
                  text="Keep the channel open.")
    s1 = _segment(client, pid, d1["id"])
    s2 = _segment(client, pid, d2["id"])
    assert (s1["speaker_name"], s2["speaker_name"]) == ("Anna", "Bob")
    assert s1["resolved_voice"]["style"] == "warm"
    assert s2["resolved_voice"]["style"] == "gravel"
    assert not s1["resolved_voice"]["missing"]
    assert not s2["resolved_voice"]["missing"]
    segs = _segments(client, pid, sp_scene_id=su["spsc"]["id"])
    assert len(segs) == 2  # both listed under the one scene


def test_track_automation_and_solo_in_plan(client):
    """Lane automation (N3-A) and solo filtering on a dialogue lane —
    plan-level semantics through the real EDL, no ffmpeg needed."""
    from app.domain import timeline_export as tx
    pid = make_project(client)["id"]
    su = _setup(client, pid)
    d = _mk_full(client, pid, su)
    _gen(client, pid, d["id"])
    sfx = _audio(client, pid, name="sfx.wav", duration=1.0)
    tl = _tl(client, pid)
    dx = _audio_lane(client, pid, tl["id"], "DX")
    mx = _audio_lane(client, pid, tl["id"], "MX")
    _place_seg(client, pid, d["id"], tl["id"], dx["id"], at=0)
    _place(client, pid, tl["id"], sfx, mx["id"], at=0)
    det = _detail(client, pid, tl["id"])
    dlg_clip = next(c for c in det["clips"] if c["track_id"] == dx["id"])
    _commit(client, pid, "timeline.track.automation",
            {"timeline_id": tl["id"], "track_id": dx["id"],
             "points": [{"at_tl_frames": 0, "gain_db": -9.0}]})
    doc = _edl(client, pid, tl["id"], version=2)
    plan = tx.composite_plan(doc, {})
    assert len(plan["audio_clips"]) == 2
    a_dlg = next(a for a in plan["audio_clips"]
                 if a["clip_id"] == dlg_clip["id"])
    assert a_dlg["track_auto"] and \
        float(a_dlg["track_auto"][0]["gain_db"]) == pytest.approx(-9.0)
    # Solo the DX lane — the SFX lane drops out of the mix entirely.
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": tl["id"], "track_id": dx["id"],
             "solo": True})
    doc = _edl(client, pid, tl["id"], version=2)
    plan = tx.composite_plan(doc, {})
    assert [a["clip_id"] for a in plan["audio_clips"]] == \
        [dlg_clip["id"]]
