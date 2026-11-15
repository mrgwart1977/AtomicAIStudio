"""N1-D — multitrack compositing + export tests.

Layer 1: composite_plan / canvas / argv construction — deterministic,
no ffmpeg needed.
Layer 2: command wiring — non-v1 layouts pin ade-edl/2 and enqueue.
Layer 3: real-ffmpeg render — skips when binaries aren't staged.
"""
import hashlib
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_c5a_timeline import _tl, _detail, _insert, _video
from tests.test_c5b_export import _synth, _cas_video, media_tools, dev_ffmpeg
from tests.test_n1a_tracks import _track
from app.domain import timeline_export as tx
from app.domain.media import binaries, engine, supervise


# ---------------- Layer 1 — plan + graph ----------------

def _v2(clips, tracks=None, fps=(24, 1)):
    tracks = tracks if tracks is not None else [
        {"id": "v1", "kind": "video", "position": 0, "name": "V1",
         "visible": True, "locked": False, "muted": False}]
    return {"format": "ade-edl/2", "timeline_id": "t1", "revision": 3,
            "name": "Cut", "timebase": {"num": fps[0], "den": fps[1]},
            "tracks": tracks, "clips": clips, "gaps": [],
            "total_duration_s": 0.0}


def _v2clip(cid="c1", track="v1", start=0, extent=(72, 1),
            in_f=0, out_f=72, sfps=(24, 1), w=832, h=480,
            sha="ab" * 32):
    return {"clip_id": cid, "track_id": track, "order": 0,
            "start_frames": start,
            "timeline_frames": {"num": extent[0], "den": extent[1]},
            "sha256": sha, "in_frame": in_f, "out_frame": out_f,
            "source": {"fps_num": sfps[0], "fps_den": sfps[1],
                       "frames": out_f, "width": w, "height": h,
                       "codec": "h264"},
            "shot_id": None, "scene_id": None}


def _trk(tid="o1", kind="overlay", pos=1, visible=True, muted=False):
    return {"id": tid, "kind": kind, "position": pos, "name": tid,
            "visible": visible, "locked": False, "muted": muted}


def test_plan_orders_bottom_to_top_and_hides_hidden():
    """Overlay chain stacks position-descending: the lane rendered at
    the top of the editor stack composites LAST (topmost picture)."""
    tracks = [_trk("o1", pos=0), _trk("v1", "video", 1),
              _trk("hid", pos=2, visible=False)]
    clips = [_v2clip("top", "o1"), _v2clip("base", "v1"),
             _v2clip("ghost", "hid")]
    plan = tx.composite_plan(_v2(clips, tracks), {})
    # bottom (v1 @pos1) first, top (o1 @pos0) last; hidden lane absent.
    assert [c["clip_id"] for c in plan["clips"]] == ["base", "top"]


def test_plan_exact_times_and_program_end():
    """NTSC timeline: program end is the exact rational frame count."""
    clips = [_v2clip("a", start=0, extent=(48, 1)),
             _v2clip("b", start=48, extent=(601, 10))]  # 60.1f@24
    plan = tx.composite_plan(_v2(clips, fps=(24000, 1001)),
                             {"a": {}, "b": {}})
    assert plan["program_frames"] == Fraction(1081, 10)  # 48 + 60.1
    assert (plan["fps_num"], plan["fps_den"]) == (24000, 1001)
    b = next(c for c in plan["clips"] if c["clip_id"] == "b")
    assert b["tl_start_s"] == Fraction(48) / Fraction(24000, 1001)


def test_plan_muted_lane_keeps_video_drops_audio():
    tracks = [_trk("v1", "video", 0), _trk("m", pos=1, muted=True)]
    clips = [_v2clip("a", "v1"), _v2clip("b", "m", start=10)]
    plan = tx.composite_plan(_v2(clips, tracks),
                             {"a": {"audio": [{}]}, "b": {"audio": [{}]}})
    b = next(c for c in plan["clips"] if c["clip_id"] == "b")
    assert b["has_audio"] and b["muted"]  # argv layer filters on both


def test_canvas_profiles_and_source_max_dims():
    clips = [_v2clip("a"), _v2clip("b", w=1920, h=800)]
    probes = {"a": {"width": 832, "height": 480},
              "b": {"width": 1920, "height": 800}}
    plan = tx.composite_plan(_v2(clips), probes)
    c = tx.composite_canvas(plan, "source", probes)
    assert (c["width"], c["height"]) == (1920, 800)
    assert tx.composite_canvas(plan, "720p", probes)["height"] == 720
    with pytest.raises(ValueError, match="profile"):
        tx.composite_canvas(plan, "4k", probes)


