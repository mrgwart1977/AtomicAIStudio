"""N1-C — advanced timeline editing: frame-accurate split, ripple
delete/insert/trim, gap management. All through the command path so
expected_timeline, idempotency, audit, and revert apply uniformly."""
import pytest

from tests.conftest import make_project
from tests.test_c4b_shot_generate import _commit, _preview, _rev
from tests.test_c5a_timeline import _video, _tl, _insert, _detail
from tests.test_n1a_tracks import _track, _edl


def _clip(d, i=0, track=None):
    clips = [c for c in d["clips"]
             if track is None or c["track_id"] == track]
    return sorted(clips, key=lambda c: c["start_frames"])[i]


def _split(client, pid, tid, clip_id, at, expect=200, **kw):
    return _commit(client, pid, "timeline.clip.split",
                   {"timeline_id": tid, "clip_id": clip_id,
                    "at_frame": at, **kw}, expect=expect)


def _gap(client, pid, tid, op, track_id, at, expect=200, **kw):
    return _commit(client, pid, f"timeline.gap.{op}",
                   {"timeline_id": tid, "track_id": track_id,
                    "at_frame": at, **kw}, expect=expect)


# ---------- split ----------

def test_split_preserves_source_and_duration(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a = _video(client, pid)  # 72f @ 24fps on a 24fps timeline
    _insert(client, pid, t["id"], a)
    d = _detail(client, pid, t["id"])
    c0 = _clip(d)
    _split(client, pid, t["id"], c0["id"], 30)
    d = _detail(client, pid, t["id"])
    assert len(d["clips"]) == 2
    left, right = sorted(d["clips"], key=lambda c: c["start_frames"])
    # Left keeps the original id and trims at the cut; right is new.
    assert left["id"] == c0["id"]
    assert (left["in_frame"], left["out_frame"]) == (0, 30)
    assert (right["in_frame"], right["out_frame"]) == (30, 72)
    assert right["start_frames"] == 30 and left["start_frames"] == 0
    # Same source pin on both fragments — provenance intact.
    for c in (left, right):
        assert c["source_asset_id"] == a["id"]
        assert c["source_version_id"] == a["version_id"]
        assert c["source_sha256"] == a["sha256"]
        assert c["src_frames"] == 72 and c["track_id"] == left["track_id"]
    # Total visible duration conserved.
    assert sum(c["out_frame"] - c["in_frame"] for c in d["clips"]) == 72


def test_split_boundaries_refuse(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    c0 = _clip(_detail(client, pid, t["id"]))
    for at, msg in [(0, "inside"), (72, "inside"), (200, "inside"),
                    (-1, "integer|between"), ("x", "integer")]:
        _split(client, pid, t["id"], c0["id"], at, expect=422)
    assert len(_detail(client, pid, t["id"])["clips"]) == 1


def test_split_mixed_rate_requires_source_frame(client):
    """30000/1001 source on a 24fps timeline: only timeline frames
    mapping to whole source frames may be cut."""
    pid = make_project(client)["id"]
    t = _tl(client, pid, "NTSC", fps_num=24, fps_den=1)
    # 91f @ 30000/1001 → extent 91*1001*24/(30000) = 72.8 tl frames
    _insert(client, pid, t["id"],
            _video(client, pid, "n.mp4", sha="c" * 64,
                   frames=91, fps=29.97, duration=91 / 29.97))
    c0 = _clip(_detail(client, pid, t["id"]))
    # Timeline offset off maps to source offset off*30000/24024 —
    # integral only at off=1001·k (24024·1250 = 30000·1001), far
    # outside this clip's ~72.8-frame extent. So every interior cut on
    # a mixed-rate clip must refuse rather than slice mid-source-frame.
    r = _split(client, pid, t["id"], c0["id"], 5, expect=422)
    assert "source frame" in r.text
    for at in (7, 13, 33, 50):
        _split(client, pid, t["id"], c0["id"], at, expect=422)
    assert len(_detail(client, pid, t["id"])["clips"]) == 1


def test_split_mixed_rate_valid_cuts(client):
    """Representable mixed-rate cuts DO split — the rule isn't a
    blanket mixed-rate refusal. 48fps src on a 24fps timeline: every
    timeline frame maps to exactly 2 source frames."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"],
            _video(client, pid, "h.mp4", sha="e" * 64,
                   frames=72, fps=48, duration=1.5))
    c0 = _clip(_detail(client, pid, t["id"]))
    _split(client, pid, t["id"], c0["id"], 10)  # tl 10 → src 20
    d = _detail(client, pid, t["id"])
    left, right = sorted(d["clips"], key=lambda c: c["start_frames"])
    assert (left["in_frame"], left["out_frame"]) == (0, 20)
    assert (right["in_frame"], right["out_frame"]) == (20, 72)
    assert right["start_frames"] == 10


def test_split_24000_1001_source_boundary(client):
    """24000/1001 src on a 24fps timeline: whole source frames land at
    tl offsets that are multiples of 1001 (off_src = off_tl·1000/1001)."""
    pid = make_project(client)["id"]
    t = _tl(client, pid, "NTSC24", fps_num=24, fps_den=1)
    # 1200f @ 24000/1001 → extent 1201.2 tl frames.
    _insert(client, pid, t["id"],
            _video(client, pid, "n24.mp4", sha="f" * 64,
                   frames=1200, fps=23.976, duration=1200 / 23.976))
    c0 = _clip(_detail(client, pid, t["id"]))
    # off=500 → 500·1000/1001 = 499.5 src frames → mid-frame → refuse.
    r = _split(client, pid, t["id"], c0["id"], 500, expect=422)
    assert "source frame" in r.text
    # off=1001 → exactly 1000 source frames → legal cut.
    _split(client, pid, t["id"], c0["id"], 1001)
    d = _detail(client, pid, t["id"])
    left, right = sorted(d["clips"], key=lambda c: c["start_frames"])
    assert (left["in_frame"], left["out_frame"]) == (0, 1000)
    assert (right["in_frame"], right["out_frame"]) == (1000, 1200)
    assert right["start_frames"] == 1001
    assert sum(c["out_frame"] - c["in_frame"] for c in d["clips"]) == 1200


def test_split_preserves_shot_scene(client):
    """Shot/scene provenance rides along on both fragments."""
    from tests.test_c4b_shot_generate import _scene, _shot
    pid = make_project(client)["id"]
    sc = _scene(client, pid, "S1")["scene"]
    sh = _shot(client, pid, sc["id"], "wide")["shot"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid),
            shot_id=sh["id"], scene_id=sc["id"])
    c0 = _clip(_detail(client, pid, t["id"]))
    _split(client, pid, t["id"], c0["id"], 30)
    d = _detail(client, pid, t["id"])
    for c in d["clips"]:
        assert c["shot_id"] == sh["id"] and c["scene_id"] == sc["id"]


def test_split_locked_track_refuses(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    d = _detail(client, pid, t["id"])
    v1 = d["tracks"][0]["id"]
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v1, "locked": True})
    _split(client, pid, t["id"], _clip(d)["id"], 30, expect=422)
    assert len(_detail(client, pid, t["id"])["clips"]) == 1


def test_split_stale_approval_and_replay(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    c0 = _clip(_detail(client, pid, t["id"]))
    payload = {"timeline_id": t["id"], "clip_id": c0["id"],
               "at_frame": 30}
    prev = _preview(client, pid, "timeline.clip.split", payload)
    stale_tl = prev["resolved_timeline"]
    # Another edit bumps the timeline revision → stale expected refuses.
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id":
             _detail(client, pid, t["id"])["tracks"][0]["id"],
             "name": "Main"})
    r = _commit(client, pid, "timeline.clip.split",
                {**payload, "expected_timeline": stale_tl}, expect=422)
    assert "re-preview" in r.text
    # Fresh commit works; replaying the same idempotency key doesn't
    # duplicate the mutation.
    first = _commit(client, pid, "timeline.clip.split", payload,
                    key="n1c-split-1")
    second = _commit(client, pid, "timeline.clip.split", payload,
                     key="n1c-split-1")
    assert second["idempotent_replay"]
    assert len(_detail(client, pid, t["id"])["clips"]) == 2


def test_split_revert_restores_single_clip(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    d0 = _detail(client, pid, t["id"])
    c0 = _clip(d0)
    _split(client, pid, t["id"], c0["id"], 30)
    _commit(client, pid, "timeline.revert",
            {"timeline_id": t["id"], "to_revision": d0["revision"]})
    d = _detail(client, pid, t["id"])
    assert len(d["clips"]) == 1
    c = d["clips"][0]
    assert (c["in_frame"], c["out_frame"]) == (0, 72)
    assert c["source_version_id"] == c0["source_version_id"]


# ---------- ripple delete ----------

def _three(client, pid, tid, **kw):
    """Three 72f clips at 0/72/144 on V1 → (v1_track_id, [clips])."""
    for name, sha in [("a", "a" * 64), ("b", "b" * 64), ("c", "c" * 64)]:
        _insert(client, pid, tid,
                _video(client, pid, f"{name}.mp4", sha=sha), **kw)
    d = _detail(client, pid, tid)
    return d["tracks"][0]["id"], sorted(
        d["clips"], key=lambda c: c["start_frames"])


def test_remove_default_leaves_gap(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _, clips = _three(client, pid, t["id"])
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": clips[1]["id"]})
    d = _detail(client, pid, t["id"])
    assert [c["start_frames"] for c in sorted(
        d["clips"], key=lambda c: c["start_frames"])] == [0, 144]


def test_ripple_delete_track_scope(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1, clips = _three(client, pid, t["id"])
    # An overlay clip in the deleted region must NOT move under 'track'.
    ov = _track(client, pid, t["id"], kind="overlay", name="O1")
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "track_id": ov["id"],
             "asset_id": _video(client, pid, "o.mp4", sha="d" * 64)["id"],
             "start_frames": 96})
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": clips[1]["id"],
             "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    v1_starts = sorted(c["start_frames"] for c in d["clips"]
                       if c["track_id"] == v1)
    assert v1_starts == [0, 72]  # c pulled left by 72
    ov_clip = next(c for c in d["clips"] if c["track_id"] == ov["id"])
    assert ov_clip["start_frames"] == 96  # untouched on other lane


def test_ripple_delete_all_scope_and_locked_guard(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1, clips = _three(client, pid, t["id"])
    v2 = _track(client, pid, t["id"], name="V2")
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "track_id": v2["id"],
             "asset_id": _video(client, pid, "x.mp4", sha="e" * 64)["id"],
             "start_frames": 200})
    # scope=all moves V2's follower too.
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": clips[1]["id"],
             "ripple_scope": "all"})
    d = _detail(client, pid, t["id"])
    v2_clip = next(c for c in d["clips"] if c["track_id"] == v2["id"])
    assert v2_clip["start_frames"] == 128
    # Now lock V2 and try another all-scope ripple that would reach it.
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v2["id"],
             "locked": True})
    d = _detail(client, pid, t["id"])
    last = sorted(d["clips"], key=lambda c: c["start_frames"],
                  reverse=True)[0]
    r = _commit(client, pid, "timeline.clip.remove",
                {"timeline_id": t["id"], "clip_id":
                 sorted(d["clips"], key=lambda c: c["start_frames"])[0]["id"],
                 "ripple_scope": "all"}, expect=422)
    assert "locked" in r.text
    # Track-scoped delete on V1 still works (V2 untouched).
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id":
             sorted(d["clips"], key=lambda c: c["start_frames"])[0]["id"],
             "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    v2_clip = next(c for c in d["clips"] if c["track_id"] == v2["id"])
    assert v2_clip["start_frames"] == 128


def test_ripple_all_scope_refusal_moves_nothing(client):
    """A locked in-scope lane refuses the WHOLE op — no partial
    movement on the unlocked lanes either (fail closed)."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1, clips = _three(client, pid, t["id"])
    v2 = _track(client, pid, t["id"], name="V2")
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "track_id": v2["id"],
             "asset_id": _video(client, pid, "x.mp4", sha="e" * 64)["id"],
             "start_frames": 200})
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v2["id"],
             "locked": True})
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": clips[1]["id"],
             "ripple_scope": "all"}, expect=422)
    d = _detail(client, pid, t["id"])
    # Nothing moved — not even the deleted clip was removed.
    assert sorted(c["start_frames"] for c in d["clips"]
                  if c["track_id"] == v1) == [0, 72, 144]
    assert len(d["clips"]) == 4


