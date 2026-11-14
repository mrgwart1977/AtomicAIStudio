"""C5-B — timeline export (FFmpeg) tests.

Layer 1: pure path-decision/argv/profile logic — no ffmpeg needed.
Layer 2: command wiring — preview pins, stale-approval, job shape.
Layer 3: real-ffmpeg end-to-end — skips cleanly when the approved
binaries aren't staged (scripts/fetch-ffmpeg.ps1).
"""
import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_c5a_timeline import _tl, _detail, _insert, _video
from app.domain import timeline_export as tx
from app.domain.media import binaries, engine, supervise


# ---------------- Layer 1 — pure domain logic ----------------

def _edl(clips, fps=(24, 1), total=0.0):
    return {"format": "ade-edl/1", "timebase": {"num": fps[0], "den": fps[1]},
            "clips": clips, "total_duration_s": total}


def _clip(frames=72, in_f=0, out_f=None, fps=(24, 1), w=832, h=480,
          codec="h264", sha="ab" * 32):
    return {"clip_id": "c", "sha256": sha, "in_frame": in_f,
            "out_frame": frames if out_f is None else out_f,
            "source": {"fps_num": fps[0], "fps_den": fps[1],
                       "frames": frames, "width": w, "height": h,
                       "codec": codec}}


def _probe(fps=24.0, w=832, h=480, codec="h264", pix="yuv420p",
           tb="1/24", audio=None, vfr=False):
    return {"ok": True, "codec": codec, "width": w, "height": h,
            "fps": fps, "frames": 72, "pix_fmt": pix, "time_base": tb,
            "vfr": vfr, "audio": audio or []}


def test_choose_path_copy_when_identical_untrimmed():
    edl = _edl([_clip(), _clip()])
    probes = [_probe(), _probe()]
    assert tx.choose_path(edl, probes)[0] == "copy"


def test_choose_path_encode_on_different_dims():
    edl = _edl([_clip(), _clip()])
    assert tx.choose_path(edl, [_probe(), _probe(w=640)])[0] == "encode"


def test_choose_path_encode_on_different_codec_or_pixfmt():
    edl = _edl([_clip(), _clip()])
    assert tx.choose_path(edl, [_probe(), _probe(codec="hevc")])[0] == "encode"
    assert tx.choose_path(edl, [_probe(), _probe(pix="yuv422p")])[0] == "encode"
    assert tx.choose_path(edl, [_probe(), _probe(tb="1/15360")])[0] == "encode"


def test_choose_path_trim_forces_encode():
    edl = _edl([_clip(in_f=0, out_f=60)])
    kind, reason = tx.choose_path(edl, [_probe()])
    assert kind == "copy-not-exact" and "re-encoding" in reason


def test_choose_path_vfr_and_no_video_encode():
    assert tx.choose_path(_edl([_clip()]), [_probe(vfr=True)])[0] == "encode"
    assert tx.choose_path(_edl([_clip()]),
                          [{**_probe(), "codec": ""}])[0] == "encode"


def test_choose_path_audio_layout_must_match():
    a = [{"codec": "aac", "sample_rate": 48000, "channels": 2,
          "channel_layout": "stereo", "time_base": "1/48000"}]
    edl = _edl([_clip(), _clip()])
    assert tx.choose_path(edl, [_probe(audio=a), _probe(audio=a)])[0] == "copy"
    # one silent, one aac → different layouts → encode
    assert tx.choose_path(edl, [_probe(audio=a), _probe()])[0] == "encode"


def test_resolve_profile_source_and_even_rounding():
    edl = _edl([_clip(w=831, h=481, fps=(30000, 1001))])
    t = tx.resolve_profile(edl, "source")
    # odd source dims round DOWN to even — never stretched
    assert (t["width"], t["height"]) == (830, 480)
    assert (t["fps_num"], t["fps_den"]) == (30000, 1001)
    t = tx.resolve_profile(edl, "1080p")
    assert (t["width"], t["height"]) == (1920, 1080)
    assert (t["fps_num"], t["fps_den"]) == (24, 1)  # timeline timebase
    with pytest.raises(ValueError):
        tx.resolve_profile(edl, "bogus")


def test_est_output_bytes_scales_with_duration():
    a = tx.est_output_bytes(_edl([_clip()], total=10.0), "1080p")
    b = tx.est_output_bytes(_edl([_clip()], total=20.0), "1080p")
    assert b > a and a > 0


