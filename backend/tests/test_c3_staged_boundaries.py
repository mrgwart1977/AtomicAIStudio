"""C3 staged export — boundaries, capacity edges, failure points,
and the ≥1000-source real-ffmpeg scale proof.

Layer 1: pure domain edges (200/201 overlap capacity, unicode + long
paths, gaps, single-frame windows, large stage counts).

Layer 3: real-ffmpeg — staged-vs-direct PCM/frame parity across
BOUNDARY-CROSSING fades, crossfades, automation, track automation and
overlapping dialogue; 1000 distinct sources end-to-end with peak
RSS + temp-disk sampling; encode-path preflight refusal.
"""
from __future__ import annotations

import array
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

import pytest

from app.domain.media import binaries, engine, supervise
from app.domain import timeline_export as tx
from app.adapters.timeline_export import TimelineExportAdapter
from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_c5a_timeline import _tl, _detail, _insert
from tests.test_n1a_tracks import _track, _edl
from tests.test_n2a_audio import _place, _atrack
from tests.test_n2d_fades import _fade, _auto, _xf
from tests.test_n3a_track_automation import _tauto
from tests.test_n2e_mixing import _wav_tone, _cas_audio, _out_path
from tests.test_c5b_export import (_synth, _cas_video, _export,
                                   _out_asset, dev_ffmpeg, media_tools)


# ---------------- Layer 1 — capacity + edge cases ----------------

def _vp(cid, s, e):
    return {"clip_id": cid, "sha256": cid, "track_position": 0,
            "muted": False, "start_frame": s, "occ_end_frame": e,
            "tl_start_s": Fraction(s, 24), "tl_end_s": Fraction(e, 24),
            "in_s": Fraction(0), "dur_s": Fraction(e - s, 24),
            "has_audio": False}


def _pl(clips, frames):
    return {"clips": clips, "audio_clips": [],
            "program_frames": Fraction(frames),
            "fps_num": 24, "fps_den": 1}


def test_capacity_exactly_200_overlapping_ok():
    clips = [_vp(f"c{i}", 0, 24) for i in range(200)]
    paths = {f"c{i}": f"/s/{i}.mp4" for i in range(200)}
    assert tx.stage_windows(_pl(clips, 24), paths) == [(0, 24)]


def test_capacity_201_overlapping_refused_before_render():
    clips = [_vp(f"c{i}", 0, 24) for i in range(201)]
    paths = {f"c{i}": f"/s/{i}.mp4" for i in range(201)}
    with pytest.raises(ValueError, match="stage capacity"):
        tx.stage_windows(_pl(clips, 24), paths, max_inputs=200)


def test_stage_windows_gap_renders_inside_window_not_empty_stage():
    # content [0,24) + [72,96): the 48f gap is part of a window (renders
    # black canvas + silence), never an empty stage
    clips = [_vp("a", 0, 24), _vp("b", 72, 96)]
    paths = {"a": "/s/a.mp4", "b": "/s/b.mp4"}
    assert tx.stage_windows(_pl(clips, 96), paths) == [(0, 96)]
    sub = tx.stage_subplan(_pl(clips, 96), 0, 96)
    assert len(sub["clips"]) == 2


def test_stage_windows_single_frame_and_many_stages():
    clips = [_vp(f"c{i}", i, i + 1) for i in range(50)]
    paths = {f"c{i}": f"/s/{i}.mp4" for i in range(50)}
    st = tx.stage_windows(_pl(clips, 50), paths, max_inputs=1)
    assert len(st) == 50 and all(e - s == 1 for s, e in st)


def test_subplan_preserves_noninteger_fps_fields():
    c = _vp("c0", 12, 60)
    c["dur_s"] = Fraction(48 * 1001, 30000)  # extent at 29.97, not 24
    c["tl_start_s"] = Fraction(12 * 1001, 30000)
    c["tl_end_s"] = Fraction(60 * 1001, 30000)
    plan = {"clips": [c],
            "audio_clips": [], "program_frames": Fraction(96),
            "fps_num": 30000, "fps_den": 1001}
    sub = tx.stage_subplan(plan, 24, 96)
    assert sub["fps_num"] == 30000 and sub["fps_den"] == 1001
    assert sub["clips"][0]["in_s"] == Fraction(12 * 1001, 30000)


