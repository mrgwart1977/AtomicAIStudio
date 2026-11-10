"""M4.0.1 — production media engine: binary verification, policy
validation, supervised execution, and degraded-mode honesty.

Fixtures are synthesized at test time by the locally installed dev ffmpeg
(a GPL build is fine as a dev tool — never bundled). Validation under test
runs against the approved BtbN LGPL binaries staged in backend/vendor.
"""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

import pytest

from app.domain.media import binaries, engine, supervise
from app.domain.media.policy import ValidationSpec


@pytest.fixture(autouse=True)
def _fresh_resolution(monkeypatch):
    binaries.reset_cache()
    monkeypatch.delenv("ATOMIC_FFMPEG", raising=False)
    monkeypatch.delenv("ATOMIC_FFPROBE", raising=False)
    yield
    binaries.reset_cache()


@pytest.fixture()
def dev_ffmpeg() -> str:
    """A full-featured ffmpeg for fixture synthesis — dev tool only.
    Prefers the system/PATH ffmpeg (gyan full build, GPL — fine for local
    dev, never bundled); falls back to the bundled LGPL binary."""
    import shutil
    which = shutil.which("ffmpeg")
    if which:
        return which
    tool = binaries.ffmpeg()
    if tool is not None:
        return tool["path"]
    pytest.skip("no ffmpeg available to synthesize fixtures")


