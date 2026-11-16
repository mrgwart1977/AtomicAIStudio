"""N2-F — audio hardening: extended fade curves (s_curve,
exponential), step automation interpolation, and preview-plan
parity. Unit tests pin the math; real-ffmpeg renders measure
decoded PCM against the curve contracts.
"""
import math

import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import _commit
from tests.test_c5a_timeline import _tl, _detail
from tests.test_c5b_export import _export
from tests.test_n2a_audio import _atrack, _place
from tests.test_n2e_mixing import (_wav_tone, _cas_audio, _out_path,
                                   _decode_pcm, _mag)

from app.domain import timeline as tld
from app.domain import timeline_export as tx

AUDIO_RATE = tx.AUDIO_RATE


@pytest.fixture()
def media_tools():
    from app.domain.media import binaries
    if binaries.ffmpeg() is None or binaries.ffprobe() is None:
        pytest.skip("ffmpeg/ffprobe not resolvable")


# ---------- unit math ----------

def test_fade_gain_new_curves():
    # s_curve: sin²/cos² pair — midpoint 0.5/0.5, gentle both ends
    assert tld.fade_gain("s_curve", 50, 100, out=False) \
        == pytest.approx(0.5, abs=1e-6)
    assert tld.fade_gain("s_curve", 50, 100, out=True) \
        == pytest.approx(0.5, abs=1e-6)
    assert tld.fade_gain("s_curve", 25, 100, out=False) \
        == pytest.approx(math.sin(math.pi * 0.125) ** 2, abs=1e-6)
    # exponential (afade exp contract): in g(t)=e^{-11.5129(1−t)},
    # out g(t)=e^{-11.5129t} — −50 dB over the full fade
    assert tld.fade_gain("exponential", 50, 100, out=False) \
        == pytest.approx(math.exp(-11.512925464970227 * 0.5), abs=1e-6)
    assert tld.fade_gain("exponential", 0, 100, out=True) \
        == pytest.approx(1.0, abs=1e-6)
    assert tld.fade_gain("exponential", 100, 100, out=False) \
        == pytest.approx(1.0, abs=1e-6)
    # unity for empty fades stays
    assert tld.fade_gain("exponential", 0, 0, out=False) == 1.0


def test_fade_curve_vocabulary():
    for c in ("linear", "equal_power", "s_curve", "exponential"):
        assert tld.check_fade_curve(c) == c
    with pytest.raises(tld.TimelineError):
        tld.check_fade_curve("wibble")


def test_automation_gain_db_step():
    pts = [{"at_frames": 0, "gain_db": 0.0},
           {"at_frames": 10, "gain_db": -12.0}]
    assert tld.automation_gain_db(pts, 5) == pytest.approx(-6.0)
    assert tld.automation_gain_db(pts, 5, interp="step") \
        == pytest.approx(0.0)
    assert tld.automation_gain_db(pts, 15, interp="step") \
        == pytest.approx(-12.0)


def test_set_automation_interp_validation(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    a = _cas_audio(client, pid, _wav_tone_for(client, pid), "a.wav")
    _place(client, pid, t["id"], a, a1["id"])
    clip = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.automation",
            {"timeline_id": t["id"], "clip_id": clip["id"],
             "points": [{"at_frames": 0, "gain_db": -3.0}],
             "interp": "step"})
    d = _detail(client, pid, t["id"])
    assert d["clips"][0].get("automation_interp") == "step"


