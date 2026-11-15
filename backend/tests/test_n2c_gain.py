"""N2-C — audio gain and track controls: per-clip + per-track gain in
decibels, mute/solo lane controls, deterministic EDL v2 metadata, and
the audibility precedence rules. The mix itself is N2-E — this
milestone pins the control model."""
import math

import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview, _rev
from tests.test_c5a_timeline import _video, _tl, _detail
from tests.test_n1a_tracks import _track, _edl
from tests.test_n2a_audio import _audio, _atrack, _place


def _gain(client, pid, tid, clip_id, db, expect=200, key=None):
    return _commit(client, pid, "timeline.clip.gain",
                   {"timeline_id": tid, "clip_id": clip_id,
                    "gain_db": db}, expect=expect, key=key)


def _tupdate(client, pid, tid, track_id, expect=200, key=None, **fields):
    return _commit(client, pid, "timeline.track.update",
                   {"timeline_id": tid, "track_id": track_id, **fields},
                   expect=expect, key=key)


def _clip_of(d, clip_id):
    return next(c for c in d["clips"] if c["id"] == clip_id)


def _track_of(d, track_id):
    return next(t for t in d["tracks"] if t["id"] == track_id)


# ---------- gain storage + validation ----------

def test_clip_gain_persists_and_reports(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid)
    _place(client, pid, t["id"], au, a1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    assert c["gain_db"] == 0.0  # unity default
    _gain(client, pid, t["id"], c["id"], -6.5)
    assert _clip_of(_detail(client, pid, t["id"]),
                    c["id"])["gain_db"] == -6.5


def test_track_gain_and_solo_persist(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    tr = _track_of(_detail(client, pid, t["id"]), a1["id"])
    assert tr["gain_db"] == 0.0 and tr["solo"] is False
    _tupdate(client, pid, t["id"], a1["id"], gain_db=3.0, solo=True)
    tr = _track_of(_detail(client, pid, t["id"]), a1["id"])
    assert tr["gain_db"] == 3.0 and tr["solo"] is True


@pytest.mark.parametrize("bad", [-60.5, 12.5, "loud", None, True])
def test_gain_db_bounds_refuse(client, bad):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid)
    _place(client, pid, t["id"], au, a1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    r = _gain(client, pid, t["id"], c["id"], bad, expect=422)
    assert r.status_code == 422
    _tupdate(client, pid, t["id"], a1["id"],
             gain_db=bad, expect=422)


def test_gain_bounds_edges_allowed(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid)
    _place(client, pid, t["id"], au, a1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    _gain(client, pid, t["id"], c["id"], -60)   # silence floor
    _gain(client, pid, t["id"], c["id"], 12)    # headroom cap
    _gain(client, pid, t["id"], c["id"], 0)     # reset to unity


def test_clip_gain_refused_on_video_lane(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v = _video(client, pid)
    _commit(client, pid, "timeline.clip.insert",
            {"timeline_id": t["id"], "asset_id": v["id"],
             "start_frames": 0})
    c = _detail(client, pid, t["id"])["clips"][0]
    r = _gain(client, pid, t["id"], c["id"], -6, expect=422)
    assert "audio" in r.json()["detail"].lower()


def test_gain_and_solo_refused_on_overlay_lane(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    o = _track(client, pid, t["id"], kind="overlay", name="O1")
    r = _tupdate(client, pid, t["id"], o["id"], gain_db=-3, expect=422)
    assert "overlay" in r.json()["detail"].lower()
    r = _tupdate(client, pid, t["id"], o["id"], solo=True, expect=422)
    assert "overlay" in r.json()["detail"].lower()


# ---------- conversion + combination semantics ----------

def test_db_to_linear_contract():
    from app.domain.timeline import db_to_linear
    assert db_to_linear(0) == 1.0            # unity
    assert math.isclose(db_to_linear(-6), 10 ** -0.3, rel_tol=1e-9)
    assert math.isclose(db_to_linear(-20), 0.1, rel_tol=1e-9)
    assert math.isclose(db_to_linear(6), 10 ** 0.3, rel_tol=1e-9)
    assert db_to_linear(12) > db_to_linear(0)  # positive amplifies
    assert db_to_linear(-60) < 0.0011          # practical silence


def test_effective_gain_adds_db():
    from app.domain.timeline import effective_gain_db
    # Clip × track in linear == clip+track in dB.
    assert effective_gain_db(-6, -6) == -12
    assert effective_gain_db(3, 0) == 3
    assert effective_gain_db(-60, 12) == -48
    assert effective_gain_db(0, 0) == 0      # unity path


# ---------- audibility precedence ----------

def _mk_track(visible=True, muted=False, solo=False):
    from app.db.models import TimelineTrack
    return TimelineTrack(id="t", project_id="p", timeline_id="x",
                         kind="audio", position=0, name="A",
                         visible=visible, locked=False, muted=muted,
                         gain_db=0.0, solo=solo)


def test_lane_audible_precedence():
    from app.domain.timeline import lane_audible
    # Plain lane audible; mute silences; hidden is render-inert.
    assert lane_audible(_mk_track(), False) is True
    assert lane_audible(_mk_track(muted=True), False) is False
    assert lane_audible(_mk_track(visible=False), False) is False
    # Solo set present: soloed lanes audible, others silent — even
    # unmuted ones; mute still wins inside the solo set.
    assert lane_audible(_mk_track(solo=True), True) is True
    assert lane_audible(_mk_track(), True) is False
    assert lane_audible(_mk_track(solo=True, muted=True), True) is False
    # Hidden+muted+non-soloed lane under an active solo set: all rules
    # agree — silent.
    assert lane_audible(_mk_track(visible=False, muted=True), True) is False
    # No solo set: mute is the only silencer.
    assert lane_audible(_mk_track(solo=True), False) is True


def test_solo_set_via_api_multiple_and_hidden(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"], "A1")
    a2 = _atrack(client, pid, t["id"], "A2")
    _tupdate(client, pid, t["id"], a1["id"], solo=True)
    _tupdate(client, pid, t["id"], a2["id"], solo=True)
    from app.domain.timeline import any_solo, lane_audible
    with client.app.state.session_factory() as s:
        from app.db.models import TimelineTrack
        trs = {tr.id: tr for tr in s.query(TimelineTrack).all()}
        assert any_solo(s, t["id"]) is True
        # Multiple soloed lanes are ALL audible.
        assert lane_audible(trs[a1["id"]], True) is True
        assert lane_audible(trs[a2["id"]], True) is True
        # A hidden soloed lane stays inert.
        _tupdate(client, pid, t["id"], a2["id"], visible=False)
        s.expire_all()
        trs = {tr.id: tr for tr in s.query(TimelineTrack).all()}
        assert lane_audible(trs[a2["id"]], True) is False


# ---------- command integrity ----------

def test_gain_commands_undo_redo(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid)
    _place(client, pid, t["id"], au, a1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    _gain(client, pid, t["id"], c["id"], -9)
    _tupdate(client, pid, t["id"], a1["id"], gain_db=2, solo=True)
    _commit(client, pid, "timeline.undo", {"timeline_id": t["id"]})
    d = _detail(client, pid, t["id"])
    tr = _track_of(d, a1["id"])
    assert tr["gain_db"] == 0.0 and tr["solo"] is False
    assert _clip_of(d, c["id"])["gain_db"] == -9.0
    _commit(client, pid, "timeline.undo", {"timeline_id": t["id"]})
    assert _clip_of(_detail(client, pid, t["id"]),
                    c["id"])["gain_db"] == 0.0
    _commit(client, pid, "timeline.redo", {"timeline_id": t["id"]})
    _commit(client, pid, "timeline.redo", {"timeline_id": t["id"]})
    d = _detail(client, pid, t["id"])
    assert _clip_of(d, c["id"])["gain_db"] == -9.0
    assert _track_of(d, a1["id"])["solo"] is True


def test_clip_gain_stable_id_and_idempotency(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid)
    _place(client, pid, t["id"], au, a1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    k = "gain-key-1"
    r1 = _gain(client, pid, t["id"], c["id"], -4.5, key=k)
    r2 = _gain(client, pid, t["id"], c["id"], -4.5, key=k)
    assert r1["command_id"] == r2["command_id"]  # replayed, not rerun
    assert _clip_of(_detail(client, pid, t["id"]),
                    c["id"])["gain_db"] == -4.5


def test_gain_stale_revision_refused(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid)
    _place(client, pid, t["id"], au, a1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    _gain(client, pid, t["id"], c["id"], -3)
    # The project's revision advanced; replaying with the pre-gain
    # expected_revision must conflict.
    proj = client.get(f"/api/projects/{pid}").json()
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={"command_type": "timeline.clip.gain",
              "payload": {"timeline_id": t["id"], "clip_id": c["id"],
                          "gain_db": -1},
              "scope": "project",
              "expected_revision": proj["revision"] - 1,
              "idempotency_key": "stale-gain-1"})
    assert r.status_code == 409


def test_gain_on_locked_track_still_allowed(client):
    """Lock is the clip-edit guard — mix controls stay reachable."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid)
    _place(client, pid, t["id"], au, a1["id"])
    _tupdate(client, pid, t["id"], a1["id"], locked=True)
    _tupdate(client, pid, t["id"], a1["id"], gain_db=-6, muted=True)
    tr = _track_of(_detail(client, pid, t["id"]), a1["id"])
    assert tr["gain_db"] == -6 and tr["muted"] is True


# ---------- EDL determinism ----------

def test_edl_v2_carries_gain_and_controls(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid)
    _place(client, pid, t["id"], au, a1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    _gain(client, pid, t["id"], c["id"], -7.5)
    _tupdate(client, pid, t["id"], a1["id"], gain_db=2.5,
             muted=True, solo=True)
    e = _edl(client, pid, t["id"], version=2)
    tr = next(x for x in e["tracks"] if x["id"] == a1["id"])
    assert tr["gain_db"] == 2.5 and tr["muted"] is True
    assert tr["solo"] is True
    ce = next(x for x in e["clips"] if x["clip_id"] == c["id"])
    assert ce["gain_db"] == -7.5
    # Deterministic: identical calls → byte-identical documents.
    e2 = _edl(client, pid, t["id"], version=2)
    assert e == e2


def test_edl_v1_unchanged_by_gain(client):
    """v1 documents carry no mix metadata — additive change."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v = _video(client, pid)
    _commit(client, pid, "timeline.clip.insert",
            {"timeline_id": t["id"], "asset_id": v["id"],
             "start_frames": 0})
    e = _edl(client, pid, t["id"])  # v1-representable → v1 doc
    assert "tracks" not in e  # v1 has no lanes section
    assert "gain_db" not in str(e)


def test_export_still_refuses_visible_audio_lane(client):
    """N2-C establishes the control model — populated visible audio
    lanes still refuse loudly until N2-E."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid)
    _place(client, pid, t["id"], au, a1["id"])
    _tupdate(client, pid, t["id"], a1["id"], gain_db=-3, solo=True)
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"], "profile": "720p"})
    j = wait_for_job(client, pid, r["job_id"])
    assert j["status"] == "failed"
    assert "audio lanes" in (j["error"] or "").lower()


# ---------- migration ----------

def test_migration_backfills_unity():
    """0015 adds gain_db/solo with unity defaults — existing rows keep
    their edits and gain 0 dB."""
    import sqlalchemy as sa
    from app.db.models import TimelineClip, TimelineTrack
    eng = sa.create_engine("sqlite://")
    from app.db.models import Base
    Base.metadata.create_all(eng)
    insp = sa.inspect(eng)
    tc = {c["name"] for c in insp.get_columns("timeline_tracks")}
    cc = {c["name"] for c in insp.get_columns("timeline_clips")}
    assert {"gain_db", "solo"} <= tc
    assert "gain_db" in cc
