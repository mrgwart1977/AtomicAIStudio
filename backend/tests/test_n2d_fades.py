"""N2-D — audio fades, crossfades, bounded gain automation.

Model: fades are SOURCE-unit counts anchored to trim edges (audio
clips only); crossfades are explicit records borrowing source
handles at render time (lanes keep non-overlap occupancy);
automation is a bounded clip-local keyframe list (linear-in-dB).
All metadata until N2-E renders it.
"""
import json

import pytest

from tests.conftest import make_project
from tests.test_c4b_shot_generate import _commit, _preview, _rev
from tests.test_c5a_timeline import _tl, _detail, _insert, _video
from tests.test_n1a_tracks import _track, _edl
from tests.test_n2a_audio import _audio, _atrack, _place


def _fade(client, pid, tid, clip_id, expect=200, key=None, **fields):
    return _commit(client, pid, "timeline.clip.fade",
                   {"timeline_id": tid, "clip_id": clip_id, **fields},
                   expect=expect, key=key)


def _auto(client, pid, tid, clip_id, points, expect=200, key=None):
    return _commit(client, pid, "timeline.clip.automation",
                   {"timeline_id": tid, "clip_id": clip_id,
                    "points": points}, expect=expect, key=key)


def _xf(client, pid, tid, expect=200, key=None, **kw):
    return _commit(client, pid, "timeline.crossfade.set",
                   {"timeline_id": tid, **kw}, expect=expect, key=key)


def _clip_of(d, clip_id):
    return next(c for c in d["clips"] if c["id"] == clip_id)


def _setup(client, rate=48000, duration=2.0):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"], "A1")
    au = _audio(client, pid, rate=rate, duration=duration)
    _place(client, pid, t["id"], au, a1["id"], at=0)
    d = _detail(client, pid, t["id"])
    return pid, t["id"], a1, d["clips"][0]


# ---------- fades ----------

def test_fades_persist_and_edl(client):
    pid, tid, a1, clip = _setup(client)
    _fade(client, pid, tid, clip["id"], fade_in_units=4800,
          fade_in_curve="equal_power", fade_out_units=9600)
    d = _detail(client, pid, tid)
    c = _clip_of(d, clip["id"])
    assert (c["fade_in_units"], c["fade_in_curve"]) == (4800, "equal_power")
    assert (c["fade_out_units"], c["fade_out_curve"]) == (9600, "linear")
    doc = _edl(client, pid, tid, version=2)
    ec = next(x for x in doc["clips"] if x["clip_id"] == clip["id"])
    assert ec["fades"] == {"in": {"units": 4800, "curve": "equal_power"},
                           "out": {"units": 9600, "curve": "linear"}}


def test_fade_bounds_and_curve_refuse(client):
    pid, tid, a1, clip = _setup(client, duration=2.0)  # 96000 samples
    for bad in (-1, 96001, "x", 1.5):
        _fade(client, pid, tid, clip["id"], fade_in_units=bad, expect=422)
    _fade(client, pid, tid, clip["id"], fade_in_curve="scurve",
          expect=422)
    # Sum exceeding the extent refuses — fades never reach past trims.
    _fade(client, pid, tid, clip["id"], fade_in_units=60000)
    _fade(client, pid, tid, clip["id"], fade_out_units=40000,
          expect=422)
    _fade(client, pid, tid, clip["id"], fade_out_units=36000)


def test_fades_refused_on_video_lane(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    d = _detail(client, pid, t["id"])
    clip = d["clips"][0]
    _fade(client, pid, t["id"], clip["id"], fade_in_units=100,
          expect=422)


def test_fade_curve_math():
    from app.domain.timeline import fade_gain
    # Linear: in ramps 0→1, out 1→0.
    assert fade_gain("linear", 0, 100) == 0.0
    assert fade_gain("linear", 50, 100) == pytest.approx(0.5)
    assert fade_gain("linear", 50, 100, out=True) == pytest.approx(0.5)
    assert fade_gain("linear", 100, 100) == 1.0
    # Equal-power pair sums to unity power at the midpoint.
    gi = fade_gain("equal_power", 50, 100)
    go = fade_gain("equal_power", 50, 100, out=True)
    assert gi == pytest.approx(2 ** 0.5 / 2)
    assert gi * gi + go * go == pytest.approx(1.0)
    assert fade_gain("linear", 10, 0) == 1.0  # zero-length = passthrough


def test_trim_refuses_into_fades(client):
    pid, tid, a1, clip = _setup(client, duration=2.0)
    _fade(client, pid, tid, clip["id"], fade_in_units=48000,
          fade_out_units=24000)
    # Trimming to 48000 total < 72000 fade sum → refuse closed.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tid, "clip_id": clip["id"],
             "out_frame": 60000}, expect=422)
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tid, "clip_id": clip["id"],
             "out_frame": 72000})


