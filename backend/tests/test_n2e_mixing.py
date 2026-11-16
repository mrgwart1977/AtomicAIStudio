"""N2-E — audio mixing & export.

Two layers. Plan-unit tests pin the pure render semantics: audibility
(visible + mute/solo set), crossfade handle extension, fail-closed
refusals, and the deterministic argv shape (source-unit atrim, afade
anchors, aeval automation, resample-last, amix normalize=0).

Real-ffmpeg tests render actual WAV sources through the export job and
measure decoded PCM — onset positions, RMS windows, per-frequency
magnitudes — instead of asserting filter-graph strings. They skip
cleanly when ffmpeg/ffprobe are unresolvable.
"""
import array
import hashlib
import math
import shutil
import struct
from fractions import Fraction
from pathlib import Path

import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit, _preview
from tests.test_c5a_timeline import _tl, _detail, _insert
from tests.test_c5b_export import (_synth, _cas_video, _export,
                                   _out_asset)
from tests.test_n1a_tracks import _track
from tests.test_n2a_audio import _atrack, _place, _audio

from app.domain import timeline_export as tx
from app.domain.media import binaries, engine, supervise

AUDIO_RATE = tx.AUDIO_RATE


# ---------- fixtures ----------

@pytest.fixture()
def media_tools():
    if binaries.ffmpeg() is None or binaries.ffprobe() is None:
        pytest.skip("ffmpeg/ffprobe not resolvable")


def _wav_tone(path: Path, *, rate=48000, channels=2, seconds=1.0,
              freq=440.0, amp=0.4) -> Path:
    """Real PCM16 sine WAV — deterministic bytes, no ffmpeg needed."""
    n = int(rate * seconds)
    frames = bytearray()
    for i in range(n):
        v = int(amp * 32767 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", v) * channels
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(frames), b"WAVE",
        b"fmt ", 16, 1, channels, rate,
        rate * channels * 2, channels * 2, 16, b"data", len(frames))
    path.write_bytes(hdr + bytes(frames))
    return path


