"""N3-A — persistent per-track gain automation.

Model: `timeline_tracks.automation_json` — a bounded keyframe list
[{id, at_tl_frames, gain_db}] keyed in ABSOLUTE timeline frames (clip
moves/trims/splits can never orphan lane keyframes), same stored
representation and interp vocabulary ('linear'|'step') as clip
automation. Command: `timeline.track.automation` (replace-all, one
undo step). The exporter folds the lane curve into each contributing
stream's single dB sum — audio lanes and video-embedded audio alike.
"""
import json
from fractions import Fraction

import pytest

from tests.conftest import make_project
from tests.test_c4b_shot_generate import _commit, _rev
from tests.test_c5a_timeline import _tl, _detail, _insert, _video
from tests.test_n1a_tracks import _track, _edl
from tests.test_n2a_audio import _audio, _atrack, _place


def _tauto(client, pid, tid, track_id, points, expect=200, key=None,
           interp=None):
    return _commit(client, pid, "timeline.track.automation",
                   {"timeline_id": tid, "track_id": track_id,
                    "points": points, **({"interp": interp}
                                         if interp else {})},
                   expect=expect, key=key)


def _track_of(d, track_id):
    return next(t for t in d["tracks"] if t["id"] == track_id)


def _setup(client):
    """Project + timeline + audio lane with one clip."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"], "A1")
    au = _audio(client, pid, duration=2.0)
    _place(client, pid, t["id"], au, a1["id"], at=0)
    return pid, t["id"], a1


# ---------- model + command ----------

def test_track_automation_persist_and_edl(client):
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"], [
        {"at_tl_frames": 48, "gain_db": -20},
        {"at_tl_frames": 0, "gain_db": 0},
        {"at_tl_frames": 24, "gain_db": -6}])
    d = _detail(client, pid, tid)
    tr = _track_of(d, a1["id"])
    pts = json.loads(tr["automation_json"])
    # sorted by absolute position; missing ids assigned deterministically
    assert [p["at_tl_frames"] for p in pts] == [0, 24, 48]
    assert all(p["id"] for p in pts)
    doc = _edl(client, pid, tid, version=2)
    et = next(x for x in doc["tracks"] if x["id"] == a1["id"])
    assert [p["gain_db"] for p in et["automation"]] == [0, -6, -20]
    assert et["automation_interp"] == "linear"


def test_track_automation_interp_field(client):
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"],
           [{"at_tl_frames": 0, "gain_db": 0},
            {"at_tl_frames": 24, "gain_db": -12}], interp="step")
    tr = _track_of(_detail(client, pid, tid), a1["id"])
    assert tr["automation_interp"] == "step"
    doc = _edl(client, pid, tid, version=2)
    et = next(x for x in doc["tracks"] if x["id"] == a1["id"])
    assert et["automation_interp"] == "step"


@pytest.mark.parametrize("points", [
    # duplicate absolute positions refused (same policy as clips)
    [{"at_tl_frames": 10, "gain_db": 0},
     {"at_tl_frames": 10, "gain_db": -6}],
    [{"at_tl_frames": -1, "gain_db": 0}],          # negative
    [{"at_tl_frames": 10.5, "gain_db": 0}],        # non-integer
    [{"at_tl_frames": 1 << 30, "gain_db": 0}],     # past the ceiling
    [{"at_tl_frames": 10, "gain_db": 99}],         # gain bound
    [{"at_tl_frames": 10, "gain_db": "x"}],
    "not-a-list",
])
def test_track_automation_refuses_malformed(client, points):
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"], points, expect=422)


def test_track_automation_max_points(client):
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"],
           [{"at_tl_frames": i, "gain_db": 0} for i in range(257)],
           expect=422)
    _tauto(client, pid, tid, a1["id"],
           [{"at_tl_frames": i, "gain_db": 0} for i in range(256)])


def test_track_automation_positions_may_exceed_program(client):
    """Points are absolute timeline frames — a ramp can start past the
    current program end (it's inert until clips reach it)."""
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"],
           [{"at_tl_frames": 5000, "gain_db": -12}])
    pts = json.loads(
        _track_of(_detail(client, pid, tid), a1["id"])
        ["automation_json"])
    assert pts[0]["at_tl_frames"] == 5000


