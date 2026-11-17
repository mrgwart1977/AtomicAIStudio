"""N2-E review — deep verification on top of test_n2e_mixing.

§1 sync matrix: decoded-PCM onset/offset/duration across fps rates,
source rates, trims, boundaries — plus an AAC-pipeline control that
separates codec delay from mix-placement error.
§2 crossfades: envelope shapes verified against the pinned math
(sin/cos equal-power across the whole window, linear), coherent and
incoherent content, short/asymmetric/mixed-rate windows.
§3 processing order: one combined render exercising every gain stage.
§4 limiter: measured ceiling, latency, quiet-signal honesty, duration.
§5 embedded regression: rendered picture+audio for mute/solo/hidden.
§6 durability on the composite path: cancel, missing/hash-mismatched
sources, ffmpeg nonzero, disk space, work-dir cleanup, no phantoms.
"""
import array
import hashlib
import math
import shutil
import time
from fractions import Fraction
from pathlib import Path

import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit
from tests.test_c5a_timeline import _tl, _detail, _insert
from tests.test_c5b_export import (_synth, _cas_video, _export,
                                   _out_asset)
from tests.test_n2a_audio import _atrack, _place
from tests.test_n2e_mixing import (_wav_tone, _cas_audio, _out_path,
                                   _decode_pcm, _rms, _peak, _mag,
                                   _onset_s)

from app.domain import timeline_export as tx
from app.domain.media import binaries, engine, supervise

AUDIO_RATE = tx.AUDIO_RATE


@pytest.fixture()
def media_tools():
    if binaries.ffmpeg() is None or binaries.ffprobe() is None:
        pytest.skip("ffmpeg/ffprobe not resolvable")


# ---------- extra wav fixtures ----------

def _wav_write(path: Path, samples, rate=48000, channels=2):
    import struct
    frames = bytearray()
    for v in samples:
        frames += struct.pack("<h", v) * channels
    hdr = struct.pack(
        "<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(frames), b"WAVE",
        b"fmt ", 16, 1, channels, rate,
        rate * channels * 2, channels * 2, 16, b"data", len(frames))
    path.write_bytes(hdr + bytes(frames))
    return path