def test_set_automation_bad_interp_refused(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    a = _cas_audio(client, pid, _wav_tone_for(client, pid), "a.wav")
    _place(client, pid, t["id"], a, a1["id"])
    clip = _detail(client, pid, t["id"])["clips"][0]
    r = _commit(client, pid, "timeline.clip.automation",
                {"timeline_id": t["id"], "clip_id": clip["id"],
                 "points": [{"at_frames": 0, "gain_db": 0.0}],
                 "interp": "cubic"}, expect=422)
    assert r.status_code == 422


def _wav_tone_for(client, pid, **kw):
    import tempfile
    from pathlib import Path
    return _wav_tone(Path(tempfile.mkdtemp()) / "t.wav", seconds=2.0,
                     **kw)


# ---------- persistence ----------

def test_interp_survives_split_and_duplicate(client):
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    a = _cas_audio(client, pid, _wav_tone_for(client, pid), "a.wav")
    _place(client, pid, t["id"], a, a1["id"])
    clip = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.automation",
            {"timeline_id": t["id"], "clip_id": clip["id"],
             "points": [{"at_frames": 4, "gain_db": -6.0}],
             "interp": "step"})
    # split at an interior whole frame
    _commit(client, pid, "timeline.clip.split",
            {"timeline_id": t["id"], "clip_id": clip["id"],
             "at_frame": 12})
    for c in _detail(client, pid, t["id"])["clips"]:
        assert c.get("automation_interp") == "step"
    # duplicate timeline preserves interp
    _commit(client, pid, "timeline.duplicate",
            {"timeline_id": t["id"], "name": "dup"})
    tls = client.get(f"/api/projects/{pid}/timelines").json()
    dup = next(x for x in tls if x["name"] == "dup")
    for c in _detail(client, pid, dup["id"])["clips"]:
        assert c.get("automation_interp") == "step"


# ---------- real-ffmpeg renders ----------

@pytest.mark.usefixtures("media_tools")
def test_render_s_curve_fade_in(client, tmp_path):
    """s_curve → afade hsin: envelope tracks sin²(πt/2N)."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    a = _cas_audio(client, pid,
                   _wav_tone(tmp_path / "s.wav", seconds=2.0), "s.wav")
    _place(client, pid, t["id"], a, a1["id"])
    clip = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.fade",
            {"timeline_id": t["id"], "clip_id": clip["id"],
             "fade_in_units": 48000, "fade_in_curve": "s_curve"})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    ref = _mag(pcm, 440, int(1.4 * AUDIO_RATE), int(1.8 * AUDIO_RATE))
    for u, exp in ((0.25, math.sin(math.pi * 0.125) ** 2),
                   (0.5, 0.5),
                   (0.75, math.sin(math.pi * 0.375) ** 2)):
        c = int(u * AUDIO_RATE)
        g = _mag(pcm, 440, c - 1500, c + 1500) / ref
        assert abs(g - exp) < 0.13, (u, g, exp)


@pytest.mark.usefixtures("media_tools")
def test_render_exponential_fade_in(client, tmp_path):
    """exponential → afade exp: envelope tracks e^{-11.5129(1−t)}
    (≈10^{-5(1−t)}) — −50 dB over the fade, verified at the tail
    where the level is measurable."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    a = _cas_audio(client, pid,
                   _wav_tone(tmp_path / "e.wav", seconds=2.0), "e.wav")
    _place(client, pid, t["id"], a, a1["id"])
    clip = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.fade",
            {"timeline_id": t["id"], "clip_id": clip["id"],
             "fade_in_units": 48000, "fade_in_curve": "exponential"})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    ref = _mag(pcm, 440, int(1.4 * AUDIO_RATE), int(1.8 * AUDIO_RATE))
    # exp fades are near-silent early (−50 dB at t=0) — probe the
    # last quarter where magnitudes clear the noise floor
    for u, exp in ((0.8, math.exp(-11.512925464970227 * 0.2)),
                   (0.9, math.exp(-11.512925464970227 * 0.1)),
                   (0.96, math.exp(-11.512925464970227 * 0.04))):
        c = int(u * AUDIO_RATE)
        g = _mag(pcm, 440, c - 1000, c + 1000) / ref
        assert g / exp == pytest.approx(1.0, rel=0.3), (u, g, exp)


