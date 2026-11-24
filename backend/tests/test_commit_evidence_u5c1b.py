"""U-5c-1b — commit-memory evidence: collector, qualification, lifecycle.

GetPerformanceInfo is exercised ONLY through the _gpi_call seam — no
real Windows memory API call, no live psutil, no discovery, no
subprocesses, no provider access. Negative synthetic inputs are
injectable via a signed-field stand-in structure.
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_hw_cacheonly_u5c1a as t1a
import test_planpreview_u5c as t5c

from app.domain import (
    hardware,
    resources,
)
from app.domain import hw_snapshot as hs

GIB = 1024 ** 3
MARGINS = t1a.MARGINS
gpu, slot, sched_state = t1a.gpu, t1a.slot, t1a.sched_state


# ------------------------------------------------------------- collector


class _SignedPI(ctypes.Structure):
    """Signed-field stand-in so invalid/negative synthetic inputs are
    injectable on any platform (real fields are unsigned)."""
    _fields_ = [("cb", ctypes.c_uint32),
                ("CommitTotal", ctypes.c_int64),
                ("CommitLimit", ctypes.c_int64),
                ("PageSize", ctypes.c_int64)]


def _win(monkeypatch):
    """Simulate the Windows branch on any platform — the real call seam
    _gpi_call is stubbed separately; no DLL is ever touched."""
    monkeypatch.setattr(resources, "_IS_WINDOWS", True)
    monkeypatch.setattr(resources, "PERFORMANCE_INFORMATION", _SignedPI)


def _gpi(charge, limit, page_size, ok=True):
    def _call(info):
        info.CommitTotal = charge
        info.CommitLimit = limit
        info.PageSize = page_size
        return ok
    return _call


def test_structure_matches_documented_layout():
    pi = resources.PERFORMANCE_INFORMATION
    names = [f[0] for f in pi._fields_]
    assert names == [
        "cb", "CommitTotal", "CommitLimit", "CommitPeak",
        "PhysicalTotal", "PhysicalAvailable", "SystemCache",
        "KernelTotal", "KernelPaged", "KernelNonpaged", "PageSize",
        "HandleCount", "ProcessCount", "ThreadCount"]
    types = dict(pi._fields_)
    assert types["cb"] is ctypes.c_uint32
    for n in names[1:11]:                    # SIZE_T = pointer-width
        assert types[n] is ctypes.c_size_t
    for n in names[11:]:
        assert types[n] is ctypes.c_uint32


def test_structure_is_import_safe_everywhere():
    # Defining the structure is not a DLL access — it instantiates on
    # any platform without touching psapi.
    info = resources.PERFORMANCE_INFORMATION()
    assert info.cb == 0


def test_cb_initialized_to_sizeof(monkeypatch):
    _win(monkeypatch)
    seen = {}

    def _call(info):
        seen["cb"] = info.cb
        info.CommitTotal, info.CommitLimit, info.PageSize = 1, 2, 4096
        return True
    monkeypatch.setattr(resources, "_gpi_call", _call)
    resources.commit_evidence()
    assert seen["cb"] == ctypes.sizeof(_SignedPI())


def test_success_measured_system_wide(monkeypatch):
    _win(monkeypatch)
    # Non-4096 page size proves the multiply uses the reported PageSize.
    monkeypatch.setattr(resources, "_gpi_call",
                        _gpi(charge=3_000_000, limit=5_000_000,
                             page_size=8192))
    ev = resources.commit_evidence()
    assert ev["commit_limit_bytes"] == 5_000_000 * 8192
    assert ev["commit_charge_bytes"] == 3_000_000 * 8192
    assert ev["commit_headroom_bytes"] == 2_000_000 * 8192
    assert ev["commit_source"] == "get_performance_info"
    assert ev["commit_scope"] == "system_wide"
    assert ev["commit_confidence"] == "measured"
    assert ev["commit_unavailable_reason"] is None


def test_measured_zero_headroom_is_zero_not_unavailable(monkeypatch):
    _win(monkeypatch)
    monkeypatch.setattr(resources, "_gpi_call",
                        _gpi(charge=5_000_000, limit=5_000_000,
                             page_size=4096))
    ev = resources.commit_evidence()
    assert ev["commit_headroom_bytes"] == 0
    assert ev["commit_confidence"] == "measured"


def test_large_values_no_truncation(monkeypatch):
    _win(monkeypatch)
    monkeypatch.setattr(resources, "_gpi_call",
                        _gpi(charge=2 ** 33, limit=2 ** 36,
                             page_size=16384))
    ev = resources.commit_evidence()
    assert ev["commit_limit_bytes"] == (2 ** 36) * 16384
    assert ev["commit_headroom_bytes"] == (2 ** 36 - 2 ** 33) * 16384


def test_bool_failure_is_api_failure(monkeypatch):
    _win(monkeypatch)
    monkeypatch.setattr(resources, "_gpi_call",
                        _gpi(0, 0, 0, ok=False))
    ev = resources.commit_evidence()
    assert ev["commit_headroom_bytes"] is None
    assert ev["commit_confidence"] == "unavailable"
    assert ev["commit_source"] == "get_performance_info"
    assert ev["commit_unavailable_reason"] == "api_failure"


def test_exception_is_api_unavailable(monkeypatch):
    _win(monkeypatch)

    def _boom(info):
        raise RuntimeError("psapi gone")
    monkeypatch.setattr(resources, "_gpi_call", _boom)
    ev = resources.commit_evidence()
    assert ev["commit_unavailable_reason"] == "api_unavailable"
    assert ev["commit_headroom_bytes"] is None


def test_missing_api_is_unavailable(monkeypatch):
    _win(monkeypatch)

    def _missing(info):
        raise AttributeError("no psapi.GetPerformanceInfo")
    monkeypatch.setattr(resources, "_gpi_call", _missing)
    assert resources.commit_evidence()[
        "commit_unavailable_reason"] == "api_unavailable"


def test_failed_call_never_reads_zeroed_struct(monkeypatch):
    _win(monkeypatch)
    # API reports failure and leaves the struct zero-initialised — the
    # collector must not treat those zeros as a measurement.
    monkeypatch.setattr(resources, "_gpi_call", lambda info: False)
    ev = resources.commit_evidence()
    assert ev["commit_headroom_bytes"] is None
    assert ev["commit_unavailable_reason"] == "api_failure"


@pytest.mark.parametrize("page", [0, -1])
def test_invalid_page_size(monkeypatch, page):
    _win(monkeypatch)
    monkeypatch.setattr(resources, "_gpi_call", _gpi(1, 2, page))
    assert resources.commit_evidence()[
        "commit_unavailable_reason"] == "invalid_page_size"


@pytest.mark.parametrize("limit", [0, -8])
def test_invalid_commit_limit(monkeypatch, limit):
    _win(monkeypatch)
    monkeypatch.setattr(resources, "_gpi_call", _gpi(1, limit, 4096))
    assert resources.commit_evidence()[
        "commit_unavailable_reason"] == "invalid_commit_limit"


def test_charge_above_limit_is_inconsistent(monkeypatch):
    _win(monkeypatch)
    monkeypatch.setattr(resources, "_gpi_call", _gpi(9, 4, 4096))
    assert resources.commit_evidence()[
        "commit_unavailable_reason"] == "inconsistent_measurements"


def test_negative_charge_is_inconsistent(monkeypatch):
    _win(monkeypatch)
    monkeypatch.setattr(resources, "_gpi_call", _gpi(-1, 4, 4096))
    assert resources.commit_evidence()[
        "commit_unavailable_reason"] == "inconsistent_measurements"


def test_unsupported_platform_never_calls_api(monkeypatch):
    monkeypatch.setattr(resources, "_IS_WINDOWS", False)
    calls = []
    monkeypatch.setattr(resources, "_gpi_call",
                        lambda info: calls.append(info) or True)
    ev = resources.commit_evidence()
    assert calls == []
    assert ev["commit_unavailable_reason"] == "unsupported_platform"
    assert ev["commit_source"] == "unavailable"
    assert ev["commit_scope"] == "unavailable"
    assert ev["commit_confidence"] == "unavailable"
    assert ev["commit_headroom_bytes"] is None


# -------------------------------------------------- collection lifecycle


@pytest.fixture(autouse=True)
def _cold_cache():
    hardware._reset_published()
    hardware._cached_probe.cache_clear()
    yield
    hardware._reset_published()
    # Stubbed collections must not leak through the REAL lru_cache into
    # later tests sharing the same 5-minute bucket. _cached_probe may still
    # be a monkeypatched lambda here (teardown ordering) — in that case the
    # real lru was never invoked by this test, so there is nothing to clear.
    if hasattr(hardware._cached_probe, "cache_clear"):
        hardware._cached_probe.cache_clear()


_COMMIT_OK = {"commit_limit_bytes": 44 * GIB,
              "commit_charge_bytes": 14 * GIB,
              "commit_headroom_bytes": 30 * GIB,
              "commit_source": "get_performance_info",
              "commit_scope": "system_wide",
              "commit_confidence": "measured",
              "commit_unavailable_reason": None}


def _stub_collection(monkeypatch, commit=_COMMIT_OK):
    """Stub every OS seam inside _cached_probe — real collection code
    runs, nothing external is touched."""
    monkeypatch.setattr(hardware.psutil, "virtual_memory",
                        lambda: SimpleNamespace(total=32 * GIB,
                                                available=24 * GIB))
    monkeypatch.setattr(hardware, "_device_info", lambda: {
        "vram_by_device": {}, "vulkan_icd_count": 0, "vulkan_dll": False})
    monkeypatch.setattr(hardware, "_query_gpus", lambda d: [gpu()])
    monkeypatch.setattr(hardware, "_disks", list)
    monkeypatch.setattr(hardware, "_runtimes", dict)
    monkeypatch.setattr(hardware, "_simulated_report", lambda: None)
    monkeypatch.setattr(resources, "commit_evidence",
                        lambda: dict(commit or {}))
    hardware._cached_probe.cache_clear()


def test_collection_merges_commit_evidence(monkeypatch):
    _stub_collection(monkeypatch)
    hardware.probe()
    mem = hardware.probe_cached()["report"]["memory"]
    assert mem["total_bytes"] == 32 * GIB
    assert mem["available_bytes"] == 24 * GIB
    assert mem["commit_headroom_bytes"] == 30 * GIB
    assert mem["commit_scope"] == "system_wide"
    assert mem["commit_confidence"] == "measured"
    assert isinstance(mem["sampled_monotonic"], float)


def test_partial_commit_failure_publishes_honest_unavailable(monkeypatch):
    _stub_collection(monkeypatch, commit={
        "commit_limit_bytes": None, "commit_charge_bytes": None,
        "commit_headroom_bytes": None, "commit_source": "unavailable",
        "commit_scope": "unavailable", "commit_confidence": "unavailable",
        "commit_unavailable_reason": "api_failure"})
    hardware.probe()
    mem = hardware.probe_cached()["report"]["memory"]
    assert mem["commit_headroom_bytes"] is None
    assert mem["commit_unavailable_reason"] == "api_failure"
    assert mem["total_bytes"] == 32 * GIB     # partial failure only


def test_newer_report_never_inherits_old_commit(monkeypatch):
    rep1 = t1a.probe_report(probed_monotonic=100.0)
    rep2 = t1a.probe_report(probed_monotonic=200.0)
    rep2["memory"] = {"total_bytes": 32 * GIB, "available_bytes": 20 * GIB,
                      "commit_limit_bytes": None,
                      "commit_charge_bytes": None,
                      "commit_headroom_bytes": None,
                      "commit_source": "get_performance_info",
                      "commit_scope": "system_wide",
                      "commit_confidence": "unavailable",
                      "commit_unavailable_reason": "api_failure"}
    monkeypatch.setattr(hardware, "_cached_probe",
                        lambda b: rep1)
    monkeypatch.setattr(hardware, "_simulated_report", lambda: None)
    hardware.probe()
    monkeypatch.setattr(hardware, "_cached_probe", lambda b: rep2)
    hardware.probe()
    mem = hardware.probe_cached()["report"]["memory"]
    # The newer collection's honest failure must not be papered over by
    # the earlier report's measured values.
    assert mem["commit_headroom_bytes"] is None
    assert mem["commit_unavailable_reason"] == "api_failure"


def test_simulated_collection_never_invokes_collector(monkeypatch):
    """Sim merge must not fire the real API to fill missing fields."""
    spy = []
    monkeypatch.setattr(resources, "commit_evidence",
                        lambda: spy.append(1) or dict(_COMMIT_OK))
    rep = t1a.probe_report()
    monkeypatch.setattr(hardware, "_cached_probe", lambda b: rep)
    monkeypatch.setattr(hardware, "_simulated_report", lambda: {
        "gpus": [gpu(name="SimGPU")], "memory": {"total_bytes": 1}})
    hardware.probe()
    rec = hardware.probe_cached()
    assert rec["simulated"] is True
    assert spy == []


def test_memory_sampled_monotonic_drives_staleness(monkeypatch):
    # Report looks fresh but its memory sample is older than the bucket —
    # staleness ages from the OLDEST relevant sample, conservatively.
    rep = t1a.probe_report(
        probed_at=time.time(), probed_monotonic=time.monotonic())
    rep["memory"]["sampled_monotonic"] = (
        time.monotonic() - hs.STALE_SNAPSHOT_SECONDS - 60)
    s = hs.build_snapshot(rep, sched_state(), MARGINS)
    assert s["stale"] is True
    # Absent memory-specific timing keeps the report-level behavior.
    rep2 = t1a.probe_report(
        probed_at=time.time(), probed_monotonic=time.monotonic())
    s2 = hs.build_snapshot(rep2, sched_state(), MARGINS)
    assert s2["stale"] is False


# --------------------------------------------- hw:v2 fingerprint canon


def _snap_with(mem_update):
    rep = t1a.probe_report(gpus=[gpu()], probed_at=1.0,
                           probed_monotonic=1.0)
    rep["memory"].update(mem_update)
    return hs.build_snapshot(rep, sched_state(), MARGINS, now=2.0,
                             monotonic_now=2.0)


def test_snapshot_id_is_hw_v2():
    assert _snap_with({})["snapshot_id"].startswith("hw:v2:")
    assert _snap_with({})["snapshot_schema_version"] == 2


@pytest.mark.parametrize("update", [
    {"commit_headroom_bytes": 0},                       # positive → zero
    {"commit_headroom_bytes": 31 * GIB},                # headroom change
    {"commit_charge_bytes": 15 * GIB},                  # charge change
    {"commit_limit_bytes": 45 * GIB},                   # limit change
    {"commit_limit_bytes": 45 * GIB,
     "commit_charge_bytes": 15 * GIB},                  # both, same headroom
    {"commit_source": "other_api"},
    {"commit_scope": "process_capped"},
    {"commit_confidence": "approximation"},
    {"commit_unavailable_reason": "api_failure"},
])
def test_material_commit_transitions_change_identity(update):
    assert _snap_with(update)["snapshot_id"] \
        != _snap_with({})["snapshot_id"]


def test_unavailable_to_measured_changes_identity():
    unavail = {"commit_limit_bytes": None,
               "commit_charge_bytes": None,
               "commit_headroom_bytes": None,
               "commit_source": "unavailable",
               "commit_scope": "unavailable",
               "commit_confidence": "unavailable",
               "commit_unavailable_reason": "api_failure"}
    assert _snap_with(unavail)["snapshot_id"] \
        != _snap_with({})["snapshot_id"]


def test_identical_evidence_stable_and_elapsed_seconds_excluded():
    a = _snap_with({})
    b = _snap_with({})
    rep = t1a.probe_report(gpus=[gpu()], probed_at=9999.0,
                           probed_monotonic=9999.0)
    c = hs.build_snapshot(rep, sched_state(), MARGINS, now=10000.0,
                          monotonic_now=10000.0)
    assert a["snapshot_id"] == b["snapshot_id"] == c["snapshot_id"]


def test_simulated_changes_identity():
    rep = t1a.probe_report(gpus=[gpu()], probed_at=1.0,
                           probed_monotonic=1.0, simulated=True)
    sim = hs.build_snapshot(rep, sched_state(), MARGINS, now=2.0,
                            monotonic_now=2.0)
    assert sim["snapshot_id"] != _snap_with({})["snapshot_id"]


def test_snapshot_memory_wire_fields():
    m = _snap_with({})["memory"]
    for k in ("total_bytes", "available_bytes", "commit_limit_bytes",
              "commit_charge_bytes", "commit_headroom_bytes",
              "commit_source", "commit_scope", "commit_confidence",
              "commit_unavailable_reason"):
        assert k in m
    # sampled_monotonic is collection metadata — not snapshot evidence.
    assert "sampled_monotonic" not in m


# -------------------------------------------- evaluate_ram qualification


_QUAL = {"commit_source": "get_performance_info",
         "commit_scope": "system_wide", "commit_confidence": "measured"}
_MEM_BASE = {"total_bytes": 32 * GIB, "available_bytes": 24 * GIB,
             "commit_limit_bytes": 44 * GIB,
             "commit_charge_bytes": 14 * GIB}


@pytest.mark.parametrize("provenance", [
    {},                                                  # legacy bare
    {"commit_confidence": "approximation",
     "commit_scope": "system_wide"},
    {"commit_confidence": "measured",
     "commit_scope": "process_capped"},
    {"commit_confidence": "measured"},                   # missing scope
])
def test_unqualified_commit_never_satisfies(provenance):
    out = resources.evaluate_ram(
        4 * GIB, known=True,
        mem={**_MEM_BASE, "commit_headroom_bytes": 30 * GIB,
             **provenance},
        margins=MARGINS)
    assert out["checks"]["commit_headroom"] == "unknown"
    assert out["checks"]["commit_evidence"] == "unqualified"
    assert out["fully_evaluated"] is False


def test_measured_zero_commit_blocks():
    out = resources.evaluate_ram(
        4 * GIB, known=True,
        mem={**_MEM_BASE, "commit_headroom_bytes": 0, **_QUAL},
        margins=MARGINS)
    assert out["checks"]["commit_headroom"] == "block"
    assert out["decision"] == "block"
    assert out["fully_evaluated"] is True


def test_measured_sufficient_commit_evaluates_ok():
    out = resources.evaluate_ram(
        4 * GIB, known=True,
        mem={**_MEM_BASE, "commit_headroom_bytes": 30 * GIB, **_QUAL},
        margins=MARGINS)
    assert out["checks"]["commit_headroom"] == "ok"
    assert out["decision"] == "ok"
    assert out["fully_evaluated"] is True


# --------------------------------------------------------- endpoint path


@pytest.fixture()
def pub(monkeypatch, client):
    """Publish a synthetic report through the real contract with a
    caller-supplied memory dict; scheduler sees the same inventory."""
    from app.domain.gpu_sched import GpuScheduler

    def _pub(memory, gpus=None):
        rep = t1a.probe_report(gpus=gpus if gpus is not None
                               else [gpu(0, free=6 * GIB)],
                               memory=memory)
        hardware._reset_published()
        hardware._publish(rep)
        sched = GpuScheduler(lambda: rep, {})
        monkeypatch.setattr(client.app.state, "gpu_scheduler", sched)
        return rep
    yield _pub
    hardware._reset_published()


_FULL_MEM = {**_MEM_BASE, "commit_headroom_bytes": 30 * GIB, **_QUAL,
             "commit_unavailable_reason": None}
_PHYS_ONLY_MEM = {"total_bytes": 32 * GIB, "available_bytes": 24 * GIB}
_UNQUAL_MEM = {**_MEM_BASE, "commit_headroom_bytes": 30 * GIB}


def test_ep_ready_with_complete_qualified_evidence(client, pub):
    """Gate 9 — full endpoint READY path on complete synthetic evidence:
    real publication → build_snapshot → evaluate_ram → aggregation,
    with only acquisition/OS seams mocked, never the decision."""
    pid = t5c._project(client)
    rep = pub(_FULL_MEM)
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                           workflow="text2video", width=832, height=480,
                           residency_mode="cpu_offload"))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["advisory_state"] == "preview_ready_advisory"
    assert b["ram_eval"]["fully_evaluated"] is True
    assert b["ram_eval"]["checks"]["commit_headroom"] == "ok"
    assert b["snapshot"]["snapshot_id"].startswith("hw:v2:")
    mem = b["snapshot"]["memory"]
    assert mem["commit_confidence"] == "measured"
    assert mem["commit_scope"] == "system_wide"
    assert mem["commit_headroom_bytes"] == 30 * GIB


def test_ep_missing_commit_stays_incomplete(client, pub):
    pid = t5c._project(client)
    rep = pub(_PHYS_ONLY_MEM)
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                           workflow="text2video",
                           residency_mode="cpu_offload"))
    b = r.json()
    assert b["advisory_state"] == "preview_incomplete"
    assert "ram_unknown" in b["reasons"]
    assert b["ram_eval"]["checks"]["commit_headroom"] == "unknown"


def test_ep_unqualified_numeric_commit_stays_incomplete(client, pub):
    pid = t5c._project(client)
    rep = pub(_UNQUAL_MEM)
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                           workflow="text2video",
                           residency_mode="cpu_offload"))
    b = r.json()
    assert b["advisory_state"] == "preview_incomplete"
    assert b["ram_eval"]["checks"]["commit_evidence"] == "unqualified"


def test_ep_partial_commit_failure_never_ready(client, pub):
    pid = t5c._project(client)
    rep = pub({"total_bytes": 32 * GIB, "available_bytes": 24 * GIB,
               "commit_limit_bytes": None, "commit_charge_bytes": None,
               "commit_headroom_bytes": None,
               "commit_source": "get_performance_info",
               "commit_scope": "system_wide",
               "commit_confidence": "unavailable",
               "commit_unavailable_reason": "api_failure"})
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                           workflow="text2video",
                           residency_mode="cpu_offload"))
    assert r.json()["advisory_state"] == "preview_incomplete"


def test_ep_zero_commit_blocks(client, pub):
    pid = t5c._project(client)
    rep = pub({**_FULL_MEM, "commit_headroom_bytes": 0})
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                           workflow="text2video",
                           residency_mode="cpu_offload"))
    b = r.json()
    assert b["advisory_state"] == "preview_blocked"
    assert "ram_insufficient" in b["reasons"]


def test_ep_never_live_collects(monkeypatch, client, pub):
    """No commit/memory API, psutil, probe or discovery fires in-request."""
    for mod, fn in ((resources, "commit_evidence"),
                    (resources, "system_memory"),
                    (hardware, "probe")):
        def _raiser(*a, _fn=fn, **k):
            raise AssertionError(f"{_fn} in request path")
        monkeypatch.setattr(mod, fn, _raiser)
    monkeypatch.setattr(hardware.psutil, "virtual_memory",
                        lambda: (_ for _ in ()).throw(
                            AssertionError("psutil in request path")))
    monkeypatch.setattr(hardware, "_cached_probe",
                        lambda b: (_ for _ in ()).throw(
                            AssertionError("discovery attempted")))
    pid = t5c._project(client)
    rep = pub(_FULL_MEM)
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                           workflow="text2video"))
    assert r.status_code == 200, r.text


def test_ep_authoritative_blocker_beats_complete_evidence(client, pub):
    # Policy hold dominates even with perfect memory evidence — the new
    # fields never weaken Group-1 blockers.
    pid = t5c._project(client)
    rep = pub(_FULL_MEM)
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, "nope:0", runtime_key="sdcpp"))
    assert r.status_code == 404
    r2 = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="comfyui",
                            workflow="text2video"))
    b = r2.json()
    assert b["advisory_state"] == "preview_blocked"
    assert "runtime_planned" in b["reasons"]


# -------------------------------------------- operational preservation


def test_system_memory_untouched_by_collector():
    # Gate 10: the operational helper is source-identical — the
    # collector is a separate function and system_memory still uses
    # GlobalMemoryStatusEx semantics for admission/watchdog paths.
    # (Session fixtures monkeypatch the symbol, so verify the FILE text
    # of the function body rather than the bound callable.)
    mod_src = Path(resources.__file__).read_text(encoding="utf-8")
    body = mod_src.split("def system_memory", 1)[1].split("\ndef ", 1)[0]
    assert "GlobalMemoryStatusEx" in body
    assert "GetPerformanceInfo" not in body
    assert "commit_evidence" not in body