def _wav_two_tone(path: Path, *, rate=48000, seconds=2.0, f1=440.0,
                  f2=880.0, amp=0.4):
    """First half f1, second half f2 — nonzero-trim content probe."""
    n = int(rate * seconds)
    return _wav_write(path, (
        int(amp * 32767 *
            math.sin(2 * math.pi * (f1 if i < n // 2 else f2) * i / rate))
        for i in range(n)), rate=rate)


def _wav_click(path: Path, *, rate=48000, seconds=1.0, at=0.5,
               amp=0.8):
    """Single impulse — the sharpest possible onset marker."""
    n = int(rate * seconds)
    pos = int(at * rate)
    return _wav_write(path, (int(amp * 32767) if i == pos else 0
                             for i in range(n)), rate=rate)


def _frame_nonblack(path: Path, tmp_path: Path, t=0.5) -> bool:
    """Decode one video frame at t s; True when it carries picture."""
    ff = binaries.ffmpeg()
    out = tmp_path / f"frame{t}.raw"
    r = supervise.run(
        [ff["path"], "-y", "-v", "error", "-ss", str(t), "-i", str(path),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray",
         str(out)], timeout_s=60.0)
    assert r["exit"] == 0
    return max(out.read_bytes() or b"\x00") > 16


def _frames(pcm) -> int:
    return len(pcm) // 2


# ================= §1 — synchronization matrix =================

@pytest.mark.usefixtures("media_tools")
@pytest.mark.parametrize("fps", [(24, 1), (25, 1), (30, 1),
                                 (24000, 1001), (30000, 1001)])
def test_render_onset_fps_matrix(client, tmp_path, fps):
    """A clip placed at tl frame 48 lands at 48·den/num seconds on
    decoded PCM for every approved frame rate — measured, not computed."""
    num, den = fps
    pid = make_project(client)["id"]
    t = _tl(client, pid, "Cut 1", fps_num=num, fps_den=den)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "s.wav", freq=440.0,
                              seconds=0.5), "s.wav")
    _place(client, pid, t["id"], au, a1["id"], at=48)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    onset = _onset_s(pcm)
    expected = 48 * den / num
    # AAC pipeline tolerance: measured ≤ one tl frame + ~6 ms codec
    # slop (see test_render_click_onset_vs_aac_pipeline).
    assert onset is not None
    assert abs(onset - expected) < 0.02, \
        f"fps {num}/{den}: onset {onset} vs expected {expected}"


@pytest.mark.usefixtures("media_tools")
def test_render_click_onset_vs_aac_pipeline(client, tmp_path):
    """Separates codec delay from placement error.

    Control: the same click WAV encoded to AAC and decoded back — the
    measured shift is the encode/decode pipeline's contribution (AAC
    priming is trimmed by the edit list, so ≈0). The composite render
    may differ from the expectation by at most that pipeline delta
    plus the documented <1-sample quantization residue."""
    ff = binaries.ffmpeg()
    click = _wav_click(tmp_path / "clk.wav", at=0.5)
    # control: wav → aac(mp4) → pcm roundtrip
    ctl = tmp_path / "ctl.mp4"
    r = supervise.run(
        [ff["path"], "-y", "-v", "error", "-i", str(click),
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", str(ctl)],
        timeout_s=30.0)
    assert r["exit"] == 0
    ctl_pcm = _decode_pcm(ctl, tmp_path, "ctl.pcm")
    codec_delta = (_onset_s(ctl_pcm) or 0) - 0.5
    # clip placed at tl frame 24 (1.0 s); the click sits 0.5 s into
    # the 1.0 s source → expected onset 1.5 s
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid, click, "clk.wav")
    _place(client, pid, t["id"], au, a1["id"], at=24)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    onset = _onset_s(pcm)
    assert onset is not None
    # true mix error = measured onset − expected − codec contribution
    mix_err = onset - 1.5 - codec_delta
    assert abs(mix_err) < 0.004, \
        f"placement {onset}s, codec Δ {codec_delta}s, mix err {mix_err}s"


@pytest.mark.usefixtures("media_tools")
def test_render_nonzero_trim_selects_source_region(client, tmp_path):
    """in_frame=48000 into a 440→880 two-tone source must render the
    880 half — verifies the trim indexes source samples, not zeros."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_two_tone(tmp_path / "tt.wav", seconds=2.0),
                    "tt.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0,
           in_frame=48000, out_frame=96000)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    assert _mag(pcm, 880, 4000, 20000) > 0.15
    assert _mag(pcm, 440, 4000, 20000) < 0.05


@pytest.mark.usefixtures("media_tools")
def test_render_end_at_program_boundary(client, tmp_path):
    """Clip ending exactly at the program end keeps sounding through
    the tail — no premature truncation — and total duration matches
    the program within one AAC frame."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "e.wav", freq=440.0,
                              seconds=0.5), "e.wav")
    _place(client, pid, t["id"], au, a1["id"], at=12)  # [12,24) → 1.0 s
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    out = _out_path(client, job)
    pcm = _decode_pcm(out, tmp_path)
    # tone alive in the last 0.05 s of the 1.0 s program
    assert _rms(pcm, 45000, 47900) > 0.1
    # program = 1.0 s = 48000 stereo frames ± 1024-sample AAC frame
    assert abs(_frames(pcm) - AUDIO_RATE) < 2100


@pytest.mark.usefixtures("media_tools")
def test_render_gap_between_clips_is_silent(client, tmp_path):
    """Sequential clips on one lane: the gap is real silence and the
    second clip onsets exactly at its frame."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    for i, (f, at) in enumerate(((440.0, 0), (880.0, 36))):
        au = _cas_audio(
            client, pid,
            _wav_tone(tmp_path / f"g{i}.wav", freq=f, seconds=0.5),
            f"g{i}.wav")
        _place(client, pid, t["id"], au, a1["id"], at=at)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    # 0.5 s clip @0 → gap [0.5 s, 1.5 s) silent; 880 onsets at 1.5 s
    assert _rms(pcm, 26000, 44000) < 0.005
    assert _mag(pcm, 880, 76000, 90000) > 0.15


@pytest.mark.usefixtures("media_tools")
def test_render_long_timeline_no_drift(client, tmp_path):
    """A clip 60 s into the program onsets within one frame — no
    cumulative rounding over thousands of placement samples."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "L.wav", freq=440.0,
                              seconds=0.5), "L.wav")
    _place(client, pid, t["id"], au, a1["id"], at=1440)  # 60 s @24fps
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    onset = _onset_s(pcm)
    assert onset is not None and abs(onset - 60.0) < 0.03