def test_ripple_scope_validation(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _, clips = _three(client, pid, t["id"])
    r = _commit(client, pid, "timeline.clip.remove",
                {"timeline_id": t["id"], "clip_id": clips[1]["id"],
                 "ripple_scope": "everything"}, expect=422)
    assert "scope" in r.text


# ---------- ripple insert ----------

def test_ripple_insert_pushes_followers(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _, clips = _three(client, pid, t["id"])
    # Ripple-place a 72f clip at frame 72 → old occupants push right.
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"],
             "asset_id": _video(client, pid, "n.mp4", sha="f" * 64)["id"],
             "start_frames": 72, "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    starts = sorted(c["start_frames"] for c in d["clips"])
    assert starts == [0, 72, 144, 216]
    # Without ripple the same place refuses (overlap).
    r = _commit(client, pid, "timeline.clip.place",
                {"timeline_id": t["id"],
                 "asset_id": _video(client, pid, "m.mp4",
                                    sha="7" * 64)["id"],
                 "start_frames": 72}, expect=422)
    assert "overlap" in r.text or "inside" in r.text


def test_ripple_insert_mid_clip_refuses(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _, clips = _three(client, pid, t["id"])
    r = _commit(client, pid, "timeline.clip.place",
                {"timeline_id": t["id"],
                 "asset_id": _video(client, pid, "n.mp4",
                                    sha="8" * 64)["id"],
                 "start_frames": 30, "ripple_scope": "track"},
                expect=422)
    assert "split" in r.text or "inside" in r.text


# ---------- ripple trim ----------

def test_ripple_trim_shifts_by_extent_delta(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _, clips = _three(client, pid, t["id"])
    # Shrink middle clip's tail by 24 frames → follower moves -24.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": clips[1]["id"],
             "out_frame": 48, "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    starts = sorted(c["start_frames"] for c in d["clips"])
    assert starts == [0, 72, 120]
    # Grow it back by 24 → follower returns.
    mid = sorted(d["clips"], key=lambda c: c["start_frames"])[1]
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": mid["id"],
             "out_frame": 72, "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    assert sorted(c["start_frames"] for c in d["clips"]) == [0, 72, 144]


def test_ripple_trim_head_and_non_ripple(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _, clips = _three(client, pid, t["id"])
    # Head trim shrinks extent — anchored start, followers close in.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": clips[0]["id"],
             "in_frame": 24, "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    starts = sorted(c["start_frames"] for c in d["clips"])
    assert starts == [0, 48, 120]  # 144 - 24
    first = sorted(d["clips"], key=lambda c: c["start_frames"])[0]
    assert (first["in_frame"], first["out_frame"]) == (24, 72)
    assert first["start_frames"] == 0  # start stays anchored
    # Non-ripple trim must NOT move followers.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": first["id"],
             "out_frame": 60})
    d = _detail(client, pid, t["id"])
    assert sorted(c["start_frames"] for c in d["clips"]) == [0, 48, 120]


def test_ripple_trim_uses_occupancy_not_source_delta(client):
    """Displacement is the change in INTEGER TIMELINE-FRAME occupancy,
    never the source-frame delta. NTSC source on a 24fps timeline:
    trimming 4 source frames changes occupancy by only 3."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    # A: 120f @ 30000/1001 trimmed to [0,91) → extent 72.8 → occupies 73.
    _insert(client, pid, t["id"],
            _video(client, pid, "n.mp4", sha="a" * 64,
                   frames=120, fps=29.97, duration=120 / 29.97),
            out_frame=91)
    d = _detail(client, pid, t["id"])
    a = _clip(d)
    assert a["start_frames"] == 0
    # B: plain 24fps clip packed right after → starts at 73.
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"],
             "asset_id": _video(client, pid, "b.mp4",
                                sha="b" * 64)["id"],
             "start_frames": 73})
    d = _detail(client, pid, t["id"])
    b = next(c for c in d["clips"] if c["start_frames"] == 73)
    # Shrink A by 4 SRC frames (91→87): extent 72.8→69.77 → ceil 73→70.
    # Occupancy delta -3 — a source-delta implementation would give -4.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": a["id"],
             "out_frame": 87, "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    b2 = next(c for c in d["clips"] if c["id"] == b["id"])
    assert b2["start_frames"] == 70  # NOT 69 — occupancy, not source
    # Grow A by 4 src frames (87→91): occupancy 70→73 → B returns to 73.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": a["id"],
             "out_frame": 91, "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    assert next(c for c in d["clips"] if c["id"] == b["id"])[
        "start_frames"] == 73


def test_ripple_trim_one_frame_and_gap_adjacent(client):
    """One-frame ripples move followers by one grid frame; a follower
    across a gap keeps its gap (shift preserves spacing)."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = _gapped(client, pid, t["id"])  # clips at 0 and 120 (48f gap)
    d = _detail(client, pid, t["id"])
    a, b = sorted(d["clips"], key=lambda c: c["start_frames"])
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": a["id"],
             "out_frame": 71, "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    b2 = next(c for c in d["clips"] if c["id"] == b["id"])
    assert b2["start_frames"] == 119  # moved -1, 48f gap preserved
    # And back: grow one frame → follower +1.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": a["id"],
             "out_frame": 72, "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    assert next(c for c in d["clips"] if c["id"] == b["id"])[
        "start_frames"] == 120


# ---------- gap management ----------

def _gapped(client, pid, tid):
    """V1: clips at 0 and 120 (gap 72..120)."""
    _insert(client, pid, tid, _video(client, pid))
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": tid,
             "asset_id": _video(client, pid, "b.mp4",
                                sha="b" * 64)["id"],
             "start_frames": 120})
    return _detail(client, pid, tid)["tracks"][0]["id"]