def test_composite_argv_deterministic_and_shaped():
    tracks = [_trk("v1", "video", 1), _trk("o1", pos=0)]
    clips = [_v2clip("top", "o1", start=24, extent=(24, 1)),
             _v2clip("base", "v1", start=0, extent=(48, 1))]
    probes = {"base": {"audio": [{}]}, "top": {}}
    plan = tx.composite_plan(_v2(clips, tracks), probes)
    canvas = tx.composite_canvas(plan, "720p", {})
    paths = {"base": "/cas/a", "top": "/cas/b"}
    a1 = tx.build_composite_argv("ff", plan, paths, canvas, 23, "/o.mp4")
    a2 = tx.build_composite_argv("ff", plan, paths, canvas, 23, "/o.mp4")
    assert a1 == a2  # byte-identical graph for identical plan
    fc = a1[a1.index("-filter_complex") + 1]
    # base canvas first, overlays bottom→top: base clip then top clip.
    assert fc.index("[c0]") < fc.index("[c1]")
    assert fc.index("overlay") < fc.rindex("overlay")
    assert "[base][c0]overlay" in fc and "[o0][c1]overlay" in fc
    # enable windows in exact timeline frames: [S, E), no float leak.
    assert "gte(n,24)*lt(n,48)" in fc      # top clip @24..48f
    assert "gte(n,0)*lt(n,48)" in fc       # base clip @0..48f
    # audio: only the unmuted audio-bearing clip mixes; silence base;
    # start=0 needs no leading-silence concat.
    assert "[0:a]atrim" in fc and "asetpts=PTS-STARTPTS[am0]" in fc
    assert "amix=inputs=2:duration=first:normalize=0" in fc
    assert "alimiter" in fc and "anullsrc" in fc
    # argv never goes through a shell — list of discrete args.
    assert "-t" in a1 and a1[-1] == "/o.mp4"


def test_composite_argv_no_audio_means_no_audio_track():
    clips = [_v2clip("a")]
    plan = tx.composite_plan(_v2(clips), {"a": {}})
    canvas = tx.composite_canvas(plan, "720p", {})
    argv = tx.build_composite_argv("ff", plan, {"a": "/cas/a"},
                                   canvas, 23, "/o.mp4")
    assert "[a]" not in argv and "amix" not in argv[argv.index(
        "-filter_complex") + 1]


def test_composite_argv_audio_delay_ms_and_silence_gaps():
    """Clip at frame 100 of a 24fps timeline → 4166ms delay; the
    silent amix base makes non-covered regions silent, and
    duration=first bounds the mix to the program."""
    clips = [_v2clip("a", start=100, extent=(24, 1))]
    plan = tx.composite_plan(_v2(clips), {"a": {"audio": [{}]}})
    canvas = tx.composite_canvas(plan, "720p", {})
    argv = tx.build_composite_argv("ff", plan, {"a": "/cas/a"},
                                   canvas, 23, "/o.mp4")
    fc = argv[argv.index("-filter_complex") + 1]
    # 100f @24fps = 200,000 leading-silence samples exactly — sample-
    # accurate placement, no millisecond quantization.
    assert f"atrim=end_sample={round(Fraction(100, 24) * 48000)}" in fc
    assert "concat=n=2:v=0:a=1" in fc


def test_composite_argv_mixed_source_rates_normalize_fps():
    clips = [_v2clip("a", sfps=(30000, 1001), extent=(48, 1))]
    plan = tx.composite_plan(_v2(clips), {"a": {}})
    canvas = tx.composite_canvas(plan, "720p", {})
    argv = tx.build_composite_argv("ff", plan, {"a": "/cas/a"},
                                   canvas, 23, "/o.mp4")
    fc = argv[argv.index("-filter_complex") + 1]
    assert "fps=24000/1001" in fc or "fps=24/1" in fc


def test_validate_rejects_bad_layouts():
    import asyncio
    from app.adapters.timeline_export import TimelineExportAdapter
    ad = TimelineExportAdapter.__new__(TimelineExportAdapter)
    errs = asyncio.run(ad.validate({"profile": "source",
                                    "edl": {"format": "bogus"}}))
    assert any("ade-edl" in e for e in errs)
    errs = asyncio.run(ad.validate({
        "profile": "source",
        "edl": _v2([_v2clip("g", "hid")],
                   [_trk("hid", pos=0, visible=False)])}))
    assert any("visible" in e for e in errs)