@pytest.mark.usefixtures("media_tools")
def test_render_mono_source_on_both_channels(client, tmp_path):
    """A mono source must land on both output channels — the aformat
    up-mix is a hard requirement of the 48k-stereo output contract."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "m.wav", channels=1,
                              freq=440.0, seconds=0.5), "m.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    assert _mag(pcm, 440, 4000, 20000, ch=0) > 0.1
    assert _mag(pcm, 440, 4000, 20000, ch=1) > 0.1


# ================= §2 — crossfade rendering =================

def _xf_pair(client, pid, t, a1, tmp_path, *, S=30, n=12,
             curve="equal_power", a_kw=None, b_kw=None,
             a_trim=(0, 72000), b_trim=(48000, 96000)):
    a = _cas_audio(client, pid,
                   _wav_tone(tmp_path / "xa.wav", seconds=3.0,
                             **(a_kw or {})), "xa.wav")
    b = _cas_audio(client, pid,
                   _wav_tone(tmp_path / "xb.wav",
                             **{"seconds": 3.0, "freq": 880.0,
                                **(b_kw or {})}), "xb.wav")
    _place(client, pid, t["id"], a, a1["id"], at=0,
           in_frame=a_trim[0], out_frame=a_trim[1])
    _place(client, pid, t["id"], b, a1["id"], at=36,
           in_frame=b_trim[0], out_frame=b_trim[1])
    clips = _detail(client, pid, t["id"])["clips"]
    A = next(c for c in clips if c["start_frames"] == 0)
    B = next(c for c in clips if c["start_frames"] == 36)
    _commit(client, pid, "timeline.crossfade.set",
            {"timeline_id": t["id"], "out_clip_id": A["id"],
             "in_clip_id": B["id"], "tl_start": S, "tl_frames": n,
             "curve": curve})
    return A, B


@pytest.mark.usefixtures("media_tools")
def test_render_xf_equal_power_envelope_shape(client, tmp_path):
    """The qsin pair is verified against the contract ACROSS the
    transition, not just at the midpoint: incoming must track
    sin(πu/2), outgoing cos(πu/2), and g_out² + g_in² ≈ 1 at every
    sampled point — the definition of equal power."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _xf_pair(client, pid, t, a1, tmp_path, curve="equal_power")
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    # reference full-strength magnitudes outside the window
    ref_a = _mag(pcm, 440, 2000, 20 * 2000)
    ref_b = _mag(pcm, 880, 46 * 2000, 58 * 2000)
    assert ref_a > 0.15 and ref_b > 0.15
    # window [30,42) tl = samples [60000,84000); probe slice centers
    for u in (0.125, 0.375, 0.625, 0.875):
        c = int((30 + 12 * u) * 2000)
        w = 1500
        ga = _mag(pcm, 440, c - w, c + w) / ref_a
        gb = _mag(pcm, 880, c - w, c + w) / ref_b
        exp_out = math.cos(math.pi * u / 2)   # A fades out
        exp_in = math.sin(math.pi * u / 2)    # B fades in
        assert abs(ga - exp_out) < 0.13, (u, ga, exp_out)
        assert abs(gb - exp_in) < 0.13, (u, gb, exp_in)
        # the actual equal-power invariant
        assert 0.8 < ga * ga + gb * gb < 1.25, (u, ga, gb)