def test_concat_list_unicode_and_quote_escape():
    out = tx.concat_list(["/s/üñíçødé.mp4", "/s/o'brien.mp4"])
    assert "üñíçødé" in out and "o'\\''brien" in out


def test_stage_argv_handles_unicode_and_long_paths(tmp_path):
    long_p = "/s/" + "/" * 8 + ".mp4"
    plan = _pl([_vp("c0", 0, 24)], 24)
    canvas = {"width": 64, "height": 48, "fps_num": 24, "fps_den": 1}
    argv, _g = tx.build_stage_argv("ff", plan, {"c0": long_p}, canvas,
                                   str(tmp_path / "s.mkv"),
                                   str(tmp_path / "g.txt"))
    assert long_p in argv  # argv array — no quoting needed at any length


# ---------------- Layer 3 — boundary-crossing parity ----------------

def _pcm(path, ffmpeg):
    r = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path),
         "-f", "f32le", "-ac", "2", "-ar", "48000", "-"],
        capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return array.array("f", r.stdout)


def _pcm_parity(d_out, s_out, ffmpeg, peak=0.05, rms=0.005):
    dm, sm = engine.probe(d_out), engine.probe(s_out)
    assert dm["frames"] == sm["frames"], (
        f"frame count {dm['frames']} != {sm['frames']}")
    assert abs(dm["duration_s"] - sm["duration_s"]) < 0.1
    a, b = _pcm(d_out, ffmpeg), _pcm(s_out, ffmpeg)
    n = min(len(a), len(b))
    step = 37
    diffs = [abs(a[i] - b[i]) for i in range(0, n, step)]
    pk, rs = max(diffs), (sum(d * d for d in diffs) / len(diffs)) ** 0.5
    assert pk < peak and rs < rms, f"pcm parity peak={pk:.4f} rms={rs:.5f}"
    return pk, rs


@pytest.mark.usefixtures("media_tools")
def test_staged_fades_automation_xf_overlap_parity(
        client, dev_ffmpeg, tmp_path, monkeypatch):
    """Dedicated audio lanes whose envelopes CROSS stage boundaries:
    clip A spans [0,192) with fade-out + automation + xf into B;
    short clips on A2 at 24-30/48-54/72-78 force windows at 36/72 —
    the fade, the xf window, and automation all straddle cuts."""
    pid = make_project(client)["id"]
    t = _tl(client, pid, fps_num=24, fps_den=1)
    a1 = _atrack(client, pid, t["id"], "A1")
    a2 = _atrack(client, pid, t["id"], "A2")

    A = _cas_audio(client, pid,
                   _wav_tone(tmp_path / "a.wav", seconds=4.0,
                             freq=220.0), "a.wav")
    _place(client, pid, t["id"], A, a1["id"], at=0)
    ca = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": ca["id"],
             "out_frame": 96000})           # 48f occupancy, tail spare
    _fade(client, pid, t["id"], ca["id"], fade_in_units=4000,
          fade_out_units=24000, fade_out_curve="equal_power")
    _auto(client, pid, t["id"], ca["id"],
          [{"at_frames": 0, "gain_db": 0},
           {"at_frames": 36, "gain_db": -6}])
    _tauto(client, pid, t["id"], a1["id"],
           [{"at_tl_frames": 0, "gain_db": 0},
            {"at_tl_frames": 72, "gain_db": -3}])

    B = _cas_audio(client, pid,
                   _wav_tone(tmp_path / "b.wav", seconds=2.0,
                             freq=330.0), "b.wav")
    _place(client, pid, t["id"], B, a1["id"], at=48)
    cb = next(c for c in _detail(client, pid, t["id"])["clips"]
              if c["id"] != ca["id"])
    _commit(client, pid, "timeline.clip.trim",
            {"timeline_id": t["id"], "clip_id": cb["id"],
             "in_frame": 8000})            # 4f head handle for the xf
    # satisfied xf [44,52): the stage cut at 48 lands mid-crossfade
    _xf(client, pid, t["id"], out_clip_id=ca["id"],
        in_clip_id=cb["id"], tl_start=44, tl_frames=8,
        curve="equal_power")

    # s0 at 30 → stage boundary at 36 splits A's fade_out [36,48)
    for i, (at, fr) in enumerate([(30, 440.0), (48, 550.0), (72, 660.0)]):
        s = _cas_audio(client, pid,
                       _wav_tone(tmp_path / f"s{i}.wav", seconds=0.25,
                                 freq=fr), f"s{i}.wav")
        _place(client, pid, t["id"], s, a2["id"], at=at)

    direct = _export(client, pid, t["id"], profile="source")
    assert direct["status"] == "succeeded", direct.get("error")
    monkeypatch.setattr(tx, "STAGE_MAX_INPUTS", 2)
    staged = _export(client, pid, t["id"], profile="source")
    assert staged["status"] == "succeeded", staged.get("error")
    pk, rs = _pcm_parity(_cas_path(client, pid, direct),
                         _cas_path(client, pid, staged), dev_ffmpeg)
    print(f"\nboundary parity: peak={pk:.4f} rms={rs:.5f}")