def _cas_audio(client, pid, src: Path, name="clip.wav"):
    """Ingest real audio bytes into CAS + register asset/version rows
    with a real probe_audio result (mirrors _cas_video)."""
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    data_dir = Path(client.app.state.settings.data_dir)
    rel = Path("blobs") / sha[:2] / sha
    dest = data_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(src, dest)
    meta = engine.probe_audio(dest)
    assert meta["ok"], meta.get("reason")
    from app.db.models import (Asset, AssetFile, AssetVersion,
                               StorageLocation, new_id)
    with client.app.state.session_factory() as s:
        loc = s.query(StorageLocation).first()
        if s.get(AssetFile, sha) is None:
            s.add(AssetFile(sha256=sha,
                            storage_location_id=loc.id if loc else None,
                            rel_path=str(rel).replace("\\", "/"),
                            byte_size=src.stat().st_size,
                            status="ready"))
        a = Asset(id=new_id(), project_id=pid, name=name, kind="audio",
                  origin="imported", uri=str(rel).replace("\\", "/"),
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
        return {"id": a.id, "version_id": v.id, "sha256": sha}


def _out_path(client, job):
    """CAS path of the rendered output asset."""
    _a, v, _j = _out_asset(client, None, job)
    from app.db.models import AssetFile
    with client.app.state.session_factory() as s:
        return client.app.state.storage.file_path(
            s, s.get(AssetFile, v.sha256))


def _decode_pcm(path: Path, tmp_path: Path, name="out.pcm",
                rate=AUDIO_RATE):
    """Decode the render's first audio stream to stereo s16le."""
    ff = binaries.ffmpeg()
    out = tmp_path / name
    r = supervise.run(
        [ff["path"], "-y", "-v", "error", "-i", str(path),
         "-map", "0:a:0", "-f", "s16le", "-ac", "2", "-ar", str(rate),
         str(out)], timeout_s=60.0)
    assert r["exit"] == 0, r["output_tail"]
    a = array.array("h")
    a.frombytes(out.read_bytes())
    return a


# ---------- pcm measurement helpers ----------

def _rms(pcm, s0, s1):
    """RMS over [s0,s1) sample-frames across both channels (0..1)."""
    seg = pcm[2 * s0:2 * s1]
    if not seg:
        return 0.0
    return math.sqrt(sum(v * v for v in seg) / len(seg)) / 32768.0


def _peak(pcm, s0, s1):
    seg = pcm[2 * s0:2 * s1]
    return max((abs(v) for v in seg), default=0) / 32768.0


def _mag(pcm, freq, s0, s1, *, rate=AUDIO_RATE, ch=0):
    """Goertzel magnitude of `freq` in [s0,s1) on channel `ch` —
    ≈ amplitude of that spectral component."""
    w = 2 * math.pi * freq / rate
    cw = 2 * math.cos(w)
    s_prev = s_prev2 = 0.0
    for i in range(s0, s1):
        s_ = pcm[2 * i + ch] / 32768.0 + cw * s_prev - s_prev2
        s_prev2, s_prev = s_prev, s_
    n = max(1, s1 - s0)
    power = (s_prev * s_prev + s_prev2 * s_prev2
             - cw * s_prev * s_prev2)
    return 2 * math.sqrt(max(0.0, power)) / n


def _onset_s(pcm, *, thresh=0.02, rate=AUDIO_RATE):
    """First sample-frame above threshold → seconds."""
    for i in range(0, len(pcm), 2):
        if abs(pcm[i]) / 32768.0 > thresh or abs(pcm[i + 1]) / 32768.0 > thresh:
            return (i // 2) / rate
    return None


# ---------- plan-unit tests (pure, no ffmpeg) ----------

def _audio_edl(*, track_kw=None, clip_kw=None, xf=None):
    t = {"id": "a1", "kind": "audio", "position": 0, "visible": True,
         "muted": False, "gain_db": 0.0, "solo": False}
    t.update(track_kw or {})
    c = {"clip_id": "c1", "track_id": "a1", "start_frames": 0,
         "in_frame": 0, "out_frame": 48000, "sha256": "x" * 64,
         "timeline_frames": {"num": 48, "den": 1}, "gain_db": 0.0,
         "fades": {"in": {"units": 0, "curve": "linear"},
                   "out": {"units": 0, "curve": "linear"}},
         "automation": [],
         "source": {"fps_num": 48000, "fps_den": 1,
                    "sample_rate": 48000}}
    c.update(clip_kw or {})
    return {"format": "ade-edl/2",
            "timebase": {"num": 24, "den": 1},
            "tracks": [t], "clips": [c], "crossfades": xf or []}


def test_plan_audibility_mute_solo_hidden():
    # muted lane → no audio input
    plan = tx.composite_plan(_audio_edl(track_kw={"muted": True}), {})
    assert plan["audio_clips"] == []
    # hidden → contributes nothing
    plan = tx.composite_plan(_audio_edl(track_kw={"visible": False}), {})
    assert plan["audio_clips"] == [] and plan["clips"] == []
    # soloed OTHER lane silences this one
    other = _audio_edl()
    other["tracks"].append({"id": "a2", "kind": "audio", "position": 1,
                            "visible": True, "muted": False,
                            "gain_db": 0.0, "solo": True})
    plan = tx.composite_plan(other, {})
    assert plan["audio_clips"] == []
    # soloed THIS lane stays audible
    plan = tx.composite_plan(_audio_edl(track_kw={"solo": True}), {})
    assert len(plan["audio_clips"]) == 1


def test_plan_audio_only_timeline():
    """No video lanes at all — the mix still renders (black canvas)."""
    plan = tx.composite_plan(_audio_edl(), {})
    assert plan["clips"] == []
    assert len(plan["audio_clips"]) == 1
    canvas = tx.composite_canvas(plan, "source", {})
    assert (canvas["width"], canvas["height"]) == (1280, 720)


def _xf_edl(*, satisfied=True, S=30, n=12, curve="equal_power"):
    """A [0,36) + B [36,60) with window [S,S+n) straddling the seam."""
    edl = _audio_edl()
    a = dict(edl["clips"][0], clip_id="A", out_frame=72000,
             timeline_frames={"num": 36, "den": 1})
    b = dict(edl["clips"][0], clip_id="B", start_frames=36,
             in_frame=48000, out_frame=96000,
             timeline_frames={"num": 24, "den": 1})
    edl["clips"] = [a, b]
    edl["crossfades"] = [{"id": "x1", "out_clip_id": "A",
                          "in_clip_id": "B", "tl_start": S,
                          "tl_frames": n, "curve": curve,
                          "satisfied": satisfied}]
    return edl


def test_plan_xf_extends_into_handles():
    plan = tx.composite_plan(_xf_edl(), {})
    a = next(x for x in plan["audio_clips"] if x["clip_id"] == "A")
    b = next(x for x in plan["audio_clips"] if x["clip_id"] == "B")
    # 24 fps @48k: 6 tl-frames = 12000 samples each side of the seam.
    assert a["seg_end_src"] == 72000 + 12000       # tail borrowed
    assert a["xf"]["off"] == 30 * 2000           # S−A.s → source units
    assert a["xf"]["n"] == 12 * 2000
    assert b["seg_start_src"] == 48000 - 12000   # head borrowed
    assert b["xf"]["off"] == 0 and b["xf"]["n"] == 12 * 2000
    assert b["head_tl"] == 6 and b["head_src"] == 12000


def test_plan_unsatisfied_xf_fails_closed():
    with pytest.raises(ValueError, match="satisfied"):
        tx.composite_plan(_xf_edl(satisfied=False), {})
    # …but a detached record on a HIDDEN lane is inert, not an error.
    edl = _xf_edl(satisfied=False)
    edl["tracks"][0]["visible"] = False
    assert tx.composite_plan(edl, {})["audio_clips"] == []


def test_plan_xf_referencing_video_clip_refuses():
    edl = _xf_edl()
    # corrupt doc: A sits on a video lane → embedded entry → xf on it
    edl["tracks"].append({"id": "v1", "kind": "video", "position": 1,
                          "visible": True, "muted": False,
                          "gain_db": 0.0, "solo": False})
    edl["clips"][0] = dict(edl["clips"][0], track_id="v1",
                           in_frame=0, out_frame=36,
                           timeline_frames={"num": 36, "den": 1},
                           source={"fps_num": 24, "fps_den": 1})
    probes = {"A": {"ok": True, "audio": [{"sample_rate": 48000}]}}
    with pytest.raises(ValueError, match="video-lane"):
        tx.composite_plan(edl, probes)


def test_lane_chain_deterministic_shape():
    edl = _audio_edl(clip_kw={
        "gain_db": -3.0,
        "fades": {"in": {"units": 4800, "curve": "equal_power"},
                  "out": {"units": 2400, "curve": "linear"}},
        "automation": [{"id": "p1", "at_frames": 0, "gain_db": 0.0},
                       {"id": "p2", "at_frames": 24, "gain_db": -6.0}]})
    plan = tx.composite_plan(edl, {})
    a = plan["audio_clips"][0]
    chain = tx._lane_chain(7, a, Fraction(24, 1))
    assert chain.startswith("[7:a]atrim=start_sample=0:end_sample=48000")
    assert "aformat=channel_layouts=stereo" in chain
    assert "aeval=exprs='val(0)" in chain
    # automation keyframes: tl frames → native samples (×2000/fps24→48k)
    assert "n-0)" in chain and "48000" in chain
    assert ("afade=t=in:start_sample=0:nb_samples=4800:curve=qsin"
            in chain)
    assert ("afade=t=out:start_sample=45600:nb_samples=2400:curve=tri"
            in chain)
    assert chain.endswith("aresample=48000,asetpts=PTS-STARTPTS")
    # resample LAST — envelopes applied at native rate
    assert chain.index("afade") < chain.index("aresample")
    # PTS reset immediately after atrim — afade anchors on frame pts,
    # so envelope windows MUST see stream-local sample indices
    assert chain.index("asetpts") < chain.index("afade")


def test_auto_exprs_interp_in_db_domain():
    a = {"head_tl": Fraction(0), "rate": 48000,
         "automation": [{"off_tl": Fraction(0), "gain_db": 0.0},
                        {"off_tl": Fraction(12), "gain_db": -12.0}]}
    expr = tx._auto_exprs(a, -3.0, Fraction(24, 1))
    # linear-in-dB between sample 0 and 24000; base −3 dB folded in
    assert "between(n,0,23999)" in expr
    assert "pow(10," in expr and "/20)" in expr
    # both channels share the envelope
    assert expr.startswith("val(0)*(") and "|val(1)*(" in expr


def test_composite_argv_audio_inputs_and_mix():
    edl = _audio_edl()
    edl["clips"][0]["clip_id"] = "au1"
    plan = tx.composite_plan(edl, {})
    canvas = {"width": 640, "height": 360, "fps_num": 24, "fps_den": 1}
    argv = tx.build_composite_argv("ffmpeg", plan,
                                   {"au1": "/src/a.wav"}, canvas,
                                   20, "/out.mp4", encoder="libx264")
    joined = " ".join(argv)
    assert "-i /src/a.wav" in joined
    assert "amix=inputs=2:duration=first:normalize=0" in joined
    assert "alimiter=limit=0.95" in joined
    assert "-map [a]" in joined and "-c:a aac" in joined
    assert "-ar 48000" in joined
    assert "atrim=start_sample=0:end_sample=48000" in joined


# ---------- real render tests ----------

def _setup_audio(client, pid, tmp_path, name="a.wav", **wav):
    p = _wav_tone(tmp_path / name, **wav)
    return _cas_audio(client, pid, p, name)


@pytest.mark.usefixtures("media_tools")
def test_render_single_audio_clip_tone(client, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _setup_audio(client, pid, tmp_path, freq=440.0, seconds=1.0)
    _place(client, pid, t["id"], au, a1["id"], at=0)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    out = _out_path(client, job)
    meta = engine.probe(out)
    assert meta["ok"] and meta["audio_streams"] >= 1
    pcm = _decode_pcm(out, tmp_path)
    # ~1 s of content; tone present at 440 Hz, absent at 880
    assert _mag(pcm, 440, 4800, 43200) > 0.2
    assert _mag(pcm, 880, 4800, 43200) < 0.02


@pytest.mark.usefixtures("media_tools")
def test_render_placement_sample_accurate(client, tmp_path):
    """Clip at tl frame 12 on a 24 fps timeline = 0.5 s in — onset
    lands within 2 samples of 24000."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _setup_audio(client, pid, tmp_path, freq=440.0, seconds=0.5)
    _place(client, pid, t["id"], au, a1["id"], at=12)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    onset = _onset_s(pcm)
    assert onset is not None and abs(onset - 0.5) < (2 / 48000 + 0.005)


@pytest.mark.usefixtures("media_tools")
def test_render_two_lane_mix_sums(client, tmp_path):
    """Same-freq tones on two lanes: mixed RMS ≈ 2× a single lane —
    amix normalize=0 never attenuates."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"], "A1")
    a2 = _atrack(client, pid, t["id"], "A2")
    au1 = _setup_audio(client, pid, tmp_path, "a1.wav", freq=440.0,
                       seconds=0.5, amp=0.2)
    au2 = _setup_audio(client, pid, tmp_path, "a2.wav", freq=440.0,
                       seconds=0.5, amp=0.2)
    _place(client, pid, t["id"], au1, a1["id"], at=0)
    _place(client, pid, t["id"], au2, a2["id"], at=0)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    mag = _mag(pcm, 440, 4800, 19200)
    assert mag > 0.3  # ≈0.4 summed, comfortably above single-lane 0.2


@pytest.mark.usefixtures("media_tools")
def test_render_mute_and_solo(client, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"], "A1")
    a2 = _atrack(client, pid, t["id"], "A2")
    au1 = _setup_audio(client, pid, tmp_path, "m1.wav", freq=440.0,
                       seconds=0.5)
    au2 = _setup_audio(client, pid, tmp_path, "m2.wav", freq=880.0,
                       seconds=0.5)
    _place(client, pid, t["id"], au1, a1["id"], at=0)
    _place(client, pid, t["id"], au2, a2["id"], at=0)
    # mute A1 → only 880 survives
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": a1["id"],
             "muted": True})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    assert _mag(pcm, 880, 4800, 19200) > 0.2
    assert _mag(pcm, 440, 4800, 19200) < 0.02
    # unmute + solo A2 → still only 880; solo A1 → only 440
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": a1["id"],
             "muted": False, "solo": True})
    job = _export(client, pid, t["id"], profile="720p")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    assert _mag(pcm, 440, 4800, 19200) > 0.2
    assert _mag(pcm, 880, 4800, 19200) < 0.02


