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


def safety_margins() -> dict[str, int]:
    """Configurable guard rails (env-overridable for tests)."""
    import os
    return {
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
    }


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


def preflight_memory_check(estimated_ram_bytes: int, *, known: bool) -> dict[str, Any]:
    """Decide whether a job may launch given its RAM estimate.
    Returns {decision: 'ok'|'warn'|'block', reason, snapshot} — the CALLER
    decides whether 'warn' needs user confirmation."""
    m, margins = system_memory(), safety_margins()
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