def test_gap_close(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = _gapped(client, pid, t["id"])
    _gap(client, pid, t["id"], "close", v1, 90)
    d = _detail(client, pid, t["id"])
    assert sorted(c["start_frames"] for c in d["clips"]) == [0, 72]


def test_gap_close_leading(client):
    """A gap before the first clip closes by pulling it to frame 0."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"],
             "asset_id": _video(client, pid)["id"],
             "start_frames": 40})
    v1 = _detail(client, pid, t["id"])["tracks"][0]["id"]
    _gap(client, pid, t["id"], "close", v1, 10)
    d = _detail(client, pid, t["id"])
    assert d["clips"][0]["start_frames"] == 0


def test_gap_close_refusals(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = _gapped(client, pid, t["id"])
    # Inside a clip.
    _gap(client, pid, t["id"], "close", v1, 20, expect=422)
    # Beyond the last clip — no gap.
    _gap(client, pid, t["id"], "close", v1, 500, expect=422)
    # Locked lane.
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v1, "locked": True})
    _gap(client, pid, t["id"], "close", v1, 90, expect=422)
    d = _detail(client, pid, t["id"])
    assert sorted(c["start_frames"] for c in d["clips"]) == [0, 120]


def test_gap_insert(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _, clips = _three(client, pid, t["id"])
    v1 = _detail(client, pid, t["id"])["tracks"][0]["id"]
    _gap(client, pid, t["id"], "insert", v1, 72, frames=24)
    d = _detail(client, pid, t["id"])
    assert sorted(c["start_frames"] for c in d["clips"]) == [0, 96, 168]


def test_gap_insert_refusals(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _, clips = _three(client, pid, t["id"])
    v1 = _detail(client, pid, t["id"])["tracks"][0]["id"]
    # Inside a clip → split first.
    r = _gap(client, pid, t["id"], "insert", v1, 30, frames=10,
             expect=422)
    assert "split" in r.text or "inside" in r.text
    # Nothing after the point → nothing to shift.
    _gap(client, pid, t["id"], "insert", v1, 999, frames=10, expect=422)
    # Missing/bad frames.
    _gap(client, pid, t["id"], "insert", v1, 72, expect=422)
    _gap(client, pid, t["id"], "insert", v1, 72, frames=-3, expect=422)


# ---------- provenance + isolation + export ----------

def test_cross_project_clip_and_track_isolation(client):
    pid = make_project(client)["id"]
    pid2 = make_project(client, "Other")["id"]
    t = _tl(client, pid)
    t2 = _tl(client, pid2, "Other")
    _insert(client, pid, t["id"], _video(client, pid))
    _insert(client, pid2, t2["id"],
            _video(client, pid2, "other.mp4", sha="9" * 64))
    foreign_clip = _clip(_detail(client, pid2, t2["id"]))
    foreign_track = _detail(client, pid2, t2["id"])["tracks"][0]["id"]
    # A clip that exists but lives on ANOTHER timeline → 404.
    r = _split(client, pid, t["id"], foreign_clip["id"], 30, expect=404)
    # Foreign track on a gap op → 404.
    r = _commit(client, pid, "timeline.gap.close",
                {"timeline_id": t["id"], "track_id": foreign_track,
                 "at_frame": 10}, expect=404)
    assert "not found" in r.text
    # Cross-PROJECT is enforced at the route layer (timeline lookup):
    # a timeline id from pid2 addressed on pid never resolves.
    r = client.post(f"/api/projects/{pid}/commands/commit",
                    json={"command_type": "timeline.clip.split",
                          "payload": {"timeline_id": t2["id"],
                                      "clip_id": "x", "at_frame": 1},
                          "scope": "project",
                          "expected_revision": _rev(client, pid),
                          "idempotency_key": "n1c-xproj"})
    assert r.status_code in (404, 422)


def test_split_and_delete_preserve_export_honesty(client):
    """Split+abut keeps a v1-compatible contiguous lane; a NON-ripple
    delete leaves a gap that v1 must refuse — never flattened."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _, clips = _three(client, pid, t["id"])
    _split(client, pid, t["id"], clips[1]["id"],
           clips[1]["start_frames"] + 24)
    # Lane is now [0..72][72..96][96..144][144..216] — contiguous → v1.
    e = _edl(client, pid, t["id"], version=1)
    assert e["format"] == "ade-edl/1"
    # Non-ripple delete of the middle 24f fragment → gap → v1 refuses.
    d = _detail(client, pid, t["id"])
    frag = next(c for c in d["clips"]
                if c["start_frames"] == 72)
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": frag["id"]})
    r = _edl(client, pid, t["id"], version=1, expect=409)
    assert "contiguous" in r.text or "gap" in r.text.lower()
    # …and v2 still serializes the honest gapped layout.
    e2 = _edl(client, pid, t["id"], version=2)
    assert e2["format"] == "ade-edl/2"
    assert any(g["duration_frames"] == 24 for g in e2["gaps"])


def test_gap_close_revert_restores_gapped_layout(client):
    """Revert restores the exact pre-op arrangement — gaps included."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = _gapped(client, pid, t["id"])  # clips at 0 and 120
    rev = _detail(client, pid, t["id"])["revision"]
    _gap(client, pid, t["id"], "close", v1, 90)
    assert sorted(c["start_frames"] for c in
                  _detail(client, pid, t["id"])["clips"]) == [0, 72]
    _commit(client, pid, "timeline.revert",
            {"timeline_id": t["id"], "to_revision": rev})
    d = _detail(client, pid, t["id"])
    assert sorted(c["start_frames"] for c in d["clips"]) == [0, 120]


def test_gap_close_restores_v1_export(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = _gapped(client, pid, t["id"])
    r = _edl(client, pid, t["id"], version=1, expect=409)
    _gap(client, pid, t["id"], "close", v1, 90)
    e = _edl(client, pid, t["id"], version=1)
    assert e["format"] == "ade-edl/1"
    assert [(c["in_frame"], c["out_frame"]) for c in e["clips"]] == [
        (0, 72), (0, 72)]
    assert e["total_duration_s"] == 6.0