@pytest.mark.usefixtures("media_tools")
def test_render_clip_and_track_gain(client, tmp_path):
    """−6 dB clip + −6 dB track → ≈ −12 dB total (ratio ~0.25)."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _setup_audio(client, pid, tmp_path, freq=440.0, seconds=0.5,
                      amp=0.4)
    _place(client, pid, t["id"], au, a1["id"], at=0)
    c = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.gain",
            {"timeline_id": t["id"], "clip_id": c["id"],
             "gain_db": -6.0})
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": a1["id"],
             "gain_db": -6.0})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    mag = _mag(pcm, 440, 4800, 19200)
    assert 0.07 < mag < 0.14  # 0.4 × 10^(-12/20) ≈ 0.10


@pytest.mark.usefixtures("media_tools")
def test_render_linear_fade_in_ramps(client, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _setup_audio(client, pid, tmp_path, freq=440.0, seconds=1.0)
    _place(client, pid, t["id"], au, a1["id"], at=0)
    c = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.fade",
            {"timeline_id": t["id"], "clip_id": c["id"],
             "fade_in_units": 48000, "fade_in_curve": "linear"})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    # linear ramp: first fifth ≈ 0.1 × amp, last fifth ≈ full
    r1 = _rms(pcm, 2000, 9000)
    r5 = _rms(pcm, 40000, 47000)
    assert r1 < 0.06 < r5 and r5 > 0.2


@pytest.mark.usefixtures("media_tools")
def test_render_equal_power_midpoint_above_linear(client, tmp_path):
    """At the midpoint, equal_power is sin(π/4)≈0.707 vs linear 0.5 —
    measurably louder."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _setup_audio(client, pid, tmp_path, freq=440.0, seconds=1.0)
    _place(client, pid, t["id"], au, a1["id"], at=0)
    c = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.fade",
            {"timeline_id": t["id"], "clip_id": c["id"],
             "fade_in_units": 48000, "fade_in_curve": "equal_power"})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    mid = _rms(pcm, 23000, 25000)
    # ≈0.20 expected; a linear midpoint would land ~0.14.
    assert mid > 0.16