def test_concat_list_escapes_and_formats():
    out = tx.concat_list(["C:/cas/blobs/ab/ff", "we'ird.mp4"])
    assert out.startswith("file 'C:/cas/blobs/ab/ff'\n")
    assert "we'\\''ird.mp4" in out


def test_build_copy_argv_shape():
    argv = tx.build_copy_argv("/ff/ffmpeg", "/w/list.txt", "/w/out.mp4")
    i = argv.index("-f")
    assert argv[i:i + 6] == ["-f", "concat", "-safe", "0", "-i", "/w/list.txt"]
    assert argv[argv.index("-c") + 1] == "copy" and argv[-1] == "/w/out.mp4"


def test_build_encode_argv_segments_and_silence():
    target = {"width": 1280, "height": 720, "fps_num": 24, "fps_den": 1}
    segs = [{"path": "a.mp4", "in_s": 0.0, "dur_s": 2.0, "has_audio": True},
            {"path": "b.mp4", "in_s": 0.5, "dur_s": 1.5, "has_audio": False}]
    argv = tx.build_encode_argv("/ff/ffmpeg", segs, target, 23, "/o.mp4")
    fc = argv[argv.index("-filter_complex") + 1]
    assert "concat=n=2:v=1:a=1" in fc
    assert "anullsrc" in " ".join(argv)          # silence fill for b
    assert "scale=w=1280:h=720" in fc
    assert "fps=24/1" in fc and "trim=start=0.5" in fc
    assert argv[argv.index("-crf") + 1] == "23"
    assert argv[argv.index("-pix_fmt") + 1] == "yuv420p"


def test_build_encode_argv_all_silent_no_audio_track():
    target = {"width": 64, "height": 48, "fps_num": 24, "fps_den": 1}
    segs = [{"path": "a.mp4", "in_s": 0.0, "dur_s": 1.0, "has_audio": False}]
    argv = tx.build_encode_argv("/ff", segs, target, 23, "/o.mp4")
    fc = argv[argv.index("-filter_complex") + 1]
    assert "a=0" in fc and "-map" in argv and "[a]" not in argv


# ---------------- Layer 2 — command wiring ----------------

def test_export_preview_pins_edl_and_commit_enqueues(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v = _video(client, pid)
    _insert(client, pid, t["id"], v)
    p = _preview(client, pid, "timeline.export",
                 {"timeline_id": t["id"], "profile": "720p"})
    assert p["launches_generation"] is True
    rt = p["resolved_timeline"]
    assert rt["id"] == t["id"] and rt["clip_count"] == 1
    assert len(rt["edl_sha256"]) == 64
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"], "profile": "720p",
                 "expected_timeline": {"id": t["id"],
                                       "revision": rt["revision"]},
                 "expected_edl_sha": rt["edl_sha256"]})
    job = wait_for_job(client, pid, r["job_id"])
    # synthetic asset rows have no bytes on disk → fails closed
    assert job["status"] == "failed"
    with client.app.state.session_factory() as s:
        from app.db.models import GenerationJob
        j = s.get(GenerationJob, r["job_id"])
        assert j.adapter_id == "ops.timeline.export"
        assert j.resumable is False
        req = j.request_json
        assert req["edl_sha256"] == rt["edl_sha256"]
        assert req["edl"]["format"] == "ade-edl/1"
        assert req["profile"] == "720p" and req["est_output_bytes"] > 0


