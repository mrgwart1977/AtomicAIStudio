"""Worker process supervision — Windows Job Objects + lifecycle helpers.

Every inference worker is assigned to a Windows Job Object configured with
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: the OS itself kills the whole process
tree when the last job-object handle closes — including when the backend
is force-killed or crashes. This removes the orphaned-sd-cli failure mode
(issue #28). On non-Windows platforms the helpers degrade gracefully to
plain process lifetime management (no job objects).
"""
from __future__ import annotations

import atexit
import ctypes
import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_live_workers: set[int] = set()          # pids supervised by this process
_job_handles: dict[int, int] = {}        # pid -> job object handle (win32)

if _IS_WINDOWS:
    _k32 = ctypes.windll.kernel32

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    _PROCESS_ALL_ACCESS = 0x1FFFFF
    _PROCESS_QUERY_INFORMATION = 0x0400


def _create_kill_on_close_job() -> int | None:
    """Create a Job Object that kills its processes when the handle closes."""
    if not _IS_WINDOWS:
        return None
    handle = _k32.CreateJobObjectW(None, None)
    if not handle:
        log.warning("CreateJobObject failed (err %s) — worker unsupervised",
                    ctypes.get_last_error())
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _k32.SetInformationJobObject(
        handle, _JobObjectExtendedLimitInformation,
        ctypes.byref(info), ctypes.sizeof(info))
    if not ok:
        log.warning("SetInformationJobObject failed (err %s)",
                    ctypes.get_last_error())
        _k32.CloseHandle(handle)
        return None
    return int(handle)


def supervise(pid: int) -> None:
    """Assign a spawned worker pid to a kill-on-close Job Object and register
    it for the atexit reaper. Must be called right after spawn (there is an
    unavoidable ~ms race before assignment — acceptable vs. no supervision)."""
    if pid in _live_workers:
        return
    _live_workers.add(pid)
    if not _IS_WINDOWS:
        return
    job = _create_kill_on_close_job()
    if job is None:
        return
    proc_handle = _k32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
    if not proc_handle:
        log.warning("OpenProcess(%d) failed — worker unsupervised", pid)
        _k32.CloseHandle(job)
        return
    if not _k32.AssignProcessToJobObject(job, proc_handle):
        log.warning("AssignProcessToJobObject(%d) failed (err %s)",
                    pid, ctypes.get_last_error())
        _k32.CloseHandle(proc_handle)
        _k32.CloseHandle(job)
        return
    _k32.CloseHandle(proc_handle)
    _job_handles[pid] = job  # keep the handle open for the worker's lifetime
    log.info("worker pid %d assigned to kill-on-close job object", pid)


def unsupervise(pid: int) -> None:
    """Worker finished — release the job handle (process already dead or the
    job would kill it) and drop from the registry."""
    _live_workers.discard(pid)
    job = _job_handles.pop(pid, None)
    if job is not None and _IS_WINDOWS:
        _k32.CloseHandle(job)


def process_alive(pid: int) -> bool:
    if not _IS_WINDOWS:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    h = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return False
    code = ctypes.c_uint32()
    ok = _k32.GetExitCodeProcess(h, ctypes.byref(code))
    _k32.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def process_rss_bytes(pid: int) -> int | None:
    """Working-set bytes for a pid (Windows); None if unavailable."""
    if not _IS_WINDOWS:
        return None
    psapi = ctypes.windll.psapi
    h = _k32.OpenProcess(_PROCESS_QUERY_INFORMATION | 0x0010, False, pid)
    if not h:
        return None
    class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    pmc = PROCESS_MEMORY_COUNTERS()
    pmc.cb = ctypes.sizeof(pmc)
    ok = psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb)
    _k32.CloseHandle(h)
    return int(pmc.WorkingSetSize) if ok else None


def terminate_tree(pid: int, grace_s: float = 5.0) -> None:
    """Graceful terminate → wait → force kill. Job objects make the tree kill
    automatic; this targets the root pid for the graceful path."""
    if not process_alive(pid):
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, timeout=grace_s + 5)
    except Exception:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _reap_at_exit() -> None:
    """Last-resort cleanup on interpreter exit. (KILL_ON_JOB_CLOSE already
    covers force-kill; this covers graceful exits with live workers.)"""
    for pid in list(_live_workers):
        if process_alive(pid):
            log.warning("atexit reaper: terminating leftover worker pid %d", pid)
            terminate_tree(pid, grace_s=2.0)
        unsupervise(pid)


atexit.register(_reap_at_exit)
