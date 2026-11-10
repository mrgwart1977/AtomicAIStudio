"""Compatibility shim — media validation lives in `app.domain.media`.

`probe_video()` keeps its legacy dict contract for existing callers/tests;
it now delegates to `media.engine.probe()`. The PyAV fallback remains for
dev environments with no ffmpeg/ffprobe binaries (PyAV is dev-only — it
is excluded from the frozen build). Container-signature checking is the
last-resort degraded mode and NEVER marks a video fully validated.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.domain.media import binaries, engine


def find_binary(name: str) -> str | None:
    """Legacy patch point — returns just the path (or None)."""
    hit = binaries.find_binary(name)
    return hit["path"] if hit else None


def ffprobe_path() -> str | None:
    return find_binary("ffprobe")


def ffmpeg_path() -> str | None:
    return find_binary("ffmpeg")


def _probe_pyav(path: Path) -> dict[str, Any] | None:
    """ffprobe alternative: PyAV (bundled ffmpeg libs, optional dep).
    Returns None when PyAV is not importable."""
    try:
        import av  # noqa: PLC0415 — optional dependency, imported lazily
    except ImportError:
        return None
    try:
        with av.open(str(path)) as c:
            v = next((s for s in c.streams if s.type == "video"), None)
            if v is None:
                return {"ok": False, "reason": "no video stream in container"}
            fps = float(v.average_rate or 0)
            dur = float(c.duration or 0) / 1_000_000
            return {
                "ok": True,
                "duration_s": round(dur, 3),
                "width": int(v.width or 0),
                "height": int(v.height or 0),
                "fps": round(fps, 3),
                "codec": v.codec_context.name or "",
                "container": c.format.name or "",
                "frames": int(v.frames or 0),
                "probe_backend": "pyav",
            }
    except Exception as exc:  # av.AVError + file errors
        return {"ok": False, "reason": f"pyav probe failed: {exc}"}


def probe_video(path: Path) -> dict[str, Any]:
    """Extract duration/resolution/fps/codec. Order: ffprobe binary →
    PyAV (dev only) → unavailable. Returns ok=False + reason on failure;
    callers decide whether that's fatal."""
    if ffprobe_path() is not None:
        return engine.probe(path)
    out = _probe_pyav(path)
    if out is not None:
        return out
    return {"ok": False, "reason": "no ffprobe/PyAV available", "probe_unavailable": True}


def container_signature_ok(path: Path) -> bool:
    """Fallback check when ffprobe is absent: does the file at least start
    with a known video container signature?"""
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return False
    if head[:4] == b"\x1a\x45\xdf\xa3":  # EBML (webm/mkv)
        return True
    return head[4:8] == b"ftyp"  # ISO-BMFF (mp4/mov)
