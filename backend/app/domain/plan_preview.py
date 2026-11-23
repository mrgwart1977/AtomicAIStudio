"""U-5c-1 — single-stage hardware-aware execution-plan preview (pure).

One genuine generation stage per request (Gate 5): the stage is derived
from a U-5b DepPlan's fills and evaluated against each eligible GPU
independently. There is no distributed execution, GPU pooling, sharding,
parallel-stage or fabricated workflow-step planning — and this module
never calls GpuScheduler.acquire(), hardware.probe(), or the database;
every input arrives as an injected, read-only view.

Memory accounting (Gate 3 / F1) is layered, each margin applied once:
  usable_after_headroom / conservative_planning_budget — hw_snapshot
  compatibility._HEADROOM_BYTES (0.7 GiB stability margin) — applied in
      the fit classification here, never inside the budget
"""

from __future__ import annotations

import hashlib
import json

from app.domain import compatibility, dep_resolve, hw_snapshot
from app.domain.lora_resolve import LORA_VRAM_OVERHEAD_BYTES

PLAN_RULESET_VERSION = 1
MAX_DEVICE_ALTERNATIVES = 8
PLAN_MAX_DEVICE_ALTERNATIVES = "plan_max_device_alternatives"

RESIDENCY_MODES = ("gpu", "cpu_offload", "te_cpu", "disk")
# Execution-adapter residency contracts: only runtimes that actually map
# a residency_mode request onto runtime flags may evaluate that mode.
# sdcpp maps all four (inference.py --offload-to-cpu / te=cpu /
# --params-backend disk); every other declared runtime: gpu only.
_RUNTIME_RESIDENCY_CONTRACT = {"sdcpp": RESIDENCY_MODES}
# Catalog capability that must be declared before a non-gpu mode can be
# evaluated — capability alone is never evidence the runtime supports it.
_RESIDENCY_CAPABILITY = {"cpu_offload": "weight_offload_cpu",
                         "te_cpu": "te_offload_cpu",
                         "disk": "disk_streaming"}

# Outcome classes (also used as fit_class on device alternatives).
FIT_WITH_HEADROOM = "fits_with_headroom"
FIT_TIGHT = "fits_tight"
FIT_INSUFFICIENT = "insufficient"
FIT_UNMEASURABLE = "unmeasurable"
FIT_ESTIMATE_UNKNOWN = "estimate_unknown"
FIT_INSUFFICIENT_EVIDENCE = "insufficient_evidence"
FIT_BACKEND_MISMATCH = "device_backend_mismatch"
FIT_BACKEND_EVIDENCE_UNKNOWN = "backend_evidence_unknown"
FIT_NOT_PRESENT = "device_not_present"

# Proven-negative outcomes — a device in this class CANNOT serve the
# stage on existing evidence (not merely unknown).
_PROVEN_FAIL = {FIT_INSUFFICIENT, FIT_BACKEND_MISMATCH, FIT_NOT_PRESENT}
# Outcomes that satisfy the stage.
_SATISFIES = {FIT_WITH_HEADROOM, FIT_TIGHT}

ADVISORY_READY = "preview_ready_advisory"
ADVISORY_INCOMPLETE = "preview_incomplete"
ADVISORY_BLOCKED = "preview_blocked"

_GIB = 1024 ** 3
# Host-side runtime overhead folded into the RAM estimate — matches the
# jobs/classify_model convention (est_ram = resident weights + 2 GiB).
_HOST_OVERHEAD_BYTES = 2 * _GIB

_FIT_ORDER = {
    FIT_WITH_HEADROOM: 0, FIT_TIGHT: 1, FIT_INSUFFICIENT: 2,
    FIT_UNMEASURABLE: 3, FIT_ESTIMATE_UNKNOWN: 4,
    FIT_INSUFFICIENT_EVIDENCE: 4, FIT_BACKEND_EVIDENCE_UNKNOWN: 4,
    FIT_BACKEND_MISMATCH: 5, FIT_NOT_PRESENT: 6,
}

_LORA_ROLES = ("lora", "lora_identity", "lora_style")


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def residency_modes_for_runtime(runtime_key: str) -> tuple:
    return _RUNTIME_RESIDENCY_CONTRACT.get(runtime_key, ("gpu",))