def test_track_automation_overlay_lane_refused(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    o1 = _track(client, pid, t["id"], kind="overlay", name="O1")
    _tauto(client, pid, t["id"], o1["id"],
           [{"at_tl_frames": 0, "gain_db": -6}], expect=422)


def test_track_automation_missing_track_404(client):
    pid, tid, _a1 = _setup(client)
    _tauto(client, pid, tid, "no-such-track",
           [{"at_tl_frames": 0, "gain_db": 0}], expect=404)


def test_track_automation_bad_interp(client):
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"],
           [{"at_tl_frames": 0, "gain_db": 0}], interp="bez", expect=422)


def test_track_automation_video_lane_allowed(client):
    """A4 — video lanes carry embedded audio; their lane automation
    applies to it."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    v1 = _detail(client, pid, t["id"])["tracks"][0]
    _tauto(client, pid, t["id"], v1["id"],
           [{"at_tl_frames": 0, "gain_db": -6}])


def test_track_automation_undo_redo(client):
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"],
           [{"at_tl_frames": 0, "gain_db": -20}])
    _commit(client, pid, "timeline.undo", {"timeline_id": tid})
    tr = _track_of(_detail(client, pid, tid), a1["id"])
    assert json.loads(tr["automation_json"]) == []
    _commit(client, pid, "timeline.redo", {"timeline_id": tid})
    tr = _track_of(_detail(client, pid, tid), a1["id"])
    assert json.loads(tr["automation_json"])[0]["gain_db"] == -20


def test_track_automation_idempotent(client):
    pid, tid, a1 = _setup(client)
    payload = [{"at_tl_frames": 0, "gain_db": -12}]
    _tauto(client, pid, tid, a1["id"], payload, key="tkauto-1")
    _tauto(client, pid, tid, a1["id"], payload, key="tkauto-1")
    pts = json.loads(_track_of(_detail(client, pid, tid), a1["id"])
                     ["automation_json"])
    assert len(pts) == 1


def test_track_automation_stale_revision(client):
    pid, tid, a1 = _setup(client)
    rev = _rev(client, pid)
    _tauto(client, pid, tid, a1["id"],
           [{"at_tl_frames": 0, "gain_db": -6}])
    r = client.post(f"/api/projects/{pid}/commands/commit",
                    json={"command_type": "timeline.track.automation",
                          "payload": {"timeline_id": tid,
                                      "track_id": a1["id"],
                                      "points": [{"at_tl_frames": 1,
                                                  "gain_db": 0}]},
                          "scope": "project",
                          "expected_revision": rev,
                          "idempotency_key": "stale-tkauto-1"})
    assert r.status_code == 409


def test_track_automation_survives_clip_moves(client):
    """The absolute-frame contract: a clip move can't orphan lane
    keyframes — the lane envelope is unchanged."""
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"],
           [{"at_tl_frames": 24, "gain_db": -12}])
    d = _detail(client, pid, tid)
    clip = d["clips"][0]
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": tid, "clip_id": clip["id"],
             "start_frames": 96})
    tr = _track_of(_detail(client, pid, tid), a1["id"])
    assert json.loads(tr["automation_json"])[0]["at_tl_frames"] == 24


def test_moved_clip_reads_lane_curve_at_new_position(client):
    """After a move the lane curve still evaluates at ABSOLUTE time:
    a keyframe ahead of the clip lands mid-stream (offset +48000);
    one behind it precedes stream start (offset −144000)."""
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"], [
        {"at_tl_frames": 24, "gain_db": -12},
        {"at_tl_frames": 120, "gain_db": 0}])
    d = _detail(client, pid, tid)
    clip = d["clips"][0]
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": tid, "clip_id": clip["id"],
             "start_frames": 96})
    from app.domain import timeline_export as tx
    plan = tx.composite_plan(_n3a_edl(client, pid, tid), {})
    a = plan["audio_clips"][0]
    chain = tx._lane_chain(7, a, Fraction(24, 1))
    # tl 24 → (24−96)·2000 = −144000, quantized to −143999 by the
    # half-up _q contract (pre-stream: flat head −12); tl 120 →
    # (120−96)·2000 = +48000 (mid-stream ramp start).
    assert "between(n,-143999,47999)" in chain


def test_track_automation_survives_trim_and_split(client):
    """Trims and splits are clip edits — lane keyframes are a lane
    property and pass through untouched, fragments included."""
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"], [
        {"at_tl_frames": 0, "gain_db": -12},
        {"at_tl_frames": 36, "gain_db": 0}])
    d = _detail(client, pid, tid)
    clip = d["clips"][0]
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": tid, "clip_id": clip["id"],
             "in_frame": 24000})
    _commit(client, pid, "timeline.clip.split",
            {"timeline_id": tid, "clip_id": clip["id"],
             "at_frame": 24})
    d = _detail(client, pid, tid)
    assert len(d["clips"]) == 2
    tr = _track_of(d, a1["id"])
    assert json.loads(tr["automation_json"]) == [
        {"id": "p0", "at_tl_frames": 0, "gain_db": -12},
        {"id": "p1", "at_tl_frames": 36, "gain_db": 0}]
    # EDL still serializes the lane automation for the edited lane
    doc = _n3a_edl(client, pid, tid)
    et = next(x for x in doc["tracks"] if x["id"] == a1["id"])
    assert [p["at_tl_frames"] for p in et["automation"]] == [0, 36]


# ---------- migration (0019_n3a) ----------

def test_migration_0019_preserves_existing_rows(tmp_path):
    """A populated 0018_n2f database upgrades to 0019_n3a keeping
    every project/timeline/track/clip row; the new columns default
    to the neutral envelope ('[]'/'linear')."""
    import sqlite3
    import uuid
    from alembic.config import Config as AlembicConfig
    from alembic import command as alembic_command
    from app.main import (_alembic_script_location,
                          run_migrations)

    url = f"sqlite:///{tmp_path}/n3a.db"
    run_migrations(url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location",
                        str(_alembic_script_location()))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.downgrade(cfg, "0018_n2f")

    db = tmp_path / "n3a.db"
    con = sqlite3.connect(str(db))
    pid, tid, trid, aid, vid, cid = (
        str(uuid.uuid4()) for _ in range(6))
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
        f"('{tid}', '{pid}', 'Cut', '', 24, 1, 'active',"
        f" 1, '[]', '[]', '{now}', '{now}')")
    con.execute(
        "INSERT INTO timeline_tracks (id, project_id, timeline_id,"
        " kind, position, name, visible, locked, muted, gain_db,"
        " solo, created_at) VALUES "
        f"('{trid}', '{pid}', '{tid}', 'audio', 0, 'A1', 1, 0, 0,"
        f" -3.0, 1, '{now}')")
    con.execute(
        "INSERT INTO assets (id, project_id, name, kind, origin,"
        " scope, status, uri, sha256, byte_size, provenance_json,"
        f" current_version_id, created_at) VALUES"
        f" ('{aid}', '{pid}', 'a.wav', 'audio', 'imported',"
        f" 'project', 'ready', 'x', 'sha', 1, '{{}}', '{vid}',"
        f" '{now}')")
    con.execute(
        "INSERT INTO asset_versions (id, asset_id, version_no,"
        f" sha256, media_json, created_at) VALUES ('{vid}',"
        f" '{aid}', 1, 'sha', '{{}}', '{now}')")
    con.execute(
        "INSERT INTO timeline_clips (id, project_id, timeline_id,"
        " track_id, position, start_frames, source_asset_id,"
        " source_version_id, source_sha256, in_frame, out_frame,"
        " src_frames, src_fps_num, src_fps_den, src_duration_s,"
        " shot_id, scene_id, gain_db, fade_in_units, fade_in_curve,"
        " fade_out_units, fade_out_curve, automation_json,"
        " automation_interp, src_has_audio, created_at) VALUES "
        f"('{cid}', '{pid}', '{tid}', '{trid}', 0, 0, '{aid}',"
        f" '{vid}', 'sha', 0, 48, 48, 24, 1, 2.0, NULL, NULL,"
        f" -3.0, 0, 'linear', 0, 'linear', '[]', 'linear', 0,"
        f" '{now}')")
    con.commit()
    con.close()

    run_migrations(url)

    con = sqlite3.connect(str(db))
    ver = con.execute(
        "SELECT version_num FROM alembic_version").fetchone()[0]
    assert ver == "0019_n3a"
    row = con.execute(
        "SELECT kind, name, position, visible, muted, gain_db, solo,"
        f" automation_json, automation_interp FROM timeline_tracks"
        f" WHERE id='{trid}'").fetchone()
    assert row[:7] == ("audio", "A1", 0, 1, 0, -3.0, 1)
    assert row[7:] == ("[]", "linear")   # neutral defaults
    clip = con.execute(
        "SELECT start_frames, in_frame, out_frame, gain_db,"
        f" automation_json FROM timeline_clips WHERE id='{cid}'"
    ).fetchone()
    assert clip == (0, 0, 48, -3.0, "[]")
    assert con.execute(
        f"SELECT COUNT(*) FROM projects WHERE id='{pid}'"
    ).fetchone()[0] == 1
    con.close()


# ---------- evaluator (shared with clip automation) ----------

def test_track_automation_evaluator_math():
    from app.domain.timeline import (
        automation_gain_db, track_automation_gain_db)
    pts = [{"at_tl_frames": 10, "gain_db": -20},
           {"at_tl_frames": 30, "gain_db": 0}]
    # flat outside the ends; linear in dB inside; absolute positions
    assert track_automation_gain_db(pts, 0) == -20
    assert track_automation_gain_db(pts, 10) == -20
    assert track_automation_gain_db(pts, 20) == -10
    assert track_automation_gain_db(pts, 30) == 0
    assert track_automation_gain_db(pts, 99) == 0
    assert track_automation_gain_db(
        pts, 20, interp="step") == -20   # holds the left value
    # same interpolation the clip evaluator implements — one contract
    assert track_automation_gain_db(pts, 20) == automation_gain_db(
        [{"at_frames": 10, "gain_db": -20},
         {"at_frames": 30, "gain_db": 0}], 20)


# ---------- export plan + chain ----------

def _n3a_edl(client, pid, tid):
    return _edl(client, pid, tid, version=2)


def test_export_plan_carries_track_automation(client):
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"], [
        {"at_tl_frames": 12, "gain_db": -12},
        {"at_tl_frames": 36, "gain_db": 0}])
    from app.domain import timeline_export as tx
    doc = _n3a_edl(client, pid, tid)
    plan = tx.composite_plan(doc, {})
    a = plan["audio_clips"][0]
    assert [p["at_tl"] for p in a["track_auto"]] == [
        Fraction(12), Fraction(36)]
    assert a["track_auto_interp"] == "linear"


def test_lane_chain_folds_track_automation(client):
    """The lane curve lands in the SAME single pow(10,x/20) sum as
    clip+track gain — one multiplication, never double-applied."""
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"], [
        {"at_tl_frames": 0, "gain_db": -6},
        {"at_tl_frames": 24, "gain_db": 0}])
    from app.domain import timeline_export as tx
    plan = tx.composite_plan(_n3a_edl(client, pid, tid), {})
    a = plan["audio_clips"][0]
    chain = tx._lane_chain(7, a, Fraction(24, 1))
    assert "aeval=exprs='val(0)" in chain
    # 24 fps → 48000-rate source: absolute tl frame 24 is stream
    # sample 48000 (clip starts at tl 0, no head borrow).
    assert "between(n,0,47999)" in chain
    # ONE envelope expression — the dB sum lives in a single aeval
    # (emitted once per channel: val(0)|val(1)), never a second stage.
    assert chain.count("pow(10,") == 2
    assert "volume=" not in chain


def test_lane_chain_track_auto_offsets_with_head_borrow(client):
    """A head-borrowed crossfade stream starts BEFORE the clip's
    occupancy — track points evaluate at absolute time, so a point at
    the clip's own start lands inside the borrowed head correctly."""
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"], [
        {"at_tl_frames": 0, "gain_db": -6},
        {"at_tl_frames": 24, "gain_db": 0}])
    from app.domain import timeline_export as tx
    plan = tx.composite_plan(_n3a_edl(client, pid, tid), {})
    a = plan["audio_clips"][0]
    # Simulate a satisfied xf borrowing 2 tl frames of head handle —
    # the stream then covers tl [−2, …). Absolute-time anchoring: the
    # point at the clip's occupancy start (tl 0) lands 2 frames INTO
    # the stream (4000 samples @24fps/48k), not at sample 0.
    a["head_tl"] = Fraction(2)
    a["head_src"] = 4000
    chain = tx._lane_chain(7, a, Fraction(24, 1))
    assert "between(n,4000,51999)" in chain


