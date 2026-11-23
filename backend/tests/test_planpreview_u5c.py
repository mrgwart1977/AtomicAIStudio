"""U-5c-1 — hardware-aware execution-plan preview tests.

Synthetic fixtures only — no files, no network, no process spawning, no
GPU, no inference, no acquire(). hw_snapshot/plan_preview are pure
functions over injected views; endpoint tests swap the probe + scheduler
with canned evidence.
"""

from __future__ import annotations

import time
from typing import ClassVar

import pytest
from sqlalchemy import select

from app.db.models import CompatVerdict, GenerationJob, ModelArtifact
from app.domain import compatibility, model_catalog
from app.domain import dep_graph as g
from app.domain import dep_resolve as dr
from app.domain import hw_snapshot as hs
from app.domain import plan_preview as pp
from app.domain.gpu_sched import GpuScheduler
from app.domain.model_families import FAMILIES
from app.domain.runtime_caps import RUNTIME_CAPS
from app.domain.workflow_defs import WORKFLOW_DEFS

GIB = 1024 ** 3
SDCPP = RUNTIME_CAPS["sdcpp"]
COMFY = RUNTIME_CAPS["comfyui"]
WF_T2V = WORKFLOW_DEFS["text2video"]
WF_T2I = WORKFLOW_DEFS["text2image"]