@pytest.mark.usefixtures("media_tools")
def test_staged_parity_noninteger_fps(client, dev_ffmpeg, tmp_path,
                                      monkeypatch):
    """30000/1001 timeline — tl frame = 1601.6 samples; the ±1-sample
    boundary re-anchor tolerance applies but stays far inside bounds."""
    pid = make_project(client)["id"]
    t = _tl(client, pid, fps_num=30000, fps_den=1001)
    a1 = _atrack(client, pid, t["id"], "A1")
    a2 = _atrack(client, pid, t["id"], "A2")
    A = _cas_audio(client, pid,
                   _wav_tone(tmp_path / "a.wav", seconds=4.0,
                             freq=220.0), "a.wav")
    _place(client, pid, t["id"], A, a1["id"], at=0)
    ca = _detail(client, pid, t["id"])["clips"][0]
    _fade(client, pid, t["id"], ca["id"], fade_out_units=24000)
    for i, (at, fr) in enumerate([(30, 440.0), (70, 550.0),
                                  (90, 660.0)]):
        s = _cas_audio(client, pid,
                       _wav_tone(tmp_path / f"s{i}.wav", seconds=0.5,
                                 freq=fr), f"s{i}.wav")
        _place(client, pid, t["id"], s, a2["id"], at=at)
    direct = _export(client, pid, t["id"], profile="source")
    assert direct["status"] == "succeeded", direct.get("error")
    monkeypatch.setattr(tx, "STAGE_MAX_INPUTS", 2)
    staged = _export(client, pid, t["id"], profile="source")
    assert staged["status"] == "succeeded", staged.get("error")
    # 29.97: allow the documented ±1-sample boundary re-anchor slack
    _pcm_parity(_cas_path(client, pid, direct),
                _cas_path(client, pid, staged),
                dev_ffmpeg, peak=0.08, rms=0.008)


def _cas_path(client, pid, job):
    from app.db.models import AssetFile
    _a, v, _j = _out_asset(client, pid, job)
    with client.app.state.session_factory() as s:
        return client.app.state.storage.file_path(
            s, s.get(AssetFile, v.sha256))


# ---------------- Layer 3 — failure at every point ----------------