@pytest.mark.usefixtures("media_tools")
def test_render_crossfade_window_no_gap(client, tmp_path):
    """A(440) + B(880) with a 12-frame equal-power crossfade straddling
    the seam at frame 36: 440 dominates before, 880 after, and the
    window never goes silent."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    # 3 s sources so both have ≥6 frames of handles.
    a_src = _setup_audio(client, pid, tmp_path, "xa.wav", freq=440.0,
                         seconds=3.0)
    b_src = _setup_audio(client, pid, tmp_path, "xb.wav", freq=880.0,
                         seconds=3.0)
    _place(client, pid, t["id"], a_src, a1["id"], at=0,
           in_frame=0, out_frame=72000)          # [0,36) tl
    _place(client, pid, t["id"], b_src, a1["id"], at=36,
           in_frame=48000, out_frame=96000)       # [36,60) tl
    clips = {c["id"]: c for c in _detail(client, pid, t["id"])["clips"]}
    A = next(c for c in clips.values() if c["start_frames"] == 0)
    B = next(c for c in clips.values() if c["start_frames"] == 36)
    _commit(client, pid, "timeline.crossfade.set",
            {"timeline_id": t["id"], "out_clip_id": A["id"],
             "in_clip_id": B["id"], "tl_start": 30, "tl_frames": 12,
             "curve": "equal_power"})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    # before window: 440 dominant; after: 880 dominant
    assert _mag(pcm, 440, 2000, 26 * 2000) > 0.15
    assert _mag(pcm, 880, 44 * 2000, 58 * 2000) > 0.15
    # inside the window both partially present — never silent
    mid_a = _mag(pcm, 440, 33 * 2000, 36 * 2000)
    mid_b = _mag(pcm, 880, 33 * 2000, 36 * 2000)
    assert mid_a > 0.05 and mid_b > 0.05
    assert _rms(pcm, 33 * 2000, 38 * 2000) > 0.1


@pytest.mark.usefixtures("media_tools")
def test_render_automation_segment_levels(client, tmp_path):
    """Keyframes 0 dB → −12 dB at the halfway point: second half is
    measurably quieter than the first."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _setup_audio(client, pid, tmp_path, freq=440.0, seconds=1.0)
    _place(client, pid, t["id"], au, a1["id"], at=0)
    c = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.automation",
            {"timeline_id": t["id"], "clip_id": c["id"],
             "points": [{"at_frames": 0, "gain_db": 0.0},
                        {"at_frames": 12, "gain_db": -12.0},
                        {"at_frames": 23, "gain_db": -12.0}]})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    first = _rms(pcm, 2000, 10000)
    last = _rms(pcm, 38000, 47000)
    assert first > 0.2 and last < first * 0.45


