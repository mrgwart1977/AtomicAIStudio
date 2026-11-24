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


# Documented GetPerformanceInfo layout — DWORD cb, ten SIZE_T
# page/size counts (pointer-width: c_size_t stays correct on 32- and
# 64-bit), then three DWORD counters. Commit*/Physical* are in PAGES,
# not bytes. The structure itself is platform-independent — only its
# USE is Windows-gated (defining it is not a DLL access).
class PERFORMANCE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("CommitTotal", ctypes.c_size_t),
        ("CommitLimit", ctypes.c_size_t),
        ("CommitPeak", ctypes.c_size_t),
        ("PhysicalTotal", ctypes.c_size_t),
        ("PhysicalAvailable", ctypes.c_size_t),
        ("SystemCache", ctypes.c_size_t),
        ("KernelTotal", ctypes.c_size_t),
        ("KernelPaged", ctypes.c_size_t),
        ("KernelNonpaged", ctypes.c_size_t),
        ("PageSize", ctypes.c_size_t),
        ("HandleCount", ctypes.c_uint32),
        ("ProcessCount", ctypes.c_uint32),
        ("ThreadCount", ctypes.c_uint32),
    ]


def _gpi_call(info) -> bool:
    """psapi GetPerformanceInfo — the ONLY system-wide commit source.

    Isolated so tests can substitute the call without touching a DLL;
    unreachable on non-Windows platforms."""
    return bool(ctypes.windll.psapi.GetPerformanceInfo(
        ctypes.byref(info), ctypes.sizeof(info)))


def commit_evidence() -> dict:
    """U-5c-1b — measured SYSTEM-WIDE commit evidence for cached reports.

    Windows: GetPerformanceInfo CommitLimit/CommitTotal are page counts —
    bytes require an explicit multiply by the reported PageSize (never a
    4096 assumption). headroom = (limit - charge) x PageSize.
    Non-Windows: honestly unavailable — no POSIX approximation is
    presented as commit evidence in this milestone.

    Scope is strictly system-wide: process-specific and job-object
    limits are outside this contract, and a positive headroom is never
    a guarantee that any single worker's allocation will succeed.
    Absent/invalid measurements produce Nones with a stable reason —
    never fabricated zeros; a MEASURED zero stays zero. This function
    performs no OS calls on import and none at all off Windows."""
    out = {"commit_limit_bytes": None, "commit_charge_bytes": None,
           "commit_headroom_bytes": None,
           "commit_source": "unavailable", "commit_scope": "unavailable",
           "commit_confidence": "unavailable",
           "commit_unavailable_reason": None}
    if not _IS_WINDOWS:
        return {**out, "commit_unavailable_reason": "unsupported_platform"}
    # Attempted measurement — provenance describes the attempt; the
    # confidence field separately records whether it produced evidence.
    attempted = {"commit_source": "get_performance_info",
                 "commit_scope": "system_wide"}
    try:
        info = PERFORMANCE_INFORMATION()
        info.cb = ctypes.sizeof(info)
        ok = _gpi_call(info)
    except (OSError, AttributeError, TypeError, ValueError,
            RuntimeError, ctypes.ArgumentError):
        return {**out, **attempted,
                "commit_unavailable_reason": "api_unavailable"}
    if not ok:
        return {**out, **attempted,
                "commit_unavailable_reason": "api_failure"}
    page = int(info.PageSize)
    limit_p, charge_p = int(info.CommitLimit), int(info.CommitTotal)
    if page <= 0:
        return {**out, **attempted,
                "commit_unavailable_reason": "invalid_page_size"}
    if limit_p <= 0:
        return {**out, **attempted,
                "commit_unavailable_reason": "invalid_commit_limit"}
    if charge_p < 0 or charge_p > limit_p:
        # charge > limit is internally inconsistent — an attempted
        # measurement that yields unusable values stays unavailable.
        return {**out, **attempted,
                "commit_unavailable_reason": "inconsistent_measurements"}
    return {"commit_limit_bytes": limit_p * page,
            "commit_charge_bytes": charge_p * page,
            "commit_headroom_bytes": (limit_p - charge_p) * page,
            "commit_source": "get_performance_info",
            "commit_scope": "system_wide",
            "commit_confidence": "measured",
            "commit_unavailable_reason": None}


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


def _commit_evidence_qualified(mem: dict) -> bool:
    """Advisory-side qualification of cached commit evidence (U-5c-1b).

    Collector-side validation alone is not trusted — cached, legacy,
    injected or malformed evidence reaches this function through paths
    that never ran the collector. Every element of the measured
    system-wide contract must hold TOGETHER:
    correct source/scope/confidence provenance, no contradictory
    unavailable reason, strict integers (bool is not an int here),
    positive limit, nonnegative charge and headroom, charge <= limit
    and headroom == limit - charge. Strings, floats, bools and partial
    measurements are never coerced — unqualified evidence degrades to
    honest unknown, in BOTH directions: it can neither prove headroom
    exists nor prove it is absent."""
    if (mem.get("commit_source") != "get_performance_info"
            or mem.get("commit_scope") != "system_wide"
            or mem.get("commit_confidence") != "measured"
            or mem.get("commit_unavailable_reason") is not None):
        return False
    limit = mem.get("commit_limit_bytes")
    charge = mem.get("commit_charge_bytes")
    headroom = mem.get("commit_headroom_bytes")
    for v in (limit, charge, headroom):
        if isinstance(v, bool) or not isinstance(v, int):
            return False
    return (limit > 0 and charge >= 0 and headroom >= 0
            and charge <= limit and headroom == limit - charge)


