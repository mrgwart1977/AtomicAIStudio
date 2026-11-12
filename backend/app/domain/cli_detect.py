"""C1.5 §5 — provider-local CLI detection, phase 1 (detection only).

Read-only executable-path discovery for catalog-declared CLI metadata.
Hard boundaries:
- No subprocess, no shell, no `--version`/`--help` — binaries are never
  executed. Discovery is `shutil.which` (PATH lookup) plus stat checks
  inside catalog-declared install-hint directories only.
- No recursive filesystem scans; hint dirs are checked one level deep.
- A detected path is NOT proof the binary is the genuine provider CLI,
  is authenticated, or can run — it is reported as an untrusted path.
- Symlinks and Windows reparse points (junctions) are rejected rather
  than followed.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# sys.platform -> catalog platform token
_PLATFORM_MAP = {"win32": "windows", "linux": "linux", "darwin": "macos"}

_WIN_SUFFIXES = (".exe", ".cmd", ".bat", ".com")


def _current_platform() -> str:
    return _PLATFORM_MAP.get(sys.platform, sys.platform)


def _exe_variants(name: str, platform: str) -> list[str]:
    if platform == "windows":
        return [name] + [name + s for s in _WIN_SUFFIXES]
    return [name]


def _is_untrusted_link(p: Path) -> bool:
    """True if the path itself is a symlink, or (on Windows) a reparse
    point — never follow these to reach a candidate."""
    try:
        if p.is_symlink():
            return True
        if sys.platform == "win32":
            # FILE_ATTRIBUTE_REPARSE_POINT covers junctions/mount points
            # and other reparse data we should not traverse.
            return bool(os.stat(p, follow_symlinks=False).st_file_attributes  # type: ignore[attr-defined]
                        & 0x400)
    except (OSError, AttributeError):
        return True  # cannot stat safely -> do not trust
    return False


def _check_candidate(p: Path) -> str | None:
    """Return 'ok'/'untrusted_link'/None (not a usable regular file)."""
    try:
        if _is_untrusted_link(p):
            return "untrusted_link"
        st = os.stat(p, follow_symlinks=False)
        if not os.path.isfile(p) or not (st.st_mode & 0o111 or sys.platform == "win32"):
            return None
        return "ok"
    except OSError:
        return None


def detect_cli(provider: dict, *, platform: str | None = None) -> dict:
    """Detect whether a catalog-declared CLI executable is present.

    Returns a live result dict (paths included for UI display only —
    durable audit records must keep only status/counts):
      status: detected | not_found | unsupported_platform
      candidates: [{name, path, via}] — untrusted, unverified paths
      skipped_untrusted: int — symlink/reparse candidates rejected
    """
    cli = provider.get("cli") or {}
    names = cli.get("executables") or []
    plat = platform or _current_platform()

    if not names:
        return {"status": "unsupported_platform", "provider_key": provider.get("key"),
                "platform": plat, "candidates": [], "skipped_untrusted": 0,
                "reason": "no documented CLI"}
    if plat not in (cli.get("platforms") or []):
        return {"status": "unsupported_platform", "provider_key": provider.get("key"),
                "platform": plat, "candidates": [], "skipped_untrusted": 0,
                "reason": f"cli not documented for {plat}"}

    candidates: list[dict] = []
    skipped = 0
    seen_paths: set[str] = set()

    def _record(p: Path, name: str, via: str) -> None:
        nonlocal skipped
        verdict = _check_candidate(p)
        if verdict == "untrusted_link":
            skipped += 1
            return
        if verdict == "ok":
            sp = str(p)
            if sp not in seen_paths:
                seen_paths.add(sp)
                candidates.append({"name": name, "path": sp, "via": via})

    # 1) PATH discovery — OS-level lookup, no recursion, no execution.
    for name in names:
        hit = shutil.which(name)
        if hit:
            _record(Path(hit), name, "PATH")

    # 2) Catalog-declared install-hint dirs — one level, exact filenames.
    for d in cli.get("install_hint_dirs") or []:
        try:
            base = Path(os.path.expandvars(os.path.expanduser(d)))
        except (OSError, ValueError):
            continue
        if not base.is_dir() or _is_untrusted_link(base):
            continue
        for name in names:
            for variant in _exe_variants(name, plat):
                _record(base / variant, name, "install_dir")

    return {"status": "detected" if candidates else "not_found",
            "provider_key": provider.get("key"), "platform": plat,
            "candidates": candidates, "skipped_untrusted": skipped,
            "reason": "" if candidates else "no documented executable found on PATH or declared locations"}