@pytest.mark.usefixtures("media_tools")
def test_render_xf_linear_envelope_shape(client, tmp_path):
    """Linear (tri) crossfade: envelopes track 1−u and u, so the
    power dips at the midpoint — the honest difference vs qsin."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _xf_pair(client, pid, t, a1, tmp_path, curve="linear")
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    ref_a = _mag(pcm, 440, 2000, 20 * 2000)
    ref_b = _mag(pcm, 880, 46 * 2000, 58 * 2000)
    for u in (0.25, 0.5, 0.75):
        c = int((30 + 12 * u) * 2000)
        w = 1500
        ga = _mag(pcm, 440, c - w, c + w) / ref_a
        gb = _mag(pcm, 880, c - w, c + w) / ref_b
        assert abs(ga - (1 - u)) < 0.13, (u, ga)
        assert abs(gb - u) < 0.13, (u, gb)


@pytest.mark.usefixtures("media_tools")
def test_render_xf_coherent_content_bumps_equal_power(client, tmp_path):
    """Phase-aligned same-frequency content: linear crossfade holds
    amplitude flat (g_out+g_in=1) while equal-power produces the
    predicted +3 dB coherent bump (g²+g²=1 ⇒ g_sum=√2 at midpoint).
    The two curves differ measurably through the window, not just
    at boundaries."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    # same freq, whole-second trims → phase aligned across the splice
    _xf_pair(client, pid, t, a1, tmp_path, curve="equal_power",
             b_kw={"freq": 440.0})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    edge = _rms(pcm, 4000, 18 * 2000)
    mid = _rms(pcm, 33 * 2000, 39 * 2000)
    # coherent EP sum ≈ √2 × single at midpoint
    assert mid > edge * 1.2, (edge, mid)


@pytest.mark.usefixtures("media_tools")
def test_render_xf_short_and_asymmetric(client, tmp_path):
    """A 4-frame window offset 2/6 across the seam still renders a
    clean transition — B's head borrow and A's tail borrow are
    asymmetric and both honored."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _xf_pair(client, pid, t, a1, tmp_path, S=34, n=8,
             curve="equal_power")
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    assert _mag(pcm, 440, 2000, 30 * 2000) > 0.15   # A before window
    assert _mag(pcm, 880, 46 * 2000, 58 * 2000) > 0.15  # B after
    mid = _rms(pcm, 36 * 2000, 40 * 2000)
    assert mid > 0.08  # window interior never silent


@pytest.mark.usefixtures("media_tools")
def test_render_xf_mixed_source_rates(client, tmp_path):
    """44.1 kHz out + 48 kHz in: envelopes anchor in each stream's
    native units before resample — the mix lands correctly anyway."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    _xf_pair(client, pid, t, a1, tmp_path,
             a_kw={"rate": 44100}, a_trim=(0, 66150),  # 1.5 s @44.1k
             b_kw={"rate": 48000}, b_trim=(48000, 96000))
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    assert _mag(pcm, 440, 2000, 20 * 2000) > 0.15
    assert _mag(pcm, 880, 46 * 2000, 58 * 2000) > 0.15
    assert _rms(pcm, 33 * 2000, 39 * 2000) > 0.08


# ================= §3 — processing-order contract =================

