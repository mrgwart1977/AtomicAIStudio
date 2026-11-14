"""C5-A — timeline assembly foundation tests."""
import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _board, _commit, _preview, _rev


def _video(client, pid, name="clip.mp4", *, sha=None, frames=72, fps=24.0,
           duration=3.0, status="active", probe_ok=True):
    """Synthetic video asset: AssetFile → Asset → AssetVersion(probe)."""
    from app.db.models import (Asset, AssetFile, AssetVersion,
                               StorageLocation, new_id)
    sha = sha or (name.encode().hex() * 8)[:64]
    with client.app.state.session_factory() as s:
        loc = s.query(StorageLocation).first()
        s.add(AssetFile(sha256=sha,
                        storage_location_id=loc.id if loc else None,
                        rel_path=f"blobs/{sha[:2]}/v", byte_size=100,
                        status="ready"))
        s.flush()
        a = Asset(id=new_id(), project_id=pid, name=name, kind="video",
                  origin="generated", uri=f"blobs/{sha[:2]}/v",
                  sha256=sha, byte_size=100, status=status)
        s.add(a)
        s.flush()
        probe = {"ok": probe_ok, "duration_s": duration, "fps": fps,
                 "frames": frames, "width": 832, "height": 480,
                 "codec": "h264"}
        v = AssetVersion(id=new_id(), asset_id=a.id, version_no=1,
                         sha256=sha, media_json={"probe": probe})
        s.add(v)
        s.flush()
        a.current_version_id = v.id
        s.commit()
        return {"id": a.id, "version_id": v.id, "sha256": sha,
                "name": name}


def _tl(client, pid, name="Cut 1", **kw):
    _commit(client, pid, "timeline.create", {"name": name, **kw})
    tls = client.get(f"/api/projects/{pid}/timelines").json()
    return next(t for t in tls if t["name"] == name)


def _detail(client, pid, tid):
    return client.get(f"/api/projects/{pid}/timelines/{tid}").json()


def _insert(client, pid, tid, asset, expect=200, **kw):
    return _commit(client, pid, "timeline.clip.insert",
                   {"timeline_id": tid, "asset_id": asset["id"], **kw},
                   expect=expect)


def test_timeline_create_and_detail(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid, "Cut 1", fps_num=24, fps_den=1)
    assert t["fps_num"] == 24 and t["revision"] == 0
    d = _detail(client, pid, t["id"])
    assert d["clips"] == [] and d["total_duration_s"] == 0


def test_timeline_create_validation(client):
    pid = make_project(client)["id"]
    _commit(client, pid, "timeline.create", {"name": "  "}, expect=422)
    _commit(client, pid, "timeline.create",
            {"name": "x", "fps_num": 0}, expect=422)
    _commit(client, pid, "timeline.create",
            {"name": "x", "fps_num": 999}, expect=422)