@pytest.mark.usefixtures("media_tools")
def test_render_step_automation(client, tmp_path):
    """step interp: gain holds constant then jumps — no ramp."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    a = _cas_audio(client, pid,
                   _wav_tone(tmp_path / "st.wav", seconds=2.0), "st.wav")
    _place(client, pid, t["id"], a, a1["id"])
    clip = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.automation",
            {"timeline_id": t["id"], "clip_id": clip["id"],
             "points": [{"at_frames": 0, "gain_db": 0.0},
                        {"at_frames": 24, "gain_db": -12.0}],
             "interp": "step"})
    job = _export(client, pid, t["id"], profile="720p")
    assert job["status"] == "succeeded", job.get("error")
    pcm = _decode_pcm(_out_path(client, job), tmp_path)
    early = _mag(pcm, 440, int(0.3 * AUDIO_RATE),
                 int(0.7 * AUDIO_RATE))
    late = _mag(pcm, 440, int(1.3 * AUDIO_RATE), int(1.7 * AUDIO_RATE))
    # step holds 0 dB until t=1.0s (frame 24 @24fps), then −12 dB
    assert late / early == pytest.approx(10 ** (-12 / 20), rel=0.12)
    # flat within each half — compare two windows inside the late half
    l1 = _mag(pcm, 440, int(1.2 * AUDIO_RATE), int(1.4 * AUDIO_RATE))
    l2 = _mag(pcm, 440, int(1.5 * AUDIO_RATE), int(1.7 * AUDIO_RATE))
    assert l1 / l2 == pytest.approx(1.0, rel=0.1)


@pytest.mark.usefixtures("media_tools")
def test_render_s_curve_crossfade_shape(client, tmp_path):
    """s_curve crossfade → afade hsin pair: g_out=cos², g_in=sin² —
    also unity-power (sin²+cos²=1), distinct from qsin's sin/cos."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    a = _cas_audio(client, pid,
                   _wav_tone(tmp_path / "xa.wav", seconds=3.0), "xa.wav")
    b = _cas_audio(client, pid,
                   _wav_tone(tmp_path / "xb.wav", seconds=3.0,
                             freq=880.0), "xb.wav")
    _place(client, pid, t["id"], a, a1["id"], at=0,
           in_frame=0, out_frame=72000)
    _place(client, pid, t["id"], b, a1["id"], at=36,
           in_frame=48000, out_frame=96000)
    clips = _detail(client, pid, t["id"])["clips"]
    A = next(c for c in clips if c["start_frames"] == 0)
    B = next(c for c in clips if c["start_frames"] == 36)
    _commit(client, pid, "timeline.crossfade.set",
            {"timeline_id": t["id"], "out_clip_id": A["id"],
             "in_clip_id": B["id"], "tl_start": 30, "tl_frames": 12,
             "curve": "s_curve"})
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
        exp_out = math.cos(math.pi * u / 2) ** 2
        exp_in = math.sin(math.pi * u / 2) ** 2
        assert abs(ga - exp_out) < 0.13, (u, ga, exp_out)
        assert abs(gb - exp_in) < 0.13, (u, gb, exp_in)


def test_plan_includes_interp_and_new_curves(client):
    """composite_plan passes automation_interp + s_curve through to
    the audio entry."""
    pid = make_project(client)["id"]
    t = _tl(client, pid)
    a1 = _atrack(client, pid, t["id"])
    a = _cas_audio(client, pid, _wav_tone_for(client, pid), "a.wav")
    _place(client, pid, t["id"], a, a1["id"])
    clip = _detail(client, pid, t["id"])["clips"][0]
    _commit(client, pid, "timeline.clip.automation",
            {"timeline_id": t["id"], "clip_id": clip["id"],
             "points": [{"at_frames": 2, "gain_db": -6.0}],
             "interp": "step"})
    _commit(client, pid, "timeline.clip.fade",
            {"timeline_id": t["id"], "clip_id": clip["id"],
             "fade_in_units": 4800, "fade_in_curve": "s_curve"})
    from app.domain.timeline_export import composite_plan
    with client.app.state.session_factory() as s:
        from app.db.models import Timeline as Tl
        tl = s.get(Tl, t["id"])
        edl = tld.edl_json_v2(s, tl)
    plan = composite_plan(edl, {})
    ac = plan["audio_clips"][0]
    assert ac["automation_interp"] == "step"
    assert ac["fade_in"]["curve"] == "s_curve"
