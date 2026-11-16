"""N2-F review §1 — shared-fixture preview/render parity.

`fixtures/audio-parity/*.json` holds language-neutral timelines plus
independently-derived expectations (timing fields and per-checkpoint
envelope gains). BOTH implementations run the same fixtures:

- this file: fixture → ade-edl/2 document → `composite_plan` → plan
  fields; then each audio entry's REAL `_lane_chain` / `_embedded_chain`
  filter graph renders a constant-amplitude signal through ffmpeg —
  the measured output amplitude IS the render envelope, so no formula
  is duplicated in Python.
- `apps/desktop/.../previewPlan.parity.test.ts`: fixture →
  TimelineDetail → `buildPreviewPlan` → same fields + env().

Checkpoint gains are absolute-amplitude with a tolerance that covers
s16 quantization and resample interpolation at the edges.
"""
from __future__ import annotations

import json
import math
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import pytest

from app.domain.media import binaries
from app.domain import timeline_export as tx

FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "audio-parity")
ENV_TOL = 3e-3            # s16 quant + resample edge interpolation
FIELD_TOL = 1e-4


@pytest.fixture()
def media_tools():
    if binaries.ffmpeg() is None or binaries.ffprobe() is None:
        pytest.skip("ffmpeg/ffprobe not resolvable")


def _fixtures() -> list[Path]:
    return sorted(FIXTURE_DIR.glob("*.json"))


def _fx_edl(fx: dict) -> dict:
    """Canonical fixture → ade-edl/2 document (thin field mapping —
    no logic, only the shape the exporter consumes)."""
    num, den = fx["tl_fps"]["num"], fx["tl_fps"]["den"]
    tl_fps = Fraction(num, den)
    clips = []
    for c in fx["clips"]:
        rate = Fraction(c["src_fps"]["num"], c["src_fps"]["den"])
        extent_tl = Fraction(c["out_frame"] - c["in_frame"]) \
            * tl_fps / rate
        clips.append({
            "clip_id": c["id"], "track_id": c["track_id"],
            "sha256": c["sha256"],
            "start_frames": c["start_frames"],
            "in_frame": c["in_frame"], "out_frame": c["out_frame"],
            "timeline_frames": {"num": extent_tl.numerator,
                                "den": extent_tl.denominator},
            "source": {"fps_num": c["src_fps"]["num"],
                       "fps_den": c["src_fps"]["den"],
                       "sample_rate": rate.numerator
                       if rate.denominator == 1 else None},
            "gain_db": c.get("gain_db", 0.0),
            "fades": {"in": {"units": c.get("fade_in_units", 0),
                             "curve": c.get("fade_in_curve", "linear")},
                      "out": {"units": c.get("fade_out_units", 0),
                              "curve": c.get("fade_out_curve",
                                             "linear")}},
            "automation": [{"at_frames": p["at_frames"],
                            "gain_db": p["gain_db"]}
                           for p in c.get("automation", [])],
            "automation_interp": c.get("automation_interp", "linear"),
        })
    return {
        "format": "ade-edl/2",
        "timebase": {"num": num, "den": den},
        "tracks": [{"id": t["id"], "kind": t["kind"],
                    "position": t.get("position", 0),
                    "visible": t.get("visible", True),
                    "muted": t.get("muted", False),
                    "solo": t.get("solo", False),
                    "gain_db": t.get("gain_db", 0.0),
                    "automation": t.get("automation", []),
                    "automation_interp": t.get("automation_interp",
                                               "linear")}
                   for t in fx["tracks"]],
        "clips": clips,
        "crossfades": [dict(x) for x in fx.get("crossfades", [])],
    }


def _plan_clip_fields(a: dict, tl_fps: Fraction) -> dict:
    """Normalize a composite_plan audio entry into the same field
    names the preview plan exposes — seconds, not source units."""
    if a.get("embedded"):
        return {
            "tl_start_s": float(a["tl_start_s"]),
            "tl_end_s": float(a["tl_start_s"] + a["dur_s"]),
            "src_start_s": float(a["in_s"]),
            "seg_dur_s": float(a["dur_s"]),
            "embedded": True,
        }
    seg_units = a["seg_end_src"] - a["seg_start_src"]
    tl_start = (a["start_tl"] - a["head_tl"]) / tl_fps
    return {
        "tl_start_s": float(tl_start),
        "tl_end_s": float(tl_start + seg_units / a["rate"]),
        "src_start_s": a["seg_start_src"] / a["rate"],
        "seg_dur_s": seg_units / a["rate"],
        "embedded": False,
    }


