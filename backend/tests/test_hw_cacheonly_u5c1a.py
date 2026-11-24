"""U-5c-1a — hardware-evidence hardening: the cache-only contract.

Focused tests for the F1 publication slot, collection-monotonic
ordering, cache-only accessor purity (Gate 2), the evidence-injection
tri-state (Gate 3), pure scheduler observation (Gate 4), CPU evidence
classes (Gate 5), pure RAM evaluation (Gate 6), deterministic advisory
aggregation (Gate 7), imported-residency + CD-9 (Gate 8) and the
plan:v2 / planresult:v1 fingerprint pair (Gate 9).

Synthetic fixtures only — no discovery, OS queries, subprocesses,
filesystem scanning, provider access or inference.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_planpreview_u5c as t5c  # noqa: E402 — shared fixtures

from app.domain import compatibility
from app.domain import dep_graph as g
from app.domain import dep_resolve as dr
from app.domain import hardware
from app.domain import hw_snapshot as hs
from app.domain import plan_preview as pp
from app.domain import resources
from app.domain.gpu_sched import GpuScheduler
from app.domain.model_families import FAMILIES
from app.domain.runtime_caps import RUNTIME_CAPS
from app.domain.workflow_defs import WORKFLOW_DEFS

GIB = 1024 ** 3
SDCPP = RUNTIME_CAPS["sdcpp"]
WF_T2V = WORKFLOW_DEFS["text2video"]
MARGINS = {"vram_headroom_bytes": 0, "os_headroom_bytes": 4 * GIB,
           "critical_commit_headroom_bytes": int(1.5 * GIB),
           "critical_avail_phys_bytes": 1 * GIB,
           "min_disk_free_bytes": 5 * GIB}


# ----------------------------------------------------------- fixtures

def gpu(index=0, name="GPU0", vendor="NVIDIA", total=8 * GIB,
        free=7 * GIB, source="nvidia-smi"):
    return {"index": index, "name": name, "vendor": vendor,
            "vram_bytes": total, "vram_source": source,
            "vram_free_bytes": free, "vram_capped_at_4gb": False,
            "driver_version": "555.0"}


def probe_report(gpus=None, backends=None, simulated=False, probed_at=None,
                 memory=None, probed_monotonic=None):
    return {
        "probed_at": time.time() if probed_at is None else probed_at,
        "probed_monotonic": (time.monotonic() if probed_monotonic is None
                             else probed_monotonic),
        "os": {}, "cpu": {"name": "cpu", "physical_cores": 8,
                          "logical_cores": 16},
        "memory": memory or {"total_bytes": 32 * GIB,
                             "available_bytes": 24 * GIB,
                             "commit_headroom_bytes": 30 * GIB},
        "gpus": gpus or [], "disks": [],
        "backends": backends or {
            "cpu": {"available": True, "measured": True},
            "cuda": {"available": True, "measured": True}},
        "runtimes": {}, "simulated": simulated}


def sched_state(slots=None):
    return {"gpu_count": len(slots or []), "free": 0,
            "slots": slots or [], "lost_devices": [],
            "prefs": {}, "simulated": False}


def slot(index=0, reserved=0, present=True, usable=7 * GIB):
    return {"index": index, "name": f"GPU{index}", "vendor": "NVIDIA",
            "vram_bytes": 8 * GIB, "vram_free_bytes": 7 * GIB,
            "usable_bytes": usable, "reserved_bytes": reserved,
            "free_after_reserve_bytes": max(0, usable - reserved),
            "jobs": {}, "busy_job_id": None, "present": present}


def snap(gpus=None, sched=None, probed_at=None, simulated=False):
    return hs.build_snapshot(
        probe_report(gpus=gpus, simulated=simulated,
                     probed_at=probed_at, probed_monotonic=probed_at),
        sched if sched is not None else sched_state(),
        MARGINS, now=probed_at, monotonic_now=probed_at)


@pytest.fixture(autouse=True)
def _cold_cache():
    hardware._reset_published()
    yield
    hardware._reset_published()


@pytest.fixture()
def published(monkeypatch):
    """Publish one canned report through the real probe() path."""
    rep = probe_report(gpus=[gpu(free=6 * GIB)],
                       probed_at=1000.0, probed_monotonic=500.0)
    monkeypatch.setattr(hardware, "_cached_probe", lambda b: rep)
    monkeypatch.setattr(hardware, "_simulated_report", lambda: None)
    return rep


# ------------------------------------------------- F1 publication slot

def test_cold_cache_returns_none_and_never_discovers(monkeypatch):
    def _boom(_bucket):
        raise AssertionError("probe_cached must never touch discovery")
    monkeypatch.setattr(hardware, "_cached_probe", _boom)
    assert hardware.probe_cached() is None


def test_probe_publishes_and_probe_cached_reads(published):
    hardware.probe()
    rec = hardware.probe_cached()
    assert rec is not None
    assert rec["report"]["gpus"][0]["vram_free_bytes"] == 6 * GIB
    assert rec["probed_at"] == 1000.0
    assert rec["probed_monotonic"] == 500.0
    assert rec["simulated"] is False


def test_cache_hits_preserve_collection_timestamps(published):
    hardware.probe()
    r1 = hardware.probe_cached()
    r2 = hardware.probe_cached()
    # Repeated reads and repeat probe() hits on the same cached bucket
    # all preserve the ORIGINAL collection monotonic — never renewed.
    hardware.probe()
    r3 = hardware.probe_cached()
    assert (r1["probed_monotonic"], r1["probed_at"]) \
        == (r2["probed_monotonic"], r2["probed_at"]) \
        == (r3["probed_monotonic"], r3["probed_at"]) == (500.0, 1000.0)


def test_expired_record_still_returned(published):
    published["probed_at"] = 1.0
    published["probed_monotonic"] = 1.0
    hardware.probe()
    rec = hardware.probe_cached()
    assert rec is not None and rec["probed_monotonic"] == 1.0


def test_older_concurrent_publish_cannot_overwrite_newer():
    newer = probe_report(probed_monotonic=900.0)
    older = probe_report(probed_monotonic=100.0,
                         memory={"total_bytes": 1})
    hardware._publish(newer)
    hardware._publish(older)                     # finishes late — loses
    rec = hardware.probe_cached()
    assert rec["probed_monotonic"] == 900.0
    assert rec["report"]["memory"]["total_bytes"] == 32 * GIB


def test_failed_refresh_preserves_previous(published, monkeypatch):
    hardware.probe()
    def _boom(_b):
        raise RuntimeError("discovery blew up")
    _boom.cache_clear = lambda: None
    monkeypatch.setattr(hardware, "_cached_probe", _boom)
    monkeypatch.setattr(hardware, "_simulated_report", lambda: None)
    with pytest.raises(RuntimeError):
        hardware.probe(force=True)
    rec = hardware.probe_cached()
    assert rec is not None and rec["probed_monotonic"] == 500.0


def test_published_report_immutable_to_callers(published):
    rep = hardware.probe()
    rep["memory"]["total_bytes"] = 1          # mutate probe()'s return
    rec = hardware.probe_cached()
    rec["report"]["memory"]["total_bytes"] = 2  # mutate a cached view
    again = hardware.probe_cached()
    assert again["report"]["memory"]["total_bytes"] == 32 * GIB


def test_probe_cached_never_reads_simulation(monkeypatch, published):
    hardware.probe()
    monkeypatch.setenv("ATOMIC_HW_JSON",
                       '{"simulated": true, "gpus": []}')
    def _boom():
        raise AssertionError("probe_cached must not consult sim sources")
    monkeypatch.setattr(hardware, "_simulated_report", _boom)
    rec = hardware.probe_cached()
    assert rec is not None and rec["simulated"] is False


def test_probe_cached_no_os_or_fs_access(monkeypatch, published):
    hardware.probe()
    import psutil
    monkeypatch.setattr(psutil, "virtual_memory",
                        lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr(hardware, "_cached_probe",
                        lambda b: (_ for _ in ()).throw(AssertionError()))
    assert hardware.probe_cached() is not None


def test_concurrent_publish_newest_collection_wins():
    # Two discoveries race; the one collected LATER must win regardless
    # of which publish lands last.
    for _ in range(20):
        hardware._reset_published()
        slow = probe_report(probed_monotonic=200.0)
        fast = probe_report(probed_monotonic=400.0,
                            memory={"total_bytes": 7})
        threads = [threading.Thread(target=hardware._publish,
                                    args=(slow,)),
                   threading.Thread(target=hardware._publish,
                                    args=(fast,))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert hardware.probe_cached()["probed_monotonic"] == 400.0


# --------------------------------------------- Gate 4: pure observation

def test_state_for_never_calls_probe_callback():
    def _boom():
        raise AssertionError("state_for must never acquire evidence")
    sched = GpuScheduler(_boom, {})
    st = sched.state_for([gpu(free=6 * GIB)], simulated=False)
    assert st["gpu_count"] == 1 and st["slots"][0]["reserved_bytes"] == 0


def test_state_for_preserves_reservations_and_lost():
    sched = GpuScheduler(lambda: probe_report(gpus=[gpu()]), {})
    sched._reserved[0] = {"j1": 2 * GIB}        # seed a live reservation
    sched._reserved[7] = {"j2": GIB}            # lost device still holding
    st = sched.state_for([gpu(0), gpu(1)], simulated=True)
    s0 = st["slots"][0]
    assert s0["reserved_bytes"] == 2 * GIB
    assert st["lost_devices"] == [7]
    assert st["simulated"] is True
    # Observational: a second read is identical — nothing mutated.
    assert sched.state_for([gpu(0), gpu(1)], simulated=True) == st


# ------------------------------------------------- Gate 6: pure RAM eval

def test_evaluate_ram_no_memory_never_measures(monkeypatch):
    monkeypatch.setattr(resources, "system_memory",
                        lambda: (_ for _ in ()).throw(AssertionError()))
    out = resources.evaluate_ram(4 * GIB, known=True, mem=None)
    assert out["decision"] == "unknown"
    assert out["fully_evaluated"] is False
    assert out["checks"]["memory_evidence"] == "unavailable"


def test_evaluate_ram_missing_commit_is_unknown_not_ok():
    # Cached probe memory carries physical fields only — the required
    # commit check degrades honestly instead of passing silently.
    out = resources.evaluate_ram(
        4 * GIB, known=True,
        mem={"total_bytes": 32 * GIB, "available_bytes": 24 * GIB},
        margins=MARGINS)
    assert out["checks"]["phys_avail"] == "ok"
    assert out["checks"]["commit_headroom"] == "unknown"
    assert out["decision"] == "unknown"
    assert out["fully_evaluated"] is False


def test_evaluate_ram_block_warn_ok_levels():
    base = {"total_bytes": 32 * GIB, "commit_headroom_bytes": 30 * GIB}
    block = resources.evaluate_ram(
        24 * GIB, known=True,
        mem={**base, "available_bytes": int(24.5 * GIB)},
        margins=MARGINS)
    assert block["decision"] == "block"
    warn = resources.evaluate_ram(
        20 * GIB, known=True,
        mem={**base, "available_bytes": int(23.5 * GIB)},
        margins=MARGINS)
    assert warn["decision"] == "warn" and warn["fully_evaluated"] is True
    ok = resources.evaluate_ram(
        4 * GIB, known=True,
        mem={**base, "available_bytes": 24 * GIB}, margins=MARGINS)
    assert ok["decision"] == "ok" and ok["fully_evaluated"] is True


def test_evaluate_ram_estimate_unknown():
    out = resources.evaluate_ram(
        None, known=False,
        mem={"total_bytes": 32 * GIB, "available_bytes": 24 * GIB,
             "commit_headroom_bytes": 30 * GIB}, margins=MARGINS)
    assert out["decision"] == "unknown"
    assert out["fully_evaluated"] is False


# ------------------------------------------------ Gate 5: CPU evidence

def _spec(cpu_fallback=True):
    import types
    return types.SimpleNamespace(
        capabilities={"cpu_fallback": cpu_fallback})


def test_cpu_evidence_class_ladder():
    spec = _spec(True)
    assert pp.cpu_evidence_class(spec, {}, True, True) == pp.CPU_UNSUPPORTED
    assert pp.cpu_evidence_class(spec, ("cpu",), None, None) \
        == pp.CPU_DECLARED
    assert pp.cpu_evidence_class(spec, ("cpu",), True, None) \
        == pp.CPU_ADAPTER_CONTRACT
    assert pp.cpu_evidence_class(spec, ("cpu",), True, True) \
        == pp.CPU_COMMAND_CONSISTENT
    no_cpu = _spec(False)
    assert pp.cpu_evidence_class(no_cpu, ("cpu",), True, True) \
        == pp.CPU_UNSUPPORTED


def _ok_eval(est, known):
    return {"decision": "ok", "reason": "", "fully_evaluated": True,
            "checks": {}, "snapshot": {}}


@pytest.mark.parametrize("cls", [pp.CPU_UNSUPPORTED, pp.CPU_DECLARED,
                                 pp.CPU_ADAPTER_CONTRACT,
                                 pp.CPU_COMMAND_CONSISTENT])
def test_no_static_cpu_evidence_can_satisfy(cls):
    lane = pp.cpu_lane(evidence_class=cls, explicitly_requested=False,
                       est_ram=GIB, ram_known=True, evaluate=_ok_eval)
    assert lane["outcome"] != pp.CPU_LANE_OK


def test_cpu_operational_would_satisfy_but_is_unreachable():
    # cpu_operational is the only satisfying class — and nothing in the
    # current contract can produce it (kept honest, tested for the day
    # real evidence exists).
    lane = pp.cpu_lane(evidence_class=pp.CPU_OPERATIONAL,
                       explicitly_requested=False, est_ram=GIB,
                       ram_known=True, evaluate=_ok_eval)
    assert lane["outcome"] == pp.CPU_LANE_OK


# ----------------------------------------- CD-9: zero free VRAM stays 0

def test_usable_vram_measured_zero_stays_zero():
    assert compatibility.usable_vram(
        {"vram_bytes": 24 * GIB, "vram_free_bytes": 0}) == 0


def test_usable_vram_unknown_free_uses_fallback():
    got = compatibility.usable_vram(
        {"vram_bytes": 24 * GIB, "vram_free_bytes": None})
    assert got == int(24 * GIB * 0.85)


# -------------------------------------- Gate 8: imported residency floor

def _dep_plan(lora_held=False, runtime_state="candidate_compatible_static"):
    return t5c.dep_plan(lora_policy_held=lora_held,
                        root_runtime_state=runtime_state)


def _preview(**kw):
    return t5c.preview(**kw)


def test_imported_declared_bytes_alone_insufficient_evidence():
    # Imported model: file bytes > budget must NOT prove insufficiency —
    # resident weight/activation requirements are unknown.
    p = _preview(spec=None, weights_declared=30 * GIB,
                 snapshot=snap(gpus=[gpu(free=6 * GIB)],
                               sched=sched_state([slot()]),
                               probed_at=1.0))
    alt = p["device_alternatives"][0]
    assert alt["fit_class"] == "insufficient_evidence"
    assert alt["weights_declared_bytes"] == 30 * GIB


# ------------------------------- Gate 7: deterministic aggregation

def test_stale_insufficient_is_incomplete_not_blocked():
    stale_at = time.monotonic() - (hs.STALE_SNAPSHOT_SECONDS + 60)
    p = _preview(snapshot=snap(gpus=[gpu(free=2 * GIB)],
                             sched=sched_state([slot()]),
                             probed_at=stale_at),
                 freshness="stale",
                 cpu_evidence=pp.CPU_UNSUPPORTED)
    assert p["advisory_state"] == "preview_incomplete"
    assert "stale_evidence" in p["reasons"]
    assert "hardware_insufficient" not in p["reasons"]


def test_simulated_insufficient_is_incomplete_not_blocked():
    p = _preview(snapshot=snap(gpus=[gpu(free=2 * GIB)],
                             sched=sched_state([slot()]),
                             probed_at=1.0, simulated=True),
                 simulated=True,
                 cpu_evidence=pp.CPU_UNSUPPORTED)
    assert p["advisory_state"] == "preview_incomplete"
    assert "simulated_evidence" in p["reasons"]


def test_unavailable_evidence_incomplete():
    # snapshot={} IS the explicit-unavailable path — the helper treats
    # None as "build a default fixture".
    p = _preview(snapshot={}, freshness="unavailable",
                 cpu_evidence=pp.CPU_UNSUPPORTED)
    assert p["advisory_state"] == "preview_incomplete"
    assert "hardware_evidence_unavailable" in p["reasons"]


def test_fresh_exhaustive_insufficiency_blocks():
    p = _preview(snapshot=snap(gpus=[gpu(free=2 * GIB)],
                             sched=sched_state([slot()]),
                             probed_at=1.0),
                 cpu_evidence=pp.CPU_UNSUPPORTED)
    assert p["advisory_state"] == "preview_blocked"
    assert "hardware_insufficient" in p["reasons"]


def test_authoritative_blocker_beats_missing_evidence():
    # A planned-runtime hold is a contract fact — it stays blocked even
    # when hardware evidence is entirely unavailable.
    p = _preview(planned=True, snapshot={}, freshness="unavailable",
                 cpu_evidence=pp.CPU_UNSUPPORTED)
    assert p["advisory_state"] == "preview_blocked"
    assert "runtime_planned" in p["reasons"]


def test_enumeration_truncation_is_incomplete():
    gpus = [gpu(i, free=1 * GIB) for i in
            range(pp.MAX_DEVICE_ALTERNATIVES + 1)]
    slots = [slot(i) for i in range(len(gpus))]
    p = _preview(snapshot=snap(gpus=gpus, sched=sched_state(slots),
                             probed_at=1.0),
                 cpu_evidence=pp.CPU_UNSUPPORTED)
    assert p["advisory_state"] == "preview_incomplete"
    assert "limits_exceeded" in p["reasons"]


def test_unverified_cpu_lane_cannot_rescue_gpu_block():
    # command_consistent lane is permitted-but-unverified → the verdict
    # degrades to honest incomplete, never READY and never a hidden pass.
    p = _preview(snapshot=snap(gpus=[gpu(free=2 * GIB)],
                             sched=sched_state([slot()]),
                             probed_at=1.0),
                 cpu_evidence=pp.CPU_COMMAND_CONSISTENT)
    assert p["advisory_state"] == "preview_incomplete"
    assert p["cpu_lane"]["outcome"] == pp.CPU_LANE_UNVERIFIED


# --------------------------------------- Gate 9: fingerprint contracts

def test_fingerprint_versions():
    p = _preview(snapshot=snap(gpus=[gpu()], probed_at=1.0))
    assert p["plan_fingerprint"].startswith("plan:v2:")
    assert p["plan_result_fingerprint"].startswith("planresult:v1:")


def _result_fp(**kw):
    return _preview(**kw)["plan_result_fingerprint"]


def test_result_fingerprint_stable_on_repeat_and_elapsed_seconds():
    s = snap(gpus=[gpu()], probed_at=1000.0)
    fp1 = _result_fp(snapshot=s)
    fp2 = _result_fp(snapshot=s)          # identical evidence → stable
    s_same = snap(gpus=[gpu()], probed_at=2000.0)   # same evidence later
    fp3 = _result_fp(snapshot=s_same)
    assert fp1 == fp2 == fp3


def test_result_fingerprint_fresh_to_stale_changes():
    fresh = _result_fp(snapshot=snap(gpus=[gpu()], probed_at=1.0))
    stale = _result_fp(snapshot=snap(gpus=[gpu()], probed_at=1.0),
                       freshness="stale")
    assert fresh != stale


def test_result_fingerprint_simulated_and_unavailable_change():
    base = _result_fp(snapshot=snap(gpus=[gpu()], probed_at=1.0))
    sim = _result_fp(snapshot=snap(gpus=[gpu()], probed_at=1.0,
                                   simulated=True), simulated=True)
    unavail = _result_fp(snapshot={}, freshness="unavailable")
    assert len({base, sim, unavail}) == 3


def test_result_fingerprint_reservation_change_same_fit():
    s1 = snap(gpus=[gpu(free=6 * GIB)],
              sched=sched_state([slot(reserved=0)]), probed_at=1.0)
    s2 = snap(gpus=[gpu(free=6 * GIB)],
              sched=sched_state([slot(reserved=GIB)]), probed_at=1.0)
    f1, f2 = _result_fp(snapshot=s1), _result_fp(snapshot=s2)
    assert f1 != f2


def test_result_fingerprint_cpu_evidence_change():
    s = snap(gpus=[gpu(free=6 * GIB)], probed_at=1.0)
    a = _result_fp(snapshot=s, cpu_evidence=pp.CPU_UNSUPPORTED)
    b = _result_fp(snapshot=s, cpu_evidence=pp.CPU_COMMAND_CONSISTENT)
    assert a != b


def test_result_fingerprint_blocker_change():
    s = snap(gpus=[gpu(free=6 * GIB)], probed_at=1.0)
    clear = _result_fp(snapshot=s)
    held = _result_fp(planned=True, snapshot=s)
    assert clear != held


# ------------------------------ endpoint transitive guards (Gate 11)

from test_planpreview_u5c import sim_gpu  # noqa: E402,F401 — fixture


def test_ep_cold_cache_explicit_unavailable(client, monkeypatch):
    """Cold cache: 200 with snapshot null + freshness unavailable —
    the endpoint NEVER falls back to discovery."""
    def _boom(**kw):
        raise AssertionError("execution-preview must never call probe()")
    monkeypatch.setattr(hardware, "probe", _boom)

    def _sched_boom():
        raise AssertionError("state_for must not invoke the probe cb")
    monkeypatch.setattr(client.app.state, "gpu_scheduler",
                        GpuScheduler(_sched_boom, {}))
    pid = t5c._project(client)
    t5c._verdict(client, t5c.WAN_AID, hw=probe_report(gpus=[gpu()]))
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                             workflow="text2video"))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["snapshot"] is None
    assert b["snapshot_freshness"] == "unavailable"
    assert b["advisory_state"] == "preview_incomplete"
    assert "hardware_evidence_unavailable" in b["reasons"]


def test_ep_never_live_measures_ram(client, sim_gpu, monkeypatch):
    """The preview path consumes only the published record — no psutil
    or system_memory call may fire inside the request."""
    monkeypatch.setattr(resources, "system_memory",
                        lambda: (_ for _ in ()).throw(AssertionError()))
    pid = t5c._project(client)
    rep, _ = sim_gpu
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                             workflow="text2video"))
    assert r.status_code == 200, r.text


def test_ep_stale_inventory_no_absence_422(client, monkeypatch):
    """A stale snapshot cannot prove device absence — a syntactically
    valid index on stale evidence evaluates unverified, never 422s."""
    rep = probe_report(
        gpus=[gpu(0, free=6 * GIB)],
        probed_at=time.time() - (hs.STALE_SNAPSHOT_SECONDS + 60),
        probed_monotonic=time.monotonic()
        - (hs.STALE_SNAPSHOT_SECONDS + 60))
    hardware._publish(rep)
    sched = GpuScheduler(lambda: rep, {})
    monkeypatch.setattr(client.app.state, "gpu_scheduler", sched)
    pid = t5c._project(client)
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                             workflow="text2video", device="3"))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["advisory_state"] == "preview_incomplete"


def test_ep_fresh_absent_index_422_preserved(client, sim_gpu):
    """Fresh complete real inventory retains the explicit-absent-index
    422 — absence is provable there."""
    pid = t5c._project(client)
    rep, _ = sim_gpu
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                             workflow="text2video", device="9"))
    assert r.status_code == 422


def test_ep_explicit_index_beyond_auto_window(client, sim_gpu):
    """An explicit index needn't be inside the 8-device auto window —
    it is evaluated directly."""
    pid = t5c._project(client)
    rep, _ = sim_gpu
    t5c._verdict(client, t5c.WAN_AID, hw=rep)
    r = client.get(t5c._purl(pid, t5c.WAN_AID, runtime_key="sdcpp",
                             workflow="text2video", device="0"))
    assert r.status_code == 200
    assert r.json()["device_alternatives"][0]["index"] == 0