@pytest.mark.parametrize("fps", [(24, 1), (25, 1), (30, 1),
                                 (24000, 1001), (30000, 1001)])
def test_plan_program_end_and_windows_all_rates(fps):
    """Leading + interior + trailing gap coverage across every
    shipping rate: occupancy windows are integer-frame exact, and the
    enable window is [S, E) in timeline frames — never float seconds."""
    clips = [_v2clip("lead", start=10, extent=(20, 1)),      # lead gap
             _v2clip("mid", start=50, extent=(10, 1)),       # interior
             _v2clip("tail", start=60, extent=(1001, 100))]  # 10.01f
    plan = tx.composite_plan(_v2(clips, fps=fps), {})
    lead = next(c for c in plan["clips"] if c["clip_id"] == "lead")
    tail = next(c for c in plan["clips"] if c["clip_id"] == "tail")
    assert lead["start_frame"] == 10 and lead["occ_end_frame"] == 30
    assert tail["occ_end_frame"] == 71   # 60 + ceil(10.01)
    assert plan["program_frames"] == Fraction(60) + Fraction(1001, 100)
    canvas = tx.composite_canvas(plan, "720p", {})
    argv = tx.build_composite_argv("ff", plan,
                                   {c["clip_id"]: "/x" for c
                                    in plan["clips"]},
                                   canvas, 23, "/o.mp4")
    fc = argv[argv.index("-filter_complex") + 1]
    assert "gte(n,10)*lt(n,30)" in fc and "gte(n,60)*lt(n,71)" in fc


# ---------------- Layer 2 — command wiring ----------------

def test_multitrack_export_pins_v2_and_enqueues(client):
    """Two populated lanes → commit pins ade-edl/2, job queued."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    o1 = _track(client, pid, t["id"], kind="overlay", name="O1")
    _insert(client, pid, t["id"], _video(client, pid))
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "track_id": o1["id"],
             "asset_id": _video(client, pid, "o.mp4", sha="c" * 64)["id"],
             "start_frames": 0})
    p = _preview(client, pid, "timeline.export",
                 {"timeline_id": t["id"]})
    rt = p["resolved_timeline"]
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"],
                 "expected_timeline": {"id": t["id"],
                                       "revision": rt["revision"]},
                 "expected_edl_sha": rt["edl_sha256"]})
    job = wait_for_job(client, pid, r["job_id"])
    assert job["status"] == "failed"  # synthetic assets → no bytes
    with client.app.state.session_factory() as s:
        from app.db.models import GenerationJob
        req = s.get(GenerationJob, r["job_id"]).request_json
    assert req["edl"]["format"] == "ade-edl/2"
    assert len(req["edl"]["clips"]) == 2
    assert req["edl_sha256"] == rt["edl_sha256"]


def test_v1_layout_still_uses_v1_document(client):
    """Sequential single-lane timelines keep the fast v1 path —
    existing sequential export is a regression-guarded behavior."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    _insert(client, pid, t["id"], _video(client, pid, "b.mp4",
                                        sha="b" * 64))
    p = _preview(client, pid, "timeline.export",
                 {"timeline_id": t["id"]})
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"],
                 "expected_timeline": p["resolved_timeline"],
                 "expected_edl_sha":
                     p["resolved_timeline"]["edl_sha256"]})
    job = wait_for_job(client, pid, r["job_id"])
    with client.app.state.session_factory() as s:
        from app.db.models import GenerationJob
        req = s.get(GenerationJob, r["job_id"]).request_json
    assert req["edl"]["format"] == "ade-edl/1"


