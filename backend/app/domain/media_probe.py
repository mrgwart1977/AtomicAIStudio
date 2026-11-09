"""FFmpeg/ffprobe discovery and video validation.

The app ships no ffmpeg binary; we locate one on PATH or via
ATOMIC_FFPROBE/ATOMIC_FFMPEG env overrides. When absent, video jobs still
run — validation degrades to "file exists + container signature" and the
probe fields stay empty (recorded as probe_unavailable so provenance is
honest about what was checked).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

_PROBE_TIMEOUT = 20

# Minimal container signatures used when ffprobe is unavailable.
_MAGIC = (
    (b"\x1a\x45\xdf\xa3", "webm/mkv"),
    (b"\x00\x00\x00", "mp4/mov (ftyp)"),  # verified further below
)


def find_binary(name: str) -> str | None:
    override = os.environ.get(f"ATOMIC_{name.upper()}")
    if override and Path(override).is_file():
        return override
    return shutil.which(name)


def ffprobe_path() -> str | None:
    return find_binary("ffprobe")


def ffmpeg_path() -> str | None:
    return find_binary("ffmpeg")


def probe_video(path: Path) -> dict[str, Any]:
    """Extract duration/resolution/fps/codec via ffprobe. Returns a dict
    with ok=False + reason when probing fails or ffprobe is missing —
    callers decide whether that's fatal."""
    ffprobe = ffprobe_path()
    if ffprobe is None:
        return {"ok": False, "reason": "ffprobe not found on PATH", "probe_unavailable": True}
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"ok": False, "reason": f"ffprobe failed: {exc}"}
    if out.returncode != 0:
        return {"ok": False, "reason": (out.stderr or "ffprobe error").strip()[:300]}
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "reason": "ffprobe returned unparseable output"}

    vstream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if vstream is None:
        return {"ok": False, "reason": "no video stream in container"}
    fps_raw = vstream.get("avg_frame_rate") or vstream.get("r_frame_rate") or "0/1"
    try:
        num, den = fps_raw.split("/")
        fps = round(int(num) / int(den), 3) if int(den) else 0.0
    except (ValueError, ZeroDivisionError, AttributeError):
        fps = 0.0
    return {
        "ok": True,
        "duration_s": round(float(data.get("format", {}).get("duration") or 0), 3),
        "width": int(vstream.get("width") or 0),
        "height": int(vstream.get("height") or 0),
        "fps": fps,
        "codec": vstream.get("codec_name") or "",
        "container": (data.get("format", {}).get("format_name") or "").split(",")[0],
        "frames": int(vstream.get("nb_frames") or 0),
    }


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