SPEC_WAN = model_catalog.get("wan21.t2v.1_3b")
SPEC_SD15 = model_catalog.get("sd15.fp16")
SD15_FILES = sum(f.size_bytes for f in SPEC_SD15.files)          # 2132586944
WAN_FILES = sum(f.size_bytes for f in SPEC_WAN.files)            # 6747264190
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
                 memory=None):
    return {
        "probed_at": time.time() if probed_at is None else probed_at,
        "os": {}, "cpu": {"name": "cpu", "physical_cores": 8,
                          "logical_cores": 16},
        "memory": memory or {"total_bytes": 32 * GIB,
                             "available_bytes": 24 * GIB},
        "gpus": gpus or [], "disks": [],
        "backends": backends or {
            "cpu": {"available": True, "measured": True},
            "vulkan": {"available": True, "measured": True},
            "cuda": {"available": True, "measured": True},
            "directml": {"available": True, "measured": False}},
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


def man(*entries):
    return tuple(g.ManifestEntry(*e) for e in entries)


def view(aid, *, family="wan", kind="model", fmt="safetensors",
         role="diffusion", manifest=(), source_type="catalog",
         content_sha256="", modality="video"):
    return dr.ArtifactView(
        artifact_id=aid, kind=kind, family=family, fmt=fmt, role=role,
        modality=modality, source_type=source_type, managed_by="atomic",
        content_sha256=content_sha256, enabled=True,
        support_tier="tested", version="", manifest=manifest,
        install_status="", install_verified=False, install_sha256="",
        inspection_level=0, requires_inspection=False)


def comp(v, *, conf="missing", policy="eligible", stale=False):
    return dr.CompEntry(view=v, verdict_confidence=conf,
                        policy_axis=policy, stale=stale)


WAN_MAN = man(
    ("wan2.1_t2v_1.3B_fp16.safetensors", "diffusion", "sha-d",
     2838303560),
    ("wan_2.1_vae.safetensors", "vae", "sha-v", 253815318),
    ("umt5-xxl-encoder-Q4_K_M.gguf", "text_encoder", "sha-t",
     3655145312))
WAN_ROOT = view("catalog:wan21.t2v.1_3b", manifest=WAN_MAN)


def dep_plan(**kw):
    """A real U-5b DepPlan over synthetic views — Wan bundle, compatible."""
    args = {"root": WAN_ROOT, "root_confidence": "tested",
            "root_verdict": None, "workflow": WF_T2V,
            "fam_spec": FAMILIES["wan"], "cap": SDCPP,
            "runtime_key": "sdcpp",
            "root_runtime_state": "candidate_compatible_static",
            "pool": {}, "cast": dr.CastContext(False), "selectors": {},
            "key_to_artifact": {}, "spec_role": lambda k: "",
            "fam_spec_for": lambda aid: None, "lora_policy_held": False,
            "include_stale": True, "fingerprint_inputs": {"t": 1}}
    args.update(kw)
    return dr.resolve_plan(**args)


_SCHED = object()


def snap(gpus=None, sched=_SCHED, margins=None, now=None, probed_at=None,
         backends=None, simulated=False):
    return hs.build_snapshot(
        probe_report(gpus=gpus, backends=backends, simulated=simulated,
                     probed_at=probed_at),
        sched_state() if sched is _SCHED else sched,
        margins or MARGINS, now=now)


def preflight(decision="ok", reason=""):
    return lambda est, known: {"decision": decision, "reason": reason,
                               "snapshot": {}}


def preview(*, plan=None, spec=SPEC_WAN, runtime_key="sdcpp",
            decl_backends=None, planned=False, snapshot=None,
            components=None, lora_apps=0, weights_declared=None,
            residency="gpu", device_sel="auto", selected_index=None,
            width=832, height=480, batch=1, wf="text2video",
            preflight_fn=None, ram_known=True, margins=None):
    return pp.build_preview(
        plan=plan or dep_plan(), spec=spec, runtime_key=runtime_key,
        runtime_decl_backends=(
            decl_backends if decl_backends is not None
            else tuple(SDCPP.backends)),
        runtime_planned=planned,
        snapshot=snapshot if snapshot is not None else snap(
            gpus=[gpu()], sched=sched_state([slot()]), probed_at=1000.0,
            now=1100.0),
        components=components or [], lora_apps=lora_apps,
        weights_declared=weights_declared, residency=residency,
        residency_basis="explicit", device_sel=device_sel,
        selected_index=selected_index, width=width, height=height,
        batch=batch, workflow_key=wf, include_stale=True,
        preflight=preflight_fn or preflight(), ram_known=ram_known,
        margins=margins or MARGINS)


# ====================================================== hw_snapshot

def test_snapshot_measured_free_is_usable():
    s = snap(gpus=[gpu(free=5 * GIB)], sched=sched_state([slot()]))
    d = s["devices"][0]
    assert d["vram_free_bytes"] == 5 * GIB
    assert d["usable_bytes"] == 5 * GIB          # measured free wins
    assert d["conservative_planning_budget_bytes"] == 5 * GIB


def test_snapshot_usable_falls_back_to_85pct_total():
    g0 = gpu(free=None, total=8 * GIB, source="registry")
    s = snap(gpus=[g0], sched=sched_state([slot()]))
    assert s["devices"][0]["usable_bytes"] == int(8 * GIB * 0.85)


def test_snapshot_unmeasurable_stays_none_never_zero():
    g0 = gpu(free=None, total=None, source=None)
    s = snap(gpus=[g0], sched=sched_state([slot()]))
    d = s["devices"][0]
    assert d["usable_bytes"] is None
    assert d["conservative_planning_budget_bytes"] is None
    assert d["optimistic_bound_bytes"] is None
    assert f"gpu{d['index']}_vram_unmeasurable" in s["unknowns"]


def test_cpb_subtracts_headroom_then_reservations():
    s = snap(gpus=[gpu(free=5 * GIB)],
             sched=sched_state([slot(reserved=2 * GIB)]),
             margins={**MARGINS, "vram_headroom_bytes": int(0.5 * GIB)})
    d = s["devices"][0]
    assert d["usable_after_headroom_bytes"] == int(5 * GIB - 0.5 * GIB)
    assert d["reserved_bytes"] == 2 * GIB
    assert d["conservative_planning_budget_bytes"] == int(2.5 * GIB)
    assert d["optimistic_bound_bytes"] == int(4.5 * GIB)
    assert d["reservation_overlap_possible"] is True


def test_overlap_flag_only_when_free_measured_and_reserved():
    a = snap(gpus=[gpu(free=5 * GIB)], sched=sched_state([slot()]))
    assert a["devices"][0]["reservation_overlap_possible"] is False
    b = snap(gpus=[gpu(free=None)],
             sched=sched_state([slot(reserved=GIB)]))
    assert b["devices"][0]["reservation_overlap_possible"] is False


def test_reserved_is_bookkeeping_never_physical():
    s = snap(gpus=[gpu(free=5 * GIB)],
             sched=sched_state([slot(reserved=GIB)]))
    d = s["devices"][0]
    assert d["vram_free_bytes"] == 5 * GIB        # driver figure untouched
    assert d["reserved_bytes"] == GIB             # separate label


def test_snapshot_lost_device_evidence_retained():
    st = sched_state([slot(0), {"index": 9, "name": None, "vendor": None,
                                "vram_bytes": None, "vram_free_bytes": None,
                                "usable_bytes": 0, "reserved_bytes": 4 * GIB,
                                "free_after_reserve_bytes": 0, "jobs": {},
                                "busy_job_id": "j1", "present": False}])
    s = snap(gpus=[gpu()], sched=st)
    assert s["lost_devices"] == [{"index": 9, "reserved_bytes": 4 * GIB}]


def test_snapshot_id_stable_across_time_only_change():
    a = snap(gpus=[gpu()], sched=sched_state([slot()]), probed_at=100.0,
             now=120.0)
    b = snap(gpus=[gpu()], sched=sched_state([slot()]), probed_at=400.0,
             now=420.0)
    assert a["snapshot_id"] == b["snapshot_id"]   # timestamps excluded
    assert a["age_s"] == 20.0 and b["age_s"] == 20.0


def test_snapshot_id_changes_on_material_evidence_change():
    a = snap(gpus=[gpu()], sched=sched_state([slot()]))
    b = snap(gpus=[gpu()], sched=sched_state([slot(reserved=GIB)]))
    c = snap(gpus=[gpu(), gpu(index=1, name="GPU1", vendor="AMD")],
             sched=sched_state([slot(), slot(1)]))
    assert a["snapshot_id"] != b["snapshot_id"]   # reservation change
    assert a["snapshot_id"] != c["snapshot_id"]   # new device


def test_snapshot_stale_flag_and_age():
    s = snap(gpus=[gpu()], sched=sched_state([slot()]),
             probed_at=1000.0, now=1301.0)
    assert s["stale"] is True and s["age_s"] == pytest.approx(301.0)
    s2 = snap(gpus=[gpu()], sched=sched_state([slot()]),
              probed_at=1000.0, now=1299.0)
    assert s2["stale"] is False


def test_snapshot_simulated_and_reservations_flags():
    s = snap(gpus=[gpu()], sched=None, simulated=True)
    assert s["simulated"] is True
    assert s["reservations_captured"] is False
    assert "scheduler_reservations_unavailable" in s["unknowns"]


# ---------------------------------------------- device backend evidence

def test_cuda_measured_requires_perdevice_smi():
    dev = gpu()                                  # NVIDIA + smi free
    grade, _ = hs.device_backend_evidence(
        dev, {"cuda": {"available": True, "measured": True}}, "cuda")
    assert grade == "measured"


def test_cuda_nvidia_without_smi_is_inferred():
    dev = gpu(free=None)
    grade, _ = hs.device_backend_evidence(
        dev, {"cuda": {"available": True, "measured": False}}, "cuda")
    assert grade == "inferred"


def test_cuda_amd_is_mismatch_unknown_vendor_is_unknown():
    amd = hs.device_backend_evidence(
        gpu(vendor="AMD"), {"cuda": {"available": True, "measured": False}},
        "cuda")
    unk = hs.device_backend_evidence(
        gpu(vendor=None), {"cuda": {"available": True, "measured": False}},
        "cuda")
    assert amd[0] == "mismatch" and unk[0] == "unknown"


def test_machine_wide_backend_is_inferred_never_measured():
    dev = gpu(vendor="AMD")
    backends = {"vulkan": {"available": True, "measured": True},
                "directml": {"available": True, "measured": False}}
    assert hs.device_backend_evidence(dev, backends, "vulkan")[0] \
        == "inferred"
    assert hs.device_backend_evidence(dev, backends, "directml")[0] \
        == "inferred"


def test_backend_unavailable_on_machine_is_unknown():
    grade, _ = hs.device_backend_evidence(
        gpu(vendor="AMD"), {"vulkan": {"available": False}}, "vulkan")
    assert grade == "unknown"


# --------------------------------------------------------- residency

def test_residency_gpu_always_permitted():
    ok, _ = pp.residency_evidence("sdcpp", SPEC_SD15, "gpu")
    assert ok is True


def test_residency_offload_requires_capability_and_contract():
    ok, _ = pp.residency_evidence("sdcpp", SPEC_WAN, "cpu_offload")
    assert ok is True                         # Wan declares weight_offload_cpu
    ok, r = pp.residency_evidence("sdcpp", SPEC_SD15, "cpu_offload")
    assert ok is False and "weight_offload_cpu" in r
    ok, r = pp.residency_evidence("comfyui", SPEC_WAN, "cpu_offload")
    assert ok is False and "comfyui" in r     # no execution contract
    ok, r = pp.residency_evidence("sdcpp", None, "cpu_offload")
    assert ok is False and "imported" in r
    ok, r = pp.residency_evidence("sdcpp", SPEC_WAN, "te_cpu")
    assert ok is False                        # capability undeclared today


def test_residency_unknown_mode_refused():
    ok, r = pp.residency_evidence("sdcpp", SPEC_SD15, "nonsense")
    assert ok is False and "residency_mode_unknown" in r


# ------------------------------------------------------- estimation

def test_sd15_vram_and_ram_estimates():
    est = pp.stage_estimates(SPEC_SD15, None, 512, 512, 1, "gpu", 0)
    # canonical path — exactly compatibility.estimate_vram, nothing else
    assert est["est_vram_bytes"] == compatibility.estimate_vram(
        SPEC_SD15, 512, 512, 1, "gpu")
    assert est["vram_basis"] == "heuristic"
    assert est["est_ram_bytes"] == int(0.3 * SD15_FILES + 2 * GIB)


def test_wan_offload_is_explicit_not_defaulted():
    gpu_est = pp.stage_estimates(SPEC_WAN, WAN_FILES, 832, 480, 1, "gpu", 0)
    off_est = pp.stage_estimates(SPEC_WAN, WAN_FILES, 832, 480, 1,
                                 "cpu_offload", 0)
    assert gpu_est["est_vram_bytes"] == WAN_FILES + int(2.5 * GIB *
                                                      (399360 / 262144)
                                                      ** 0.5)
    # cpu_offload: activation + ~30% weights buffer — much smaller
    assert off_est["est_vram_bytes"] < gpu_est["est_vram_bytes"]
    assert off_est["est_ram_bytes"] == WAN_FILES + 2 * GIB


def test_lora_overhead_added_exactly_once():
    a = pp.stage_estimates(SPEC_SD15, None, 512, 512, 1, "gpu", 0)
    b = pp.stage_estimates(SPEC_SD15, None, 512, 512, 1, "gpu", 2)
    assert b["est_vram_bytes"] - a["est_vram_bytes"] == 2 * 768 * 1024 * 1024


def test_imported_artifact_partial_estimate():
    est = pp.stage_estimates(None, int(4.1 * GIB), 512, 512, 1, "gpu", 0)
    assert est["est_vram_bytes"] is None
    assert est["vram_basis"] == "unknown"
    assert est["ram_basis"] == "partial"
    assert est["runtime_overhead_basis"] == "unknown"
    assert est["est_ram_bytes"] == int(4.1 * GIB + 2 * GIB)


# --------------------------------------------------------- device fit

def test_fit_classes_full_spectrum():
    snap0 = snap(gpus=[gpu(free=5 * GIB)], sched=sched_state([slot()]))
    dev = snap0["devices"][0]
    backends = {"vulkan": {"available": True, "measured": True},
                "cuda": {"available": True, "measured": True}}
    fit = pp.evaluate_device(dev, backends, ("vulkan", "cuda"), 3 * GIB, 0)
    assert fit["fit_class"] == "fits_with_headroom"      # 3+0.7 <= 5
    fit = pp.evaluate_device(dev, backends, ("vulkan",), int(4.5 * GIB), 0)
    assert fit["fit_class"] == "fits_tight"              # 4.5<=5 but +0.7>5
    fit = pp.evaluate_device(dev, backends, ("vulkan",), 6 * GIB, 0)
    assert fit["fit_class"] == "insufficient"


def test_fit_unmeasurable_and_estimate_unknown():
    g0 = gpu(free=None, total=None, source=None)
    s = snap(gpus=[g0], sched=sched_state([slot()]))
    backends = {"vulkan": {"available": True, "measured": True}}
    fit = pp.evaluate_device(s["devices"][0], backends, ("vulkan",), GIB, 0)
    assert fit["fit_class"] == "unmeasurable"
    fit = pp.evaluate_device(s["devices"][0], backends, ("vulkan",),
                             None, int(1 * GIB))
    assert fit["fit_class"] == "unmeasurable"     # budget also unknown
    # measurable budget + unknown estimate → insufficient_evidence
    g1 = gpu(free=2 * GIB)
    s1 = snap(gpus=[g1], sched=sched_state([slot()]))
    fit = pp.evaluate_device(s1["devices"][0], backends, ("vulkan",),
                             None, int(1 * GIB))
    assert fit["fit_class"] == "insufficient_evidence"
    # declared weights alone exceeding budget proves insufficiency
    fit = pp.evaluate_device(s1["devices"][0], backends, ("vulkan",),
                             None, int(4 * GIB))
    assert fit["fit_class"] == "insufficient"


def test_device_backend_mismatch_and_lost_device():
    s = snap(gpus=[gpu(vendor="AMD")], sched=sched_state([slot()]),
             backends={"cpu": {"available": True, "measured": True},
                       "vulkan": {"available": False, "measured": False},
                       "cuda": {"available": True, "measured": True}})
    fit = pp.evaluate_device(s["devices"][0], s["backends"],
                             ("cuda",), GIB, 0)
    assert fit["fit_class"] == "device_backend_mismatch"
    assert fit["eligible"] is False
    lost = {"index": 3, "present": False}
    fit = pp.evaluate_device(lost, {}, ("vulkan",), GIB, 0)
    assert fit["fit_class"] == "device_not_present"


def test_headroom_applied_once_not_twice():
    """budget already nets reservations+margin; the 0.7 GiB stability
    margin lives ONLY in the fit comparison."""
    s = snap(gpus=[gpu(free=5 * GIB)],
             sched=sched_state([slot(reserved=GIB)]),
             margins={**MARGINS, "vram_headroom_bytes": GIB})
    d = s["devices"][0]
    assert d["conservative_planning_budget_bytes"] == 3 * GIB
    fit = pp.evaluate_device(d, {"vulkan": {"available": True}},
                             ("vulkan",), int(2.5 * GIB), 0)
    # 2.5 + 0.7 = 3.2 > 3.0 → tight, not double-headroomed into insufficient
    assert fit["fit_class"] == "fits_tight"


# ------------------------------------------------------------- cpu lane

def test_cpu_lane_requires_declared_support():
    lane = pp.cpu_lane(declared_cap=True, cpu_backend_declared=True,
                       explicitly_requested=False, est_ram=3 * GIB,
                       ram_known=True, preflight=preflight())
    assert lane["permitted"] and lane["outcome"] == "cpu_lane_ok"
    lane = pp.cpu_lane(declared_cap=False, cpu_backend_declared=True,
                       explicitly_requested=True, est_ram=None,
                       ram_known=True, preflight=preflight())
    assert lane["permitted"] is False
    assert lane["outcome"] == "cpu_fallback_unsupported"
    lane = pp.cpu_lane(declared_cap=True, cpu_backend_declared=False,
                       explicitly_requested=False, est_ram=GIB,
                       ram_known=True, preflight=preflight())
    assert lane["permitted"] is False


def test_cpu_lane_ram_decisions_propagate():
    lane = pp.cpu_lane(declared_cap=True, cpu_backend_declared=True,
                       explicitly_requested=False, est_ram=40 * GIB,
                       ram_known=True, preflight=preflight("block", "low"))
    assert lane["outcome"] == "cpu_lane_blocked"
    assert lane["ram_decision"] == "block"
    lane = pp.cpu_lane(declared_cap=True, cpu_backend_declared=True,
                       explicitly_requested=False, est_ram=10 * GIB,
                       ram_known=False,
                       preflight=preflight("warn", "unknown"))
    assert "ram_estimate_unknown" in lane["limitations"]


# ------------------------------------------------- aggregation (F3)

def test_ready_advisory_single_fitting_gpu():
    s = snap(gpus=[gpu(free=6 * GIB)], sched=sched_state([slot()]),
             probed_at=1000.0, now=1100.0)
    p = preview(snapshot=s, width=832, height=480, residency="cpu_offload")
    assert p["advisory_state"] == "preview_ready_advisory"
    assert p["advisory"] is True
    assert p["device_alternatives"][0]["fit_class"] == "fits_with_headroom"


def test_policy_hold_overrides_hardware_fit():
    bind = (dr.CastBinding("c1", "lora.id.x"),)
    cast = dr.CastContext(True, ("c1",), bind)
    plan = dep_plan(cast=cast, lora_policy_held=True,
                    workflow=WORKFLOW_DEFS["lora_character"],
                    key_to_artifact={"lora.id.x": "catalog:lora.id.x"},
                    spec_role=lambda k: "identity",
                    pool={"catalog:lora.id.x": comp(
                        view("catalog:lora.id.x", family="sd1.5",
                             kind="adapter", role="lora:identity",
                             modality="lora"))})
    assert plan.policy_state == "held"
    s = snap(gpus=[gpu(free=6 * GIB)], sched=sched_state([slot()]),
             probed_at=1.0, now=2.0)
    p = preview(plan=plan, spec=SPEC_SD15, snapshot=s, width=512,
                height=512, wf="lora_character")
    assert p["policy_state"] == "held"
    assert p["advisory_state"] == "preview_blocked"


def test_planned_runtime_blocked_even_when_everything_fits():
    p = preview(runtime_key="comfyui",
                decl_backends=tuple(COMFY.backends), planned=True,
                residency="gpu")
    assert p["advisory_state"] == "preview_blocked"
    assert "runtime_planned" in p["reasons"]


def test_blocked_runtime_verdict_blocked():
    p = preview(plan=dep_plan(root_runtime_state="candidate_blocked"))
    assert p["advisory_state"] == "preview_blocked"


def test_ram_block_is_blocked_not_incomplete():
    s = snap(gpus=[gpu(free=6 * GIB)], sched=sched_state([slot()]),
             probed_at=1.0, now=2.0)
    p = preview(snapshot=s, residency="cpu_offload",
                preflight_fn=preflight("block", "insufficient system RAM"))
    assert p["advisory_state"] == "preview_blocked"
    assert "ram_insufficient" in p["reasons"]


def test_all_devices_insufficient_is_blocked():
    s = snap(gpus=[gpu(free=2 * GIB)], sched=sched_state([slot()]),
             probed_at=1.0, now=2.0)
    p = preview(snapshot=s, residency="cpu_offload",
                preflight_fn=preflight("block", "ram"))
    assert p["advisory_state"] == "preview_blocked"


def test_unmeasurable_never_upgraded_to_ready():
    # Spec WITHOUT cpu_fallback: unmeasurable device and no lane →
    # nothing adequately evidenced can satisfy → incomplete, never ready.
    class _NoCpuSpec:
        capabilities: ClassVar[dict] = {}
        files = ()
        family = "sd1.5"
        vram_baseline_bytes = int(4 * GIB)
        vram_weight_bytes = int(2 * GIB)
    g0 = gpu(free=None, total=None, source=None)
    s = snap(gpus=[g0], sched=sched_state([slot()]),
             probed_at=1.0, now=2.0)
    p = preview(spec=_NoCpuSpec(), snapshot=s, residency="gpu")
    assert p["advisory_state"] == "preview_incomplete"


def test_unknown_runtime_is_incomplete():
    p = preview(plan=dep_plan(root_runtime_state="candidate_unknown"))
    assert p["advisory_state"] == "preview_incomplete"


def test_ram_unknown_is_incomplete():
    s = snap(gpus=[gpu(free=6 * GIB)], sched=sched_state([slot()]),
             probed_at=1.0, now=2.0)
    p = preview(snapshot=s, residency="cpu_offload", ram_known=False)
    assert p["advisory_state"] == "preview_incomplete"


def test_device_index_scoping_and_proven_mismatch():
    s = snap(gpus=[gpu(0, vendor="AMD"), gpu(1, vendor="NVIDIA")],
             sched=sched_state([slot(), slot(1)]), probed_at=1.0, now=2.0)
    p = preview(snapshot=s, residency="cpu_offload", device_sel="index",
                selected_index=0)
    assert len(p["device_alternatives"]) == 1
    # AMD on cuda-only runtime decl — proven mismatch → blocked
    cuda_only = preview(
        snapshot=s, residency="cpu_offload", device_sel="index",
        selected_index=0, decl_backends=("cuda",))
    assert cuda_only["advisory_state"] == "preview_blocked"


def test_device_cpu_explicit_lane():
    s = snap(gpus=[gpu()], sched=sched_state([slot()]),
             probed_at=1.0, now=2.0)
    p = preview(spec=SPEC_SD15, snapshot=s, device_sel="cpu",
                width=512, height=512)
    assert p["cpu_lane"]["permitted"] is True
    assert p["cpu_lane"]["outcome"] == "cpu_lane_ok"
    assert p["device_alternatives"] == []      # cpu lane only
    assert p["advisory_state"] == "preview_ready_advisory"


def test_device_cpu_without_fallback_is_blocked():
    class _NoCpu:
        capabilities: ClassVar[dict] = {}
        files = ()
        vram_baseline_bytes = vram_weight_bytes = None
    p = preview(spec=_NoCpu(), snapshot=snap(probed_at=1.0, now=2.0),
                device_sel="cpu")
    assert p["cpu_lane"]["permitted"] is False
    assert p["advisory_state"] == "preview_blocked"


def test_mixed_device_outcomes_ready_via_better_device():
    s = snap(gpus=[gpu(0, free=2 * GIB), gpu(1, name="GPU1", free=6 * GIB)],
             sched=sched_state([slot(), slot(1)]), probed_at=1.0, now=2.0)
    p = preview(snapshot=s, residency="cpu_offload")
    assert p["advisory_state"] == "preview_ready_advisory"
    assert p["device_alternatives"][0]["index"] == 1  # best first


def test_stale_snapshot_is_limitation_not_state_change():
    s = snap(gpus=[gpu(free=6 * GIB)], sched=sched_state([slot()]),
             probed_at=0.0, now=1000.0)
    p = preview(snapshot=s, residency="cpu_offload")
    assert p["advisory_state"] == "preview_ready_advisory"
    assert "snapshot_stale" in p["limitations"]
    assert p["snapshot_stale"] is True


def test_simulated_hardware_flagged():
    s = snap(gpus=[gpu(free=6 * GIB)], sched=sched_state([slot()]),
             probed_at=1.0, now=2.0, simulated=True)
    p = preview(snapshot=s, residency="cpu_offload")
    assert "simulated_hardware" in p["limitations"]


def test_dep_axes_flow_through():
    p = preview(plan=dep_plan(root_runtime_state="candidate_limited"))
    assert p["dependency_state"] == "complete"
    assert p["runtime_state"] == "candidate_limited"


def test_imported_artifact_incomplete_not_ready():
    p = preview(spec=None, weights_declared=int(4.1 * GIB), residency="gpu",
                ram_known=False)
    assert p["advisory_state"] == "preview_incomplete"
    assert p["stage"]["est_vram"]["basis"] == "unknown"
    assert "partial_estimate" in p["limitations"]


def test_imported_weights_alone_can_prove_insufficient():
    s = snap(gpus=[gpu(free=2 * GIB)], sched=sched_state([slot()]),
             probed_at=1.0, now=2.0)
    p = preview(spec=None, weights_declared=int(4.1 * GIB),
                residency="gpu", snapshot=s, ram_known=False)
    assert p["advisory_state"] == "preview_blocked"


def test_wan_offload_alternative_present_sd15_none():
    p = preview(residency="gpu")
    alts = p["stage"]["offload_alternatives"]
    assert [a["residency_mode"] for a in alts] == ["cpu_offload"]
    assert alts[0]["requires_capability"] == "weight_offload_cpu"
    p2 = preview(spec=SPEC_SD15, residency="gpu", width=512, height=512)
    assert p2["stage"]["offload_alternatives"] == []


def test_no_parallel_or_distributed_fields():
    p = preview()
    assert p["stage"]["stage_id"] == "generate"
    assert p["stage"]["kind"] == "generation"
    assert not any(k in p for k in ("parallel_alternative", "stages"))


# ------------------------------------------------- enumeration bound

def test_more_than_eight_gpus_truncated_and_incomplete():
    gpus = [gpu(i, name=f"GPU{i}", free=6 * GIB) for i in range(9)]
    st = sched_state([slot(i) for i in range(9)])
    s = snap(gpus=gpus, sched=st, probed_at=1.0, now=2.0)
    p = preview(snapshot=s, residency="cpu_offload")
    assert p["device_alternatives_truncated"] is True
    assert p["devices_total"] == 9
    assert len(p["device_alternatives"]) == 8
    assert "plan_max_device_alternatives" in p["limits_exceeded"]
    assert p["advisory_state"] == "preview_incomplete"


def test_explicit_index_beyond_window_still_evaluated():
    gpus = [gpu(i, name=f"GPU{i}", free=6 * GIB) for i in range(9)]
    st = sched_state([slot(i) for i in range(9)])
    s = snap(gpus=gpus, sched=st, probed_at=1.0, now=2.0)
    p = preview(snapshot=s, residency="cpu_offload", device_sel="index",
                selected_index=8)
    assert len(p["device_alternatives"]) == 1
    assert p["device_alternatives"][0]["index"] == 8
    assert p["device_alternatives_truncated"] is False
    assert "plan_max_device_alternatives" not in p["limits_exceeded"]


# ------------------------------------------------------- fingerprint

def test_fingerprint_stable_time_only_change():
    a = preview(snapshot=snap(gpus=[gpu(free=6 * GIB)],
                              sched=sched_state([slot()]),
                              probed_at=100.0, now=120.0),
                residency="cpu_offload")
    b = preview(snapshot=snap(gpus=[gpu(free=6 * GIB)],
                              sched=sched_state([slot()]),
                              probed_at=400.0, now=410.0),
                residency="cpu_offload")
    assert a["plan_fingerprint"] == b["plan_fingerprint"]
    assert a["plan_fingerprint"].startswith("plan:v1:")


def test_fingerprint_changes_on_reservation_change():
    a = preview(snapshot=snap(gpus=[gpu(free=6 * GIB)],
                              sched=sched_state([slot()]),
                              probed_at=1.0, now=2.0),
                residency="cpu_offload")
    b = preview(snapshot=snap(gpus=[gpu(free=6 * GIB)],
                              sched=sched_state([slot(reserved=GIB)]),
                              probed_at=1.0, now=2.0),
                residency="cpu_offload")
    assert a["plan_fingerprint"] != b["plan_fingerprint"]


def test_fingerprint_changes_on_request_and_margins():
    base = preview()
    other_w = preview(width=512, height=512)
    other_m = preview(margins={**MARGINS, "vram_headroom_bytes": GIB})
    other_dev = preview(device_sel="index", selected_index=0)
    assert base["plan_fingerprint"] != other_w["plan_fingerprint"]
    assert base["plan_fingerprint"] != other_m["plan_fingerprint"]
    assert base["plan_fingerprint"] != other_dev["plan_fingerprint"]


def test_deterministic_alternative_ordering():
    gpus = [gpu(0, free=2 * GIB), gpu(1, name="B", free=4 * GIB),
            gpu(2, name="C", free=6 * GIB)]
    s = snap(gpus=gpus, sched=sched_state([slot(i) for i in range(3)]),
             probed_at=1.0, now=2.0)
    a = preview(snapshot=s, residency="cpu_offload")
    b = preview(snapshot=s, residency="cpu_offload")
    assert [d["index"] for d in a["device_alternatives"]] == \
        [d["index"] for d in b["device_alternatives"]]
    assert a["plan_fingerprint"] == b["plan_fingerprint"]


# ======================================================= API endpoint

WAN_AID = "catalog:wan21.t2v.1_3b"
SD_AID = "catalog:sd15.fp16"


def _project(client):
    r = client.post("/api/projects", json={"name": "P",
                                           "template": "blank"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _verdict(client, aid, runtime_key="sdcpp", hw=None):
    from app.domain import compat_verdicts
    base = {"arch_axis": "supported", "arch_confidence": "tested",
            "runtime_axis": "operational", "hardware_axis": "fits",
            "policy_axis": "eligible", "level": 4,
            "overall": "compatible", "confidence": "tested",
            "reasons_json": [], "missing_json": [], "warnings_json": [],
            "hardware_limits_json": [], "actions_json": [],
            "blocking_axes_json": [],
            "domain_levels_json": {"inspection": 1, "family": 2,
                                   "runtime_static": 3, "hardware": 4},
            "ruleset_version": "u4:fam1.caps1"}
    with client.app.state.session_factory() as s:
        row = s.get(ModelArtifact, aid)
        base["inputs_fingerprint"] = \
            compat_verdicts.fingerprint_for_runtime(
                s, row, runtime_key, hw, compat_verdicts.gpu_prefs(s))
        s.add(CompatVerdict(artifact_id=aid, runtime_key=runtime_key,
                            **base))
        s.commit()


def _purl(pid, aid, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return (f"/api/projects/{pid}/models/artifacts/{aid}"
            f"/execution-preview?{qs}")


@pytest.fixture()
def sim_gpu(request, client, monkeypatch):
    """Simulated hardware for the endpoint: patch the probe AND swap the
    app's scheduler for one built on the same canned report."""
    gpus = [gpu(0, free=6 * GIB)]
    rep = probe_report(gpus=gpus)
    monkeypatch.setattr("app.domain.hardware.probe", lambda **kw: rep)
    sched = GpuScheduler(lambda: rep, {})
    monkeypatch.setattr(client.app.state, "gpu_scheduler", sched)
    return rep, sched


def test_ep_404s(client, sim_gpu):
    pid = _project(client)
    assert client.get(_purl(pid, "nope:0", runtime_key="sdcpp")
                      ).status_code == 404
    assert client.get(_purl("proj-no", WAN_AID, runtime_key="sdcpp")
                      ).status_code == 404


def test_ep_400s_and_422s(client, sim_gpu):
    pid = _project(client)
    assert client.get(_purl(pid, WAN_AID)).status_code == 400
    assert client.get(_purl(pid, WAN_AID, runtime_key="nope")
                      ).status_code == 400
    assert client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                            workflow="nope")).status_code == 400
    assert client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                            residency_mode="bogus")).status_code == 400
    assert client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                            width=9000)).status_code == 400
    assert client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                            batch=99)).status_code == 400
    assert client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                            device="bogus")).status_code == 400
    assert client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                            device="7")).status_code == 422
    # te_cpu has no declared capability anywhere today → 422
    assert client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                            residency_mode="te_cpu")).status_code == 422
    # cpu_offload on sd15 — capability not declared → 422
    assert client.get(_purl(pid, SD_AID, runtime_key="sdcpp",
                            residency_mode="cpu_offload")
                      ).status_code == 422


