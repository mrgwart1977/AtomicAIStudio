"""N1-A — multitrack data foundation: tracks, frame-accurate clip
placement, ade-edl/2, v1 collapse-or-refuse, migration."""
import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_c5a_timeline import _video, _tl, _insert, _detail


def _track(client, pid, tid, kind="video", name="V2"):
    r = _commit(client, pid, "timeline.track.add",
                {"timeline_id": tid, "kind": kind, "name": name})
    d = _detail(client, pid, tid)
    return next(t for t in d["tracks"] if t["name"] == name)


def _edl(client, pid, tid, version=1, expect=200):
    r = client.get(
        f"/api/projects/{pid}/timelines/{tid}/edl?version={version}")
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


# ---------- schema + default lane ----------

def test_create_timeline_has_default_v1_track(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    d = _detail(client, pid, t["id"])
    assert len(d["tracks"]) == 1
    tr = d["tracks"][0]
    assert tr["kind"] == "video" and tr["name"] == "V1"
    assert tr["position"] == 0 and tr["visible"] and not tr["locked"]


def test_legacy_inserts_land_on_default_track(client):
    """C5-A callers never name a track — their clips must still land
    on V1 packed sequentially."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    _insert(client, pid, t["id"], _video(client, pid, "b.mp4", sha="b" * 64))
    d = _detail(client, pid, t["id"])
    v1 = d["tracks"][0]["id"]
    starts = [(c["track_id"], c["start_frames"]) for c in d["clips"]]
    assert starts == [(v1, 0), (v1, 72)]


def test_track_crud_via_commands(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    tr = _track(client, pid, t["id"], name="V2")
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": tr["id"],
             "name": "Overlays", "locked": True})
    d = _detail(client, pid, t["id"])
    tr = next(x for x in d["tracks"] if x["id"] == tr["id"])
    assert tr["name"] == "Overlays" and tr["locked"]
    _commit(client, pid, "timeline.track.reorder",
            {"timeline_id": t["id"], "track_id": tr["id"],
             "to_position": 0})
    d = _detail(client, pid, t["id"])
    assert [x["position"] for x in
            sorted(d["tracks"], key=lambda x: x["position"])] == [0, 1]
    _commit(client, pid, "timeline.track.remove",
            {"timeline_id": t["id"], "track_id": tr["id"]})
    d = _detail(client, pid, t["id"])
    assert len(d["tracks"]) == 1


def test_track_remove_requires_empty_and_last_guard(client):
    """Removing V1 while clips sit on it must refuse; the last video
    track on a timeline with clips can't silently orphan them."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    v1 = _detail(client, pid, t["id"])["tracks"][0]["id"]
    r = _commit(client, pid, "timeline.track.remove",
                {"timeline_id": t["id"], "track_id": v1}, expect=422)
    assert "empty" in r.text or "clip" in r.text


def test_track_cross_timeline_isolation(client):
    """A track from timeline A cannot be named in timeline B's
    commands — even within one project."""
    pid = make_project(client)["id"]
    ta, tb = _tl(client, pid, "A"), _tl(client, pid, "B")
    v1a = _detail(client, pid, ta["id"])["tracks"][0]["id"]
    r = _commit(client, pid, "timeline.track.update",
                {"timeline_id": tb["id"], "track_id": v1a,
                 "name": "hijack"}, expect=404)
    assert "not found" in r.text
    v = _video(client, pid)
    r = _insert(client, pid, tb["id"], v, track_id=v1a, expect=404)
    assert "not found" in r.text


def test_track_cross_project_isolation(client):
    pid = make_project(client)["id"]
    pid2 = make_project(client, "Other")["id"]
    t2 = _tl(client, pid2, "Other TL")
    v1_other = _detail(client, pid2, t2["id"])["tracks"][0]["id"]
    t = _tl(client, pid)
    r = _commit(client, pid, "timeline.track.update",
                {"timeline_id": t["id"], "track_id": v1_other,
                 "name": "hijack"}, expect=404)


# ---------- placement ----------

def test_explicit_place_with_gap(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "asset_id":
             _video(client, pid, "b.mp4", sha="b" * 64)["id"],
             "start_frames": 200})
    d = _detail(client, pid, t["id"])
    starts = sorted(c["start_frames"] for c in d["clips"])
    assert starts == [0, 200]  # gap 72..200 is legal