def _stageable_timeline(client, pid, t, tmp_path, dev_ffmpeg):
    _track(client, pid, t["id"], kind="video", name="V2")
    d = _detail(client, pid, t["id"])
    v1 = next(x for x in d["tracks"] if x["name"] == "V1")
    v2 = next(x for x in d["tracks"] if x["name"] == "V2")
    made = []
    for i in range(4):
        src = _synth(dev_ffmpeg, tmp_path / f"c{i}.mp4", dur=1.0,
                     size=f"{64 + i * 2}x48", rate=24)
        made.append(_cas_video(client, pid, src, f"c{i}"))
        _place(client, pid, t["id"], made[-1], v1["id"], at=i * 24)
    src = _synth(dev_ffmpeg, tmp_path / "x.mp4", dur=1.0, size="32x32",
                 rate=24)
    made.append(_cas_video(client, pid, src, "x"))
    _place(client, pid, t["id"], made[-1], v2["id"], at=0)
    return made


@pytest.mark.usefixtures("media_tools")
def test_staged_failure_at_final_concat_publishes_nothing(
        client, dev_ffmpeg, tmp_path, monkeypatch):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _stageable_timeline(client, pid, t, tmp_path, dev_ffmpeg)
    real = supervise.run

    def fail_final(argv, **kw):
        if argv[-1].endswith("-export.mp4"):
            return {"exit": 1, "timed_out": False, "cancelled": False,
                    "output_tail": "injected final-encode failure",
                    "elapsed_s": 0.0}
        return real(argv, **kw)

    monkeypatch.setattr(supervise, "run", fail_final)
    monkeypatch.setattr(tx, "STAGE_MAX_INPUTS", 2)
    job = _export(client, pid, t["id"], profile="source")
    assert job["status"] == "failed"
    a, _v, j = _out_asset(client, pid, job)
    assert a is None or j.output_asset_id is None


@pytest.mark.usefixtures("media_tools")
def test_staged_cancel_before_first_stage_publishes_nothing(
        client, dev_ffmpeg, tmp_path, monkeypatch):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _stageable_timeline(client, pid, t, tmp_path, dev_ffmpeg)
    calls = {"n": 0}
    real = supervise.run

    def cancel_first(argv, **kw):
        if str(argv[-1]).endswith(".mkv"):
            calls["n"] += 1
            return {"exit": None, "timed_out": False, "cancelled": True,
                    "output_tail": "", "elapsed_s": 0.0}
        return real(argv, **kw)

    monkeypatch.setattr(supervise, "run", cancel_first)
    monkeypatch.setattr(tx, "STAGE_MAX_INPUTS", 2)
    job = _export(client, pid, t["id"], profile="source")
    assert job["status"] in ("cancelled", "failed")
    a, _v, _j = _out_asset(client, pid, job)
    assert a is None
    assert calls["n"] == 1  # never reached stage 2 / final encode


@pytest.mark.usefixtures("media_tools")
def test_staged_missing_source_fails_before_render(
        client, dev_ffmpeg, tmp_path, monkeypatch):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    made = _stageable_timeline(client, pid, t, tmp_path, dev_ffmpeg)
    from app.db.models import AssetFile
    with client.app.state.session_factory() as s:
        af = s.get(AssetFile, made[0]["sha256"])
        p = client.app.state.storage.file_path(s, af)
    p.unlink()
    calls = {"n": 0}
    real = supervise.run

    def count(argv, **kw):
        calls["n"] += 1
        return real(argv, **kw)

    monkeypatch.setattr(supervise, "run", count)
    monkeypatch.setattr(tx, "STAGE_MAX_INPUTS", 2)
    job = _export(client, pid, t["id"], profile="source")
    assert job["status"] == "failed"
    assert "unavailable" in (job.get("error") or "") or \
        "missing" in (job.get("error") or "")
    assert calls["n"] == 0