def test_ep_wan_gpu_default_and_offload(client, sim_gpu):
    pid = _project(client)
    rep, _ = sim_gpu
    _verdict(client, WAN_AID, hw=rep)
    r = client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                         workflow="text2video", width=832, height=480))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["advisory"] is True
    assert b["stage"]["residency_mode"] == "gpu"
    assert b["stage"]["residency_basis"] == "default"
    # gpu residency: weights+act ≈ 9.4 GiB → device alternative
    # insufficient on 6 GiB; the DECLARED cpu_fallback lane satisfies —
    # an explicit honest alternative, never a silent fallback.
    assert b["device_alternatives"][0]["fit_class"] == "insufficient"
    assert b["cpu_lane"]["outcome"] == "cpu_lane_ok"
    assert b["advisory_state"] == "preview_ready_advisory"
    off = [a["residency_mode"]
           for a in b["stage"]["offload_alternatives"]]
    assert off == ["cpu_offload"]                  # declared alt shown
    # explicit offload path fits
    r2 = client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                          workflow="text2video", width=832, height=480,
                          residency_mode="cpu_offload"))
    b2 = r2.json()
    assert b2["advisory_state"] == "preview_ready_advisory"
    assert b2["device_alternatives"][0]["fit_class"] \
        == "fits_with_headroom"
    assert b2["stage"]["residency_basis"] == "explicit"