def test_overlap_refused_on_same_track(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    r = _commit(client, pid, "timeline.clip.place",
                {"timeline_id": t["id"], "asset_id":
                 _video(client, pid, "b.mp4", sha="b" * 64)["id"],
                 "start_frames": 10}, expect=422)
    assert "overlap" in r.text


def test_place_on_second_track_avoids_overlap(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    v2 = _track(client, pid, t["id"], name="V2")
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "asset_id":
             _video(client, pid, "b.mp4", sha="b" * 64)["id"],
             "track_id": v2["id"], "start_frames": 10})
    d = _detail(client, pid, t["id"])
    by_track = {}
    for c in d["clips"]:
        by_track.setdefault(c["track_id"], []).append(c["start_frames"])
    assert by_track[v2["id"]] == [10]


def test_move_to_track_and_frame(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    v2 = _track(client, pid, t["id"], name="V2")
    cid = _detail(client, pid, t["id"])["clips"][0]["id"]
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"], "clip_id": cid,
             "to_track_id": v2["id"], "start_frames": 40})
    c = _detail(client, pid, t["id"])["clips"][0]
    assert c["track_id"] == v2["id"] and c["start_frames"] == 40


def test_move_explicit_into_overlap_refused(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    _insert(client, pid, t["id"], _video(client, pid, "b.mp4", sha="b" * 64))
    c2 = _detail(client, pid, t["id"])["clips"][1]["id"]
    r = _commit(client, pid, "timeline.clip.move",
                {"timeline_id": t["id"], "clip_id": c2,
                 "start_frames": 10}, expect=422)
    assert "overlap" in r.text


def test_positional_insert_refused_on_gapped_lane(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "asset_id":
             _video(client, pid, "b.mp4", sha="b" * 64)["id"],
             "start_frames": 200})
    r = _insert(client, pid, t["id"],
                _video(client, pid, "c.mp4", sha="c" * 64),
                position=1, expect=422)
    assert "contiguous" in r.text


def test_rational_extent_packs_by_ceiling(client):
    """Mixed-rate append: 60f@30000/1001 on a 24/1 grid has extent
    48.048 — the next clip packs at ceil(96.048)=97, never 96."""
    pid = make_project(client)["id"]
    t = _tl(client, pid, fps_num=24, fps_den=1)
    _insert(client, pid, t["id"], _video(client, pid, "a.mp4",
                                        sha="aa" * 32, frames=48,
                                        fps=24.0, duration=2.0))
    _insert(client, pid, t["id"], _video(client, pid, "b.mp4",
                                        sha="ab" * 32, frames=60,
                                        fps=29.97, duration=2.002))
    _insert(client, pid, t["id"], _video(client, pid, "c.mp4",
                                        sha="ac" * 32, frames=48,
                                        fps=24.0, duration=2.0))
    starts = [c["start_frames"]
              for c in _detail(client, pid, t["id"])["clips"]]
    assert starts == [0, 48, 97]


# ---------- locking / visibility ----------

def test_locked_track_blocks_clip_ops(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    v1 = _detail(client, pid, t["id"])["tracks"][0]["id"]
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v1, "locked": True})
    cid = _detail(client, pid, t["id"])["clips"][0]["id"]
    r = _insert(client, pid, t["id"],
                _video(client, pid, "b.mp4", sha="b" * 64), expect=422)
    assert "locked" in r.text
    for ct, extra in (("timeline.clip.trim", {"in_frame": 4}),
                      ("timeline.clip.move", {"to_index": 0}),
                      ("timeline.clip.remove", {})):
        r = _commit(client, pid, ct,
                    {"timeline_id": t["id"], "clip_id": cid, **extra},
                    expect=422)
        assert "locked" in r.text, ct


def test_hidden_track_keeps_its_clips(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    v1 = _detail(client, pid, t["id"])["tracks"][0]["id"]
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v1, "visible": False})
    d = _detail(client, pid, t["id"])
    assert len(d["clips"]) == 1 and d["clips"][0]["track_id"] == v1


# ---------- EDL v1 / v2 ----------

def test_edl_v1_still_works_for_sequential(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    _insert(client, pid, t["id"], _video(client, pid, "b.mp4", sha="b" * 64))
    e = _edl(client, pid, t["id"], version=1)
    assert e["format"] == "ade-edl/1" and len(e["clips"]) == 2


def test_edl_v1_refuses_gap(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "asset_id":
             _video(client, pid, "b.mp4", sha="b" * 64)["id"],
             "start_frames": 200})
    r = _edl(client, pid, t["id"], version=1, expect=409)
    assert "contiguous" in r.text or "gap" in r.text


