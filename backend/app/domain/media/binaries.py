"""ffmpeg/ffprobe binary resolution + provenance capture.

Resolution order (first hit wins):
  1. ATOMIC_FFMPEG / ATOMIC_FFPROBE env overrides (dev/test only)
  2. Bundled binaries — frozen: _MEIPASS/ffmpeg/bin; dev: backend/vendor/ffmpeg/bin
  3. PATH (developer machines / unbundled installs)

`toolchain_info()` records which binary answered + its version/build config
into validation provenance so audit records name the exact tool used.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_cache: dict[str, str | None] = {}


def _bundled_bin() -> Path | None:
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates += [base / "ffmpeg" / "bin", base.parent / "ffmpeg" / "bin"]
    else:
        # app/domain/media/binaries.py → parents[3] == backend/
        candidates.append(
            Path(__file__).resolve().parents[3] / "vendor" / "ffmpeg" / "bin")
    for cand in candidates:
        if (cand / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")).is_file():
            return cand
    return None


def find_binary(name: str) -> dict[str, Any] | None:
    """Resolve one tool. Returns {path, source} or None."""
    if name in _cache:
        cached = _cache[name]
        return {"path": cached[0], "source": cached[1]} if cached else None
    exe = f"{name}.exe" if sys.platform == "win32" else name
    found: tuple[str, str] | None = None
    override = os.environ.get(f"ATOMIC_{name.upper()}")
    if override and Path(override).is_file():
        found = (override, "env")
    else:
        bundled = _bundled_bin()
        if bundled and (bundled / exe).is_file():
            found = (str(bundled / exe), "bundled")
        else:
            which = shutil.which(name)
            if which:
                found = (which, "path")
    _cache[name] = found
    return {"path": found[0], "source": found[1]} if found else None


def ffmpeg() -> dict[str, Any] | None:
    return find_binary("ffmpeg")


def ffprobe() -> dict[str, Any] | None:
    return find_binary("ffprobe")


def _banner(path: str) -> dict[str, str]:
    """`ffmpeg -version` style first line + build configuration."""
    try:
        out = subprocess.run([path, "-version"], capture_output=True,
                             text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    lines = (out.stdout or "").splitlines()
    info: dict[str, str] = {}
    if lines:
        info["version"] = lines[0].strip()[:200]
    for line in lines:
        if line.strip().startswith("configuration:"):
            info["configuration"] = line.strip()[len("configuration:"):].strip()[:2000]
            break
    return info


def toolchain_info() -> dict[str, Any]:
    """Resolved tool paths + version banners for validation provenance."""
    out: dict[str, Any] = {}
    for name in ("ffmpeg", "ffprobe"):
        hit = find_binary(name)
        if hit is None:
            out[name] = {"available": False}
            continue
        out[name] = {"available": True, "path": hit["path"],
                     "source": hit["source"], **_banner(hit["path"])}
    return out


def reset_cache() -> None:
    """Tests: re-resolve after monkeypatching env/PATH."""
    _cache.clear()