def test_clip_insert_full_source_defaults(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v = _video(client, pid)
    r = _insert(client, pid, t["id"], v)
    d = _detail(client, pid, t["id"])
    assert len(d["clips"]) == 1
    c = d["clips"][0]
    assert c["in_frame"] == 0 and c["out_frame"] == 72
    assert c["src_frames"] == 72 and c["source_sha256"] == v["sha256"]
    assert c["source_version_id"] == v["version_id"]
    assert d["total_duration_s"] == pytest.approx(3.0)


def test_clip_insert_requires_video_active_project_owned(client, tmp_path):
    pid = make_project(client)["id"]
    pid2 = make_project(client, "Other")["id"]
    t = _tl(client, pid)
    # cross-project asset → 404
    v2 = _video(client, pid2, "other.mp4", sha="e" * 64)
    r = _insert(client, pid, t["id"], v2, expect=404)
    assert "not found in this project" in r.text
    # image asset → 422
    from tests.test_c4b_shot_generate import _import
    img = _import(client, pid, "i.png", tmp_path, (1, 2, 3))
    r = _insert(client, pid, t["id"], {"id": img["id"]}, expect=422)
    assert "video asset" in r.text
    # archived source → 422
    va = _video(client, pid, "old.mp4", sha="d" * 64, status="archived")
    r = _insert(client, pid, t["id"], va, expect=422)
    assert "archived" in r.text


def test_clip_insert_requires_measured_duration(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    bad = _video(client, pid, "noprobe.mp4", sha="c" * 64, probe_ok=False)
    r = _insert(client, pid, t["id"], bad, expect=422)
    assert "no measured duration" in r.text


def test_clip_trim_validation(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v = _video(client, pid)
    cid = _detail(client, pid, t["id"])["clips"]
    _insert(client, pid, t["id"], v)
    cid = _detail(client, pid, t["id"])["clips"][0]["id"]
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": cid,
             "in_frame": 10, "out_frame": 50})
    c = _detail(client, pid, t["id"])["clips"][0]
    assert c["in_frame"] == 10 and c["out_frame"] == 50
    # out of bounds / inverted / non-integer all refuse
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": cid,
             "out_frame": 73}, expect=422)
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": cid,
             "in_frame": 50, "out_frame": 40}, expect=422)
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": cid,
             "in_frame": 1.5}, expect=422)
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": cid,
             "in_frame": -1}, expect=422)


def test_clip_move_and_remove_renumber(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    for i in range(3):
        _insert(client, pid, t["id"],
                _video(client, pid, f"c{i}.mp4", sha=f"{i}" * 64))
    clips = _detail(client, pid, t["id"])["clips"]
    assert [c["position"] for c in clips] == [0, 1, 2]
    # move last → first
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"], "clip_id": clips[2]["id"],
             "to_index": 0})
    clips = _detail(client, pid, t["id"])["clips"]
    assert [c["position"] for c in clips] == [0, 1, 2]
    assert clips[0]["source_sha256"] == "2" * 64
    # move out of range refuses
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"], "clip_id": clips[0]["id"],
             "to_index": 9}, expect=422)
    # remove middle → renumber
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": clips[1]["id"]})
    clips = _detail(client, pid, t["id"])["clips"]
    assert len(clips) == 2
    assert [c["position"] for c in clips] == [0, 1]