def test_lane_chain_track_auto_only_still_uses_aeval(client):
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"],
           [{"at_tl_frames": 0, "gain_db": -6}])
    from app.domain import timeline_export as tx
    plan = tx.composite_plan(_n3a_edl(client, pid, tid), {})
    a = plan["audio_clips"][0]
    assert a["automation"] == []           # no clip keyframes
    chain = tx._lane_chain(7, a, Fraction(24, 1))
    assert "aeval=exprs=" in chain         # lane curve → aeval, not volume


def test_embedded_chain_folds_track_automation(client):
    """A4 — embedded audio takes the lane envelope at 48 kHz."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    v1 = _detail(client, pid, t["id"])["tracks"][0]
    _tauto(client, pid, t["id"], v1["id"], [
        {"at_tl_frames": 24, "gain_db": 0},
        {"at_tl_frames": 72, "gain_db": -12}], interp="step")
    from app.domain import timeline_export as tx
    doc = _n3a_edl(client, pid, t["id"])
    probes = {doc["clips"][0]["clip_id"]: {"audio": [{"sample_rate": 48000}]}}
    plan = tx.composite_plan(doc, probes)
    a = plan["audio_clips"][0]
    assert a["embedded"] is True
    assert a["track_auto_interp"] == "step"
    chain = tx._embedded_chain(0, a, Fraction(24, 1))
    assert "aeval=exprs=" in chain
    # Video clip at tl 0: keyframes 24f→48000 samples, 72f→144000.
    assert "between(n,48000,143999)" in chain
    assert chain.count("pow(10,") == 2   # one envelope, per channel


def test_embedded_chain_no_automation_keeps_volume(client):
    """Unchanged lanes keep the cheap volume stage — aeval only
    appears when there's an envelope to evaluate."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    v1 = _detail(client, pid, t["id"])["tracks"][0]
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v1["id"],
             "gain_db": -6})
    from app.domain import timeline_export as tx
    doc = _n3a_edl(client, pid, t["id"])
    probes = {doc["clips"][0]["clip_id"]: {"audio": [{"sample_rate": 48000}]}}
    a = tx.composite_plan(doc, probes)["audio_clips"][0]
    chain = tx._embedded_chain(0, a, Fraction(24, 1))
    assert "aeval" not in chain
    assert "volume=" in chain