def residency_evidence(runtime_key: str, spec, mode: str) -> tuple[bool, str]:
    """(permitted, reason) — Gate 6 three-layer check: the mode must be
    inside the runtime's execution contract AND declared by the catalog
    (or be the default 'gpu'). Capability alone never selects a mode —
    residency is a request parameter, never inferred."""
    if mode not in RESIDENCY_MODES:
        return False, f"residency_mode_unknown:{mode}"
    if mode not in residency_modes_for_runtime(runtime_key):
        return False, (f"runtime '{runtime_key}' has no execution "
                       f"contract for residency '{mode}'")
    if mode == "gpu":
        return True, ""
    cap_key = _RESIDENCY_CAPABILITY[mode]
    if spec is None:
        return False, ("imported artifact — no catalog capability "
                       f"declares '{cap_key}'")
    if not (spec.capabilities or {}).get(cap_key):
        return False, (f"catalog capability '{cap_key}' is not declared "
                       "for this model")
    return True, ""


def _weights_by_role(spec) -> dict:
    by: dict[str, int] = {}
    for f in spec.files:
        by[f.role or "diffusion"] = by.get(f.role or "diffusion", 0) + f.size_bytes
    return by


def stage_estimates(spec, weights_declared: int | None, width: int,
                    height: int, batch: int, residency: str,
                    lora_apps: int) -> dict:
    """Canonical estimates (Gate 6 / R4).

    VRAM — compatibility.estimate_vram is the ONE authoritative path for
    catalog models; manifest member sizes are residency DISPLAY evidence
    only, never added a second time. LoRA admission overhead is applied
    exactly once (LORA_VRAM_OVERHEAD_BYTES per application — the same
    constant commands.py uses). Imported artifacts have no activation
    anchor: weights_declared is a declared floor and the estimate stays
    unknown — it cannot become a fit.
    """
    if spec is None:
        est_ram = (weights_declared + _HOST_OVERHEAD_BYTES
                   if weights_declared else None)
        return {"est_vram_bytes": None, "vram_basis": "unknown",
                "est_ram_bytes": est_ram, "ram_basis": "partial",
                "runtime_overhead_basis": "unknown",
                "weights_declared_bytes": weights_declared}
    est_v = compatibility.estimate_vram(spec, width, height, batch, residency)
    if est_v is not None:
        est_v += LORA_VRAM_OVERHEAD_BYTES * lora_apps
    by = _weights_by_role(spec)
    weights_total = sum(by.values()) or (spec.vram_weight_bytes or 0)
    te = by.get("text_encoder", 0)
    if residency in ("cpu_offload", "disk"):
        est_ram = weights_total + _HOST_OVERHEAD_BYTES
    elif residency == "te_cpu":
        est_ram = te + int(0.3 * (weights_total - te)) + _HOST_OVERHEAD_BYTES
    else:
        est_ram = int(0.3 * weights_total) + _HOST_OVERHEAD_BYTES
    return {"est_vram_bytes": est_v, "vram_basis": "heuristic",
            "est_ram_bytes": est_ram, "ram_basis": "heuristic",
            "runtime_overhead_basis": "included_in_anchor",
            "weights_declared_bytes": weights_total}


