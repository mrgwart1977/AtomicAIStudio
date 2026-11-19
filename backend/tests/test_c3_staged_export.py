"""C3 staged submix export — bounded multi-stage rendering.

Layer 1: stage-window partitioning, subplan rebasing (every envelope
anchor is stream-local so a cut shifts it by the trimmed prefix), and
argv bounds that hold at ANY source count.

Layer 3: real-ffmpeg parity — a multitrack timeline rendered staged
(forced via STAGE_MAX_INPUTS) must match the single-pass export, and
stage/final failures must publish nothing and clean intermediates.
"""
from __future__ import annotations

import array
import subprocess
from fractions import Fraction

import pytest

from app.domain.media import binaries, engine, supervise
from app.domain import timeline_export as tx
from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_c5a_timeline import _tl, _detail
from tests.test_n1a_tracks import _track, _edl
from tests.test_n2a_audio import _place
from tests.test_c5b_export import (_synth, _cas_video, _export,
                                   _out_asset, dev_ffmpeg, media_tools)


# ---------------- Layer 1 — pure domain ----------------

def _vclip(cid, start, occ_end, in_s=0.0, dur_s=1.0, pos=0):
    return {"clip_id": cid, "sha256": cid, "track_position": pos,
            "muted": False, "start_frame": start,
            "occ_end_frame": occ_end,
            "tl_start_s": Fraction(start, 24),
            "tl_end_s": Fraction(occ_end, 24),
            "in_s": Fraction(in_s).limit_denominator(10**6),
            "dur_s": Fraction(dur_s).limit_denominator(10**6),
            "has_audio": False}


def _aclip(cid, start_tl, extent_tl, *, head_tl=0, rate=48000,
           seg_start=0, head_src=0, xf=None, track_auto=None):
    seg_len = head_src + int(extent_tl * rate / 24)
    return {"clip_id": cid, "sha256": cid, "embedded": False,
            "rate": rate,
            "seg_start_src": seg_start, "seg_end_src": seg_start + seg_len,
            "extent_src": int(extent_tl * rate / 24),
            "start_tl": Fraction(start_tl), "extent_tl": Fraction(extent_tl),
            "head_tl": Fraction(head_tl), "head_src": head_src,
            "tail_src": 0, "clip_db": 0.0, "track_db": 0.0,
            "fade_in": {"n": 0, "curve": "linear"},
            "fade_out": {"n": 0, "curve": "linear"},
            "automation": [], "automation_interp": "linear",
            "track_auto": track_auto or [],
            "track_auto_interp": "linear", "xf": xf}


def _plan(clips, audio=None, frames=96):
    return {"clips": clips, "audio_clips": audio or [],
            "program_frames": Fraction(frames), "fps_num": 24,
            "fps_den": 1}


def test_stage_windows_partition_respects_budget():
    clips = [_vclip(f"c{i}", i * 24, (i + 1) * 24) for i in range(4)]
    paths = {f"c{i}": f"/src/{i}.mp4" for i in range(4)}
    assert tx.stage_windows(_plan(clips), paths, max_inputs=2) == \
        [(0, 48), (48, 96)]


def test_stage_windows_single_when_under_budget():
    clips = [_vclip(f"c{i}", i * 24, (i + 1) * 24) for i in range(4)]
    paths = {f"c{i}": f"/src/{i}.mp4" for i in range(4)}
    assert tx.stage_windows(_plan(clips), paths, max_inputs=200) == \
        [(0, 96)]


def test_stage_windows_overlap_capacity_fails_closed():
    clips = [_vclip(f"c{i}", 0, 24) for i in range(3)]
    paths = {f"c{i}": f"/src/{i}.mp4" for i in range(3)}
    with pytest.raises(ValueError, match="stage capacity"):
        tx.stage_windows(_plan(clips), paths, max_inputs=2)


def test_stage_subplan_video_rebases_cut_clip():
    # clip spans [24, 72); window [36, 96): skip 12 tl frames of source
    c = _vclip("c0", 24, 72, in_s=0.0, dur_s=2.0)
    sub = tx.stage_subplan(_plan([c]), 36, 96)
    nc = sub["clips"][0]
    assert nc["start_frame"] == 0 and nc["occ_end_frame"] == 36
    assert nc["in_s"] == Fraction(1, 2)      # 12f / 24fps
    assert nc["dur_s"] == Fraction(3, 2)
    assert nc["tl_start_s"] == 0 and nc["tl_end_s"] == Fraction(3, 2)


def test_stage_subplan_audio_stream_anchors_shift_by_skip():
    # stream covers [24, 120) tl (24f head borrow + 72f occupancy);
    # window [60, 120): skip_tl = 36f → 72000 src samples @48k/24fps
    a = _aclip("a0", start_tl=48, extent_tl=72, head_tl=24,
               head_src=48000, xf={"role": "in", "curve": "tri",
                                   "off": 0, "n": 96000},
               track_auto=[{"at_tl": Fraction(72), "gain_db": -6.0}])
    sub = tx.stage_subplan(_plan([], [a], frames=120), 60, 120)
    na = sub["audio_clips"][0]
    assert na["seg_start_src"] == 72000
    assert na["seg_end_src"] == 72000 + 120000  # take 60f = 120000 smp
    assert na["head_src"] == 48000 - 72000
    assert na["head_tl"] == Fraction(24) - 36
    assert na["start_tl"] == Fraction(-12)
    assert na["xf"]["off"] == -72000
    assert na["track_auto"][0]["at_tl"] == 12