def test_seg_expr_quantized_collapse():
    """Two keyframes that quantize onto the same sample collapse to a
    deterministic hold — the left value wins, never a zero-div ramp."""
    from app.domain.timeline_export import _seg_expr
    expr = _seg_expr([100, 100], [-6.0, 0.0], False)
    assert "eq(n,100)" in expr
    assert "(n-100)" not in expr  # no div-by-zero ramp


def test_track_automation_audit_label(client):
    pid, tid, a1 = _setup(client)
    r = _tauto(client, pid, tid, a1["id"],
               [{"at_tl_frames": 0, "gain_db": -6}])
    d = _detail(client, pid, tid)
    assert d["undo_label"] == "Set track automation"
    assert r.get("ok", True)


def test_undo_across_pre0019_boundary_clears_automation(client):
    """Snapshots written before 0019 carry no automation fields on the
    track dict. Restoring one must reset the lane to the column
    defaults — keep-if-absent would leak post-edit automation through
    an undo that crossed the migration boundary."""
    pid, tid, a1 = _setup(client)
    _tauto(client, pid, tid, a1["id"],
           [{"at_tl_frames": 5, "gain_db": -12}], interp="step")
    engine = client.app.state.engine
    from sqlalchemy.orm import Session
    from app.db.models import EntityRevision, Timeline, new_id
    from app.domain.timeline import timeline_snapshot
    with Session(engine) as s:
        tl = s.get(Timeline, tid)
        snap = timeline_snapshot(s, tl)
        for st in snap["tracks"]:
            st.pop("automation_json", None)
            st.pop("automation_interp", None)
        s.add(EntityRevision(id=new_id(), project_id=pid,
                             entity_type="timeline", entity_id=tid,
                             revision=999, snapshot_json=snap,
                             summary="pre-0019"))
        tl.undo_stack_json = [{"rev": 999, "label": "pre-0019"}]
        s.commit()
    _commit(client, pid, "timeline.undo", {"timeline_id": tid})
    tr = _track_of(_detail(client, pid, tid), a1["id"])
    assert json.loads(tr["automation_json"]) == []
    assert tr["automation_interp"] == "linear"