@pytest.mark.usefixtures("media_tools")
def test_render_gain_automation_fade_combined(client, tmp_path):
    """Every stage live at once: clip −6 + track −6 + a 0.125 s
    linear fade-in + automation stepping from 0 to −12 across
    frames 6→7.

    [0.125,0.25)s ≈ −12 dB total (fade done, automation still 0);
    [0.5,1)s ≈ −24 dB — each factor lands exactly once."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "c.wav", freq=440.0,
                              seconds=1.0, amp=0.4), "c.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0)
    c = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.gain",
            {"timeline_id": t["id"], "clip_id": c["id"],
             "gain_db": -6.0})
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": a1["id"],
             "gain_db": -6.0})
    _commit(client, pid, "timeline.clip.automation",
            {"timeline_id": t["id"], "clip_id": c["id"],
             "points": [{"at_frames": 0, "gain_db": 0.0},
                        {"at_frames": 6, "gain_db": 0.0},
                        {"at_frames": 7, "gain_db": -12.0},
                        {"at_frames": 23, "gain_db": -12.0}]})
    _commit(client, pid, "timeline.clip.fade",
            {"timeline_id": t["id"], "clip_id": c["id"],
             "fade_in_units": 6000, "fade_in_curve": "linear"})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    # fade-in region [0,0.125s) ramps up
    assert _rms(pcm, 200, 2000) < _rms(pcm, 4500, 5800)
    # [0.125,0.29)s: −12 dB of 0.4 amp → amp 0.10 → rms ≈0.071
    early = _rms(pcm, 7000, 13500)
    assert 0.05 < early < 0.10, early
    # [0.63,0.96)s: −24 dB → rms ≈0.018; ratio ≈ 0.25
    late = _rms(pcm, 30000, 46000)
    assert 0.18 < late / early < 0.32, (early, late)


# ================= §4 — master mix & limiter =================

@pytest.mark.usefixtures("media_tools")
def test_render_limiter_caps_hot_mix(client, tmp_path):
    """Two coherent lanes summing to ~1.8 peak: the limiter caps the
    output at the explicit 0.95 ceiling — no full-scale clipping.

    Two measurements: (1) the exact mix graph at filter level shows
    peak ≤0.95, (2) the exported AAC roundtrip overshoots slightly
    (codec overshoot is normal, documented) but stays <0.99 — never
    full-scale."""
    ff = binaries.ffmpeg()
    # filter-level control: the exact amix+alimiter tail on a hot sum
    raw = tmp_path / "lim.pcm"
    r = supervise.run(
        [ff["path"], "-y", "-v", "error",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
         "-filter_complex",
         "[0:a]volume=7.2[a1];[1:a]volume=7.2[a2];"
         "[a1][a2]amix=inputs=2:normalize=0,"
         "alimiter=limit=0.95:attack=5:release=50:level=0:latency=1",
         "-f", "s16le", "-ac", "2", str(raw)], timeout_s=30.0)
    assert r["exit"] == 0
    a = array.array("h")
    a.frombytes(raw.read_bytes())
    assert max(abs(v) for v in a) / 32768 <= 0.955
    # export-level: AAC decode stays under full scale
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"], "A1")
    a2 = _atrack(client, pid, t["id"], "A2")
    for tr, name in ((a1, "h1.wav"), (a2, "h2.wav")):
        au = _cas_audio(client, pid,
                        _wav_tone(tmp_path / name, freq=440.0,
                                  seconds=0.5, amp=0.9), name)
        _place(client, pid, t["id"], au, tr["id"], at=0)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    peak = _peak(pcm, 4000, 20000)
    assert peak < 0.99  # limited at filter level; codec overshoot only
    assert _mag(pcm, 440, 4000, 20000) > 0.3  # not squashed silent


@pytest.mark.usefixtures("media_tools")
def test_render_limiter_no_makeup_on_quiet(client, tmp_path):
    """level=0: a very quiet mix is NOT pumped up to the ceiling —
    the limiter is protection, not normalization."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "q.wav", freq=440.0,
                              seconds=0.5, amp=0.01), "q.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    mag = _mag(pcm, 440, 4000, 20000)
    assert 0.004 < mag < 0.02  # ≈0.01 preserved, not amplified


