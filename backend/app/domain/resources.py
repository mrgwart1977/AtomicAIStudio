"""System memory + commit-headroom measurement (Windows-first, no deps).

Distinguishes physical RAM from committed virtual memory — paging is NOT
treated as an acceptable substitute for RAM (issue #28).
"""
from __future__ import annotations

import ctypes
import sys
from typing import Any

_IS_WINDOWS = sys.platform == "win32"
GIB = 1024 ** 3

if _IS_WINDOWS:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),          # % phys in use
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]


def system_memory() -> dict[str, Any]:
    """Snapshot of physical RAM + commit charge. Values in bytes."""
    if _IS_WINDOWS:
        s = MEMORYSTATUSEX()
        s.dwLength = ctypes.sizeof(s)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
        commit_limit = int(s.ullTotalPageFile)      # RAM + pagefile = commit limit
        commit_charge = commit_limit - int(s.ullAvailPageFile)
        return {
            "total_phys_bytes": int(s.ullTotalPhys),
            "avail_phys_bytes": int(s.ullAvailPhys),
            "phys_load_pct": int(s.dwMemoryLoad),
            "commit_limit_bytes": commit_limit,
            "commit_charge_bytes": commit_charge,
            "commit_headroom_bytes": int(s.ullAvailPageFile),
        }
    # POSIX fallback — good enough for dev machines
    try:
        pages = __import__("os").sysconf("SC_PHYS_PAGES")
        avail = __import__("os").sysconf("SC_AVPHYS_PAGES")
        size = __import__("os").sysconf("SC_PAGE_SIZE")
        total, free = pages * size, avail * size
        return {"total_phys_bytes": total, "avail_phys_bytes": free,
                "phys_load_pct": round(100 * (1 - free / max(total, 1))),
                "commit_limit_bytes": total, "commit_charge_bytes": total - free,
                "commit_headroom_bytes": free}
    except (ValueError, OSError, AttributeError):
        return {"total_phys_bytes": 0, "avail_phys_bytes": 0, "phys_load_pct": 0,
                "commit_limit_bytes": 0, "commit_charge_bytes": 0,
                "commit_headroom_bytes": 0}


def safety_margins(overrides: dict | None = None) -> dict[str, int]:
    """Configurable guard rails (env-overridable for tests; caller may
    pass DB-setting overrides in the same byte-keyed shape)."""
    import os
    m = {
        # keep this much physical RAM free for the OS/apps at all times
        "os_headroom_bytes": int(os.environ.get("ATOMIC_OS_HEADROOM_BYTES",
                                                str(int(4 * GIB)))),
        # abort a running worker if commit headroom falls below this
        "critical_commit_headroom_bytes": int(
            os.environ.get("ATOMIC_CRITICAL_COMMIT_BYTES", str(int(1.5 * GIB)))),
        # abort if available physical RAM falls below this
        "critical_avail_phys_bytes": int(
            os.environ.get("ATOMIC_CRITICAL_PHYS_BYTES", str(int(1.0 * GIB)))),
        # worker wall-clock ceiling (seconds); 0 = disabled
        "max_worker_seconds": int(os.environ.get("ATOMIC_MAX_WORKER_SECONDS", "0")),
        # C4-E — admission requires at least this much free disk on the
        # output/work volumes, on top of the job's own estimate
        "min_disk_free_bytes": int(
            os.environ.get("ATOMIC_MIN_DISK_BYTES", str(int(5 * GIB)))),
        # extra headroom subtracted from usable VRAM before a job is
        # admitted to a device (driver reports often lag allocation)
        "vram_headroom_bytes": int(
            os.environ.get("ATOMIC_VRAM_HEADROOM_BYTES", "0")),
    }
    if overrides:
        for k, v in overrides.items():
            if k in m and v is not None:
                m[k] = int(v)
    return m


def settings_overrides(session) -> dict[str, int]:
    """C4-E — read the user-configured resource thresholds from
    app_settings (`res_*_gib` keys written by PUT /api/settings/
    resources). GiB floats → the byte-keyed margin names."""
    from app.domain.model_files import _get_setting
    out: dict[str, int] = {}
    for key, margin in (
            ("res_os_headroom_gib", "os_headroom_bytes"),
            ("res_critical_commit_gib", "critical_commit_headroom_bytes"),
            ("res_critical_phys_gib", "critical_avail_phys_bytes"),
            ("res_min_disk_gib", "min_disk_free_bytes"),
            ("res_vram_headroom_gib", "vram_headroom_bytes")):
        v = _get_setting(session, key)
        if v is not None:
            try:
                out[margin] = int(float(v) * GIB)
            except (TypeError, ValueError):
                continue
    return out