def test_split_reattaches_fades(client):
    pid, tid, a1, clip = _setup(client, duration=2.0)
    _fade(client, pid, tid, clip["id"], fade_in_units=2400,
          fade_out_units=4800)
    # 48kHz source on a 24fps tl → 2000 samples/frame; split at tl 24
    # = source 48000 (midpoint).
    _commit(client, pid, "timeline.clip.split",
            {"timeline_id": tid, "clip_id": clip["id"],
             "at_frame": 24})
    d = _detail(client, pid, tid)
    left = _clip_of(d, clip["id"])
    right = next(c for c in d["clips"] if c["id"] != clip["id"])
    # Left keeps the IN fade and loses OUT; right mirrors.
    assert (left["fade_in_units"], left["fade_out_units"]) == (2400, 0)
    assert (right["fade_in_units"], right["fade_out_units"]) == (0, 4800)
    # Split inside a fade region refuses.
    _fade(client, pid, tid, left["id"], fade_in_units=48000)
    _commit(client, pid, "timeline.clip.split",
            {"timeline_id": tid, "clip_id": left["id"],
             "at_frame": 12}, expect=422)


# ---------- automation ----------

def test_automation_persist_validate_edl(client):
    pid, tid, a1, clip = _setup(client)
    _auto(client, pid, tid, clip["id"], [
        {"at_frames": 0, "gain_db": -20},
        {"at_frames": 24, "gain_db": 0},
        {"at_frames": 40, "gain_db": -6}])
    d = _detail(client, pid, tid)
    pts = json.loads(_clip_of(d, clip["id"])["automation_json"])
    assert [p["at_frames"] for p in pts] == [0, 24, 40]
    assert all(p["id"] for p in pts)
    doc = _edl(client, pid, tid, version=2)
    ec = next(x for x in doc["clips"] if x["clip_id"] == clip["id"])
    assert [p["gain_db"] for p in ec["automation"]] == [-20, 0, -6]


@pytest.mark.parametrize("pts", [
    [{"at_frames": 0, "gain_db": -20}, {"at_frames": 0, "gain_db": 0}],
    [{"at_frames": -1, "gain_db": 0}],
    [{"at_frames": 10, "gain_db": 99}],
    [{"at_frames": 10.5, "gain_db": 0}],
    [{"at_frames": 99999, "gain_db": 0}],
])
def test_automation_refuses_malformed(client, pts):
    pid, tid, a1, clip = _setup(client)
    _auto(client, pid, tid, clip["id"], pts, expect=422)


def test_automation_max_points(client):
    pid, tid, a1, clip = _setup(client)
    _auto(client, pid, tid, clip["id"],
          [{"at_frames": i, "gain_db": 0} for i in range(65)],
          expect=422)


def test_automation_interpolation():
    from app.domain.timeline import automation_gain_db
    pts = [{"at_frames": 0, "gain_db": -20},
           {"at_frames": 10, "gain_db": 0}]
    assert automation_gain_db(pts, -5) == -20
    assert automation_gain_db(pts, 0) == -20
    assert automation_gain_db(pts, 5) == -10   # linear in dB
    assert automation_gain_db(pts, 10) == 0
    assert automation_gain_db(pts, 99) == 0
    assert automation_gain_db([], 5) == 0.0


def test_automation_split_rebases(client):
    pid, tid, a1, clip = _setup(client)
    _auto(client, pid, tid, clip["id"], [
        {"at_frames": 0, "gain_db": -12},
        {"at_frames": 30, "gain_db": -3}])
    _commit(client, pid, "timeline.clip.split",
            {"timeline_id": tid, "clip_id": clip["id"],
             "at_frame": 24})
    d = _detail(client, pid, tid)
    left = json.loads(_clip_of(d, clip["id"])["automation_json"])
    right = json.loads(next(
        c for c in d["clips"] if c["id"] != clip["id"])["automation_json"])
    assert [p["at_frames"] for p in left] == [0]
    assert right == [{"id": right[0]["id"], "at_frames": 6,
                      "gain_db": -3}]