def _measure_env(ffmpeg: str, tmp: Path, a: dict,
                 tl_fps: Fraction, checkpoints: list[dict]) -> list[float]:
    """Render the entry's REAL filter chain on a constant 0.8 signal;
    measured output amplitude is the envelope. No formula reuse —
    ffmpeg + the production chain do the work."""
    rate = int(a.get("rate") or 48000)
    if a.get("embedded"):
        chain = tx._embedded_chain(0, a, tl_fps)
        total_s = float(a["in_s"] + a["dur_s"]) + 0.5
    else:
        chain = tx._lane_chain(0, a, tl_fps)
        total_s = a["seg_end_src"] / rate + 0.5
    src = tmp / "const.wav"
    subprocess.run(
        [ffmpeg, "-y", "-v", "error",
         "-f", "lavfi", "-i",
         f"aevalsrc=exprs=0.8|0.8:d={total_s:.3f}:s={rate}",
         str(src)], check=True)
    # PCM goes to stdout, not a file — Windows Defender was
    # quarantining .pcm payloads in temp dirs ("virus or potentially
    # unwanted software"), making the measured file unopenable.
    r = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", str(src),
         "-filter_complex", chain, "-f", "s16le", "-ac", "2", "-"],
        capture_output=True)
    assert r.returncode == 0, f"ffmpeg failed: {r.stderr[:300]}"
    pcm = r.stdout
    n = len(pcm) // 4  # stereo s16 frames

    def at(u: float) -> float:
        idx = int(u * 48000 / rate) if rate != 48000 else int(u)
        if idx < 0 or idx >= n:
            return 0.0
        v = int.from_bytes(pcm[idx * 4:idx * 4 + 2],
                           "little", signed=True)
        return abs(v) / 32768.0 / 0.8
    return [at(c["u"]) for c in checkpoints]


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_fixture_plan_fields(path, media_tools):
    fx = json.loads(path.read_text())
    plan = tx.composite_plan(_fx_edl(fx), fx.get("probes") or {})
    tl_fps = Fraction(fx["tl_fps"]["num"], fx["tl_fps"]["den"])
    got = {a["clip_id"]: a for a in plan["audio_clips"]}
    exp = fx["expect"]
    assert sorted(got) == sorted(exp["clip_ids"]), (
        f"{path.name}: audible clip set "
        f"{sorted(got)} != {sorted(exp['clip_ids'])}")
    for cid, e in exp["clips"].items():
        f = _plan_clip_fields(got[cid], tl_fps)
        for key in ("tl_start_s", "tl_end_s", "src_start_s",
                    "seg_dur_s"):
            assert f[key] == pytest.approx(e[key], abs=FIELD_TOL), (
                f"{path.name}:{cid}.{key} {f[key]} != {e[key]}")
        if "embedded" in e:
            assert f["embedded"] == e["embedded"]


@pytest.mark.parametrize("path", _fixtures(), ids=lambda p: p.stem)
def test_fixture_envelope(path, media_tools, tmp_path):
    fx = json.loads(path.read_text())
    plan = tx.composite_plan(_fx_edl(fx), fx.get("probes") or {})
    tl_fps = Fraction(fx["tl_fps"]["num"], fx["tl_fps"]["den"])
    got = {a["clip_id"]: a for a in plan["audio_clips"]}
    ffmpeg = binaries.ffmpeg()["path"]
    for cid, e in fx["expect"]["clips"].items():
        cps = e.get("env") or []
        if not cps:
            continue
        (tmp_path / cid).mkdir(parents=True, exist_ok=True)
        measured = _measure_env(ffmpeg, tmp_path / cid, got[cid],
                                tl_fps, cps)
        for cp, m in zip(cps, measured):
            assert m == pytest.approx(cp["g"], abs=ENV_TOL), (
                f"{path.name}:{cid} env({cp['u']})={m:.6f} "
                f"expected {cp['g']}")
