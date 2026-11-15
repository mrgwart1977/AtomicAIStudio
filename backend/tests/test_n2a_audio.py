"""N2-A — audio editing foundation: audio-kind assets probe into CAS
with stream metadata; audio clips pin source SAMPLES (src_fps_* =
sample rate, in/out = sample indices); the lane↔asset kind rule is
enforced at place/move; EDL v2 carries stream identity; visible
populated audio lanes refuse export until N2-E."""
import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _rev
from tests.test_c5a_timeline import _video, _tl, _detail
from tests.test_n1a_tracks import _track, _edl


def _audio(client, pid, name="clip.wav", *, sha=None, rate=48000,
           channels=2, duration=2.0, samples=None, probe_ok=True,
           status="ready"):
    """Synthetic audio asset: AssetFile → Asset → AssetVersion(probe)."""
    from app.db.models import (Asset, AssetFile, AssetVersion,
                               StorageLocation, new_id)
    sha = sha or (name.encode().hex() * 8)[:64]
    samples = samples if samples is not None else int(duration * rate)
    with client.app.state.session_factory() as s:
        loc = s.query(StorageLocation).first()
        s.add(AssetFile(sha256=sha,
                        storage_location_id=loc.id if loc else None,
                        rel_path=f"blobs/{sha[:2]}/a", byte_size=50,
                        status="ready"))
        s.flush()
        a = Asset(id=new_id(), project_id=pid, name=name, kind="audio",
                  origin="imported", uri=f"blobs/{sha[:2]}/a",
                  sha256=sha, byte_size=50, status=status)
        s.add(a)
        s.flush()
        probe = {"ok": probe_ok, "kind": "audio", "codec": "pcm_s16le",
                 "container": "wav", "sample_rate": rate,
                 "channels": channels, "channel_layout": "stereo",
                 "duration_s": duration, "samples": samples}
        v = AssetVersion(id=new_id(), asset_id=a.id, version_no=1,
                         sha256=sha, media_json={"probe": probe})
        s.add(v)
        s.flush()
        a.current_version_id = v.id
        s.commit()
        return {"id": a.id, "version_id": v.id, "sha256": sha}


def _atrack(client, pid, tid, name="A1"):
    return _track(client, pid, tid, kind="audio", name=name)


def _place(client, pid, tid, asset, track_id, at=0, expect=200,
           key=None, **kw):
    return _commit(client, pid, "timeline.clip.place",
                   {"timeline_id": tid, "asset_id": asset["id"],
                    "track_id": track_id, "start_frames": at, **kw},
                   expect=expect, key=key)


# ---------- placement + geometry ----------

def test_audio_clip_places_with_sample_geometry(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid, rate=48000, duration=2.0)
    _place(client, pid, t["id"], au, a1["id"], at=10)
    d = _detail(client, pid, t["id"])
    c = d["clips"][0]
    assert c["track_id"] == a1["id"] and c["start_frames"] == 10
    # Source units are SAMPLES: rate pinned as src fps, sample count
    # as src_frames, trims default to the full source range.
    assert c["src_fps_num"] == 48000 and c["src_fps_den"] == 1
    assert c["src_frames"] == 96000
    assert c["in_frame"] == 0 and c["out_frame"] == 96000


def test_audio_clip_stable_id_and_undo(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"], _audio(client, pid), a1["id"])
    cid = _detail(client, pid, t["id"])["clips"][0]["id"]
    _commit(client, pid, "timeline.undo", {"timeline_id": t["id"]})
    assert _detail(client, pid, t["id"])["clips"] == []
    _commit(client, pid, "timeline.redo", {"timeline_id": t["id"]})
    assert _detail(client, pid, t["id"])["clips"][0]["id"] == cid


def test_audio_trim_in_samples_and_undo(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"], _audio(client, pid), a1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    # Trim to a 1-second middle at 48k — integer sample bounds.
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": c["id"],
             "in_frame": 24000, "out_frame": 72000})
    c = _detail(client, pid, t["id"])["clips"][0]
    assert (c["in_frame"], c["out_frame"]) == (24000, 72000)
    _commit(client, pid, "timeline.undo", {"timeline_id": t["id"]})
    c = _detail(client, pid, t["id"])["clips"][0]
    assert (c["in_frame"], c["out_frame"]) == (0, 96000)


