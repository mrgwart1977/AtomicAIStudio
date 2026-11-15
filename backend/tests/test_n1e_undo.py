"""N1-E — undo/redo on the revision infrastructure: per-timeline
stacks of {rev,label} pointers into entity_revisions snapshots,
managed inside the same transaction as every mutation. New edits
clear the redo stack (divergent branch); undo/redo themselves are
commands, so expected_timeline, idempotency, and audit all apply."""
import pytest

from tests.conftest import make_project
from tests.test_c4b_shot_generate import _commit, _preview, _rev
from tests.test_c5a_timeline import _video, _tl, _insert, _detail
from tests.test_n1a_tracks import _track


def _undo(client, pid, tid, expect=200, key=None, **kw):
    return _commit(client, pid, "timeline.undo",
                   {"timeline_id": tid, **kw}, expect=expect, key=key)


def _redo(client, pid, tid, expect=200, key=None, **kw):
    return _commit(client, pid, "timeline.redo",
                   {"timeline_id": tid, **kw}, expect=expect, key=key)


def _place(client, pid, tid, asset, track_id, at=0, **kw):
    return _commit(client, pid, "timeline.clip.place",
                   {"timeline_id": tid, "asset_id": asset["id"],
                    "track_id": track_id, "start_frames": at, **kw})


def _split(client, pid, tid, clip_id, at, **kw):
    return _commit(client, pid, "timeline.clip.split",
                   {"timeline_id": tid, "clip_id": clip_id,
                    "at_frame": at, **kw})


def _ripple_rm(client, pid, tid, clip_id, scope="all", **kw):
    return _commit(client, pid, "timeline.clip.remove",
                   {"timeline_id": tid, "clip_id": clip_id,
                    "ripple_scope": scope, **kw})


def _trim(client, pid, tid, clip_id, field, n, **kw):
    return _commit(client, pid, "timeline.clip.trim",
                   {"timeline_id": tid, "clip_id": clip_id,
                    field: n, **kw})


def _setup(client, pid):
    t = _tl(client, pid)
    a, b = _video(client, pid, "v1.mp4"), _video(client, pid, "v2.mp4")
    _insert(client, pid, t["id"], a)
    _insert(client, pid, t["id"], b)
    return t, a, b


def test_undo_redo_clip_remove_roundtrip(client):
    pid = make_project(client)["id"]
    t, a, _ = _setup(client, pid)
    d = _detail(client, pid, t["id"])
    assert d["undo_label"] and "clip" in d["undo_label"].lower()
    clip = sorted(d["clips"], key=lambda c: c["start_frames"])[-1]
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": clip["id"]})
    d = _detail(client, pid, t["id"])
    assert len(d["clips"]) == 1 and d["undo_label"]
    _undo(client, pid, t["id"])
    d = _detail(client, pid, t["id"])
    assert len(d["clips"]) == 2
    # stable identity: the removed clip returns with its ORIGINAL id
    assert clip["id"] in {c["id"] for c in d["clips"]}
    assert d["redo_label"]  # the removed state is redoable
    _redo(client, pid, t["id"])
    assert len(_detail(client, pid, t["id"])["clips"]) == 1