def disk_free_bytes(path) -> int | None:
    """Free bytes on the volume holding `path`; None when unresolvable."""
    try:
        import shutil
        return int(shutil.disk_usage(str(path)).free)
    except (OSError, TypeError, ValueError):
        return None


def preflight_disk_check(needed_bytes: int, free_bytes: int | None,
                         *, label: str, margins: dict | None = None,
                         known: bool = True) -> dict[str, Any]:
    """C4-E — decide whether a job may write `needed_bytes` on a volume.
    Same decision shape as preflight_memory_check: ok | warn | block.
    An unknown estimate is a warn (conservative), never a silent pass."""
    margins = margins or safety_margins()
    min_free = margins["min_disk_free_bytes"]
    snap = {"label": label, "needed_bytes": needed_bytes,
            "free_bytes": free_bytes, "min_free_bytes": min_free,
            "estimate_known": known}
    if free_bytes is None:
        return {"decision": "warn",
                "reason": f"free disk space on {label} could not be "
                          "probed — proceeding with monitoring",
                "snapshot": snap}
    if free_bytes < needed_bytes + min_free:
        return {"decision": "block",
                "reason": f"insufficient disk space on {label}: job "
                          f"needs ~{needed_bytes / GIB:.2f} GiB plus "
                          f"~{min_free / GIB:.1f} GiB headroom, only "
                          f"~{free_bytes / GIB:.2f} GiB free",
                "snapshot": snap}
    return {"decision": "ok", "reason": "", "snapshot": snap}


def pressure_state() -> str:
    """'ok' | 'warn' | 'critical' — from physical RAM + commit headroom."""
    m, margins = system_memory(), safety_margins()
    if (m["avail_phys_bytes"] < margins["critical_avail_phys_bytes"]
            or m["commit_headroom_bytes"] < margins["critical_commit_headroom_bytes"]):
        return "critical"
    if (m["avail_phys_bytes"] < margins["os_headroom_bytes"]
            or m["phys_load_pct"] >= 90):
        return "warn"
    return "ok"


def preflight_memory_check(estimated_ram_bytes: int, *, known: bool,
                           margins: dict | None = None) -> dict[str, Any]:
    """Decide whether a job may launch given its RAM estimate.
    Returns {decision: 'ok'|'warn'|'block', reason, snapshot} — the CALLER
    decides whether 'warn' needs user confirmation."""
    m = system_memory()
    margins = margins or safety_margins()
    headroom_after = m["avail_phys_bytes"] - estimated_ram_bytes
    commit_after = m["commit_headroom_bytes"] - estimated_ram_bytes
    snap = {**m, "estimated_ram_bytes": estimated_ram_bytes,
            "headroom_after_bytes": headroom_after,
            "commit_after_bytes": commit_after, "estimate_known": known}
    if not known:
        return {"decision": "warn", "reason": "memory requirement unknown — "
                "conservative monitoring applies", "snapshot": snap}
    if commit_after < margins["critical_commit_headroom_bytes"]:
        return {"decision": "block",
                "reason": f"insufficient commit headroom: job needs ~"
                          f"{estimated_ram_bytes / GIB:.1f} GiB committed, only "
                          f"{m['commit_headroom_bytes'] / GIB:.1f} GiB of "
                          "commit limit free",
                "snapshot": snap}
    if headroom_after < margins["critical_avail_phys_bytes"]:
        return {"decision": "block",
                "reason": f"insufficient system RAM: job needs ~"
                          f"{estimated_ram_bytes / GIB:.1f} GiB, only "
                          f"{m['avail_phys_bytes'] / GIB:.1f} GiB free",
                "snapshot": snap}
    if headroom_after < margins["os_headroom_bytes"]:
        return {"decision": "warn",
                "reason": f"job may leave the system low on memory "
                          f"(~{headroom_after / GIB:.1f} GiB free afterward)",
                "snapshot": snap}
    return {"decision": "ok", "reason": "", "snapshot": snap}