def test_edl_v1_refuses_multitrack(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    v2 = _track(client, pid, t["id"], name="V2")
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "asset_id":
             _video(client, pid, "b.mp4", sha="b" * 64)["id"],
             "track_id": v2["id"], "start_frames": 0})
    r = _edl(client, pid, t["id"], version=1, expect=409)
    assert "track" in r.text.lower()


def test_edl_v1_refuses_hidden_populated_track(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    v1 = _detail(client, pid, t["id"])["tracks"][0]["id"]
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v1, "visible": False})
    _edl(client, pid, t["id"], version=1, expect=409)


def test_edl_v2_deterministic_and_complete(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v = _video(client, pid)
    _insert(client, pid, t["id"], v)
    v2 = _track(client, pid, t["id"], name="V2")
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "asset_id":
             _video(client, pid, "b.mp4", sha="b" * 64)["id"],
             "track_id": v2["id"], "start_frames": 24})
    e1 = _edl(client, pid, t["id"], version=2)
    e2 = _edl(client, pid, t["id"], version=2)
    assert e1 == e2 and e1["format"] == "ade-edl/2"
    assert e1["timebase"] == {"num": 24, "den": 1}
    assert {tr["name"] for tr in e1["tracks"]} == {"V1", "V2"}
    c1 = next(c for c in e1["clips"] if c["track_id"] == v2["id"])
    assert c1["start_frames"] == 24
    assert c1["in_frame"] == 0 and c1["out_frame"] == 72
    assert c1["sha256"] == "b" * 64
    assert e1["revision"] == _detail(
        client, pid, t["id"])["revision"]