def test_ep_sd15_cpu_lane(client, sim_gpu):
    pid = _project(client)
    rep, _ = sim_gpu
    _verdict(client, SD_AID, hw=rep)
    r = client.get(_purl(pid, SD_AID, runtime_key="sdcpp",
                         workflow="text2image", device="cpu"))
    b = r.json()
    assert b["cpu_lane"]["permitted"] is True
    assert b["cpu_lane"]["est_ram_bytes"] == int(0.3 * SD15_FILES
                                               + 2 * GIB)
    assert b["device_alternatives"] == []


def test_ep_device_index_evaluation(client, sim_gpu):
    pid = _project(client)
    rep, _ = sim_gpu
    _verdict(client, WAN_AID, hw=rep)
    r = client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                         workflow="text2video", device="0",
                         residency_mode="cpu_offload"))
    b = r.json()
    assert len(b["device_alternatives"]) == 1
    assert b["device_alternatives"][0]["index"] == 0


def test_ep_reservations_reduce_budget(client, sim_gpu):
    pid = _project(client)
    rep, sched = sim_gpu
    _verdict(client, WAN_AID, hw=rep)
    r1 = client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                          workflow="text2video",
                          residency_mode="cpu_offload"))
    sched._reserved[0] = {"job-x": 4 * GIB}        # fixture-side claim
    r2 = client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                          workflow="text2video",
                          residency_mode="cpu_offload"))
    b1, b2 = r1.json(), r2.json()
    assert b2["snapshot"]["devices"][0]["reserved_bytes"] == 4 * GIB
    assert b2["snapshot"]["devices"][0][
        "conservative_planning_budget_bytes"] == 2 * GIB
    assert b2["snapshot"]["devices"][0][
        "reservation_overlap_possible"] is True
    assert b1["plan_fingerprint"] != b2["plan_fingerprint"]
    sched._reserved.clear()