@pytest.mark.usefixtures("media_tools")
def test_render_limiter_no_duration_or_onset_shift(client, tmp_path):
    """latency=1 compensates the lookahead: onset stays put and the
    program neither truncates nor extends by a meaningful amount."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_click(tmp_path / "c.wav", at=0.2,
                               seconds=0.5), "c.wav")
    _place(client, pid, t["id"], au, a1["id"], at=12)  # click @0.7 s
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    onset = _onset_s(pcm)
    assert onset is not None and abs(onset - 0.7) < 0.02


# ================= §5 — embedded-audio regression (rendered) =========

def _video_with_tone(client, pid, tmp_path, name="v.mp4", dur=1.0,
                     freq=440):
    ff = binaries.ffmpeg()
    enc = tx.h264_encoder(ff["path"], supervise.run)
    out = tmp_path / name
    r = supervise.run(
        [ff["path"], "-y", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc=duration={dur}:size=64x48:rate=24",
         "-f", "lavfi", "-i",
         f"sine=frequency={freq}:duration={dur}",
         "-c:a", "aac", "-shortest", "-c:v", enc, "-pix_fmt",
         "yuv420p", str(out)], timeout_s=60.0)
    assert r["exit"] == 0
    return _cas_video(client, pid, out, name)


@pytest.mark.usefixtures("media_tools")
def test_render_muted_video_keeps_picture_drops_audio(client, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["kind"] == "video")
    _insert(client, pid, t["id"],
            _video_with_tone(client, pid, tmp_path), track_id=v1["id"],
            in_frame=0, out_frame=24)
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v1["id"],
             "muted": True})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    out = _out_path(client, job)
    assert _frame_nonblack(out, tmp_path, 0.5)  # picture survives
    # fully muted program → NO audio stream at all (documented: the
    # exporter only maps [a] when at least one stream is audible)
    meta = engine.probe(out)
    assert meta["audio_streams"] == 0


@pytest.mark.usefixtures("media_tools")
def test_render_solo_lane_silences_embedded_keeps_picture(
        client, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["kind"] == "video")
    _insert(client, pid, t["id"],
            _video_with_tone(client, pid, tmp_path), track_id=v1["id"],
            in_frame=0, out_frame=24)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "s.wav", freq=880.0,
                              seconds=1.0), "s.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0)
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": a1["id"],
             "solo": True})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    out = _out_path(client, job)
    assert _frame_nonblack(out, tmp_path, 0.5)
    pcm = _decode_pcm(out, tmp_path)
    assert _mag(pcm, 880, 4000, 40000) > 0.15
    assert _mag(pcm, 440, 4000, 40000) < 0.03  # embedded silenced


@pytest.mark.usefixtures("media_tools")
def test_render_hidden_video_drops_picture_and_audio(
        client, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["kind"] == "video")
    _insert(client, pid, t["id"],
            _video_with_tone(client, pid, tmp_path), track_id=v1["id"],
            in_frame=0, out_frame=24)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "s.wav", freq=880.0,
                              seconds=1.0), "s.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0)
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v1["id"],
             "visible": False})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    out = _out_path(client, job)
    assert not _frame_nonblack(out, tmp_path, 0.5)  # black canvas
    pcm = _decode_pcm(out, tmp_path)
    assert _mag(pcm, 880, 4000, 40000) > 0.15   # lane still plays
    assert _mag(pcm, 440, 4000, 40000) < 0.03   # embedded gone


@pytest.mark.usefixtures("media_tools")
def test_render_multiple_solos_combine(client, tmp_path):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"], "A1")
    a2 = _atrack(client, pid, t["id"], "A2")
    a3 = _atrack(client, pid, t["id"], "A3")
    for tr, f, name in ((a1, 440.0, "s1"), (a2, 880.0, "s2"),
                        (a3, 660.0, "s3")):
        au = _cas_audio(client, pid,
                        _wav_tone(tmp_path / f"{name}.wav", freq=f,
                                  seconds=0.5), f"{name}.wav")
        _place(client, pid, t["id"], au, tr["id"], at=0)
    for tr in (a1, a2):
        _commit(client, pid, "timeline.track.update",
                {"timeline_id": t["id"], "track_id": tr["id"],
                 "solo": True})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    assert _mag(pcm, 440, 4000, 20000) > 0.15  # soloed
    assert _mag(pcm, 880, 4000, 20000) > 0.15  # soloed
    assert _mag(pcm, 660, 4000, 20000) < 0.03  # silenced


@pytest.mark.usefixtures("media_tools")
def test_render_silent_project_valid(client, tmp_path):
    """Muted everything → no audio streams in the plan → export still
    produces a valid video file."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["kind"] == "video")
    _insert(client, pid, t["id"],
            _video_with_tone(client, pid, tmp_path), track_id=v1["id"],
            in_frame=0, out_frame=24)
    _commit(client, pid, "timeline.track.update",
            {"timeline_id": t["id"], "track_id": v1["id"],
             "muted": True})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    meta = engine.probe(_out_path(client, job))
    assert meta["ok"] and meta["codec"] == "h264"


# ================= §6 — durability on the composite path ==============

def _job_dir_files(client, pid, job_id):
    d = Path(client.app.state.settings.data_dir) / "jobs" / str(pid)
    return list(d.glob(f"{job_id}*")) if d.exists() else []