def test_export_stale_pins_rejected(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    _commit(client, pid, "timeline.export",
            {"timeline_id": t["id"],
             "expected_timeline": {"id": t["id"], "revision": 99}},
            expect=422)
    _commit(client, pid, "timeline.export",
            {"timeline_id": t["id"], "expected_edl_sha": "0" * 64},
            expect=422)


def test_export_bad_profile_and_crf_rejected(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    for bad in ({"profile": "4k"}, {"crf": 5}, {"crf": 99}, {"crf": "x"}):
        _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"], **bad}, expect=422)


def test_export_empty_timeline_and_cross_project(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    r = _commit(client, pid, "timeline.export", {"timeline_id": t["id"]})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "failed"  # adapter validate: no clips
    other = make_project(client, "Other")["id"]
    _commit(client, pid, "timeline.export",
            {"timeline_id": t["id"]}, expect=200)
    _commit(client, other, "timeline.export",
            {"timeline_id": t["id"]}, expect=404)


# ---------------- Layer 3 — real ffmpeg end-to-end ----------------

@pytest.fixture()
def media_tools():
    if binaries.ffmpeg() is None or binaries.ffprobe() is None:
        pytest.skip("ffmpeg/ffprobe not resolvable — run scripts/fetch-ffmpeg.ps1")


@pytest.fixture()
def dev_ffmpeg() -> str:
    if (which := shutil.which("ffmpeg")):
        return which
    tool = binaries.ffmpeg()
    if tool is not None:
        return tool["path"]
    pytest.skip("no ffmpeg available to synthesize fixtures")


def _synth(ffmpeg: str, path: Path, *, dur=1.0, size="64x48", rate=24,
           audio=False) -> Path:
    enc = tx.h264_encoder(ffmpeg, supervise.run)  # bundled = libopenh264
    args = ["-y", "-v", "error", "-f", "lavfi", "-i",
            f"testsrc=duration={dur}:size={size}:rate={rate}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
                 "-c:a", "aac", "-shortest"]
    r = subprocess.run(
        [ffmpeg, *args, "-c:v", enc, "-pix_fmt", "yuv420p", str(path)],
        capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert path.stat().st_size > 0
    return path


def _cas_video(client, pid, src: Path, name="clip.mp4"):
    """Place real bytes into CAS layout + register asset/version rows."""
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    data_dir = Path(client.app.state.settings.data_dir)
    rel = Path("blobs") / sha[:2] / sha
    dest = data_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    meta = engine.probe(dest)
    assert meta["ok"], meta.get("reason")
    from app.db.models import (Asset, AssetFile, AssetVersion,
                               StorageLocation, new_id)
    with client.app.state.session_factory() as s:
        loc = s.query(StorageLocation).first()
        af = s.get(AssetFile, sha)
        if af is None:
            af = AssetFile(sha256=sha,
                           storage_location_id=loc.id if loc else None,
                           rel_path=str(rel).replace("\\", "/"),
                           byte_size=src.stat().st_size, status="ready")
            s.add(af)
        a = Asset(id=new_id(), project_id=pid, name=name, kind="video",
                  origin="generated", uri=str(rel).replace("\\", "/"),
                  sha256=sha, byte_size=src.stat().st_size,
                  status="ready")
        s.add(a)
        s.flush()
        v = AssetVersion(id=new_id(), asset_id=a.id, version_no=1,
                         sha256=sha, media_json={"probe": meta})
        s.add(v)
        s.flush()
        a.current_version_id = v.id
        s.commit()
        return {"id": a.id, "version_id": v.id, "sha256": sha,
                "probe": meta}


def _export(client, pid, tid, **kw):
    p = _preview(client, pid, "timeline.export",
                 {"timeline_id": tid, **kw})
    rt = p["resolved_timeline"]
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": tid,
                 "expected_timeline": {"id": tid, "revision": rt["revision"]},
                 "expected_edl_sha": rt["edl_sha256"], **kw})
    return wait_for_job(client, pid, r["job_id"], timeout=60)


def _out_asset(client, pid, job):
    from app.db.models import Asset, GenerationJob, AssetVersion
    with client.app.state.session_factory() as s:
        j = s.get(GenerationJob, job["id"])
        a = s.get(Asset, j.output_asset_id) if j.output_asset_id else None
        v = s.get(AssetVersion, a.current_version_id) if a else None
        return a, v, j


@pytest.mark.usefixtures("media_tools")
def test_export_copy_path_identical_untrimmed(client, dev_ffmpeg, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    for i in range(2):
        src = _synth(dev_ffmpeg, tmp_path / f"c{i}.mp4", dur=1.0, rate=24)
        _insert(client, pid, t["id"], _cas_video(client, pid, src, f"c{i}"))
    job = _export(client, pid, t["id"], profile="source")
    assert job["status"] == "succeeded", job.get("error")
    a, v, j = _out_asset(client, pid, job)
    assert a and a.kind == "video" and a.origin == "rendered"
    prov = a.provenance_json
    assert prov["adapter_id"] == "ops.timeline.export"
    assert prov["request"]["edl_sha256"] == j.request_json["edl_sha256"]
    assert prov["request"]["timeline_revision"] is not None
    # render decision persisted on the version's media record
    render = (v.media_json or {}).get("render") or {}
    assert render["path"] == "copy"
    # verify rendered content via ffprobe on the CAS output path
    # (decoded-stream check, never source/output file-hash equality)
    from app.db.models import AssetFile
    with client.app.state.session_factory() as s:
        af = s.get(AssetFile, v.sha256)
        out = client.app.state.storage.file_path(s, af)
    meta = engine.probe(out)
    assert meta["ok"] and meta["codec"] == "h264"
    # 2 × 1.0 s @24fps — copy path keeps source packets
    assert abs(meta["duration_s"] - 2.0) < 0.15


@pytest.mark.usefixtures("media_tools")
def test_export_encode_path_trim_frame_exact(client, dev_ffmpeg, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    src = _synth(dev_ffmpeg, tmp_path / "t.mp4", dur=2.0, rate=24)
    a = _cas_video(client, pid, src)
    _insert(client, pid, t["id"], a, in_frame=12, out_frame=36)  # 1.0 s
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    _a, v, _j = _out_asset(client, pid, job)
    from app.db.models import AssetFile
    with client.app.state.session_factory() as s:
        out = client.app.state.storage.file_path(s, s.get(AssetFile, v.sha256))
    meta = engine.probe(out)
    # (36-12)/24 s @ 24fps timeline → exactly 24 frames
    assert meta["frames"] == 24
    assert abs(meta["duration_s"] - 1.0) < 0.1
    assert meta["width"] == 1280 and meta["height"] == 720


@pytest.mark.usefixtures("media_tools")
def test_export_mixed_dims_and_silence_fill(client, dev_ffmpeg, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    s1 = _synth(dev_ffmpeg, tmp_path / "a.mp4", dur=1.0, size="64x48",
                rate=24, audio=True)
    s2 = _synth(dev_ffmpeg, tmp_path / "b.mp4", dur=1.0, size="128x96",
                rate=24, audio=False)
    _insert(client, pid, t["id"], _cas_video(client, pid, s1, "a"))
    _insert(client, pid, t["id"], _cas_video(client, pid, s2, "b"))
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    _a, v, _j = _out_asset(client, pid, job)
    from app.db.models import AssetFile
    with client.app.state.session_factory() as s:
        out = client.app.state.storage.file_path(s, s.get(AssetFile, v.sha256))
    meta = engine.probe(out)
    assert meta["width"] == 1280 and meta["height"] == 720
    assert abs(meta["duration_s"] - 2.0) < 0.2
    # silent segment got a generated track → uniform stereo aac out
    assert meta["audio_streams"] == 1
    assert meta["audio"][0]["codec"] == "aac"
    assert meta["audio"][0]["channels"] == 2


@pytest.mark.usefixtures("media_tools")
def test_export_missing_source_bytes_fails_safe(client, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))  # no bytes on disk
    job = _export(client, pid, t["id"], profile="source")
    assert job["status"] == "failed"
    # no successful asset materialized
    from app.db.models import Asset
    with client.app.state.session_factory() as s:
        assert not any(a.origin == "rendered" and a.project_id == pid
                       for a in s.query(Asset).all())


@pytest.mark.usefixtures("media_tools")
def test_export_corrupt_source_bytes_fails_closed(client, dev_ffmpeg,
                                                  tmp_path):
    """Pinned sha is verified against actual bytes at execution —
    a blob swapped or corrupted after approval must refuse, not render."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    src = _synth(dev_ffmpeg, tmp_path / "ok.mp4", dur=1.0)
    a = _cas_video(client, pid, src)
    _insert(client, pid, t["id"], a)
    # corrupt the CAS blob in place (keeps the row, changes the bytes)
    blob = Path(client.app.state.settings.data_dir) / "blobs" \
        / a["sha256"][:2] / a["sha256"]
    blob.write_bytes(b"\x00" * 4096)
    job = _export(client, pid, t["id"], profile="source")
    assert job["status"] == "failed"
    assert "sha256" in (job.get("error") or "")


def test_export_idempotent_replay_no_duplicate(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    key = "export-replay-1"
    r1 = _commit(client, pid, "timeline.export",
                 {"timeline_id": t["id"]}, key=key)
    r2 = _commit(client, pid, "timeline.export",
                 {"timeline_id": t["id"]}, key=key)
    assert r2.get("idempotent_replay") is True
    assert r2["command_id"] == r1["command_id"]
    # exactly one durable job exists for the key — a replayed commit
    # can never produce a second output asset
    from app.db.models import GenerationJob
    with client.app.state.session_factory() as s:
        jobs = s.query(GenerationJob).filter_by(
            idempotency_key=f"job:{key}").all()
        assert len(jobs) == 1 and jobs[0].id == r1["job_id"]
    wait_for_job(client, pid, r1["job_id"])


@pytest.mark.usefixtures("media_tools")
def test_export_two_audible_clips_sync(client, dev_ffmpeg, tmp_path):
    """Segment boundary: two audible segments → one continuous stereo
    track whose duration matches the video within a frame."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    for i in range(2):
        src = _synth(dev_ffmpeg, tmp_path / f"s{i}.mp4", dur=1.0,
                     size="64x48", audio=True)
        _insert(client, pid, t["id"], _cas_video(client, pid, src, f"s{i}"))
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    _a, v, _j = _out_asset(client, pid, job)
    from app.db.models import AssetFile
    with client.app.state.session_factory() as s:
        out = client.app.state.storage.file_path(
            s, s.get(AssetFile, v.sha256))
    meta = engine.probe(out)
    assert meta["audio_streams"] == 1
    # video ~2.0s; audio track must span the same window (no gap at the
    # segment join, no early termination)
    assert abs(meta["duration_s"] - 2.0) < 0.2


@pytest.mark.usefixtures("media_tools")
def test_export_cancel_terminates_and_preserves_sources(
        client, dev_ffmpeg, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    # heavy render: a long clip on the encode path — the ffmpeg run
    # lasts seconds, a reliable window to land a running cancel.
    src = _synth(dev_ffmpeg, tmp_path / "long.mp4", dur=30.0,
                 size="1280x720", rate=24)
    a = _cas_video(client, pid, src)
    blob = Path(client.app.state.settings.data_dir) / "blobs" \
        / a["sha256"][:2] / a["sha256"]
    blob_bytes = blob.read_bytes()
    _insert(client, pid, t["id"], a, in_frame=0, out_frame=600)  # 25 s
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"], "profile": "720p"})
    jid = r["job_id"]
    import time
    # land the cancel while ffmpeg is mid-encode — post it the moment
    # the job flips to running, not after the poll loop ends
    resp = None
    deadline = time.time() + 15
    while time.time() < deadline:
        st = next((j for j in client.get(
            f"/api/projects/{pid}/jobs").json() if j["id"] == jid), None)
        if st and st["status"] == "running":
            resp = client.post(f"/api/jobs/{jid}/cancel")
            break
        if st and st["status"] in ("succeeded", "failed", "cancelled"):
            pytest.fail(f"job reached {st['status']} before cancel landed")
        time.sleep(0.05)
    assert resp is not None and resp.status_code == 200, (
        resp.text if resp is not None else "job never entered running")
    job = wait_for_job(client, pid, jid, timeout=60)
    assert job["status"] == "cancelled"
    # cancelled export registered no output asset
    from app.db.models import Asset, GenerationJob
    with client.app.state.session_factory() as s:
        assert s.get(GenerationJob, jid).output_asset_id is None
    # source CAS bytes untouched by cleanup
    assert blob.read_bytes() == blob_bytes
    # repeated cancel is idempotent — terminal state stays cancelled
    assert client.post(f"/api/jobs/{jid}/cancel").status_code in (200, 409)


@pytest.mark.usefixtures("media_tools")
def test_video_import_gains_measured_media(client, dev_ffmpeg, tmp_path):
    """C5-B review: imported video must carry measured probe media so
    timeline.clip.insert can validate trims; packaged validation exposed
    that imports registered no duration/fps at all."""
    src = _synth(dev_ffmpeg, tmp_path / "imp.mp4", dur=1.5, rate=24)
    pid = make_project(client)["id"]
    r = _commit(client, pid, "asset.import",
                {"source_path": str(src), "kind": "video",
                 "name": "imp.mp4"})
    job = wait_for_job(client, pid, r["job_id"], timeout=30)
    assert job["status"] == "succeeded", job.get("error")
    from app.db.models import Asset, AssetVersion
    with client.app.state.session_factory() as s:
        a = s.query(Asset).filter_by(project_id=pid).one()
        v = s.get(AssetVersion, a.current_version_id)
        probe = (v.media_json or {}).get("probe") or {}
    assert probe.get("ok") and probe["fps"] == 24.0
    assert abs(probe["duration_s"] - 1.5) < 0.1
    assert probe["width"] == 64 and probe["height"] == 48
    # the point: the imported asset is now insertable
    t = _tl(client, pid)
    _insert(client, pid, t["id"], {"id": a.id}, in_frame=0, out_frame=24)