def test_duplicate_source_controlled(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v = _video(client, pid)
    _insert(client, pid, t["id"], v)
    r = _insert(client, pid, t["id"], v, expect=422)
    assert "already on the timeline" in r.text
    _insert(client, pid, t["id"], v, allow_duplicate=True)
    assert len(_detail(client, pid, t["id"])["clips"]) == 2


def test_version_pinning_stable_across_new_versions(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v = _video(client, pid)
    _insert(client, pid, t["id"], v)
    # asset gets a NEW version — clip keeps the pinned one
    from app.db.models import (Asset, AssetFile, AssetVersion,
                               StorageLocation, new_id)
    with client.app.state.session_factory() as s:
        loc = s.query(StorageLocation).first()
        s.add(AssetFile(sha256="b" * 64,
                        storage_location_id=loc.id if loc else None,
                        rel_path="blobs/bb/v2", byte_size=200,
                        status="ready"))
        s.flush()
        a = s.get(Asset, v["id"])
        nv = AssetVersion(id=new_id(), asset_id=a.id, version_no=2,
                          sha256="b" * 64,
                          media_json={"probe": {"ok": True,
                                                "duration_s": 9.0,
                                                "fps": 24, "frames": 216}})
        s.add(nv)
        s.flush()
        a.current_version_id = nv.id
        s.commit()
    c = _detail(client, pid, t["id"])["clips"][0]
    assert c["source_version_id"] == v["version_id"]
    assert c["source_sha256"] == v["sha256"]
    assert c["src_frames"] == 72  # pinned geometry, not 216


def test_archived_timeline_refuses_clip_ops(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _commit(client, pid, "timeline.archive", {"timeline_id": t["id"]})
    r = _insert(client, pid, t["id"], _video(client, pid), expect=422)
    assert "archived" in r.text
    _commit(client, pid, "timeline.unarchive", {"timeline_id": t["id"]})
    _insert(client, pid, t["id"], _video(client, pid, "b.mp4", sha="9" * 64))


def test_timeline_revert_restores_clips(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    rev_with_clip = _detail(client, pid, t["id"])["revision"]
    cid = _detail(client, pid, t["id"])["clips"][0]["id"]
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": cid})
    assert _detail(client, pid, t["id"])["clips"] == []
    _commit(client, pid, "timeline.revert",
            {"timeline_id": t["id"], "to_revision": rev_with_clip})
    clips = _detail(client, pid, t["id"])["clips"]
    assert len(clips) == 1 and clips[0]["out_frame"] == 72
    # history endpoint shows the trail
    revs = client.get(
        f"/api/projects/{pid}/timelines/{t['id']}/revisions").json()
    assert revs[0]["summary"].startswith("Reverted")
    _commit(client, pid, "timeline.revert",
            {"timeline_id": t["id"], "to_revision": 999}, expect=404)


def test_expected_timeline_stale_approval(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v = _video(client, pid)
    p = _preview(client, pid, "timeline.clip.insert",
                 {"timeline_id": t["id"], "asset_id": v["id"]})
    assert p["resolved_timeline"]["revision"] == t["revision"]
    # first insert bumps revision → the stale expected refuses
    _insert(client, pid, t["id"], _video(client, pid, "x.mp4", sha="8" * 64))
    r = _commit(client, pid, "timeline.clip.insert",
                {"timeline_id": t["id"], "asset_id": v["id"],
                 "expected_timeline": p["resolved_timeline"]},
                expect=422)
    assert "changed since preview" in r.text


def test_shot_scene_provenance(client, tmp_path):
    from tests.test_c4b_shot_generate import _scene, _shot
    pid = make_project(client)["id"]
    sc = _scene(client, pid, "S1")["scene"]
    sh = _shot(client, pid, sc["id"], "wide")["shot"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid),
            shot_id=sh["id"], scene_id=sc["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    assert c["shot_id"] == sh["id"] and c["scene_id"] == sc["id"]
    # bogus provenance refuses
    r = _insert(client, pid, t["id"],
                _video(client, pid, "p.mp4", sha="7" * 64),
                shot_id="nope", expect=422)
    assert "shot not found" in r.text


def test_edl_serialization_deterministic(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v = _video(client, pid)
    _insert(client, pid, t["id"], v, in_frame=5, out_frame=60,
            shot_id=None)
    e1 = client.get(f"/api/projects/{pid}/timelines/{t['id']}/edl").json()
    e2 = client.get(f"/api/projects/{pid}/timelines/{t['id']}/edl").json()
    assert e1 == e2
    assert e1["format"] == "ade-edl/1"
    assert e1["timebase"] == {"num": 24, "den": 1}
    c = e1["clips"][0]
    assert c["sha256"] == v["sha256"] and c["version_id"] == v["version_id"]
    assert c["in_frame"] == 5 and c["out_frame"] == 60
    assert c["source"]["fps_num"] == 24 and c["source"]["codec"] == "h264"
    assert e1["total_duration_s"] == pytest.approx(55 / 24, abs=1e-4)


def test_idempotent_replay(client):
    pid = make_project(client)["id"]
    kw = {"name": "Idem"}
    _commit(client, pid, "timeline.create", kw, key="tl-idem-1")
    r = _commit(client, pid, "timeline.create", kw, key="tl-idem-1")
    assert r.get("idempotent_replay") is True
    assert len(client.get(f"/api/projects/{pid}/timelines").json()) == 1


def test_timeline_isolation_cross_project(client):
    pid = make_project(client)["id"]
    pid2 = make_project(client, "Other")["id"]
    t = _tl(client, pid)
    r = client.get(f"/api/projects/{pid2}/timelines/{t['id']}")
    assert r.status_code == 404
    _commit(client, pid2, "timeline.update",
            {"timeline_id": t["id"], "name": "hijack"}, expect=404)