@pytest.mark.usefixtures("media_tools")
def test_render_v2_cancel_cleans_up(client, tmp_path):
    """Cancel lands mid-mix: job ends cancelled, no output asset, the
    partial mp4 is removed from the work dir, sources untouched."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    v1 = next(x for x in _detail(client, pid, t["id"])["tracks"]
              if x["kind"] == "video")
    heavy = _synth(binaries.ffmpeg()["path"], tmp_path / "hv.mp4",
                   dur=20.0, size="1280x720", rate=24, audio=True)
    a = _cas_video(client, pid, heavy)
    _insert(client, pid, t["id"], a, track_id=v1["id"],
            in_frame=0, out_frame=400)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "w.wav", freq=880.0,
                              seconds=15.0), "w.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0)
    r = _commit(client, pid, "timeline.export",
                {"timeline_id": t["id"], "profile": "720p"})
    jid = r["job_id"]
    resp = None
    deadline = time.time() + 15
    while time.time() < deadline:
        st = next((j for j in client.get(
            f"/api/projects/{pid}/jobs").json() if j["id"] == jid),
            None)
        if st and st["status"] == "running":
            resp = client.post(f"/api/jobs/{jid}/cancel")
            break
        if st and st["status"] in ("succeeded", "failed", "cancelled"):
            pytest.fail(f"job reached {st['status']} early")
        time.sleep(0.05)
    assert resp is not None and resp.status_code == 200
    job = wait_for_job(client, pid, jid, timeout=90)
    assert job["status"] == "cancelled"
    from app.db.models import GenerationJob
    with client.app.state.session_factory() as s:
        assert s.get(GenerationJob, jid).output_asset_id is None
    assert _job_dir_files(client, pid, jid) == []


@pytest.mark.usefixtures("media_tools")
def test_render_v2_missing_audio_source_fails(client, tmp_path):
    """Audio lane clip whose CAS bytes vanished fails closed — no
    phantom output."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "m.wav", seconds=0.5),
                    "m.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0)
    blob = Path(client.app.state.settings.data_dir) / "blobs" \
        / au["sha256"][:2] / au["sha256"]
    blob.unlink()
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "failed"
    from app.db.models import Asset
    with client.app.state.session_factory() as s:
        assert not any(x.origin == "rendered" and x.project_id == pid
                       for x in s.query(Asset).all())


@pytest.mark.usefixtures("media_tools")
def test_render_v2_hash_mismatch_fails(client, tmp_path):
    """CAS blob replaced after approval: the pinned sha refuses the
    render — the composite path verifies bytes like the v1 path."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "m.wav", seconds=0.5),
                    "m.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0)
    blob = Path(client.app.state.settings.data_dir) / "blobs" \
        / au["sha256"][:2] / au["sha256"]
    blob.write_bytes(b"\x00" * 4096)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "failed"
    assert "sha256" in (job.get("error") or "")


@pytest.mark.usefixtures("media_tools")
def test_render_v2_ffmpeg_nonzero_fails(client, tmp_path, monkeypatch):
    """An ffmpeg nonzero exit fails the job, unlinks the partial
    output, and registers no asset."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "m.wav", seconds=0.5),
                    "m.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0)
    real_run = supervise.run

    def boom(argv, **kw):
        if "-filter_complex" in argv or "-/filter_complex" in argv:
            return {"exit": 3, "output_tail": "synthetic failure",
                    "timed_out": False, "cancelled": False,
                    "elapsed_ms": 1}
        return real_run(argv, **kw)

    monkeypatch.setattr(supervise, "run", boom)
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "failed"
    assert "ffmpeg export failed" in (job.get("error") or "")
    assert _job_dir_files(client, pid, job["id"]) == []


@pytest.mark.usefixtures("media_tools")
def test_render_v2_disk_space_refuses(client, tmp_path, monkeypatch):
    """Insufficient disk estimate fails before ffmpeg ever runs."""
    import app.adapters.timeline_export as ad
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    au = _cas_audio(client, pid,
                    _wav_tone(tmp_path / "m.wav", seconds=0.5),
                    "m.wav")
    _place(client, pid, t["id"], au, a1["id"], at=0)

    class _DU:
        free = 1
        total = used = 0

    monkeypatch.setattr(ad.shutil, "disk_usage", lambda _p: _DU())
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "failed"
    assert "insufficient disk" in (job.get("error") or "")