def test_ep_snapshot_labels_and_freshness(client, sim_gpu):
    pid = _project(client)
    rep, _ = sim_gpu
    _verdict(client, WAN_AID, hw=rep)
    b = client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                         residency_mode="cpu_offload")).json()
    dev = b["snapshot"]["devices"][0]
    assert dev["vram_bytes"] == 8 * GIB            # physical total
    assert dev["vram_free_bytes"] == 6 * GIB       # driver measured
    assert dev["usable_bytes"] == 6 * GIB          # derived
    assert dev["index_stability"] == "per_boot"
    assert b["snapshot"]["snapshot_id"].startswith("hw:v1:")
    assert b["snapshot"]["captured_at"] is not None


def test_ep_backend_evidence_labels(client, sim_gpu):
    pid = _project(client)
    rep, _ = sim_gpu
    _verdict(client, WAN_AID, hw=rep)
    b = client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                         residency_mode="cpu_offload")).json()
    ev = b["device_alternatives"][0]["backend_evidence"]
    assert ev["cuda"] == "measured"                # smi-matched NVIDIA
    assert ev["vulkan"] == "inferred"              # machine-level ICD


def test_ep_comfyui_planned_blocked(client, sim_gpu):
    pid = _project(client)
    r = client.get(_purl(pid, WAN_AID, runtime_key="comfyui",
                         workflow="text2video"))
    b = r.json()
    assert b["runtime_state"] == "candidate_blocked"
    assert b["advisory_state"] == "preview_blocked"