def evaluate_ram(estimated_ram_bytes: int | None, *, known: bool,
                 mem: dict | None, margins: dict | None = None
                 ) -> dict[str, Any]:
    """Pure advisory RAM evaluation against SUPPLIED memory evidence.

    `mem` is a previously collected memory view —
    {"total_bytes","available_bytes","commit_headroom_bytes"?} — or None.
    This function NEVER calls system_memory(), GlobalMemoryStatusEx,
    psutil or any other OS query: absent evidence is reported as
    "unknown", never measured and never fabricated.

    A bare `commit_headroom_bytes` number is NOT usable evidence — the
    commit check additionally requires the complete U-5c-1b measured
    system-wide contract (_commit_evidence_qualified), so legacy,
    approximate, process-capped, malformed or internally inconsistent
    commit values all degrade honestly to "unknown".
    `decision`: block > warn > unknown > ok; `fully_evaluated` is False
    whenever a required check is unknown. An "ok" or "warn" without full
    evaluation must not establish advisory readiness."""
    margins = margins or safety_margins()
    mem = mem or {}
    snap = {"estimated_ram_bytes": estimated_ram_bytes,
            "total_phys_bytes": mem.get("total_bytes"),
            "avail_phys_bytes": mem.get("available_bytes"),
            "commit_headroom_bytes": mem.get("commit_headroom_bytes"),
            "estimate_known": known}
    if mem.get("available_bytes") is None:
        return {"decision": "unknown", "fully_evaluated": False,
                "checks": {"memory_evidence": "unavailable"},
                "reason": "memory evidence unavailable — RAM cannot be "
                          "evaluated",
                "snapshot": snap}
    avail = int(mem["available_bytes"])
    checks: dict[str, str] = {}
    reasons: list[str] = []
    if not known or estimated_ram_bytes is None:
        checks["estimate"] = "unknown"
    else:
        est = int(estimated_ram_bytes)
        headroom_after = avail - est
        snap["headroom_after_bytes"] = headroom_after
        if headroom_after < margins["critical_avail_phys_bytes"]:
            checks["phys_avail"] = "block"
            reasons.append(
                f"insufficient system RAM: job needs ~{est / GIB:.1f} GiB, "
                f"only ~{avail / GIB:.1f} GiB free")
        elif headroom_after < margins["os_headroom_bytes"]:
            checks["phys_avail"] = "warn"
            reasons.append(
                f"job may leave the system low on memory "
                f"(~{headroom_after / GIB:.1f} GiB free afterward)")
        else:
            checks["phys_avail"] = "ok"
        commit = mem.get("commit_headroom_bytes")
        # R3 evidence qualification: a bare number never counts — only
        # measured system-wide provenance may establish the commit check.
        # Legacy reports, approximations, process-capped values and
        # malformed evidence all degrade to honest unknown.
        commit_qualified = _commit_evidence_qualified(mem)
        if commit is None or not commit_qualified:
            # Required by the execution-preflight contract but absent or
            # unqualified in the cached report — honest unknown.
            checks["commit_headroom"] = "unknown"
            if commit is None:
                reasons.append("commit headroom not present in cached "
                               "memory evidence")
            else:
                checks["commit_evidence"] = "unqualified"
                reasons.append("commit evidence lacks measured "
                               "system-wide provenance — not usable "
                               "for readiness")
        else:
            commit_after = int(commit) - est
            snap["commit_after_bytes"] = commit_after
            if commit_after < margins["critical_commit_headroom_bytes"]:
                checks["commit_headroom"] = "block"
                reasons.append(
                    f"insufficient commit headroom: job needs ~"
                    f"{est / GIB:.1f} GiB committed, only "
                    f"~{int(commit) / GIB:.1f} GiB of commit limit free")
            else:
                checks["commit_headroom"] = "ok"
    vals = set(checks.values())
    fully_evaluated = bool(checks) and "unknown" not in vals
    if "block" in vals:
        decision = "block"
    elif "warn" in vals:
        decision = "warn"
    elif "unknown" in vals:
        decision = "unknown"
        if not reasons:
            reasons.append("required memory evidence is unknown")
    else:
        decision = "ok"
    return {"decision": decision, "fully_evaluated": fully_evaluated,
            "checks": checks, "reason": "; ".join(reasons),
            "snapshot": snap}


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