def evaluate_device(device: dict, machine_backends: dict,
                    decl_backends: tuple, est_vram: int | None,
                    weights_declared: int | None) -> dict:
    """One device alternative — independent evidence layers evaluated,
    never blended (Gate 4 / F2)."""
    reasons: list[str] = []
    out = {"index": device.get("index"), "name": device.get("name"),
           "vendor": device.get("vendor"), "eligible": False,
           "backend_evidence": {},
           "est_vram_bytes": est_vram,
           "budget_bytes": device.get("conservative_planning_budget_bytes"),
           "reservation_overlap_possible":
               device.get("reservation_overlap_possible", False),
           "reasons": reasons, "limitations": []}
    if not device.get("present", True):
        out["outcome"] = out["fit_class"] = FIT_NOT_PRESENT
        reasons.append("device is not present in this snapshot "
                       "(lost while holding reservations)")
        return out

    be: dict[str, str] = {}
    for b in decl_backends or ():
        if b == "cpu":
            continue            # cpu is a lane, not a device backend
        grade, reason = hw_snapshot.device_backend_evidence(
            device, machine_backends, b)
        be[b] = grade
        if grade in (hw_snapshot.EVIDENCE_MISMATCH,
                     hw_snapshot.EVIDENCE_UNKNOWN):
            reasons.append(reason)
    out["backend_evidence"] = be
    if not decl_backends:
        reasons.append("runtime declares no device backends")
    eligible = any(g in (hw_snapshot.EVIDENCE_MEASURED,
                         hw_snapshot.EVIDENCE_INFERRED) for g in be.values())
    out["eligible"] = eligible
    if not eligible:
        out["outcome"] = out["fit_class"] = (
            FIT_BACKEND_MISMATCH
            if be and all(g == hw_snapshot.EVIDENCE_MISMATCH
                          for g in be.values())
            else FIT_BACKEND_EVIDENCE_UNKNOWN)
        if not reasons:
            reasons.append("no runtime-declared backend can be evidenced "
                           "on this device")
        return out
    if any(g == hw_snapshot.EVIDENCE_INFERRED for g in be.values()):
        out["limitations"].append("device_backend_inferred")
    if all(g == hw_snapshot.EVIDENCE_INFERRED for g in be.values()):
        out["limitations"].append("no_measured_device_backend")

    cpb = device.get("conservative_planning_budget_bytes")
    if est_vram is None:
        if weights_declared is not None and cpb is not None \
                and weights_declared > cpb:
            out["outcome"] = out["fit_class"] = FIT_INSUFFICIENT
            reasons.append(
                f"declared weights alone (~{weights_declared / _GIB:.2f} GiB) "
                f"exceed planning budget (~{cpb / _GIB:.2f} GiB)")
        else:
            out["outcome"] = out["fit_class"] = (
                FIT_UNMEASURABLE if cpb is None else FIT_INSUFFICIENT_EVIDENCE)
            reasons.append("no activation/runtime anchor — the VRAM "
                           "estimate is unknown and cannot become a fit")
        return out
    if cpb is None:
        out["outcome"] = out["fit_class"] = FIT_UNMEASURABLE
        reasons.append("device memory could not be measured")
        return out
    if est_vram > cpb:
        out["outcome"] = out["fit_class"] = FIT_INSUFFICIENT
        reasons.append(
            f"estimate ~{est_vram / _GIB:.2f} GiB exceeds planning "
            f"budget ~{cpb / _GIB:.2f} GiB")
    elif est_vram + compatibility._HEADROOM_BYTES <= cpb:
        out["outcome"] = out["fit_class"] = FIT_WITH_HEADROOM
    else:
        out["outcome"] = out["fit_class"] = FIT_TIGHT
        out["limitations"].append("fits_tight")
    return out


def cpu_lane(*, declared_cap: bool, cpu_backend_declared: bool,
             explicitly_requested: bool, est_ram: int | None,
             ram_known: bool, preflight) -> dict:
    """The CPU lane is an explicit alternative — never a silent fallback.
    `preflight` is the injected resources.preflight_memory_check."""
    permitted = bool(declared_cap and cpu_backend_declared)
    reasons: list[str] = []
    if not permitted:
        if not cpu_backend_declared:
            reasons.append("runtime does not declare a cpu backend")
        if not declared_cap:
            reasons.append("model does not declare cpu_fallback — "
                           "CPU execution is not a supported path")
        return {"permitted": False, "outcome": "cpu_fallback_unsupported",
                "est_ram_bytes": None, "ram_decision": None,
                "reasons": reasons, "limitations": [],
                "explicitly_requested": explicitly_requested}
    gate = preflight(est_ram or 0, ram_known)
    decision = gate.get("decision")
    if gate.get("reason"):
        reasons.append(gate["reason"])
    limitations = []
    if decision == "warn":
        limitations.append("ram_warn")
    if not ram_known:
        limitations.append("ram_estimate_unknown")
    return {"permitted": True,
            "outcome": ("cpu_lane_ok" if decision in ("ok", "warn")
                        else "cpu_lane_blocked"),
            "est_ram_bytes": est_ram, "ram_decision": decision,
            "ram_decision_reason": gate.get("reason") or "",
            "reasons": reasons, "limitations": limitations,
            "explicitly_requested": explicitly_requested}