def test_undo_after_split_restores_single_clip(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a = _video(client, pid, "v3.mp4")
    _insert(client, pid, t["id"], a)
    c0 = _detail(client, pid, t["id"])["clips"][0]
    _split(client, pid, t["id"], c0["id"], 30)
    assert len(_detail(client, pid, t["id"])["clips"]) == 2
    _undo(client, pid, t["id"])
    d = _detail(client, pid, t["id"])
    assert len(d["clips"]) == 1
    c = d["clips"][0]
    assert (c["in_frame"], c["out_frame"]) == (
        c0["in_frame"], c0["out_frame"])
    assert c["source_sha256"] == a["sha256"]


def test_undo_after_ripple_delete_restores_positions(client):
    pid = make_project(client)["id"]
    t, a, b = _setup(client, pid)
    _insert(client, pid, t["id"], _video(client, pid, "v4.mp4"))
    before = _detail(client, pid, t["id"])
    starts_before = sorted(c["start_frames"] for c in before["clips"])
    clip0 = sorted(before["clips"], key=lambda c: c["start_frames"])[0]
    _ripple_rm(client, pid, t["id"], clip0["id"])
    _undo(client, pid, t["id"])
    after = _detail(client, pid, t["id"])
    assert sorted(c["start_frames"] for c in after["clips"]) == starts_before
    # source pins intact on every restored clip
    for c in after["clips"]:
        assert c["source_sha256"] and c["source_version_id"]


def test_undo_after_trim_restores_source_range(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a = _video(client, pid, "v5.mp4")
    _insert(client, pid, t["id"], a)
    c0 = _detail(client, pid, t["id"])["clips"][0]
    _trim(client, pid, t["id"], c0["id"], "out_frame", 40)
    _undo(client, pid, t["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    assert (c["in_frame"], c["out_frame"]) == (
        c0["in_frame"], c0["out_frame"])


def test_undo_after_track_remove_restores_lane_and_clips(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    tr = _track(client, pid, t["id"], "overlay")
    _insert(client, pid, t["id"], _video(client, pid, "v6.mp4"))
    _place(client, pid, t["id"], _video(client, pid, "v7.mp4"), tr["id"])
    before = _detail(client, pid, t["id"])
    # remove requires an empty lane: remove its clip first, then the
    # lane — undo of the lane removal must restore the empty lane,
    # and undo of the clip removal restores the clip onto it.
    clip_on_tr = next(c for c in before["clips"]
                      if c["track_id"] == tr["id"])
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": clip_on_tr["id"]})
    _commit(client, pid, "timeline.track.remove",
            {"timeline_id": t["id"], "track_id": tr["id"]})
    _undo(client, pid, t["id"])            # restore lane (empty)
    d = _detail(client, pid, t["id"])
    assert tr["id"] in {x["id"] for x in d["tracks"]}
    _undo(client, pid, t["id"])            # restore clip on it
    after = _detail(client, pid, t["id"])
    assert {tr2["id"] for tr2 in after["tracks"]} == {
        tr2["id"] for tr2 in before["tracks"]}
    assert len(after["clips"]) == len(before["clips"])
    # no orphans: every clip rides an existing track
    tids = {tr2["id"] for tr2 in after["tracks"]}
    assert all(c["track_id"] in tids for c in after["clips"])


def test_undo_after_track_reorder_restores_positions(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    tr = _track(client, pid, t["id"], "overlay")
    before = _detail(client, pid, t["id"])
    pos_before = {x["id"]: x["position"] for x in before["tracks"]}
    _commit(client, pid, "timeline.track.reorder",
            {"timeline_id": t["id"], "track_id": tr["id"],
             "to_position": 0})
    _undo(client, pid, t["id"])
    after = _detail(client, pid, t["id"])
    assert {x["id"]: x["position"] for x in after["tracks"]} == pos_before


def test_new_edit_clears_redo_branch(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a = _video(client, pid, "v8.mp4")
    _insert(client, pid, t["id"], a)
    _insert(client, pid, t["id"], _video(client, pid, "v9.mp4"))
    _undo(client, pid, t["id"])          # redo tip = second insert
    _trim(client, pid, t["id"],
          _detail(client, pid, t["id"])["clips"][0]["id"],
          "out_frame", 10)               # divergent edit
    d = _detail(client, pid, t["id"])
    assert d["redo_label"] is None
    _redo(client, pid, t["id"], expect=422)


def test_undo_redo_sequence_walks_history(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid, "v10.mp4"))
    _insert(client, pid, t["id"], _video(client, pid, "v11.mp4"))
    _insert(client, pid, t["id"], _video(client, pid, "v12.mp4"))
    for want in (2, 1, 0):
        _undo(client, pid, t["id"])
        assert len(_detail(client, pid, t["id"])["clips"]) == want
    _undo(client, pid, t["id"], expect=422)  # bottom of stack
    for want in (1, 2, 3):
        _redo(client, pid, t["id"])
        assert len(_detail(client, pid, t["id"])["clips"]) == want
    _redo(client, pid, t["id"], expect=422)  # top of stack


def test_undo_stale_revision_refused(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid, "v13.mp4"))
    d = _detail(client, pid, t["id"])
    # commit another edit, then undo with the OLD expected revision
    _insert(client, pid, t["id"], _video(client, pid, "v14.mp4"))
    _undo(client, pid, t["id"], expect=422,
          expected_timeline={"id": t["id"], "revision": d["revision"]})
    assert len(_detail(client, pid, t["id"])["clips"]) == 2


def test_undo_idempotent_replay(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid, "v15.mp4"))
    r1 = _undo(client, pid, t["id"], key="undo-key-1")
    r2 = _undo(client, pid, t["id"], key="undo-key-1")
    assert len(_detail(client, pid, t["id"])["clips"]) == 0
    # replayed, not double-applied: a second real undo would 422/—
    _undo(client, pid, t["id"], expect=422)


def test_undo_nothing_refused_fresh_timeline(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _undo(client, pid, t["id"], expect=422)
    _redo(client, pid, t["id"], expect=422)


def test_undo_preserves_edl_determinism(client):
    pid = make_project(client)["id"]
    t, a, b = _setup(client, pid)
    before = _detail(client, pid, t["id"])
    clip = sorted(before["clips"], key=lambda c: c["start_frames"])[-1]
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": clip["id"]})
    _undo(client, pid, t["id"])
    after = _detail(client, pid, t["id"])
    # stable identity: restored rows keep their ORIGINAL clip ids, so
    # the full layout — ids, timing, source pins, provenance — is equal
    shape = lambda d: sorted(
        (c["id"], c["track_id"], c["start_frames"], c["in_frame"],
         c["out_frame"], c["source_sha256"], c.get("shot_id"),
         c.get("scene_id")) for c in d["clips"])
    assert shape(after) == shape(before)
    assert {x["id"]: (x["position"], x["kind"], x["visible"],
                      x["muted"]) for x in after["tracks"]} == {
        x["id"]: (x["position"], x["kind"], x["visible"], x["muted"])
        for x in before["tracks"]}


def test_undo_labels_exposed_and_interpretable(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid, "v16.mp4"))
    _trim(client, pid, t["id"],
          _detail(client, pid, t["id"])["clips"][0]["id"],
          "in_frame", 5)
    d = _detail(client, pid, t["id"])
    assert d["undo_label"] and "trim" in d["undo_label"].lower()


def test_undo_cross_project_isolation(client):
    p1, p2 = make_project(client), make_project(client)
    t1 = _tl(client, p1["id"])
    _insert(client, p1["id"], t1["id"], _video(client, p1["id"]))
    # project 2's command scope cannot reach project 1's timeline —
    # the scoped lookup refuses, and project 1's stack is untouched.
    r = _undo(client, p2["id"], t1["id"], expect=404)
    d = _detail(client, p1["id"], t1["id"])
    assert d["undo_label"] is not None  # still undoable from p1


# ---------- §2 — remaining edit surfaces ----------

def test_undo_after_gap_insert_and_close(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = _detail(client, pid, t["id"])["tracks"][0]["id"]
    _insert(client, pid, t["id"], _video(client, pid, "g1.mp4"))
    _insert(client, pid, t["id"], _video(client, pid, "g2.mp4"))
    before = _detail(client, pid, t["id"])
    _commit(client, pid, "timeline.gap.insert",
            {"timeline_id": t["id"], "track_id": v1,
             "at_frame": 72, "frames": 24})
    d = _detail(client, pid, t["id"])
    assert any(g["start"] == 72 and g["frames"] == 24
               for g in d.get("gaps", []) if isinstance(g, dict)) or \
        [c["start_frames"] for c in sorted(
            d["clips"], key=lambda c: c["start_frames"])] == [0, 96]
    _undo(client, pid, t["id"])
    assert [c["start_frames"] for c in _detail(
        client, pid, t["id"])["clips"]] == \
        [c["start_frames"] for c in before["clips"]]


def test_undo_after_gap_close(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = _detail(client, pid, t["id"])["tracks"][0]["id"]
    _insert(client, pid, t["id"], _video(client, pid, "g3.mp4"))
    _insert(client, pid, t["id"], _video(client, pid, "g4.mp4"))
    _commit(client, pid, "timeline.gap.insert",
            {"timeline_id": t["id"], "track_id": v1,
             "at_frame": 72, "frames": 24})
    starts_gapped = sorted(c["start_frames"] for c in
                           _detail(client, pid, t["id"])["clips"])
    _commit(client, pid, "timeline.gap.close",
            {"timeline_id": t["id"], "track_id": v1, "at_frame": 80})
    _undo(client, pid, t["id"])
    assert sorted(c["start_frames"] for c in
                  _detail(client, pid, t["id"])["clips"]) == starts_gapped


def test_undo_after_track_update_visibility_lock_mute(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    tr = _track(client, pid, t["id"], "overlay")
    orig = next(x for x in _detail(client, pid, t["id"])["tracks"]
                if x["id"] == tr["id"])
    for field, val in (("visible", False), ("locked", True),
                       ("muted", True)):
        _commit(client, pid, "timeline.track.update",
                {"timeline_id": t["id"], "track_id": tr["id"],
                 field: val})
        _undo(client, pid, t["id"])
        got = next(x for x in _detail(client, pid, t["id"])["tracks"]
                   if x["id"] == tr["id"])
        assert got[field] == orig[field]


def test_undo_after_clip_move_across_lanes(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    tr = _track(client, pid, t["id"], "overlay")
    _insert(client, pid, t["id"], _video(client, pid, "mv.mp4"))
    c0 = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"], "clip_id": c0["id"],
             "to_track_id": tr["id"], "start_frames": 24})
    _undo(client, pid, t["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    assert c["id"] == c0["id"] and c["track_id"] == c0["track_id"]
    assert c["start_frames"] == c0["start_frames"]


# ---------- §6 — export correctness after undo/redo ----------

def _edl(client, pid, tid, version=2):
    from tests.test_n1a_tracks import _edl as _e
    return _e(client, pid, tid, version)


def _strip_rev(edl):
    return {k: v for k, v in edl.items() if k != "revision"}


def test_undo_split_restores_edl_document(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid, "s.mp4"))
    edl_before = _edl(client, pid, t["id"], version=1)
    c0 = _detail(client, pid, t["id"])["clips"][0]
    _split(client, pid, t["id"], c0["id"], 30)
    _undo(client, pid, t["id"])
    edl_after = _edl(client, pid, t["id"], version=1)
    # the undoable document is identical modulo the revision counter —
    # INCLUDING clip ids (stable-identity restore)
    assert _strip_rev(edl_after) == _strip_rev(edl_before)


def test_undo_ripple_delete_restores_composite_pin(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    o1 = _track(client, pid, t["id"], kind="overlay", name="O1")
    _insert(client, pid, t["id"], _video(client, pid, "r1.mp4"))
    _insert(client, pid, t["id"], _video(client, pid, "r2.mp4"))
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "track_id": o1["id"],
             "asset_id": _video(client, pid, "r3.mp4")["id"],
             "start_frames": 12})
    edl_before = _edl(client, pid, t["id"])
    clip0 = sorted(_detail(client, pid, t["id"])["clips"],
                   key=lambda c: c["start_frames"])[0]
    _ripple_rm(client, pid, t["id"], clip0["id"])
    _undo(client, pid, t["id"])
    edl_after = _edl(client, pid, t["id"])
    assert _strip_rev(edl_after) == _strip_rev(edl_before)


def test_redo_trim_then_export_document_matches(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid, "tr.mp4"))
    c0 = _detail(client, pid, t["id"])["clips"][0]
    _trim(client, pid, t["id"], c0["id"], "out_frame", 40)
    edl_trimmed = _edl(client, pid, t["id"], version=1)
    _undo(client, pid, t["id"])
    _redo(client, pid, t["id"])
    edl_redone = _edl(client, pid, t["id"], version=1)
    assert _strip_rev(edl_redone) == _strip_rev(edl_trimmed)


def test_undo_reorder_restores_composite_stack(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    o1 = _track(client, pid, t["id"], kind="overlay", name="O1")
    _insert(client, pid, t["id"], _video(client, pid, "st.mp4"))
    edl_before = _edl(client, pid, t["id"])
    _commit(client, pid, "timeline.track.reorder",
            {"timeline_id": t["id"], "track_id": o1["id"],
             "to_position": 0})
    _undo(client, pid, t["id"])
    assert _strip_rev(_edl(client, pid, t["id"])) == \
        _strip_rev(edl_before)


def test_undo_hidden_restore_composite_document(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    o1 = _track(client, pid, t["id"], kind="overlay", name="O1")
    _insert(client, pid, t["id"], _video(client, pid, "h.mp4"))
    edl_before = _edl(client, pid, t["id"])
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": o1["id"],
             "visible": False})
    _undo(client, pid, t["id"])
    assert _strip_rev(_edl(client, pid, t["id"])) == \
        _strip_rev(edl_before)