@pytest.mark.usefixtures("media_tools")
def test_render_detached_xf_fails_closed(client, tmp_path):
    """A crossfade that detaches after a move refuses the export —
    never silently renders a partial transition."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    a_src = _setup_audio(client, pid, tmp_path, "da.wav", seconds=3.0)
    b_src = _setup_audio(client, pid, tmp_path, "db.wav", freq=880.0,
                         seconds=3.0)
    _place(client, pid, t["id"], a_src, a1["id"], at=0,
           in_frame=0, out_frame=72000)
    _place(client, pid, t["id"], b_src, a1["id"], at=36,
           in_frame=48000, out_frame=96000)
    clips = _detail(client, pid, t["id"])["clips"]
    A = next(c for c in clips if c["start_frames"] == 0)
    B = next(c for c in clips if c["start_frames"] == 36)
    _commit(client, pid, "timeline.crossfade.set",
            {"timeline_id": t["id"], "out_clip_id": A["id"],
             "in_clip_id": B["id"], "tl_start": 30, "tl_frames": 12})
    # detach: move B away from the window
    _commit(client, pid, "timeline.clip.move",
            {"timeline_id": t["id"], "clip_id": B["id"],
             "to_track_id": a1["id"], "start_frames": 96})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "failed"
    assert "satisfied" in (job.get("error") or "")


@pytest.mark.usefixtures("media_tools")
def test_render_mixed_sample_rates(client, tmp_path):
    """44.1 kHz + 48 kHz sources mix onto the 48 kHz output grid with
    correct placement — no drift, no missing lane."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    lo = _setup_audio(client, pid, tmp_path, "r44.wav", rate=44100,
                      channels=1, freq=440.0, seconds=0.5)
    hi = _setup_audio(client, pid, tmp_path, "r48.wav", rate=48000,
                      channels=2, freq=880.0, seconds=0.5)
    _place(client, pid, t["id"], lo, a1["id"], at=0)
    _place(client, pid, t["id"], hi, a1["id"], at=12)  # 0.5 s mark
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    assert _mag(pcm, 440, 4000, 20000) > 0.15
    assert _mag(pcm, 880, 4000, 20000) < 0.05   # 880 not yet playing
    assert _mag(pcm, 880, 26000, 44000) > 0.15  # …now it is