def _fingerprint(plan, snapshot_id, alternatives, cpu, axes, request,
                 margins, limits_exceeded) -> str:
    fp = {
        "ruleset": [PLAN_RULESET_VERSION, plan.resolution_version],
        "dep": [plan.artifact_id, plan.workflow_key, plan.advisory_state],
        "request": request,
        "snapshot": snapshot_id,
        "axes": axes,
        "alternatives": [[a["index"], a["outcome"], a["budget_bytes"],
                          a["est_vram_bytes"],
                          sorted(a["backend_evidence"].items())]
                         for a in alternatives],
        "cpu_lane": [cpu["permitted"], cpu["outcome"], cpu["est_ram_bytes"],
                     cpu["ram_decision"]] if cpu else None,
        "margins": {k: int(v) for k, v in sorted((margins or {}).items())
                    if isinstance(v, int)},
        "limits": list(limits_exceeded),
    }
    return "plan:v1:" + hashlib.sha256(_canon(fp).encode()).hexdigest()[:16]


def build_preview(*, plan: dep_resolve.DepPlan, spec, runtime_key: str,
                  runtime_decl_backends: tuple, runtime_planned: bool,
                  snapshot: dict, components: list, lora_apps: int,
                  weights_declared: int | None, residency: str,
                  residency_basis: str, device_sel: str,
                  selected_index: int | None, width: int, height: int,
                  batch: int, workflow_key: str, include_stale: bool,
                  preflight, ram_known: bool,
                  margins: dict) -> dict:
    """Aggregate the preview — deterministic, advisory only (Gate 7/F3).

    preflight — injected callable (est_ram, known) -> decision dict.
    """
    limits_exceeded = list(plan.limits_exceeded)
    reasons = list(plan.reasons)
    limitations = list(plan.limitations)

    permitted, res_reason = residency_evidence(runtime_key, spec, residency)
    residency_error = None if permitted else res_reason

    est = stage_estimates(spec, weights_declared, width, height, batch,
                          residency, lora_apps)

    # ---- device alternatives ----------------------------------------
    devices = snapshot.get("devices") or []
    lost = snapshot.get("lost_devices") or []
    alternatives: list[dict] = []
    truncated = False
    if device_sel == "cpu":
        pass                                       # lane evaluated below
    elif device_sel == "index":
        dev = next((d for d in devices
                    if d["index"] == selected_index), None)
        if dev is None:
            dev = next((l for l in lost
                        if l["index"] == selected_index), None)
            if dev is None:
                dev = {"index": selected_index, "present": False}
            else:
                dev = {**dev, "present": False}
        alternatives.append(evaluate_device(
            dev, snapshot.get("backends") or {}, runtime_decl_backends,
            est["est_vram_bytes"], est["weights_declared_bytes"]))
    else:                                          # auto: bounded window
        total = len(devices)
        if total > MAX_DEVICE_ALTERNATIVES:
            truncated = True
            limits_exceeded.append(PLAN_MAX_DEVICE_ALTERNATIVES)
        for dev in devices[:MAX_DEVICE_ALTERNATIVES]:
            alternatives.append(evaluate_device(
                dev, snapshot.get("backends") or {}, runtime_decl_backends,
                est["est_vram_bytes"], est["weights_declared_bytes"]))
        # present devices beyond the window are honestly reported
        for dev in devices[MAX_DEVICE_ALTERNATIVES:]:
            reasons.append(f"gpu{dev['index']} not evaluated — "
                           f"{PLAN_MAX_DEVICE_ALTERNATIVES}")

    alternatives.sort(key=lambda a: (_FIT_ORDER.get(a["fit_class"], 9),
                                     a["index"] if a["index"] is not None else 9))

    # ---- cpu lane ----------------------------------------------------
    cpu_requested = device_sel == "cpu"
    cpu_declared = bool(spec and (spec.capabilities or {}).get("cpu_fallback"))
    lane = None
    if device_sel == "cpu" or device_sel == "auto":
        lane = cpu_lane(
            declared_cap=cpu_declared,
            cpu_backend_declared="cpu" in (runtime_decl_backends or ()),
            explicitly_requested=cpu_requested,
            est_ram=est["est_ram_bytes"], ram_known=ram_known,
            preflight=preflight)

    # Every evaluated alternative's outcome — eligible device fits,
    # proven ineligibility, or the cpu lane when it is permitted or was
    # explicitly requested.
    device_outcomes = [a for a in alternatives if a["eligible"]]
    _LANE_FAIL = {"cpu_lane_blocked", "cpu_fallback_unsupported"}
    evaluated: list[str] = [a["fit_class"] for a in alternatives]
    if lane is not None and (lane["permitted"]
                             or lane["explicitly_requested"]):
        evaluated.append(lane["outcome"])
    satisfies = any(o in _SATISFIES or o == "cpu_lane_ok"
                    for o in evaluated)
    all_proven_fail = bool(evaluated) and all(
        o in _PROVEN_FAIL or o in _LANE_FAIL for o in evaluated)
    any_unknown = any(
        o not in _PROVEN_FAIL | _SATISFIES | _LANE_FAIL
        and o != "cpu_lane_ok" for o in evaluated)

    # ---- axis: hardware_state ---------------------------------------
    if not device_outcomes and lane is None:
        hardware_state = "not_evaluated"
    elif any(a["fit_class"] == FIT_WITH_HEADROOM for a in device_outcomes) \
            or (lane is not None and lane["outcome"] == "cpu_lane_ok"
                and not device_outcomes):
        hardware_state = "fits"
    elif any(a["fit_class"] == FIT_TIGHT for a in device_outcomes):
        hardware_state = "fits_tight"
    elif device_outcomes and all(
            a["fit_class"] in _PROVEN_FAIL for a in device_outcomes):
        hardware_state = "insufficient"
    elif not device_outcomes:
        hardware_state = "no_eligible_devices"
    else:
        hardware_state = "unmeasurable"

    ram_decision = lane["ram_decision"] if lane else None
    ram_state = ram_decision or ("unknown" if not ram_known else "not_evaluated")

    # ---- aggregation (F3 — authoritative precedence, advisory only) ---
    if runtime_planned:
        advisory, block_reason = ADVISORY_BLOCKED, "runtime_planned"
    elif plan.policy_state != dep_resolve.POLICY_CLEAR:
        advisory = ADVISORY_BLOCKED
        block_reason = f"policy_{plan.policy_state}"
    elif plan.runtime_state == "candidate_blocked":
        advisory, block_reason = ADVISORY_BLOCKED, "runtime_blocked"
    elif plan.structural_state == dep_resolve.STRUCT_CONFLICTED:
        advisory, block_reason = ADVISORY_BLOCKED, "dependency_conflicted"
    elif plan.capacity_state == dep_resolve.CAP_EXCEEDS:
        advisory, block_reason = ADVISORY_BLOCKED, "lora_capacity_exceeded"
    elif ram_decision == "block":
        advisory, block_reason = ADVISORY_BLOCKED, "ram_insufficient"
    elif residency_error:
        advisory, block_reason = ADVISORY_BLOCKED, "residency_not_supported"
    elif all_proven_fail:
        advisory, block_reason = ADVISORY_BLOCKED, "hardware_insufficient"
    elif not evaluated:
        advisory, block_reason = ADVISORY_BLOCKED, "no_permitted_alternative"
    else:
        block_reason = None
        advisory = None

    if advisory is None:
        if limits_exceeded:
            advisory = ADVISORY_INCOMPLETE
            block_reason = "limits_exceeded"
        elif plan.structural_state in (dep_resolve.STRUCT_INCOMPLETE_MISSING,
                                       dep_resolve.STRUCT_AMBIGUOUS,
                                       dep_resolve.STRUCT_UNKNOWN):
            advisory, block_reason = ADVISORY_INCOMPLETE, "dependency_incomplete"
        elif plan.runtime_state in ("candidate_unknown", "unevaluated"):
            advisory, block_reason = ADVISORY_INCOMPLETE, "runtime_unknown"
        elif plan.capacity_state == dep_resolve.CAP_UNKNOWN and lora_apps:
            advisory, block_reason = ADVISORY_INCOMPLETE, "capacity_unknown"
        elif plan.evidence_state == dep_resolve.EV_MISSING:
            advisory, block_reason = ADVISORY_INCOMPLETE, "evidence_missing"
        elif not ram_known:
            advisory, block_reason = ADVISORY_INCOMPLETE, "ram_unknown"
        elif not satisfies:
            advisory = ADVISORY_INCOMPLETE
            block_reason = ("insufficient_evidence" if any_unknown
                            else "no_permitted_alternative")
        else:
            advisory = ADVISORY_READY
    if block_reason and block_reason not in reasons:
        reasons.append(block_reason)

    # ---- limitations -------------------------------------------------
    if snapshot.get("stale"):
        limitations.append("snapshot_stale")
    if snapshot.get("simulated"):
        limitations.append("simulated_hardware")
    if not snapshot.get("reservations_captured", True):
        limitations.append("scheduler_reservations_unavailable")
    if any(a.get("reservation_overlap_possible") for a in alternatives):
        limitations.append("reservation_overlap_possible")
    if hardware_state == "fits_tight":
        limitations.append("fits_tight")
    if ram_decision == "warn":
        limitations.append("ram_warn")
    if not ram_known:
        limitations.append("ram_estimate_unknown")
    if any("device_backend_inferred" in a["limitations"]
           for a in alternatives):
        limitations.append("device_backend_inferred")
    if est["vram_basis"] in ("partial", "unknown") \
            or est["ram_basis"] == "partial":
        limitations.append("partial_estimate")
    for a in alternatives:
        limitations.extend(a["limitations"])
    if lane is not None:
        limitations.extend(lane["limitations"])
    limitations = list(dict.fromkeys(limitations))

    # ---- stage view ---------------------------------------------------
    residency_notes = []
    if spec is not None and (spec.capabilities or {}).get("vae_on_cpu"):
        residency_notes.append("vae_expected_on_cpu")
    stage = {
        "stage_id": "generate",
        "kind": "generation",
        "workflow_key": workflow_key,
        "runtime_key": runtime_key,
        "residency_mode": residency,
        "residency_basis": residency_basis,
        "residency_notes": residency_notes,
        "components": components,
        "lora_applications": lora_apps,
        "est_vram": {"bytes": est["est_vram_bytes"],
                     "basis": est["vram_basis"],
                     "weights_declared_bytes": est["weights_declared_bytes"]},
        "est_ram": {"bytes": est["est_ram_bytes"], "basis": est["ram_basis"]},
        "runtime_overhead_basis": est["runtime_overhead_basis"],
        "offload_alternatives": [
            {"residency_mode": m,
             "est_vram_bytes": compatibility.estimate_vram(
                 spec, width, height, batch, m),
             "basis": "heuristic",
             "requires_capability": _RESIDENCY_CAPABILITY.get(m)}
            for m in residency_modes_for_runtime(runtime_key)
            if m != residency and spec is not None
            and residency_evidence(runtime_key, spec, m)[0]],
    }

    axes = {
        "dependency_state": plan.structural_state,
        "runtime_state": plan.runtime_state,
        "policy_state": plan.policy_state,
        "capacity_state": plan.capacity_state,
        "hardware_state": hardware_state,
        "ram_state": ram_state,
        "evidence_state": plan.evidence_state,
    }
    request = [workflow_key, runtime_key, width, height, batch,
               device_sel, selected_index, residency, include_stale]
    fingerprint = _fingerprint(plan, snapshot["snapshot_id"], alternatives,
                               lane, axes, request, margins, limits_exceeded)

    return {
        "advisory": True,
        "advisory_state": advisory,
        "plan_fingerprint": fingerprint,
        "artifact_id": plan.artifact_id,
        "workflow_key": workflow_key,
        "runtime_key": runtime_key,
        "dependency_plan_ref": plan.resolution_version,
        "snapshot": snapshot,
        "snapshot_stale": bool(snapshot.get("stale")),
        "stage": stage,
        "device_alternatives": alternatives,
        "devices_total": snapshot.get("devices_total", len(devices)),
        "device_alternatives_truncated": truncated,
        "cpu_lane": lane,
        **axes,
        "limitations": limitations,
        "reasons": reasons,
        "limits_exceeded": sorted(set(limits_exceeded)),
        "limits": {"max_device_alternatives": MAX_DEVICE_ALTERNATIVES,
                   **{f"dep_{k}": v for k, v in plan.limits.items()}},
    }


__all__ = [
    "ADVISORY_BLOCKED",
    "ADVISORY_INCOMPLETE",
    "ADVISORY_READY",
    "FIT_BACKEND_EVIDENCE_UNKNOWN",
    "FIT_BACKEND_MISMATCH",
    "FIT_ESTIMATE_UNKNOWN",
    "FIT_INSUFFICIENT",
    "FIT_INSUFFICIENT_EVIDENCE",
    "FIT_NOT_PRESENT",
    "FIT_TIGHT",
    "FIT_UNMEASURABLE",
    "FIT_WITH_HEADROOM",
    "MAX_DEVICE_ALTERNATIVES",
    "PLAN_MAX_DEVICE_ALTERNATIVES",
    "PLAN_RULESET_VERSION",
    "RESIDENCY_MODES",
    "build_preview",
    "cpu_lane",
    "evaluate_device",
    "residency_evidence",
    "residency_modes_for_runtime",
    "stage_estimates",
]
