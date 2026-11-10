"""Media engine — probe, full-decode check, and policy validation.

Everything here treats the input file as untrusted: argv lists only, no
shell, resolved absolute paths, supervised spawn, bounded runtime.

`validate()` is the single entry used by the generation commit boundary
(and future editor/import paths). A container signature alone NEVER
marks a video fully validated — `fully_validated` requires a real
ffprobe pass AND a clean full-stream ffmpeg decode.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.domain.media import binaries, supervise
from app.domain.media.policy import ValidationSpec


def probe(path: Path, *, timeout_s: float = 30.0,
          cancel: threading.Event | None = None) -> dict[str, Any]:
    """ffprobe metadata: container, streams, duration, dims, fps, codec."""
    tool = binaries.ffprobe()
    if tool is None:
        return {"ok": False, "reason": "ffprobe unavailable", "probe_unavailable": True}
    r = supervise.run(
        [tool["path"], "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        timeout_s=timeout_s, cancel=cancel)
    if r["timed_out"]:
        return {"ok": False, "reason": "ffprobe timed out"}
    if r["cancelled"]:
        return {"ok": False, "reason": "ffprobe cancelled", "cancelled": True}
    if r["exit"] != 0:
        return {"ok": False, "reason": (r["output_tail"] or "ffprobe error").strip()[:300] or "ffprobe error"}
    try:
        data = json.loads(_stdout_json(r["output_tail"]))
    except json.JSONDecodeError:
        return {"ok": False, "reason": "ffprobe returned unparseable output"}
    vstream = next((s for s in data.get("streams", [])
                    if s.get("codec_type") == "video"), None)
    if vstream is None:
        return {"ok": False, "reason": "no video stream in container"}
    fps_raw = vstream.get("avg_frame_rate") or vstream.get("r_frame_rate") or "0/1"
    try:
        num, den = fps_raw.split("/")
        fps = round(int(num) / int(den), 3) if int(den) else 0.0
    except (ValueError, ZeroDivisionError, AttributeError):
        fps = 0.0
    astreams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    return {
        "ok": True,
        "duration_s": round(float(data.get("format", {}).get("duration") or 0), 3),
        "width": int(vstream.get("width") or 0),
        "height": int(vstream.get("height") or 0),
        "fps": fps,
        "codec": vstream.get("codec_name") or "",
        "container": (data.get("format", {}).get("format_name") or "").split(",")[0],
        "frames": int(vstream.get("nb_frames") or 0),
        "audio_streams": len(astreams),
        "probe_backend": "ffprobe",
    }


def _stdout_json(text: str) -> str:
    """ffprobe JSON lands on stdout; supervise merges stderr — take the
    largest JSON-looking slice."""
    start = text.find("{")
    end = text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text


def count_decoded_frames(path: Path, *, timeout_s: float = 60.0,
                         cancel: threading.Event | None = None) -> dict[str, Any]:
    """ffprobe -count_frames: number of frames actually decodable."""
    tool = binaries.ffprobe()
    if tool is None:
        return {"ok": False, "reason": "ffprobe unavailable"}
    r = supervise.run(
        [tool["path"], "-v", "error", "-select_streams", "v:0",
         "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "csv=p=0", str(path)],
        timeout_s=timeout_s, cancel=cancel)
    if r["timed_out"]:
        return {"ok": False, "reason": "frame count timed out"}
    if r["cancelled"]:
        return {"ok": False, "reason": "frame count cancelled", "cancelled": True}
    if r["exit"] != 0:
        return {"ok": False, "reason": (r["output_tail"] or "frame count failed").strip()[:300]}
    n: int | None = None
    for line in reversed(r["output_tail"].strip().splitlines()):
        line = line.strip()
        if line.isdigit():
            n = int(line)
            break
    if n is None:
        return {"ok": False, "reason": "frame count unparseable"}
    return {"ok": True, "frames": n}


def decode_check(path: Path, *, timeout_s: float = 120.0,
                 cancel: threading.Event | None = None) -> dict[str, Any]:
    """Full video-stream decode. `-xerror` makes any decode error a
    nonzero exit; stderr tail is retained for diagnostics."""
    tool = binaries.ffmpeg()
    if tool is None:
        return {"ok": False, "ran": False, "reason": "ffmpeg unavailable"}
    r = supervise.run(
        [tool["path"], "-nostdin", "-v", "error", "-xerror",
         "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
        timeout_s=timeout_s, cancel=cancel)
    out: dict[str, Any] = {
        "ran": True,
        "exit": r["exit"],
        "elapsed_ms": r["elapsed_ms"],
    }
    if r["timed_out"]:
        out.update(ok=False, reason="decode timed out")
    elif r["cancelled"]:
        out.update(ok=False, reason="decode cancelled", cancelled=True)
    elif r["exit"] != 0:
        tail = (r["output_tail"] or "").strip()
        out.update(ok=False, reason=f"decode failed (exit {r['exit']}): {tail[-300:]}"
                   if tail else f"decode failed (exit {r['exit']})")
        out["stderr_tail"] = tail[-1000:]
    else:
        out["ok"] = True
    return out


def validate(path: Path, spec: ValidationSpec | None = None,
             cancel: threading.Event | None = None) -> dict[str, Any]:
    """Ordered technical-integrity validation against `spec`.

    Result keys: ok, fully_validated, validation_mode
    (full / probe_only / signature_only / unavailable), reason on first
    failure, checks[] trace, probe, decode, frame_check, toolchain.
    """
    spec = spec or ValidationSpec()
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: Any = None) -> bool:
        checks.append({"name": name, "ok": ok, "detail": detail})
        return ok

    def cancelled() -> bool:
        if cancel is not None and cancel.is_set():
            result.update(reason="validation cancelled", cancelled=True)
            return True
        return False

    result: dict[str, Any] = {
        "ok": False, "fully_validated": False, "validation_mode": "unavailable",
        "checks": checks, "toolchain": binaries.toolchain_info(),
    }

    if cancelled():
        return result

    if not check("exists_nonempty", path.is_file() and path.stat().st_size > 0,
                 None if path.is_file() else "missing"):
        result["reason"] = "file missing or empty"
        return result

    have_ffprobe = binaries.ffprobe() is not None
    have_ffmpeg = binaries.ffmpeg() is not None

    if not have_ffprobe and not have_ffmpeg:
        # Degraded dev-only path: signature honesty, never "validated".
        from app.domain.media_probe import container_signature_ok
        sig = container_signature_ok(path)
        result["validation_mode"] = "signature_only"
        check("container_signature", sig)
        result["ok"] = sig
        result["probe_unavailable"] = True
        if not sig:
            result["reason"] = "unrecognized container signature"
        return result

    p = probe(path, timeout_s=spec.probe_timeout_s, cancel=cancel)
    result["probe"] = p
    if p.get("cancelled"):
        result.update(reason="validation cancelled", cancelled=True)
        return result
    if not check("ffprobe", p.get("ok", False), p.get("reason")):
        result.update(reason=f"probe failed — {p.get('reason')}",
                      validation_mode="probe_only")
        return result

    if not check("codec_allowed", p["codec"] in spec.codecs, p["codec"]):
        result.update(reason=f"unexpected codec {p['codec']!r}",
                      validation_mode="probe_only")
        return result
    if spec.width and not check("width", p["width"] == spec.width,
                                {"got": p["width"], "want": spec.width}):
        result.update(reason=f"width {p['width']} != expected {spec.width}",
                      validation_mode="probe_only")
        return result
    if spec.height and not check("height", p["height"] == spec.height,
                                 {"got": p["height"], "want": spec.height}):
        result.update(reason=f"height {p['height']} != expected {spec.height}",
                      validation_mode="probe_only")
        return result
    if spec.fps and not check(
            "fps", abs(p["fps"] - spec.fps) <= spec.fps * spec.fps_rel_tol,
            {"got": p["fps"], "want": spec.fps}):
        result.update(reason=f"fps {p['fps']} != expected {spec.fps}",
                      validation_mode="probe_only")
        return result

    if spec.duration_s:
        tol = spec.duration_tolerance_s()
        dur_ok = abs(p["duration_s"] - spec.duration_s) <= tol
        if not check("duration", dur_ok,
                     {"got": p["duration_s"], "want": spec.duration_s, "tol": tol}):
            result.update(reason=f"duration {p['duration_s']}s outside "
                                 f"{spec.duration_s}±{tol}s",
                          validation_mode="probe_only")
            return result

    if not spec.require_full_decode:
        result.update(ok=True, validation_mode="probe_only")
        return result
    if cancelled():
        return result

    if not have_ffmpeg:
        # ffprobe present but no decoder binary: honest degraded state.
        result.update(ok=True, validation_mode="probe_only",
                      reason="full decode skipped — ffmpeg unavailable")
        return result

    d = decode_check(path, timeout_s=spec.decode_timeout_s, cancel=cancel)
    result["decode"] = d
    if d.get("cancelled"):
        result.update(reason="validation cancelled", cancelled=True,
                      validation_mode="probe_only")
        return result
    if not check("full_decode", d.get("ok", False), d.get("reason")):
        result.update(reason=d.get("reason"), validation_mode="probe_only")
        return result

    if spec.frames is not None:
        fc = count_decoded_frames(path, timeout_s=spec.decode_timeout_s,
                                  cancel=cancel)
        result["frame_check"] = fc
        if fc.get("cancelled"):
            result.update(reason="validation cancelled", cancelled=True)
            return result
        if not check("frame_count", fc.get("ok") and fc.get("frames") == spec.frames,
                     {"got": fc.get("frames"), "want": spec.frames,
                      "reason": fc.get("reason")}):
            got = fc.get("frames")
            result.update(
                reason=(f"decoded frame count {got} != expected {spec.frames}"
                        if fc.get("ok") else f"frame count failed — {fc.get('reason')}"),
                validation_mode="probe_only")
            return result

    result.update(ok=True, fully_validated=True, validation_mode="full")
    return result