def test_ep_no_acquire_no_jobs_no_writes(client, sim_gpu):
    pid = _project(client)
    rep, sched = sim_gpu
    _verdict(client, WAN_AID, hw=rep)
    client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                     residency_mode="cpu_offload"))
    client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                     residency_mode="cpu_offload"))
    assert sched.reservations() == {}              # nothing claimed
    with client.app.state.session_factory() as s:
        assert s.execute(select(GenerationJob)).scalars().all() == []
        assert len(s.execute(select(CompatVerdict)).scalars().all()) == 1


def test_ep_no_paths_in_response(client, sim_gpu):
    import json
    pid = _project(client)
    rep, _ = sim_gpu
    _verdict(client, WAN_AID, hw=rep)
    b = client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                         residency_mode="cpu_offload"))
    body = json.dumps(b.json())
    for token in ("install_root", "source_uri", "C:\\", "models_root"):
        assert token not in body


def test_ep_read_only_probe_never_forced(client, monkeypatch):
    """The endpoint must consume the standard cached probe — probe()
    is never invoked with force=True."""
    calls = []
    rep = probe_report(gpus=[gpu()])
    monkeypatch.setattr("app.domain.hardware.probe",
                        lambda **kw: (calls.append(kw), rep)[1])
    monkeypatch.setattr(client.app.state, "gpu_scheduler",
                        GpuScheduler(lambda: rep, {}))
    pid = _project(client)
    _verdict(client, WAN_AID, hw=rep)
    client.get(_purl(pid, WAN_AID, runtime_key="sdcpp"))
    assert all(kw.get("force") is not True for kw in calls)


def test_ep_fingerprint_repeatable(client, sim_gpu):
    pid = _project(client)
    rep, _ = sim_gpu
    _verdict(client, WAN_AID, hw=rep)
    a = client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                         residency_mode="cpu_offload")).json()
    b = client.get(_purl(pid, WAN_AID, runtime_key="sdcpp",
                         residency_mode="cpu_offload")).json()
    assert a["plan_fingerprint"] == b["plan_fingerprint"]
    assert a["snapshot"]["snapshot_id"] == b["snapshot"]["snapshot_id"]