def test_fade_automation_undo(client):
    pid, tid, a1, clip = _setup(client)
    _fade(client, pid, tid, clip["id"], fade_in_units=4800)
    _auto(client, pid, tid, clip["id"],
          [{"at_frames": 0, "gain_db": -20}])
    for _ in range(2):
        _commit(client, pid, "timeline.undo", {"timeline_id": tid})
    c = _clip_of(_detail(client, pid, tid), clip["id"])
    assert c["fade_in_units"] == 0
    assert json.loads(c["automation_json"]) == []
    for _ in range(2):
        _commit(client, pid, "timeline.redo", {"timeline_id": tid})
    c = _clip_of(_detail(client, pid, tid), clip["id"])
    assert c["fade_in_units"] == 4800


# ---------- crossfades ----------

def _xf_setup(client):
    """Clip-free audio lane for crossfade scenarios."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"], "A1")
    return pid, t["id"], a1


def _two_clips(client, pid, tid, a1):
    """A[0..24f) with 24f of tail handle, then B[24..69.6f) with
    4800 samples of head handle — adjacent on one audio lane.
    Both clips are 2s@48k (96000 samples); A trims its tail to
    48000, B trims its head to 4800."""
    a = _audio(client, pid, name="a.wav", sha="aa" * 32, duration=2.0)
    b = _audio(client, pid, name="b.wav", sha="bb" * 32, duration=2.0)
    _place(client, pid, tid, a, a1["id"], at=0)
    d = _detail(client, pid, tid)
    ca = d["clips"][0]
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tid, "clip_id": ca["id"],
             "out_frame": 48000})
    _place(client, pid, tid, b, a1["id"], at=24)
    d = _detail(client, pid, tid)
    ca, cb = sorted(d["clips"], key=lambda c: c["start_frames"])
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tid, "clip_id": cb["id"],
             "in_frame": 4800})
    return ca, cb


def test_crossfade_create_persist_edl(client):
    pid, tid, a1 = _xf_setup(client)
    ca, cb = _two_clips(client, pid, tid, a1)
    # Window [22,26): 2f of A tail (A has 48000 spare) + 2f of B
    # head (B has 4800 spare ≥ 2×2000).
    _xf(client, pid, tid, out_clip_id=ca["id"], in_clip_id=cb["id"],
        tl_start=22, tl_frames=4, curve="equal_power")
    d = _detail(client, pid, tid)
    assert len(d["crossfades"]) == 1
    x = d["crossfades"][0]
    assert x["satisfied"] is True
    doc = _edl(client, pid, tid, version=2)
    assert doc["crossfades"][0]["curve"] == "equal_power"
    assert doc["crossfades"][0]["satisfied"] is True


def test_crossfade_requires_handles(client):
    pid, tid, a1 = _xf_setup(client)
    ca, cb = _two_clips(client, pid, tid, a1)
    # Window [46,50) needs 26f of A tail (52000 > 48000 spare) —
    # refused closed.
    _xf(client, pid, tid, out_clip_id=ca["id"], in_clip_id=cb["id"],
        tl_start=46, tl_frames=4, expect=422)
    # A window inside B's span needing only 6000 of A tail — works.
    _xf(client, pid, tid, out_clip_id=ca["id"], in_clip_id=cb["id"],
        tl_start=25, tl_frames=2)


def test_crossfade_window_must_stay_within_both_spans(client):
    """A window reaching before the out-clip or past the in-clip is
    not a crossfade — only one clip could sound there. Refuse."""
    pid, tid, a1 = _xf_setup(client)
    a = _audio(client, pid, name="a.wav", sha="aa" * 32, duration=2.0)
    b = _audio(client, pid, name="b.wav", sha="bb" * 32, duration=2.0)
    _place(client, pid, tid, a, a1["id"], at=48)   # A [48,96f)
    _place(client, pid, tid, b, a1["id"], at=96)   # B [96,144f)
    d = _detail(client, pid, tid)
    ca, cb = sorted(d["clips"], key=lambda c: c["start_frames"])
    _xf(client, pid, tid, out_clip_id=ca["id"], in_clip_id=cb["id"],
        tl_start=40, tl_frames=8, expect=422)      # starts before A
    _xf(client, pid, tid, out_clip_id=ca["id"], in_clip_id=cb["id"],
        tl_start=140, tl_frames=8, expect=422)     # ends past B


def test_crossfade_same_lane_and_order(client):
    pid, tid, a1, clip = _setup(client)
    a2 = _atrack(client, pid, tid, "A2")
    au2 = _audio(client, pid, name="x.wav", sha="cc" * 32)
    _place(client, pid, tid, au2, a2["id"], at=0)
    d = _detail(client, pid, tid)
    other = next(c for c in d["clips"] if c["id"] != clip["id"])
    _xf(client, pid, tid, out_clip_id=clip["id"],
        in_clip_id=other["id"], tl_start=0, tl_frames=4, expect=422)
    _xf(client, pid, tid, out_clip_id=clip["id"],
        in_clip_id=clip["id"], tl_start=0, tl_frames=4, expect=422)


def test_crossfade_satisfied_flag_after_move(client):
    pid, tid, a1 = _xf_setup(client)
    ca, cb = _two_clips(client, pid, tid, a1)
    _xf(client, pid, tid, out_clip_id=ca["id"], in_clip_id=cb["id"],
        tl_start=22, tl_frames=4)
    # Move B far right — the window no longer intersects its span.
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": tid, "clip_id": cb["id"],
             "start_frames": 200})
    d = _detail(client, pid, tid)
    assert d["crossfades"][0]["satisfied"] is False
    # Move back — satisfied again (record was never dropped).
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": tid, "clip_id": cb["id"],
             "start_frames": 24})
    assert _detail(client, pid, tid)["crossfades"][0]["satisfied"]


def test_crossfade_trim_and_split_guards(client):
    pid, tid, a1 = _xf_setup(client)
    ca, cb = _two_clips(client, pid, tid, a1)
    _xf(client, pid, tid, out_clip_id=ca["id"], in_clip_id=cb["id"],
        tl_start=22, tl_frames=4)
    # Trimming B's head DOWN (4800→3000) leaves less free handle than
    # the 4000 samples the window borrows — refused closed.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tid, "clip_id": cb["id"],
             "in_frame": 3000}, expect=422)
    # Split inside the window refuses; outside it passes.
    _commit(client, pid, "timeline.clip.split",
            {"timeline_id": tid, "clip_id": ca["id"],
             "at_frame": 23}, expect=422)


def test_crossfade_clear_and_undo(client):
    pid, tid, a1 = _xf_setup(client)
    ca, cb = _two_clips(client, pid, tid, a1)
    _xf(client, pid, tid, out_clip_id=ca["id"], in_clip_id=cb["id"],
        tl_start=22, tl_frames=4)
    xid = _detail(client, pid, tid)["crossfades"][0]["id"]
    _commit(client, pid, "timeline.crossfade.clear",
            {"timeline_id": tid, "crossfade_id": xid})
    assert _detail(client, pid, tid)["crossfades"] == []
    _commit(client, pid, "timeline.undo", {"timeline_id": tid})
    d = _detail(client, pid, tid)
    assert len(d["crossfades"]) == 1
    assert d["crossfades"][0]["id"] == xid   # stable id through undo


def test_crossfade_idempotent_commit(client):
    pid, tid, a1 = _xf_setup(client)
    ca, cb = _two_clips(client, pid, tid, a1)
    r1 = _xf(client, pid, tid, out_clip_id=ca["id"],
             in_clip_id=cb["id"], tl_start=22, tl_frames=4,
             key="xf-idem-key-1")
    r2 = _xf(client, pid, tid, out_clip_id=ca["id"],
             in_clip_id=cb["id"], tl_start=22, tl_frames=4,
             key="xf-idem-key-1")
    assert len(_detail(client, pid, tid)["crossfades"]) == 1


def test_stale_revision_refused(client):
    pid, tid, a1, clip = _setup(client)
    # An expected_revision from before the fade → 409.
    rev = _rev(client, pid)
    _fade(client, pid, tid, clip["id"], fade_in_units=4800)
    r = client.post(f"/api/projects/{pid}/commands/commit",
                    json={"command_type": "timeline.clip.fade",
                          "payload": {"timeline_id": tid,
                                      "clip_id": clip["id"],
                                      "fade_out_units": 4800},
                          "scope": "project",
                          "expected_revision": rev,
                          "idempotency_key": "stale-fade-1"})
    assert r.status_code == 409


def test_export_still_refuses_with_fades(client):
    """N2-D is metadata only — a populated visible audio lane still
    refuses export until N2-E."""
    pid, tid, a1, clip = _setup(client)
    _fade(client, pid, tid, clip["id"], fade_in_units=4800)
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": tid, "profile": "720p"}, expect=200)
    from tests.conftest import wait_for_job
    j = wait_for_job(client, pid, r["job_id"])
    assert j["status"] == "failed" and "audio lanes" in j["error"]
