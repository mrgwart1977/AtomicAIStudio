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


def test_export_renders_soloed_audio_lane_now(client):
    """N2-E renders audio lanes — a soloed audio lane no longer
    refuses on lane grounds; the synthetic fixture fails only on
    missing source bytes, never on 'audio lanes'."""
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
    assert "audio lanes" not in (j["error"] or "").lower()


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


# ---------- N2-C review additions ----------

def test_gain_db_refuses_nan_and_inf():
    """check_gain_db is the fail-closed gate — NaN and ±inf must never
    reach storage even if a non-JSON caller bypasses the API schema."""
    from app.domain.timeline import check_gain_db
    for bad in (math.nan, math.inf, -math.inf):
        with pytest.raises(ValueError):
            check_gain_db(bad)
    # Unity and both bounds are the accepted envelope.
    assert check_gain_db(-60.0) == -60.0
    assert check_gain_db(12.0) == 12.0


def test_solo_never_removes_video_from_composite():
    """Solo is an audibility flag only — a soloed audio lane must not
    hide any video lane's picture. composite_plan consults `visible`
    alone, so the video clip stays in the plan."""
    from app.domain import timeline_export as tx
    from tests.test_n1d_composite import _v2, _v2clip, _trk
    tracks = [_trk("v1", "video", 0),
              {**_trk("a1", "audio", 1), "solo": True}]
    clips = [_v2clip("pic", "v1")]
    plan = tx.composite_plan(_v2(clips, tracks), {"pic": {}})
    assert [c["clip_id"] for c in plan["clips"]] == ["pic"]
    # And the hidden-video-lane audio policy: invisible ⇒ absent from
    # the plan entirely — its embedded audio cannot reach the mix.
    tracks[0]["visible"] = False
    plan = tx.composite_plan(_v2(clips, tracks), {"pic": {}})
    assert plan["clips"] == []


def test_migration_0014_upgrade_preserves_flags(tmp_path):
    """Real pre-0015 upgrade: muted/hidden track flags and clip rows
    keep their values while gaining unity defaults (0 dB, solo=false).
    Old snapshots restore via .get defaults — verified implicitly by
    the backfill never touching non-column state."""
    import sqlite3
    import uuid
    from alembic.config import Config as AlembicConfig
    from alembic import command as alembic_command
    from app.main import _alembic_script_location, run_migrations

    url = f"sqlite:///{tmp_path}/pre.db"
    run_migrations(url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location",
                        str(_alembic_script_location()))
    cfg.set_main_option("sqlalchemy.url", url)
    # Back down to the pre-N2-C shape (0015 has a real downgrade).
    alembic_command.downgrade(cfg, "0014_n1e")

    con = sqlite3.connect(str(tmp_path / "pre.db"))
    pid, tl, tr, cl = (str(uuid.uuid4()) for _ in range(4))
    now = "2024-01-01 00:00:00"
    con.execute(
        "INSERT INTO projects (id, name, template, kind, status,"
        " aspect_ratio, width, height, fps_num, fps_den, style,"
        " settings_json, revision, created_at, updated_at) VALUES "
        f"('{pid}', 'Legacy', 'blank', 'film', 'active', '16:9',"
        f" 1920, 1080, 24, 1, '{{}}', '{{}}', 0, '{now}', '{now}')")
    con.execute(
        "INSERT INTO timelines (id, project_id, name, description,"
        " fps_num, fps_den, status, revision, undo_stack_json,"
        " redo_stack_json, created_at, updated_at) VALUES "
        f"('{tl}', '{pid}', 'Old Cut', '', 24, 1, 'active',"
        f" 2, '[]', '[]', '{now}', '{now}')")
    # Muted + hidden audio lane at the 0014 shape (no gain_db/solo).
    tr_cols = [r[1] for r in con.execute(
        "PRAGMA table_info(timeline_tracks)")]
    assert "gain_db" not in tr_cols and "solo" not in tr_cols
    con.execute(
        "INSERT INTO timeline_tracks (id, project_id, timeline_id,"
        " kind, position, name, visible, locked, muted, created_at)"
        f" VALUES ('{tr}', '{pid}', '{tl}', 'audio', 0, 'A1',"
        f" 0, 0, 1, '{now}')")   # hidden + muted legacy lane
    cl_cols = [r[1] for r in con.execute(
        "PRAGMA table_info(timeline_clips)")]
    assert "gain_db" not in cl_cols
    con.execute(
        "INSERT INTO timeline_clips (id, project_id, timeline_id,"
        " track_id, position, start_frames, source_asset_id,"
        " source_version_id, source_sha256, in_frame, out_frame,"
        " src_frames, src_fps_num, src_fps_den, src_duration_s,"
        f" created_at) VALUES ('{cl}', '{pid}', '{tl}', '{tr}',"
        f" 0, 0, 'a1', 'v1', '{'ab'*32}', 0, 48000, 48000, 48000,"
        f" 1, 1.0, '{now}')")
    con.commit()
    con.close()

    alembic_command.upgrade(cfg, "head")
    con = sqlite3.connect(str(tmp_path / "pre.db"))
    row = con.execute(
        "SELECT visible, locked, muted, gain_db, solo FROM"
        f" timeline_tracks WHERE id='{tr}'").fetchone()
    # Flags preserved; new columns backfilled to documented defaults.
    assert row == (0, 0, 1, 0.0, 0)
    row = con.execute(
        f"SELECT gain_db FROM timeline_clips WHERE id='{cl}'").fetchone()
    assert row == (0.0,)
    con.close()