def _mk(tmp_path: Path, ffmpeg: str, name: str, *args: str) -> Path:
    out = tmp_path / name
    r = subprocess.run([ffmpeg, "-y", "-v", "error", *args, str(out)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert out.is_file() and out.stat().st_size > 0
    return out


def webm_17f(tmp_path: Path, ffmpeg: str) -> Path:
    return _mk(tmp_path, ffmpeg, "ok.webm",
               "-f", "lavfi", "-i", "testsrc=duration=1:size=64x48:rate=24",
               "-c:v", "libvpx", "-frames:v", "17")


def mp4_12f(tmp_path: Path, ffmpeg: str) -> Path:
    # libx264 when the dev ffmpeg has it (gyan), else mpeg4 — every ffmpeg
    # build can encode that natively.
    for codec in ("libx264", "mpeg4"):
        try:
            return _mk(tmp_path, ffmpeg, "ok.mp4",
                       "-f", "lavfi", "-i",
                       "testsrc=duration=1:size=64x48:rate=24",
                       "-c:v", codec, "-pix_fmt", "yuv420p", "-frames:v", "12")
        except AssertionError:
            continue
    raise AssertionError("no usable mp4 encoder on dev ffmpeg")


# ------------------------------------------------------------ binaries


def test_bundled_binary_resolves_and_is_lgpl():
    tool = binaries.ffmpeg()
    assert tool is not None and tool["source"] == "bundled"
    info = binaries.toolchain_info()["ffmpeg"]
    assert "version" in info
    conf = info.get("configuration", "")
    assert "--disable-libx264" in conf and "--disable-libx265" in conf
    assert "--enable-gpl" not in conf and "--enable-nonfree" not in conf


def test_env_override_wins(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.setenv("ATOMIC_FFMPEG", str(fake))
    binaries.reset_cache()
    assert binaries.ffmpeg()["source"] == "env"


# ------------------------------------------------------------ supervise


def test_supervised_run_captures_output_and_exit():
    r = supervise.run([sys.executable, "-c", "print('hi')"], timeout_s=15)
    assert r["exit"] == 0 and "hi" in r["output_tail"]


def test_supervised_timeout_kills_tree():
    r = supervise.run(
        [sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.5)
    assert r["timed_out"] is True


def test_supervised_cancel_event():
    ev = threading.Event()
    ev.set()
    r = supervise.run(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        timeout_s=30, cancel=ev)
    assert r["cancelled"] is True


# ------------------------------------------------------------ probe


def test_probe_valid_webm(tmp_path, dev_ffmpeg):
    p = engine.probe(webm_17f(tmp_path, dev_ffmpeg))
    assert p["ok"] is True
    assert p["width"] == 64 and p["height"] == 48
    assert p["codec"] == "vp8" and p["fps"] == 24.0
    assert p["duration_s"] > 0 and p["probe_backend"] == "ffprobe"


def test_probe_valid_mp4(tmp_path, dev_ffmpeg):
    p = engine.probe(mp4_12f(tmp_path, dev_ffmpeg))
    assert p["ok"] is True and p["codec"] in ("h264", "mpeg4")


def test_probe_audio_only_rejected(tmp_path, dev_ffmpeg):
    audio = _mk(tmp_path, dev_ffmpeg, "audio.mp3",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1")
    p = engine.probe(audio)
    assert p["ok"] is False and "no video stream" in p["reason"]


# ------------------------------------------------------------ validate


def test_validate_full_pipeline(tmp_path, dev_ffmpeg):
    v = engine.validate(
        webm_17f(tmp_path, dev_ffmpeg),
        ValidationSpec(width=64, height=48, fps=24.0, frames=17))
    assert v["ok"] is True and v["fully_validated"] is True
    assert v["validation_mode"] == "full"
    assert v["decode"]["ok"] is True
    assert all(c["ok"] for c in v["checks"])
    assert v["frame_check"]["frames"] == 17


def test_validate_wrong_resolution_fails(tmp_path, dev_ffmpeg):
    v = engine.validate(
        webm_17f(tmp_path, dev_ffmpeg),
        ValidationSpec(width=320, height=240))
    assert v["ok"] is False and "width" in v["reason"]


def test_validate_wrong_frame_count_fails(tmp_path, dev_ffmpeg):
    v = engine.validate(
        webm_17f(tmp_path, dev_ffmpeg), ValidationSpec(frames=12))
    assert v["ok"] is False and "frame count" in v["reason"]


def test_validate_unexpected_codec_fails(tmp_path, dev_ffmpeg):
    v = engine.validate(
        webm_17f(tmp_path, dev_ffmpeg),
        ValidationSpec(codecs=frozenset({"h264"})))
    assert v["ok"] is False and "codec" in v["reason"]


def test_validate_empty_and_junk(tmp_path):
    empty = tmp_path / "empty.webm"
    empty.write_bytes(b"")
    assert engine.validate(empty)["reason"] == "file missing or empty"

    junk = tmp_path / "junk.webm"
    junk.write_bytes(b"not a video at all" * 100)
    v = engine.validate(junk)
    assert v["ok"] is False and v["fully_validated"] is False


def test_validate_truncated_fails(tmp_path, dev_ffmpeg):
    src = webm_17f(tmp_path, dev_ffmpeg)
    trunc = tmp_path / "trunc.webm"
    trunc.write_bytes(src.read_bytes()[: src.stat().st_size // 3])
    v = engine.validate(trunc, ValidationSpec(frames=17))
    assert v["ok"] is False


def test_validate_missing_video_stream_fails(tmp_path, dev_ffmpeg):
    audio = _mk(tmp_path, dev_ffmpeg, "a.m4a",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=1")
    v = engine.validate(audio)
    assert v["ok"] is False and "no video stream" in v["reason"]


def test_validate_cancelled(tmp_path, dev_ffmpeg):
    ev = threading.Event()
    ev.set()
    v = engine.validate(webm_17f(tmp_path, dev_ffmpeg),
                        ValidationSpec(frames=17), cancel=ev)
    assert v["ok"] is False and v["cancelled"] is True


def test_validate_no_decode_when_policy_allows(tmp_path, dev_ffmpeg):
    v = engine.validate(
        webm_17f(tmp_path, dev_ffmpeg),
        ValidationSpec(require_full_decode=False))
    assert v["ok"] is True and v["validation_mode"] == "probe_only"
    assert v["fully_validated"] is False


# ------------------------------------------------------------ degraded modes


def test_signature_only_never_fully_validated(tmp_path, monkeypatch):
    monkeypatch.setattr(binaries, "find_binary", lambda name: None)
    from app.domain.media_probe import container_signature_ok
    good = tmp_path / "x.webm"
    good.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
    v = engine.validate(good)
    assert v["validation_mode"] == "signature_only"
    assert v["fully_validated"] is False
    assert v["ok"] is container_signature_ok(good)

    bad = tmp_path / "y.webm"
    bad.write_bytes(b"junkjunk" * 16)
    v2 = engine.validate(bad)
    assert v2["ok"] is False


def test_probe_only_when_ffmpeg_missing(tmp_path, dev_ffmpeg, monkeypatch):
    clip = webm_17f(tmp_path, dev_ffmpeg)
    real = binaries.find_binary

    def no_ffmpeg(name):
        return None if name == "ffmpeg" else real(name)

    monkeypatch.setattr(binaries, "find_binary", no_ffmpeg)
    v = engine.validate(clip, ValidationSpec(width=64, height=48))
    assert v["ok"] is True and v["validation_mode"] == "probe_only"
    assert v["fully_validated"] is False


# ------------------------------------------------------------ concurrency


def test_concurrent_validations(tmp_path, dev_ffmpeg):
    clips = []
    for i in range(3):
        d = tmp_path / f"d{i}"
        d.mkdir()
        clips.append(webm_17f(d, dev_ffmpeg))
    results: list[dict] = []
    threads = [
        threading.Thread(
            target=lambda c=c: results.append(engine.validate(c, ValidationSpec(frames=17))))
        for c in clips]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    assert len(results) == 3
    assert all(r["ok"] and r["fully_validated"] for r in results)


# ------------------------------------------------------------ compat shim


def test_media_probe_shim_delegates(tmp_path, dev_ffmpeg):
    from app.domain import media_probe
    out = media_probe.probe_video(webm_17f(tmp_path, dev_ffmpeg))
    assert out["ok"] is True and out["probe_backend"] == "ffprobe"
    assert media_probe.container_signature_ok(tmp_path / "ok.webm")