def test_audio_move_within_and_across_audio_lanes(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"], "A1")
    a2 = _atrack(client, pid, t["id"], "A2")
    _place(client, pid, t["id"], _audio(client, pid), a1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"], "clip_id": c["id"],
             "to_track_id": a2["id"], "start_frames": 48})
    c = _detail(client, pid, t["id"])["clips"][0]
    assert c["track_id"] == a2["id"] and c["start_frames"] == 48


# ---------- kind rule refusals ----------

def test_audio_asset_refused_on_video_lane(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["kind"] == "video")
    r = _place(client, pid, t["id"], _audio(client, pid), v1["id"],
               expect=422)
    assert "audio" in r.json()["detail"].lower()


def test_video_asset_refused_on_audio_lane(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    r = _place(client, pid, t["id"], _video(client, pid), a1["id"],
               expect=422)
    assert "audio assets only" in r.json()["detail"]


def test_video_move_to_audio_lane_refused(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["kind"] == "video")
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"], _video(client, pid), v1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    r = _commit(client, pid, "timeline.clip.move",
                {"timeline_id": t["id"], "clip_id": c["id"],
                 "to_track_id": a1["id"], "start_frames": 0},
                expect=422)
    assert "audio assets only" in r.json()["detail"]


def test_unprobeable_audio_refused(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    r = _place(client, pid, t["id"],
               _audio(client, pid, probe_ok=False), a1["id"],
               expect=422)
    assert "rate/duration" in r.json()["detail"]


def test_audio_overlap_and_source_bounds(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid, duration=2.0)  # 2s @48k = 48 tl-frames
    _place(client, pid, t["id"], au, a1["id"], at=0)
    _place(client, pid, t["id"], au, a1["id"], at=10,
           expect=422, allow_duplicate=True)
    _place(client, pid, t["id"], au, a1["id"], at=0,
           expect=422, allow_duplicate=True, out_frame=96001)


# ---------- invariants: lock / stale / idempotency / isolation ----------

def test_audio_lane_locked_refuses(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": a1["id"],
             "locked": True})
    r = _place(client, pid, t["id"], _audio(client, pid), a1["id"],
               expect=422)
    assert "locked" in r.json()["detail"].lower()


def test_audio_place_idempotent(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _audio(client, pid)
    r1 = _place(client, pid, t["id"], au, a1["id"], key="audio-key-1")
    r2 = _place(client, pid, t["id"], au, a1["id"], key="audio-key-1")
    assert r1["command_id"] == r2["command_id"]
    assert len(_detail(client, pid, t["id"])["clips"]) == 1


def test_audio_cross_project_isolation(client):
    p1, p2 = make_project(client)["id"], make_project(client)["id"]
    t = _tl(client, p1)
    a1 = _atrack(client, p1, t["id"])
    r = _place(client, p1, t["id"], _audio(client, p2), a1["id"],
               expect=404)
    assert r.status_code == 404


# ---------- EDL + export ----------

def test_edl_v2_carries_audio_stream_identity(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"],
           _audio(client, pid, rate=44100, channels=1), a1["id"])
    e = _edl(client, pid, t["id"], version=2)
    tr = next(x for x in e["tracks"] if x["kind"] == "audio")
    c = next(x for x in e["clips"] if x["track_id"] == tr["id"])
    src = c["source"]
    assert src["sample_rate"] == 44100 and src["channels"] == 1
    assert src["channel_layout"] == "stereo"
    assert src["samples"] == 88200 and src["fps_num"] == 44100
    # Determinism: identical state → identical doc.
    assert _edl(client, pid, t["id"], version=2) == e


def test_edl_v1_refuses_populated_audio_lane(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"], _audio(client, pid), a1["id"])
    r = _edl(client, pid, t["id"], version=1, expect=409)
    assert "video lanes only" in r.json()["detail"] \
        or "multiple tracks" in r.json()["detail"]


def test_export_preview_refuses_visible_audio_lane(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["kind"] == "video")
    _place(client, pid, t["id"], _video(client, pid), v1["id"])
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"], _audio(client, pid), a1["id"])
    # Commit queues the job (durable); adapter validation then fails
    # it closed — nothing is silently discarded.
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"], "profile": "720p"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "failed"
    assert "audio lanes" in (job.get("error") or "")


def test_export_ok_when_audio_lane_hidden(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["kind"] == "video")
    _place(client, pid, t["id"], _video(client, pid), v1["id"])
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"], _audio(client, pid), a1["id"])
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": a1["id"],
             "visible": False})
    # Hidden audio lanes contribute nothing — the audio-lane refusal
    # does not fire. The job still fails on the synthetic fixture's
    # missing bytes, but never on the audio rule.
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"], "profile": "720p"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "failed"
    assert "audio lanes" not in (job.get("error") or "")