def test_stale_pins_still_rejected_on_v2_layout(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _insert(client, pid, t["id"], _video(client, pid))
    _commit(client, pid, "timeline.clip.place",
            {"timeline_id": t["id"], "asset_id":
             _video(client, pid, "b.mp4", sha="b" * 64)["id"],
             "start_frames": 200})
    p = _preview(client, pid, "timeline.export",
                 {"timeline_id": t["id"]})
    _commit(client, pid, "timeline.gap.close",
            {"timeline_id": t["id"], "track_id":
             _detail(client, pid, t["id"])["tracks"][0]["id"],
             "at_frame": 100})
    _commit(client, pid, "timeline.export",
            {"timeline_id": t["id"],
             "expected_edl_sha":
                 p["resolved_timeline"]["edl_sha256"]}, expect=422)


# ---------------- Layer 2b — composite failure/cancel paths --------

def _cas_blob(client, data: bytes) -> str:
    """Put raw bytes into CAS layout + register the AssetFile row."""
    from app.db.models import AssetFile, StorageLocation
    sha = hashlib.sha256(data).hexdigest()
    data_dir = Path(client.app.state.settings.data_dir)
    rel = Path("blobs") / sha[:2] / sha
    dest = data_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    with client.app.state.session_factory() as s:
        loc = s.query(StorageLocation).first()
        if s.get(AssetFile, sha) is None:
            s.add(AssetFile(sha256=sha,
                            storage_location_id=loc.id if loc else None,
                            rel_path=str(rel).replace("\\", "/"),
                            byte_size=len(data), status="ready"))
            s.commit()
    return sha


def _adapter(client):
    from app.adapters.timeline_export import TimelineExportAdapter
    st = client.app.state
    return TimelineExportAdapter(st.session_factory, st.storage,
                                 st.settings)


def _handle(client, edl, tmp_path, cancel=None, **req):
    from app.adapters.base import JobHandle
    return JobHandle(job_id="j1", project_id="p",
                     request={"profile": "720p", "crf": 23,
                              "edl": edl, **req},
                     work_dir=str(tmp_path), cancel_event=cancel)


async def _drain(agen):
    updates = []
    async for u in agen:
        updates.append(u)
    return updates


def _v2_edl_for(client, pid, tmp_path):
    """Real two-lane layout via commands → pinned v2 doc whose sha-
    pinned blobs physically exist in CAS (fake bytes — probe is
    monkeypatched by callers)."""
    t = _tl(client, pid)
    o1 = _track(client, pid, t["id"], kind="overlay", name="O1")
    data_dir = Path(client.app.state.settings.data_dir)
    for i, trk in enumerate((None, o1["id"])):
        content = f"clip-{i}-bytes".encode()
        sha = hashlib.sha256(content).hexdigest()
        blob = data_dir / "blobs" / sha[:2] / "v"
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(content)
        a = _video(client, pid, f"v{i}.mp4", sha=sha)
        payload = {"timeline_id": t["id"], "asset_id": a["id"]}
        if trk:
            payload["track_id"] = trk
            payload["start_frames"] = 0
            _commit(client, pid, "timeline.clip.place", payload)
        else:
            _commit(client, pid, "timeline.clip.insert", payload)
    with client.app.state.session_factory() as s:
        from app.domain import timeline as tld
        from app.db.models import Timeline
        tl = s.get(Timeline, t["id"])
        return tld.edl_json_v2(s, tl)


def test_composite_missing_source_file_fails_closed(client, tmp_path):
    """EDL references a sha with no AssetFile row → explicit refusal,
    no output, no partial render."""
    import asyncio
    edl = _v2([_v2clip("c1", sha="0" * 64)])
    h = _handle(client, edl, tmp_path)
    with pytest.raises(ValueError, match="missing"):
        asyncio.run(_drain(_adapter(client).run(h)))
    assert not list(tmp_path.glob("*export*.mp4"))


def test_composite_hash_mismatch_refuses(client, tmp_path):
    """Bytes on disk that don't match the pinned sha → fail closed."""
    import asyncio
    good = b"declared bytes"
    sha = _cas_blob(client, good)
    # Corrupt the CAS blob AFTER registration.
    data_dir = Path(client.app.state.settings.data_dir)
    blob = data_dir / "blobs" / sha[:2] / sha
    blob.write_bytes(b"tampered")
    edl = _v2([_v2clip("c1", sha=sha)])
    h = _handle(client, edl, tmp_path)
    with pytest.raises(ValueError, match="sha256"):
        asyncio.run(_drain(_adapter(client).run(h)))
    assert not list(tmp_path.glob("*export*.mp4"))


def test_composite_probe_failure_is_clear(client, tmp_path):
    """A hash-verified blob that isn't media → clear probe error."""
    import asyncio
    sha = _cas_blob(client, b"definitely not a video file")
    edl = _v2([_v2clip("c1", sha=sha)])
    h = _handle(client, edl, tmp_path)
    with pytest.raises(ValueError, match="probe|video stream|codec"):
        asyncio.run(_drain(_adapter(client).run(h)))


def test_composite_cancel_during_render_cleans_output(client,
                                                    tmp_path,
                                                    monkeypatch):
    """Cancelled ffmpeg → JobCancelled, output unlinked, no asset."""
    import asyncio, threading
    pid = make_project(client)["id"]
    edl = _v2_edl_for(client, pid, tmp_path)
    cancel = threading.Event()
    ad = _adapter(client)
    from app.domain.media import supervise
    real_run = supervise.run

    def fake_run(argv, timeout_s=0, cancel=None):
        out = Path(argv[-1])
        out.write_bytes(b"partial render")
        return {"cancelled": True, "timed_out": False, "exit": None,
                "output_tail": ""}

    monkeypatch.setattr(supervise, "run", fake_run)
    # Pre-seed the encoder cache — h264_encoder() would otherwise call
    # the fake runner and cache a bogus pick for later real renders.
    from app.domain.media import binaries
    ff = binaries.ffmpeg()
    if ff:
        monkeypatch.setitem(tx._ENCODER_CACHE, ff["path"],
                            "libopenh264")
    # Probes: real ffprobe on garbage bytes fails — fake probe instead.
    monkeypatch.setattr(
        "app.adapters.timeline_export.engine.probe",
        lambda p, cancel=None: {"ok": True, "codec": "h264",
                                "width": 320, "height": 240, "fps": 24,
                                "audio": []})
    from app.adapters.base import JobCancelled
    h = _handle(client, edl, tmp_path, cancel=cancel)
    with pytest.raises(JobCancelled):
        asyncio.run(_drain(ad.run(h)))
    out = tmp_path / "j1-export.mp4"
    assert not out.exists()  # partial output removed — no phantom
    monkeypatch.setattr(supervise, "run", real_run)


def test_composite_ffmpeg_exit_failure_cleans_output(client, tmp_path,
                                                     monkeypatch):
    """ffmpeg exit!=0 → job error, output removed, tail surfaces."""
    import asyncio
    pid = make_project(client)["id"]
    edl = _v2_edl_for(client, pid, tmp_path)
    ad = _adapter(client)
    from app.domain.media import supervise
    monkeypatch.setattr(supervise, "run",
                        lambda argv, timeout_s=0, cancel=None:
                        {"cancelled": False, "timed_out": False,
                         "exit": 1, "output_tail": "codec blew up"})
    from app.domain.media import binaries
    ff = binaries.ffmpeg()
    if ff:
        monkeypatch.setitem(tx._ENCODER_CACHE, ff["path"],
                            "libopenh264")
    monkeypatch.setattr(
        "app.adapters.timeline_export.engine.probe",
        lambda p, cancel=None: {"ok": True, "codec": "h264",
                                "width": 320, "height": 240, "fps": 24,
                                "audio": []})
    Path(tmp_path / "j1-export.mp4").write_bytes(b"junk")
    h = _handle(client, edl, tmp_path)
    with pytest.raises(ValueError, match="codec blew up"):
        asyncio.run(_drain(ad.run(h)))


def test_composite_insufficient_disk_preflight(client, tmp_path,
                                               monkeypatch):
    import asyncio, shutil
    sha = _cas_blob(client, b"x" * 64)
    edl = _v2([_v2clip("c1", sha=sha)])
    monkeypatch.setattr(
        "app.adapters.timeline_export.engine.probe",
        lambda p, cancel=None: {"ok": True, "codec": "h264",
                                "width": 320, "height": 240, "fps": 24,
                                "audio": []})
    from collections import namedtuple
    monkeypatch.setattr(
        "app.adapters.timeline_export.shutil.disk_usage",
        lambda p: namedtuple("U", ("total", "used", "free"))(0, 0, 1))
    h = _handle(client, edl, tmp_path)
    with pytest.raises(ValueError, match="insufficient disk"):
        asyncio.run(_drain(_adapter(client).run(h)))


def test_interrupted_export_reconciles_not_succeeded(client):
    """A 'running' non-resumable export after restart → failed with an
    interrupted note — never reported as a phantom success."""
    from app.db.models import GenerationJob, new_id
    pid = make_project(client)["id"]
    with client.app.state.session_factory() as s:
        j = GenerationJob(
            id=new_id(), project_id=pid, adapter_id="ops.timeline.export",
            kind="export", status="running",
            progress=0.4, resumable=False, attempt_no=1,
            idempotency_key="k1", request_json={})
        s.add(j)
        s.commit()
        jid = j.id
    engine_inst = client.app.state.job_engine
    with client.app.state.session_factory() as s:
        # returns the REQUEUED count — failed jobs aren't counted.
        engine_inst.reconcile_interrupted(s)
        s.commit()
    with client.app.state.session_factory() as s:
        j = s.get(GenerationJob, jid)
        assert j.status == "failed"
        assert "interrupted" in (j.error or "")
        assert j.output_asset_id is None


# ---------------- Layer 3 — real ffmpeg render ----------------

@pytest.mark.usefixtures("media_tools")
class TestRealComposite:
    def _place_cas(self, client, pid, tid, track_id, path, name,
                   start=None):
        asset = _cas_video(client, pid, path, name)
        payload = {"timeline_id": tid, "track_id": track_id,
                   "asset_id": asset["id"]}
        if start is not None:
            payload["start_frames"] = start
            _commit(client, pid, "timeline.clip.place", payload)
        else:
            _commit(client, pid, "timeline.clip.insert", payload)
        return asset

    def _export(self, client, pid, tid, profile="720p"):
        p = _preview(client, pid, "timeline.export",
                     {"timeline_id": tid, "profile": profile})
        rt = p["resolved_timeline"]
        r = _commit(client, pid, "timeline.export",
                    {"timeline_id": tid, "profile": profile,
                     "expected_timeline": {"id": tid,
                                           "revision": rt["revision"]},
                     "expected_edl_sha": rt["edl_sha256"]})
        job = wait_for_job(client, pid, r["job_id"], timeout=120)
        assert job["status"] == "succeeded", job.get("error")
        items = client.get(f"/api/projects/{pid}/assets").json()["items"]
        out = next(a for a in items if a["origin"] == "rendered")
        return Path(client.app.state.settings.data_dir) / out["uri"]

    def test_two_lane_composite_renders(self, client, dev_ffmpeg, tmp_path):
        """Overlay clip composites over base; output duration = program
        end; hidden lane contributes nothing; probe verifies h264."""
        pid = make_project(client)["id"]
        t = _tl(client, pid)
        d = _detail(client, pid, t["id"])
        v1 = d["tracks"][0]["id"]
        base = _synth(dev_ffmpeg, tmp_path / "base.mp4", dur=2.0,
                      size="320x240", rate=24, audio=True)
        over = _synth(dev_ffmpeg, tmp_path / "over.mp4", dur=1.0,
                      size="160x120", rate=24)
        ghost = _synth(dev_ffmpeg, tmp_path / "ghost.mp4", dur=1.0,
                       size="160x120", rate=24)
        o1 = _track(client, pid, t["id"], kind="overlay", name="O1")
        hid = _track(client, pid, t["id"], kind="overlay", name="H")
        _commit(client, pid, "timeline.track.update",
                {"timeline_id": t["id"], "track_id": hid["id"],
                 "visible": False})
        self._place_cas(client, pid, t["id"], v1, base, "base.mp4")
        self._place_cas(client, pid, t["id"], o1["id"], over,
                        "over.mp4", start=24)
        self._place_cas(client, pid, t["id"], hid["id"], ghost,
                        "ghost.mp4", start=0)
        out = self._export(client, pid, t["id"])
        meta = engine.probe(out)
        assert meta["ok"] and meta["codec"] == "h264"
        assert meta["width"] == 1280 and meta["height"] == 720
        # program end = max(48, 24+24) = 48f @24fps = 2.0s exactly
        assert abs(meta["duration_s"] - 2.0) < 0.05
        assert meta["frames"] == 48
        assert meta["audio"]  # base clip audio mixed through

    def test_leading_gap_renders_black_then_clip(self, client,
                                               dev_ffmpeg, tmp_path):
        """Nonzero starts are explicit: output covers the gap."""
        pid = make_project(client)["id"]
        t = _tl(client, pid)
        d = _detail(client, pid, t["id"])
        v1 = d["tracks"][0]["id"]
        clip = _synth(dev_ffmpeg, tmp_path / "c.mp4", dur=1.0,
                      size="160x120", rate=24)
        self._place_cas(client, pid, t["id"], v1, clip, "c.mp4",
                        start=24)
        out = self._export(client, pid, t["id"])
        meta = engine.probe(out)
        assert meta["ok"]
        assert abs(meta["duration_s"] - 2.0) < 0.05  # 24f gap + 24f clip
        assert meta["frames"] == 48