@pytest.mark.usefixtures("media_tools")
def test_render_fractional_fps_no_drift(client, tmp_path):
    """30000/1001 timeline: a clip placed 600 frames in lands at
    ~20.02 s — no cumulative rounding across the timeline."""
    pid = make_project(client)["id"]
    t = _tl(client, pid, "Cut 1", fps_num=30000, fps_den=1001)
    a1 = _atrack(client, pid, t["id"])
    au = _setup_audio(client, pid, tmp_path, freq=440.0, seconds=1.0)
    # 30000/1001 fps: 600 frames ≈ 20.02 s in; a 48k clip occupies
    # ~29.97 tl-frames — placement must be sample-exact anyway.
    _place(client, pid, t["id"], au, a1["id"], at=600)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    onset = _onset_s(pcm)
    expected = 600 * 1001 / 30000
    assert onset is not None and abs(onset - expected) < 0.01


@pytest.mark.usefixtures("media_tools")
def test_render_audio_only_black_canvas(client, tmp_path):
    """An audio-only timeline still produces an mp4 — black canvas +
    the mix, honest and playable."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _setup_audio(client, pid, tmp_path, freq=440.0, seconds=0.5)
    _place(client, pid, t["id"], au, a1["id"], at=0)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    meta = engine.probe(_out_path(client, job))
    assert meta["ok"] and meta["codec"] == "h264"
    assert meta["audio_streams"] >= 1


@pytest.mark.usefixtures("media_tools")
def test_render_video_embedded_plus_lane_audio(client, tmp_path):
    """Video embedded audio (440) + lane audio (880) both land in the
    one mix — the C5-B path and N2-E path share the graph."""
    from tests.test_c5b_export import _synth as synth
    ff = binaries.ffmpeg()
    vid = synth(ff["path"], tmp_path / "v.mp4", dur=0.5, rate=24,
                audio=True)
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["kind"] == "video")
    _insert(client, pid, t["id"],
            _cas_video(client, pid, vid, "v"), track_id=v1["id"])
    a1 = _atrack(client, pid, t["id"])
    au = _setup_audio(client, pid, tmp_path, "e.wav", freq=880.0,
                      seconds=0.5)
    _place(client, pid, t["id"], au, a1["id"], at=0)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    assert _mag(pcm, 440, 4000, 20000) > 0.05
    assert _mag(pcm, 880, 4000, 20000) > 0.15