def test_mixed_sample_rates_one_timeline(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"], _audio(client, pid, "a48.wav",
           sha="a" * 64, rate=48000, duration=2.0), a1["id"], at=0)
    _place(client, pid, t["id"], _audio(client, pid, "a44.wav",
           sha="b" * 64, rate=44100, duration=2.0), a1["id"], at=60)
    d = _detail(client, pid, t["id"])
    rates = {c["src_fps_num"] for c in d["clips"]}
    assert rates == {48000, 44100}


def test_gap_and_split_on_audio_lane(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"], _audio(client, pid), a1["id"], at=0)
    _place(client, pid, t["id"],
           _audio(client, pid, "b.wav", sha="c" * 64), a1["id"], at=60)
    _commit(client, pid, "timeline.gap.insert",
            {"timeline_id": t["id"], "track_id": a1["id"],
             "at_frame": 50, "frames": 12})
    d = _detail(client, pid, t["id"])
    assert {c["start_frames"] for c in d["clips"]} == {0, 72}
    c0 = next(c for c in d["clips"] if c["start_frames"] == 0)
    # Split lands at a timeline-frame offset (the clip spans 48
    # tl-frames); the backend maps it to the exact sample boundary.
    _commit(client, pid, "timeline.clip.split",
            {"timeline_id": t["id"], "clip_id": c0["id"],
             "at_frame": 24})
    d = _detail(client, pid, t["id"])
    assert len(d["clips"]) == 3
    # Source range conserved across the split in samples.
    halves = [c for c in d["clips"]
              if c["source_asset_id"] == c0["source_asset_id"]]
    assert sum(c["out_frame"] - c["in_frame"]
               for c in halves) == 96000
    assert {c["in_frame"] for c in halves} == {0, 48000}


def test_audio_remove_ripple(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"], _audio(client, pid), a1["id"], at=0)
    _place(client, pid, t["id"],
           _audio(client, pid, "b.wav", sha="d" * 64), a1["id"], at=60)
    d = _detail(client, pid, t["id"])
    c0 = next(c for c in d["clips"] if c["start_frames"] == 0)
    _commit(client, pid, "timeline.clip.remove",
            {"timeline_id": t["id"], "clip_id": c0["id"],
             "ripple_scope": "track"})
    d = _detail(client, pid, t["id"])
    assert len(d["clips"]) == 1
    assert d["clips"][0]["start_frames"] == 12  # 60 - 48 (2s extent)


def test_composite_plan_refuses_audio_lane_direct():
    """Unit: composite_plan raises on a visible populated audio lane."""
    from app.domain.timeline_export import composite_plan
    edl = {"format": "ade-edl/2",
           "timebase": {"num": 24, "den": 1},
           "tracks": [{"id": "t1", "kind": "audio", "position": 0,
                       "visible": True, "muted": False}],
           "clips": [{"clip_id": "c1", "track_id": "t1",
                      "start_frames": 0, "in_frame": 0,
                      "out_frame": 48000, "sha256": "x" * 64,
                      "timeline_frames": {"num": 48, "den": 1},
                      "source": {"fps_num": 48000, "fps_den": 1}}]}
    with pytest.raises(ValueError, match="audio lanes"):
        composite_plan(edl, {})
    # Hidden lane is a render decision — planning proceeds empty.
    edl["tracks"][0]["visible"] = False
    assert composite_plan(edl, {})["clips"] == []


# ---------- real ffprobe + import adapter ----------

@pytest.fixture()
def media_tools():
    from app.domain.media import binaries
    if binaries.ffmpeg() is None or binaries.ffprobe() is None:
        pytest.skip("ffmpeg/ffprobe not resolvable")


def _wav(path, *, rate=48000, channels=2, seconds=0.5):
    """Minimal valid PCM16 WAV — silence, no tooling needed."""
    import struct
    n = int(rate * seconds)
    data = b"\x00\x00" * channels * n
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(data), b"WAVE",
        b"fmt ", 16, 1, channels, rate,
        rate * channels * 2, channels * 2, 16, b"data", len(data))
    path.write_bytes(hdr + data)
    return path


@pytest.mark.usefixtures("media_tools")
def test_probe_audio_real_wav(client, tmp_path):
    from app.domain.media import engine, binaries
    if binaries.ffprobe() is None:
        pytest.skip("no ffprobe")
    p = _wav(tmp_path / "t.wav", rate=44100, channels=1, seconds=0.5)
    info = engine.probe_audio(p)
    assert info["ok"], info
    assert info["sample_rate"] == 44100 and info["channels"] == 1
    assert info["samples"] == 22050 and info["samples_exact"] is True
    assert info["codec"].startswith("pcm_")
    # garbage refuses
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not audio at all")
    assert engine.probe_audio(bad)["ok"] is False
    # video-only container has an audio stream? our synthetic wav is
    # audio-only; a zero-byte file refuses either way
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    assert engine.probe_audio(empty)["ok"] is False


@pytest.mark.usefixtures("media_tools")
def test_import_audio_asset_real_wav(client, tmp_path):
    from app.domain.media import binaries
    if binaries.ffprobe() is None:
        pytest.skip("no ffprobe")
    pid = make_project(client)["id"]
    p = _wav(tmp_path / "vox.wav", rate=48000, channels=2, seconds=0.25)
    r = _commit(client, pid, "asset.import",
                {"source_path": str(p), "name": "vox.wav",
                 "kind": "audio"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    a = next(x for x in items if x["name"] == "vox.wav")
    assert a["kind"] == "audio"
    probe = a["media_json"]["probe"]
    assert probe["ok"] is True and probe["sample_rate"] == 48000
    assert probe["channels"] == 2 and probe["samples"] == 12000
    # ...and it is immediately placeable on an audio lane.
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _place(client, pid, t["id"], {"id": a["id"]}, a1["id"])
    c = _detail(client, pid, t["id"])["clips"][0]
    assert c["src_fps_num"] == 48000 and c["out_frame"] == 12000


@pytest.mark.usefixtures("media_tools")
def test_import_audio_invalid_file_fails_closed(client, tmp_path):
    pid = make_project(client)["id"]
    p = tmp_path / "broken.wav"
    p.write_bytes(b"definitely not a wav")
    r = _commit(client, pid, "asset.import",
                {"source_path": str(p), "name": "broken.wav",
                 "kind": "audio"})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "succeeded", job  # CAS ingest succeeds…
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    a = next(x for x in items if x["name"] == "broken.wav")
    # …but the asset is NOT usable: probe.ok stays false and timeline
    # placement refuses it.
    assert a["media_json"]["probe"]["ok"] is False
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    r = _place(client, pid, t["id"], {"id": a["id"]}, a1["id"],
               expect=422)
    assert "rate/duration" in r.json()["detail"]