def test_export_refuses_unsupported_layout(client):
    """timeline.export must fail closed on a gapped timeline — no
    misleading output, no silent flatten."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "asset_id":
             _video(client, pid, "b.mp4", sha="b" * 64)["id"],
             "start_frames": 200})
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"]}, expect=422)
    assert "ade-edl" in r.text or "contiguous" in r.text


# ---------- concurrency / revision safety ----------

def test_stale_expected_timeline_refused_on_track_op(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    p = _preview(client, pid, "timeline.track.add",
                 {"timeline_id": t["id"], "kind": "video", "name": "V2"})
    exp = p["resolved_timeline"]
    _insert(client, pid, t["id"], _video(client, pid))  # bump revision
    r = _commit(client, pid, "timeline.track.add",
                {"timeline_id": t["id"], "kind": "video",
                 "name": "V2", "expected_timeline": exp}, expect=422)
    assert "re-preview" in r.text


def test_track_op_bumps_revision_and_is_audited(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    r0 = _detail(client, pid, t["id"])["revision"]
    _track(client, pid, t["id"], name="V2")
    d = _detail(client, pid, t["id"])
    assert d["revision"] == r0 + 1
    revs = client.get(
        f"/api/projects/{pid}/timelines/{t['id']}/revisions").json()
    assert revs[0]["revision"] == r0 + 1  # newest first


def test_revert_restores_tracked_layout(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    r1 = _detail(client, pid, t["id"])["revision"]
    v2 = _track(client, pid, t["id"], name="V2")
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "asset_id":
             _video(client, pid, "b.mp4", sha="b" * 64)["id"],
             "track_id": v2["id"], "start_frames": 24})
    _commit(client, pid, "timeline.revert",
            {"timeline_id": t["id"], "to_revision": r1})
    d = _detail(client, pid, t["id"])
    assert len(d["tracks"]) == 1 and len(d["clips"]) == 1
    assert d["clips"][0]["track_id"] == d["tracks"][0]["id"]


def test_idempotent_replay_track_add(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    payload = {"timeline_id": t["id"], "kind": "video", "name": "V2"}
    _commit(client, pid, "timeline.track.add", payload, key="n1a-idem")
    again = _commit(client, pid, "timeline.track.add", payload,
                    key="n1a-idem")
    assert again.get("idempotent_replay") is True
    d = _detail(client, pid, t["id"])
    assert len([tr for tr in d["tracks"] if tr["name"] == "V2"]) == 1


# ---------- migration ----------

def _pre_n1_db(tmp_path, name):
    """A genuinely pre-N1 database: migrate to head, then downgrade to
    0012_c5a — migration 0001 creates tables from live metadata, so an
    upgrade-stop can't produce the old shape; the N1-A downgrade can.
    Returns (url, cfg) stamped 0012_c5a."""
    from alembic.config import Config as AlembicConfig
    from alembic import command as alembic_command
    from app.main import _alembic_script_location, run_migrations

    url = f"sqlite:///{tmp_path}/{name}.db"
    run_migrations(url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location",
                      str(_alembic_script_location()))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.downgrade(cfg, "0012_c5a")
    return url, cfg


def test_migration_backfill_realistic_pre_n1(tmp_path):
    """Pre-N1 DB with real sequential clips upgrades cleanly: clips
    join V1 with exact packed starts, trims and ids preserved."""
    import sqlite3
    import uuid
    from app.main import run_migrations

    url, _cfg = _pre_n1_db(tmp_path, "pre_n1")
    db = tmp_path / "pre_n1.db"
    con = sqlite3.connect(str(db))
    pid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    now = "2024-01-01 00:00:00"
    con.execute(
        "INSERT INTO projects (id, name, template, kind, status,"
        " aspect_ratio, width, height, fps_num, fps_den, style,"
        " settings_json, revision, created_at, updated_at) VALUES "
        f"('{pid}', 'Legacy', 'blank', 'film', 'active', '16:9',"
        f" 1920, 1080, 24, 1, '{{}}', '{{}}', 0, '{now}', '{now}')")
    con.execute(
        "INSERT INTO timelines (id, project_id, name, description,"
        " fps_num, fps_den, status, revision, created_at, updated_at)"
        f" VALUES ('{tid}', '{pid}', 'Old Cut', '', 24, 1, 'active',"
        f" 2, '{now}', '{now}')")
    # three sequential legacy clips: 48f@24, 60f@30000/1001, 48f@24 —
    # the third exercises ceil-packing (exact boundary 96.048 → 97)
    clip_ids = []
    for i, (in_f, out_f, fsn, fsd) in enumerate(
            ((0, 48, 24, 1), (0, 60, 30000, 1001), (0, 48, 24, 1))):
        cid, aid, vid = (str(uuid.uuid4()) for _ in range(3))
        clip_ids.append(cid)
        con.execute(
            "INSERT INTO assets (id, project_id, name, kind, origin,"
            " scope, status, uri, sha256, byte_size, provenance_json,"
            f" current_version_id, created_at) VALUES"
            f" ('{aid}', '{pid}', 'c{i}.mp4', 'video', 'generated',"
            f" 'project', 'ready', 'x', 'sha{i}', 1, '{{}}', '{vid}',"
            f" '{now}')")
        con.execute(
            "INSERT INTO asset_versions (id, asset_id, version_no,"
            f" sha256, media_json, created_at) VALUES ('{vid}',"
            f" '{aid}', 1, 'sha{i}', '{{}}', '{now}')")
        con.execute(
            "INSERT INTO timeline_clips (id, project_id, timeline_id,"
            " position, source_asset_id, source_version_id,"
            " source_sha256, in_frame, out_frame, src_frames,"
            " src_fps_num, src_fps_den, src_duration_s, shot_id,"
            " scene_id, created_at) VALUES "
            f"('{cid}', '{pid}', '{tid}', {i}, '{aid}', '{vid}',"
            f" 'sha{i}', {in_f}, {out_f}, {out_f}, {fsn}, {fsd}, 2.0,"
            f" NULL, NULL, '{now}')")
    con.commit()
    con.close()

    run_migrations(url)

    con = sqlite3.connect(str(db))
    tracks = con.execute(
        "SELECT id, kind, name, position FROM timeline_tracks WHERE"
        f" timeline_id='{tid}'").fetchall()
    assert len(tracks) == 1
    assert tracks[0][1:] == ("video", "V1", 0)
    clips = con.execute(
        "SELECT id, position, start_frames, track_id, in_frame,"
        f" out_frame FROM timeline_clips WHERE timeline_id='{tid}'"
        " ORDER BY position").fetchall()
    assert [c[0] for c in clips] == clip_ids  # stable ids
    assert clips[0][2] == 0 and clips[0][4:] == (0, 48)
    assert clips[1][2] == 48 and clips[1][4:] == (0, 60)
    # third packs at ceil(48 + 48.048 = 96.048) = 97, never overlapping
    assert clips[2][2] == 97 and clips[2][4:] == (0, 48)
    assert clips[0][3] == clips[1][3] == clips[2][3] == tracks[0][0]
    con.close()


def test_migration_idempotent_backfill(tmp_path):
    """Re-running the upgrade's backfill must not duplicate tracks or
    disturb converted clips."""
    import sqlite3
    import uuid
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.db.session import create_engine_for
    from app.main import run_migrations

    url, _cfg = _pre_n1_db(tmp_path, "idem")
    con = sqlite3.connect(str(tmp_path / "idem.db"))
    pid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    now = "2024-01-01 00:00:00"
    con.execute(
        "INSERT INTO projects (id, name, template, kind, status,"
        " aspect_ratio, width, height, fps_num, fps_den, style,"
        " settings_json, revision, created_at, updated_at) VALUES "
        f"('{pid}', 'L', 'blank', 'film', 'active', '16:9', 1920,"
        f" 1080, 24, 1, '{{}}', '{{}}', 0, '{now}', '{now}')")
    con.execute(
        "INSERT INTO timelines (id, project_id, name, description,"
        " fps_num, fps_den, status, revision, created_at, updated_at)"
        f" VALUES ('{tid}', '{pid}', 'T', '', 24, 1, 'active', 0,"
        f" '{now}', '{now}')")
    cid, aid, vid = (str(uuid.uuid4()) for _ in range(3))
    con.execute(
        "INSERT INTO assets (id, project_id, name, kind, origin,"
        " scope, status, uri, sha256, byte_size, provenance_json,"
        f" created_at) VALUES ('{aid}', '{pid}', 'c.mp4', 'video',"
        f" 'generated', 'project', 'ready', 'x', 's', 1, '{{}}',"
        f" '{now}')")
    con.execute(
        "INSERT INTO asset_versions (id, asset_id, version_no, sha256,"
        f" media_json, created_at) VALUES ('{vid}', '{aid}', 1, 's',"
        f" '{{}}', '{now}')")
    con.execute(
        "INSERT INTO timeline_clips (id, project_id, timeline_id,"
        " position, source_asset_id, source_version_id, source_sha256,"
        " in_frame, out_frame, src_frames, src_fps_num, src_fps_den,"
        " src_duration_s, created_at) VALUES "
        f"('{cid}', '{pid}', '{tid}', 0, '{aid}', '{vid}', 's', 0, 48,"
        f" 48, 24, 1, 2.0, '{now}')")
    con.commit()
    con.close()

    run_migrations(url)

    # Re-run the migration's own _backfill under an operations
    # context — the same call upgrade() makes, a second time.
    import importlib.util
    from pathlib import Path
    mig_path = (Path(__file__).parent.parent / "app" / "db" / "alembic"
                / "versions" / "0013_n1a_tracks.py")
    spec = importlib.util.spec_from_file_location("mig0013", mig_path)
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    e = create_engine_for(url)
    with e.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            mig._backfill()
    e.dispose()

    con = sqlite3.connect(str(tmp_path / "idem.db"))
    assert con.execute(
        f"SELECT COUNT(*) FROM timeline_tracks WHERE timeline_id='{tid}'"
    ).fetchone()[0] == 1
    row = con.execute(
        "SELECT track_id, start_frames FROM timeline_clips WHERE id=?"
        , (cid,)).fetchone()
    assert row == (con.execute(
        "SELECT id FROM timeline_tracks WHERE timeline_id=?"
        , (tid,)).fetchone()[0], 0)
    con.close()


def test_fresh_and_upgraded_schemas_equivalent(tmp_path):
    """A fresh migrate-all and a pre-N1-then-upgrade DB expose the
    same timeline_tracks/clip columns."""
    from sqlalchemy import inspect
    from app.db.session import create_engine_for
    from app.main import run_migrations

    url1 = f"sqlite:///{tmp_path}/fresh.db"
    run_migrations(url1)
    url2, _cfg = _pre_n1_db(tmp_path, "upgraded")
    run_migrations(url2)

    def cols(url, table):
        e = create_engine_for(url)
        c = {x["name"] for x in inspect(e).get_columns(table)}
        e.dispose()
        return c

    assert (cols(url1, "timeline_tracks")
            == cols(url2, "timeline_tracks"))
    for col in ("track_id", "start_frames"):
        assert col in cols(url1, "timeline_clips")
        assert col in cols(url2, "timeline_clips")