def test_stage_subplan_drops_outside_and_keeps_extentsrc():
    a = _aclip("a0", start_tl=96, extent_tl=24)
    sub = tx.stage_subplan(_plan([], [a], frames=120), 0, 48)
    assert sub["audio_clips"] == []
    sub = tx.stage_subplan(_plan([], [a], frames=120), 96, 120)
    # tail uncut → extent_src stays full so fade_out anchoring is exact
    assert sub["audio_clips"][0]["extent_src"] == a["extent_src"]


def test_stage_subplan_embedded_rebases_seconds():
    a = {"clip_id": "v0", "sha256": "v0", "embedded": True,
         "in_s": Fraction(0), "dur_s": Fraction(2),
         "tl_start_s": Fraction(2), "tl_end_s": Fraction(4),
         "start_tl": Fraction(48), "track_db": 0.0, "clip_db": 0.0,
         "automation": [], "xf": None, "fade_in": None, "fade_out": None,
         "track_auto": [{"at_tl": Fraction(60), "gain_db": -3.0}],
         "track_auto_interp": "linear"}
    sub = tx.stage_subplan(_plan([], [a], frames=96), 48, 96)
    na = sub["audio_clips"][0]
    assert na["start_tl"] == 0 and na["tl_start_s"] == 0
    assert na["in_s"] == 0 and na["dur_s"] == 2
    assert na["track_auto"][0]["at_tl"] == 12


def test_build_stage_argv_lossless_no_limiter(tmp_path):
    paths = {"c0": "/src/0.mp4"}
    plan = _plan([_vclip("c0", 0, 24)], frames=24)
    canvas = {"width": 64, "height": 48, "fps_num": 24, "fps_den": 1}
    argv, graph = tx.build_stage_argv(
        "ff", plan, paths, canvas, str(tmp_path / "s0.mkv"),
        str(tmp_path / "g.txt"))
    assert "ffv1" in argv and "alimiter" not in graph
    assert argv[-1].endswith("s0.mkv")


def test_build_final_argv_single_limiter_and_bounded():
    argv = tx.build_final_argv("/ff", "/w/list.txt", 4.0, "/w/out.mp4",
                               "libx264", 23, has_audio=True)
    assert "concat" in argv and "-safe" in argv
    assert "alimiter=limit=0.95" in argv[argv.index("-filter_complex") + 1]
    assert "160k" in argv and "-t" in argv


def test_stage_argv_bounded_at_900_sources(tmp_path):
    # argv length is the real ceiling — prove per-stage boundedness at
    # a source count the single-pass path cannot express (~32k chars).
    n = 900
    clips = [_vclip(f"c{i}", i, i + 1) for i in range(n)]
    paths = {f"c{i}": f"/very/long/cas/blob/path/{i:064d}.mp4"
             for i in range(n)}
    plan = _plan(clips, frames=n)
    canvas = {"width": 64, "height": 48, "fps_num": 24, "fps_den": 1}
    stages = tx.stage_windows(plan, paths, max_inputs=200)
    assert len(stages) >= 5
    for si, (F, M) in enumerate(stages):
        argv, _g = tx.build_stage_argv(
            "ff", tx.stage_subplan(plan, F, M), paths, canvas,
            str(tmp_path / f"s{si}.mkv"), str(tmp_path / f"g{si}.txt"))
        assert sum(len(a) + 1 for a in argv) < 32767
        assert argv.count("-i") <= 200


# ---------------- Layer 3 — real ffmpeg end-to-end ----------------