@pytest.mark.usefixtures("media_tools")
def test_staged_corrupt_source_fails_before_render(
        client, dev_ffmpeg, tmp_path, monkeypatch):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    made = _stageable_timeline(client, pid, t, tmp_path, dev_ffmpeg)
    # corrupt one pinned CAS blob — sha check refuses before any
    # ffmpeg invocation
    from app.db.models import AssetFile
    with client.app.state.session_factory() as s:
        af = s.get(AssetFile, made[0]["sha256"])
        p = client.app.state.storage.file_path(s, af)
    p.write_bytes(b"corrupted-not-media")
    calls = {"n": 0}
    real = supervise.run

    def count(argv, **kw):
        calls["n"] += 1
        return real(argv, **kw)

    monkeypatch.setattr(supervise, "run", count)
    monkeypatch.setattr(tx, "STAGE_MAX_INPUTS", 2)
    job = _export(client, pid, t["id"], profile="source")
    assert job["status"] == "failed"
    assert "pinned sha256" in (job.get("error") or "")
    assert calls["n"] == 0


# ---------------- Layer 3 — F6 encode-path argv preflight -----------

@pytest.mark.usefixtures("media_tools")
def test_encode_path_preflight_refuses_argv_overflow(
        client, tmp_path, monkeypatch):
    """v1 single-pass encode: N unique sources → N -i args → refuse
    BEFORE rendering once argv would exceed the Windows bound."""
    import asyncio
    import hashlib
    pid = make_project(client)["id"]
    data_dir = Path(client.app.state.settings.data_dir)
    from app.db.models import AssetFile, StorageLocation

    n, shas = 450, []
    with client.app.state.session_factory() as s:
        loc = s.query(StorageLocation).first()
        for i in range(n):
            blob = b"fake-media-" + str(i).encode()
            sha = hashlib.sha256(blob).hexdigest()
            rel = Path("blobs") / sha[:2] / sha
            dest = data_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                dest.write_bytes(blob)
            if s.get(AssetFile, sha) is None:
                s.add(AssetFile(sha256=sha,
                                storage_location_id=loc.id if loc
                                else None,
                                rel_path=str(rel).replace("\\", "/"),
                                byte_size=len(blob), status="ready"))
            shas.append(sha)
        s.commit()

    tf = {"num": 24, "den": 1}
    edl = {"format": "ade-edl/1",
           "clips": [{"clip_id": f"c{i}", "sha256": shas[i],
                      "in_frame": 0, "out_frame": 24,
                      "start_frames": i * 24, "timeline_frames": tf,
                      "source": {"fps_num": 24, "fps_den": 1,
                                 "width": 640, "height": 480}}
                     for i in range(n)],
           "canvas": {"width": 64, "height": 48,
                      "fps_num": 24, "fps_den": 1},
           "total_duration_s": n}
    fake_meta = {"ok": True, "codec": "h264", "width": 640,
                 "height": 480, "fps": 24.0, "duration_s": 1.0,
                 "frames": 24, "audio": False}
    _i = {"n": 0}

    def fake_probe(*a, **k):
        _i["n"] += 1
        # varying dims → byte-incompatible → encode path (not copy)
        return {**fake_meta, "width": 640 + (_i["n"] % 2) * 32}

    monkeypatch.setattr(engine, "probe", fake_probe)
    ran: list[str] = []
    real = supervise.run

    def count(argv, **kw):
        ran.append(str(argv[-1]))
        return real(argv, **kw)

    monkeypatch.setattr(supervise, "run", count)
    from app.adapters.base import JobHandle
    handle = JobHandle(job_id="c3-f6", project_id=pid,
                       request={"edl": edl, "profile": "source",
                                "timeline_name": "big"},
                       work_dir=str(tmp_path / "work"))
    adapter = TimelineExportAdapter(
        client.app.state.session_factory, client.app.state.storage,
        client.app.state.settings)

    async def _run():
        async for _e in adapter.run(handle):
            pass

    with pytest.raises(ValueError, match="single-pass encode bound"):
        asyncio.run(_run())
    # only encoder-detection probes ran — the render never started
    assert not any(t.endswith("-export.mp4") for t in ran)


# ---------------- Layer 3 — the 1000-source export (C3-F2) --------

def _distinct_mp4s(ffmpeg, out_dir: Path, n: int) -> list[Path]:
    """n byte-distinct tiny mp4s — distinct lavfi colors → distinct
    sha → distinct CAS objects → distinct -i inputs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # Detect the encoder the resolved binary actually ships — the
    # bundled CI build has libopenh264, full Gyan builds have libx264.
    enc = tx.h264_encoder(ffmpeg, supervise.run)

    def make(i):
        p = out_dir / f"s{i}.mp4"
        # 16 levels/channel spaced ≥16 apart — survives yuv420p chroma
        # quantization; 4096 distinct colors, bijective for i < 4096
        r8, g8, b8 = (i % 16) * 16 + 8, ((i // 16) % 16) * 16 + 8, \
            ((i // 256) % 16) * 16 + 8
        r = subprocess.run(
            [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
             f"color=c=#{r8:02x}{g8:02x}{b8:02x}:s=32x32:r=24:d=0.25",
             "-c:v", enc, "-pix_fmt", "yuv420p", str(p)],
            capture_output=True, timeout=60)
        assert r.returncode == 0, r.stderr
        return p
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(make, range(n)))


@pytest.mark.usefixtures("media_tools")
def test_export_1000_distinct_sources_real(client, dev_ffmpeg,
                                           tmp_path, monkeypatch):
    """C3-F2: a REAL completed export with 1000 distinct sources driven
    through the adapter's staged path — argv never overflows, every
    source contributes (frame-color spot checks), ffprobe-validated,
    peak ffmpeg RSS + peak temp disk sampled."""
    import asyncio
    import hashlib
    import psutil
    pid = make_project(client)["id"]
    n = 1000
    data_dir = Path(client.app.state.settings.data_dir)
    files = _distinct_mp4s(dev_ffmpeg, tmp_path / "srcs", n)

    # Seed AssetFile rows directly — CAS layout identical to ingestion
    from app.db.models import AssetFile, StorageLocation
    shas = []
    with client.app.state.session_factory() as s:
        loc = s.query(StorageLocation).first()
        for f in files:
            sha = hashlib.sha256(f.read_bytes()).hexdigest()
            rel = Path("blobs") / sha[:2] / sha
            dest = data_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if not dest.exists():
                shutil.copy2(f, dest)
            if s.get(AssetFile, sha) is None:
                s.add(AssetFile(sha256=sha,
                                storage_location_id=loc.id if loc
                                else None,
                                rel_path=str(rel).replace("\\", "/"),
                                byte_size=f.stat().st_size,
                                status="ready"))
            shas.append(sha)
        s.commit()
    assert len(set(shas)) == n, "synthetic sources collapsed — not distinct"

    # v2 EDL: 500 unique clips on V1 at frames 0-499 + 500 on V2 at
    # 495-994 (5f overlap → real multitrack compositing); each clip
    # occupies ONE tl frame (timeline_frames is the occupancy, NOT the
    # fps) → program = 995f @24fps ≈ 41.5s
    tf = {"num": 1, "den": 1}
    clips = []
    for i, sha in enumerate(shas):
        lane, at = ("v1", i) if i < 500 else ("v2", i - 5)
        clips.append({"clip_id": f"c{i}", "sha256": sha,
                      "track_id": lane, "in_frame": 0, "out_frame": 1,
                      "start_frames": at, "timeline_frames": tf,
                      "source": {"fps_num": 24, "fps_den": 1}})
    edl = {"format": "ade-edl/2", "clips": clips,
           "tracks": [
               {"id": "v1", "kind": "video", "name": "V1",
                "position": 0, "visible": True},
               {"id": "v2", "kind": "video", "name": "V2",
                "position": 1, "visible": True}],
           "canvas": {"width": 32, "height": 32,
                      "fps_num": 24, "fps_den": 1},
           "total_duration_s": 995 / 24}
    work = tmp_path / "work"
    from app.adapters.base import JobHandle
    handle = JobHandle(job_id="c3-f2", project_id=pid,
                       request={"edl": edl, "profile": "source",
                                "timeline_name": "scale"},
                       work_dir=str(work))
    adapter = TimelineExportAdapter(
        client.app.state.session_factory, client.app.state.storage,
        client.app.state.settings)

    stats = {"rss": 0, "disk": 0, "done": False}

    def sampler():
        # process_iter is ~50-100ms on Windows — sample at 2 Hz so the
        # monitor thread never starves the export's probe/thread pool
        while not stats["done"]:
            try:
                rss = 0
                for p in psutil.process_iter(["name"]):
                    try:
                        if p.info["name"] and \
                                "ffmpeg" in p.info["name"].lower():
                            rss += p.memory_info().rss
                    except (psutil.NoSuchProcess,
                            psutil.AccessDenied):
                        continue
                stats["rss"] = max(stats["rss"], rss)
                if work.is_dir():
                    stats["disk"] = max(
                        stats["disk"],
                        sum(f.stat().st_size for f in work.rglob("*")
                            if f.is_file()))
            except Exception:
                pass
            time.sleep(0.5)

    th = threading.Thread(target=sampler, daemon=True)
    th.start()

    # Probe metadata stubbed: every synthetic source is 32x32 h264,
    # 24fps, 6 frames — identical. Per-source ffprobe is a real measured
    # bottleneck on Windows (~1.3s spawn each → ~20min serial, ~8min at
    # 8-way; recorded in docs/c3-evidence) — stubbing it keeps the
    # RENDER path 100% real while making the test re-runnable.
    fake = {"ok": True, "codec": "h264", "width": 32, "height": 32,
            "fps": 24.0, "duration_s": 0.25, "frames": 6,
            "audio": False, "container": "mp4"}
    real_probe = engine.probe
    monkeypatch.setattr(engine, "probe", lambda *a, **k: dict(fake))

    async def _run():
        ev = None
        async for e in adapter.run(handle):
            ev = e
        return ev

    t0 = time.perf_counter()
    final = asyncio.run(_run())
    elapsed = time.perf_counter() - t0
    stats["done"] = True
    th.join(timeout=5)

    out = Path(final["output"]["path"])
    assert out.is_file() and out.stat().st_size > 0
    meta = real_probe(out)
    assert meta["ok"] and meta["codec"] == "h264"
    assert meta["frames"] == 995
    assert abs(meta["duration_s"] - 995 / 24) < 0.2
    assert meta["fps"] == 24.0

    # contribution spot check: frame f<495 shows V1 clip f; f>=495
    # shows V2 clip 500+f-495 (top lane in the 495-499 overlap, sole
    # lane after 500)
    def want_idx(f):
        return f if f < 495 else f + 5

    for f in (0, 123, 250, 494, 497, 700, 994):
        r = subprocess.run(
            [dev_ffmpeg, "-v", "error", "-i", str(out),
             "-vf", f"select=eq(n\\,{f})", "-frames:v", "1",
             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, timeout=60)
        assert r.returncode == 0 and len(r.stdout) == 32 * 32 * 3
        px = r.stdout
        src = want_idx(f)
        want = ((src % 16) * 16 + 8, ((src // 16) % 16) * 16 + 8,
                ((src // 256) % 16) * 16 + 8)
        got = (px[0], px[1], px[2])
        assert all(abs(a - b) < 40 for a, b in zip(want, got)), \
            f"frame {f}: got {got}, want ~{want}"

    leftovers = [p.name for p in work.rglob("*") if p.is_file()
                 and (p.suffix == ".mkv" or "concat" in p.name
                      or p.name.startswith("g") and p.suffix == ".txt")]
    assert leftovers == [], f"intermediates leaked: {leftovers[:5]}"
    print(f"\n1000-source export: elapsed={elapsed:.1f}s "
          f"peak_ffmpeg_rss={stats['rss'] / 1e6:.0f}MB "
          f"peak_work_disk={stats['disk'] / 1e6:.1f}MB "
          f"frames={meta['frames']} dur={meta['duration_s']:.2f}s")