def _pcm(path, ffmpeg):
    r = subprocess.run(
        [ffmpeg, "-v", "error", "-i", str(path),
         "-f", "f32le", "-ac", "2", "-ar", "48000", "-"],
        capture_output=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return array.array("f", r.stdout)


def _cas_out(client, job):
    from app.db.models import AssetFile
    _a, v, _j = _out_asset(client, job["project_id"], job)
    with client.app.state.session_factory() as s:
        return client.app.state.storage.file_path(s, s.get(AssetFile,
                                                           v.sha256))


@pytest.mark.usefixtures("media_tools")
def test_staged_export_parity_with_direct(client, dev_ffmpeg, tmp_path,
                                          monkeypatch):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v2 = _track(client, pid, t["id"], kind="video", name="V2")
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["name"] == "V1")
    for i in range(4):
        # distinct pixels per clip — identical bytes would collapse to
        # one CAS object and one FFmpeg input
        src = _synth(dev_ffmpeg, tmp_path / f"c{i}.mp4", dur=1.0,
                     size=f"{64 + i * 2}x48", rate=24, audio=True)
        _place(client, pid, t["id"],
               _cas_video(client, pid, src, f"c{i}"),
               v1["id"], at=i * 24)
    o = _synth(dev_ffmpeg, tmp_path / "o.mp4", dur=1.0, size="32x32",
               rate=24, audio=True)
    _place(client, pid, t["id"], _cas_video(client, pid, o, "o"),
           v2["id"], at=12)

    direct = _export(client, pid, t["id"], profile="source")
    assert direct["status"] == "succeeded", direct.get("error")
    monkeypatch.setattr(tx, "STAGE_MAX_INPUTS", 2)
    staged = _export(client, pid, t["id"], profile="source")
    assert staged["status"] == "succeeded", staged.get("error")

    d_out, s_out = _cas_out(client, direct), _cas_out(client, staged)
    dm, sm = engine.probe(d_out), engine.probe(s_out)
    assert dm["frames"] == sm["frames"]
    assert abs(dm["duration_s"] - sm["duration_s"]) < 0.1
    a, b = _pcm(d_out, dev_ffmpeg), _pcm(s_out, dev_ffmpeg)
    n = min(len(a), len(b))
    peak = max(abs(a[i] - b[i]) for i in range(0, n, 97))
    rms = (sum((a[i] - b[i]) ** 2 for i in range(0, n, 97))
           / (n // 97)) ** 0.5
    # staged omits the limiter inside windows; boundary state can
    # differ ≤1 sample — far inside these tolerances.
    assert peak < 0.05 and rms < 0.005


@pytest.mark.usefixtures("media_tools")
def test_staged_export_stage_failure_publishes_nothing(
        client, dev_ffmpeg, tmp_path, monkeypatch):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _track(client, pid, t["id"], kind="video", name="V2")
    d = _detail(client, pid, t["id"])
    v1 = next(x for x in d["tracks"] if x["name"] == "V1")
    v2 = next(x for x in d["tracks"] if x["name"] == "V2")
    for i in range(4):
        src = _synth(dev_ffmpeg, tmp_path / f"c{i}.mp4", dur=1.0,
                     size=f"{64 + i * 2}x48", rate=24)
        _place(client, pid, t["id"],
               _cas_video(client, pid, src, f"c{i}"),
               v1["id"], at=i * 24)
    src = _synth(dev_ffmpeg, tmp_path / "x.mp4", dur=1.0, size="32x32",
                 rate=24)
    _place(client, pid, t["id"], _cas_video(client, pid, src, "x"),
           v2["id"], at=0)

    real_run = supervise.run
    calls = {"n": 0}

    def flaky(argv, **kw):
        calls["n"] += 1
        if argv[-1].endswith("stage1.mkv"):
            return {"exit": 1, "timed_out": False, "cancelled": False,
                    "output_tail": "injected stage failure",
                    "elapsed_s": 0.0}
        return real_run(argv, **kw)

    monkeypatch.setattr(supervise, "run", flaky)
    monkeypatch.setattr(tx, "STAGE_MAX_INPUTS", 2)
    job = _export(client, pid, t["id"], profile="source")
    assert job["status"] == "failed"
    assert "stage 1" in (job.get("error") or "")
    a, _v, _j = _out_asset(client, pid, job)
    assert a is None or _j.output_asset_id is None


@pytest.mark.usefixtures("media_tools")
def test_staged_export_cancel_at_stage_boundary(
        client, dev_ffmpeg, tmp_path, monkeypatch):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    _track(client, pid, t["id"], kind="video", name="V2")
    d = _detail(client, pid, t["id"])
    v1 = next(x for x in d["tracks"] if x["name"] == "V1")
    for i in range(4):
        src = _synth(dev_ffmpeg, tmp_path / f"c{i}.mp4", dur=1.0,
                     size=f"{64 + i * 2}x48", rate=24)
        _place(client, pid, t["id"],
               _cas_video(client, pid, src, f"c{i}"),
               v1["id"], at=i * 24)
    src = _synth(dev_ffmpeg, tmp_path / "x.mp4", dur=1.0, size="32x32",
                 rate=24)
    _place(client, pid, t["id"], _cas_video(client, pid, src, "x"),
           next(x for x in d["tracks"] if x["name"] == "V2")["id"],
           at=0)

    real_run = supervise.run

    def cancelling(argv, cancel=None, **kw):
        if argv[-1].endswith("stage1.mkv") and cancel is not None:
            cancel.set()
            return {"exit": -1, "timed_out": False, "cancelled": True,
                    "output_tail": "", "elapsed_s": 0.0}
        return real_run(argv, cancel=cancel, **kw)

    monkeypatch.setattr(supervise, "run", cancelling)
    monkeypatch.setattr(tx, "STAGE_MAX_INPUTS", 2)
    job = _export(client, pid, t["id"], profile="source")
    assert job["status"] in ("cancelled", "failed")
    a, _v, _j = _out_asset(client, pid, job)
    assert a is None or _j.output_asset_id is None
