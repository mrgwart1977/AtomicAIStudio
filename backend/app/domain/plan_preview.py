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

PLAN_RULESET_VERSION = 2   # plan:v2 — canon adds evidence-classification inputs
PLAN_RESULT_VERSION = 1    # planresult:v1 — advisory result identity
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
FIT_PRESENCE_UNVERIFIED = "presence_unverified"

# CPU capability evidence classes (Gate F2) — the ladder is strictly
# ordered; only an operational class could satisfy, and none exists
# under the current contract.
CPU_UNSUPPORTED = "cpu_unsupported"
CPU_DECLARED = "cpu_declared"
CPU_ADAPTER_CONTRACT = "cpu_adapter_contract"
CPU_COMMAND_CONSISTENT = "cpu_command_consistent"
CPU_OPERATIONAL = "cpu_operational"
_CPU_STATIC_SUPPORTED = (CPU_ADAPTER_CONTRACT, CPU_COMMAND_CONSISTENT,
                         CPU_OPERATIONAL)

# Proven-negative outcomes — a device in this class CANNOT serve the
# stage on existing evidence (not merely unknown).
_PROVEN_FAIL = {FIT_INSUFFICIENT, FIT_BACKEND_MISMATCH, FIT_NOT_PRESENT}
# Outcomes that satisfy the stage.
_SATISFIES = {FIT_WITH_HEADROOM, FIT_TIGHT}

ADVISORY_READY = "preview_ready_advisory"
ADVISORY_INCOMPLETE = "preview_incomplete"
ADVISORY_BLOCKED = "preview_blocked"

CPU_LANE_OK = "cpu_lane_ok"
CPU_LANE_BLOCKED = "cpu_lane_blocked"
CPU_LANE_UNVERIFIED = "cpu_lane_unverified"
CPU_LANE_UNSUPPORTED = "cpu_fallback_unsupported"

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


def cpu_evidence_class(spec, decl_backends, adapter_cpu: bool | None,
                       command_cpu: bool | None) -> str:
    """Classify CPU-execution evidence strictly from static contracts.

    catalog cpu_fallback + runtime cpu backend          -> cpu_declared
    + adapter profile supports.cpu_fallback             -> cpu_adapter_contract
    + command payload allow_cpu for this command type   -> cpu_command_consistent
    (cpu_operational would require verified execution evidence —
    none exists under the current contract, so it is never produced.)
    """
    if not decl_backends or "cpu" not in decl_backends:
        return CPU_UNSUPPORTED
    if not (spec and (spec.capabilities or {}).get("cpu_fallback")):
        return CPU_UNSUPPORTED
    if adapter_cpu is not True:
        return CPU_DECLARED
    if command_cpu is not True:
        return CPU_ADAPTER_CONTRACT
    return CPU_COMMAND_CONSISTENT


def stage_estimates(spec, weights_declared: int | None, width: int,
                    height: int, batch: int, residency: str,
                    lora_apps: int, cpu_only: bool = False) -> dict:
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
    if cpu_only:
        # CPU-only execution: ALL weights and activations live in host
        # RAM — distinct from the GPU staging formula. The activation
        # component reuses the same catalog anchor as estimate_vram's
        # "gpu" residency; an uncomputable activation stays disclosed,
        # never fabricated.
        est_v_gpu = compatibility.estimate_vram(
            spec, width, height, batch, "gpu")
        act_est = (max(0, est_v_gpu - weights_total)
                   if est_v_gpu is not None else None)
        est_ram = weights_total + (act_est or 0) + _HOST_OVERHEAD_BYTES
        ram_basis = "heuristic" if act_est is not None else "partial"
    elif residency in ("cpu_offload", "disk"):
        est_ram = weights_total + _HOST_OVERHEAD_BYTES
        ram_basis = "heuristic"
    elif residency == "te_cpu":
        est_ram = te + int(0.3 * (weights_total - te)) + _HOST_OVERHEAD_BYTES
        ram_basis = "heuristic"
    else:
        est_ram = int(0.3 * weights_total) + _HOST_OVERHEAD_BYTES
        ram_basis = "heuristic"
    return {"est_vram_bytes": est_v, "vram_basis": "heuristic",
            "est_ram_bytes": est_ram, "ram_basis": ram_basis,
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
           "weights_declared_bytes": weights_declared,
           "budget_bytes": device.get("conservative_planning_budget_bytes"),
           "reservation_overlap_possible":
               device.get("reservation_overlap_possible", False),
           "reasons": reasons, "limitations": []}
    if not device.get("present", True):
        if device.get("present") == "unverified":
            out["outcome"] = out["fit_class"] = FIT_PRESENCE_UNVERIFIED
            reasons.append("device presence cannot be verified — "
                           "hardware inventory is unavailable, stale "
                           "or simulated")
            return out
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
            # Imported declared file bytes are NOT proven GPU-resident
            # weight bytes — dtype/quantization transforms and partial
            # loading can shrink residency below file size, and no
            # current runtime contract establishes a byte floor. This
            # is insufficient evidence, never a proven capacity fail.
            out["outcome"] = out["fit_class"] = FIT_INSUFFICIENT_EVIDENCE
            out["limitations"].append("imported_residency_unverified")
            out["limitations"].append("activation_unknown")
            reasons.append(
                f"declared file bytes (~{weights_declared / _GIB:.2f} GiB) "
                f"exceed planning budget (~{cpb / _GIB:.2f} GiB) but "
                "resident weight size under the runtime's residency "
                "contract is unverified")
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


def cpu_lane(*, evidence_class: str, explicitly_requested: bool,
             est_ram: int | None, ram_known: bool, evaluate) -> dict:
    """The CPU lane is an explicit alternative — never a silent fallback.
    `evaluate` is the injected resources.evaluate_ram callable.

    Static contracts alone can never satisfy: `cpu_lane_ok` requires
    cpu_operational evidence, which does not exist under the current
    contract — the best reachable outcome is `cpu_lane_unverified`."""
    reasons: list[str] = []
    if evidence_class == CPU_UNSUPPORTED:
        reasons.append("no static CPU-execution contract — the model "
                       "does not declare cpu_fallback or the runtime "
                       "has no cpu backend")
        return {"permitted": False, "outcome": CPU_LANE_UNSUPPORTED,
                "evidence_class": evidence_class, "est_ram_bytes": None,
                "ram_decision": None, "reasons": reasons,
                "limitations": [],
                "explicitly_requested": explicitly_requested}
    limitations: list[str] = []
    if evidence_class not in _CPU_STATIC_SUPPORTED:
        limitations.append("cpu_declared_only")
        reasons.append("cpu capability is declared only — no adapter "
                       "contract supports cpu_fallback for this runtime")
        return {"permitted": False, "outcome": CPU_LANE_UNVERIFIED,
                "evidence_class": evidence_class,
                "est_ram_bytes": est_ram, "ram_decision": None,
                "reasons": reasons, "limitations": limitations,
                "explicitly_requested": explicitly_requested}
    gate = evaluate(est_ram, ram_known)
    decision = gate.get("decision")
    if gate.get("reason"):
        reasons.append(gate["reason"])
    if decision == "warn":
        limitations.append("ram_warn")
    if not ram_known:
        limitations.append("ram_estimate_unknown")
    if not gate.get("fully_evaluated"):
        limitations.append("ram_evidence_incomplete")
    if decision == "block":
        outcome = CPU_LANE_BLOCKED
    elif evidence_class == CPU_OPERATIONAL:
        outcome = CPU_LANE_OK            # unreachable today — kept honest
    else:
        limitations.append("cpu_operational_evidence_absent")
        outcome = CPU_LANE_UNVERIFIED
    return {"permitted": True, "outcome": outcome,
            "evidence_class": evidence_class,
            "est_ram_bytes": est_ram, "ram_decision": decision,
            "ram_decision_reason": gate.get("reason") or "",
            "reasons": reasons, "limitations": limitations,
            "explicitly_requested": explicitly_requested}


def _fingerprint(plan, snapshot_id, alternatives, cpu, axes, request,
                 margins, limits_exceeded, cpu_evidence, ram_eval,
                 verdict_staleness) -> str:
    """plan:v2 — planning-evidence identity. v2 extends v1's canon with
    the evidence-classification inputs introduced by U-5c-1a; consumers
    keying on v1 values will not collide."""
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
        "cpu_evidence": cpu_evidence,
        "ram": [ram_eval.get("decision"),
                ram_eval.get("fully_evaluated"),
                sorted((ram_eval.get("checks") or {}).items())]
        if ram_eval else None,
        "verdict_staleness": sorted((verdict_staleness or {}).items()),
        "margins": {k: int(v) for k, v in sorted((margins or {}).items())
                    if isinstance(v, int)},
        "limits": list(limits_exceeded),
    }
    return "plan:v2:" + hashlib.sha256(_canon(fp).encode()).hexdigest()[:16]


def _result_fingerprint(plan_fp, snapshot_id, advisory, block_reason, axes,
                        freshness, simulated, cpu_evidence, ram_eval,
                        ram_evidence, reservations_captured,
                        verdict_staleness, alternatives, limits_exceeded,
                        truncated) -> str:
    """planresult:v1 — advisory RESULT identity. Material outcome and
    evidence-confidence transitions are visible here even when the
    planning fingerprint is unchanged (e.g. fresh -> stale with
    identical device evidence). Elapsed seconds are excluded."""
    fp = {
        "v": PLAN_RESULT_VERSION,
        "plan": plan_fp,
        "snapshot": snapshot_id or "unavailable",
        "advisory": advisory,
        "block_reason": block_reason,
        "axes": sorted(axes.items()),
        "freshness": freshness,
        "simulated": bool(simulated),
        "cpu_evidence": cpu_evidence,
        "ram_eval": [ram_eval.get("decision"),
                     ram_eval.get("fully_evaluated"),
                     sorted((ram_eval.get("checks") or {}).items())]
        if ram_eval else None,
        "ram_evidence": ram_evidence,
        "reservations": ("captured" if reservations_captured
                         else "unavailable"),
        "verdict_staleness": sorted((verdict_staleness or {}).items()),
        "alternatives": [[a["index"], a["outcome"]] for a in alternatives],
        "limits": list(limits_exceeded),
        "truncated": bool(truncated),
    }
    return ("planresult:v1:"
            + hashlib.sha256(_canon(fp).encode()).hexdigest()[:16])


def build_preview(*, plan: dep_resolve.DepPlan, spec, runtime_key: str,
                  runtime_decl_backends: tuple, runtime_planned: bool,
                  snapshot: dict | None, components: list, lora_apps: int,
                  weights_declared: int | None, residency: str,
                  residency_basis: str, device_sel: str,
                  selected_index: int | None, width: int, height: int,
                  batch: int, workflow_key: str, include_stale: bool,
                  evaluate_ram, ram_known: bool,
                  margins: dict, freshness: str, simulated: bool,
                  cpu_evidence: str,
                  runtime_blocked_authoritative: bool,
                  verdict_staleness: dict | None) -> dict:
    """Aggregate the preview — deterministic, advisory only (Gate 7/F3).

    snapshot — a hw_snapshot.build_snapshot() view or None (hardware
        evidence unavailable — never fabricated).
    freshness — "fresh" | "stale" | "unavailable"; independent of
        `simulated` (a simulated snapshot may also be stale).
    evaluate_ram — injected callable (est_ram, known) -> decision dict
        (resources.evaluate_ram against snapshot memory evidence).
    cpu_evidence — classify_cpu_evidence() output; cpu_lane_ok requires
        cpu_operational, which the current contract cannot produce.
    runtime_blocked_authoritative — the stored candidate_blocked verdict
        is a policy/contract block (True) vs a hardware-dependent one
        requiring fresh applicable evidence (False).
    verdict_staleness — {state: count} per-verdict freshness classes
        (current | stale | unevaluable | simulated).
    """
    snapshot = snapshot or {}
    limits_exceeded = list(plan.limits_exceeded)
    reasons = list(plan.reasons)
    limitations = list(plan.limitations)
    reservations_captured = bool(snapshot.get("reservations_captured"))
    evidence_adequate = freshness == "fresh" and not simulated

    permitted, res_reason = residency_evidence(runtime_key, spec, residency)
    residency_error = None if permitted else res_reason

    est = stage_estimates(spec, weights_declared, width, height, batch,
                          residency, lora_apps,
                          cpu_only=(device_sel == "cpu"))

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
            if dev is not None:
                dev = {**dev, "present": False}
            else:
                # Absence is only provable on fresh real inventory —
                # stale/simulated/unavailable evidence cannot turn a
                # syntactically valid selector into device_not_present.
                dev = {"index": selected_index,
                       "present": (False if evidence_adequate
                                   else "unverified")}
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
    lane = None
    if device_sel in ("cpu", "auto"):
        # The lane consumes CPU-only host residency — all weights and
        # activations in RAM — never the GPU staging estimate.
        est_lane = est["est_ram_bytes"] if device_sel == "cpu" else \
            stage_estimates(spec, weights_declared, width, height, batch,
                            residency, lora_apps,
                            cpu_only=True)["est_ram_bytes"]
        lane = cpu_lane(
            evidence_class=cpu_evidence,
            explicitly_requested=cpu_requested,
            est_ram=est_lane, ram_known=ram_known,
            evaluate=evaluate_ram)

    # Uniform advisory RAM evaluation for the requested path — host RAM
    # is consumed regardless of device selection, so this runs for
    # auto, explicit GPU and explicit CPU alike.
    ram_eval = evaluate_ram(est["est_ram_bytes"], ram_known)
    ram_decision = ram_eval.get("decision")
    ram_state = ram_decision or "unknown"

    # Every evaluated alternative's outcome — eligible device fits,
    # proven ineligibility, or the cpu lane when it is permitted or was
    # explicitly requested.
    device_outcomes = [a for a in alternatives if a["eligible"]]
    _LANE_FAIL = {CPU_LANE_BLOCKED, CPU_LANE_UNSUPPORTED}
    evaluated: list[str] = [a["fit_class"] for a in alternatives]
    if lane is not None and (lane["permitted"]
                             or lane["explicitly_requested"]):
        evaluated.append(lane["outcome"])
    satisfies = any(o in _SATISFIES or o == CPU_LANE_OK
                    for o in evaluated)
    # Exhaustiveness gate: a truncated enumeration can never prove that
    # no permitted alternative remains — the unevaluated tail may fit.
    all_proven_fail = (not truncated) and bool(evaluated) and all(
        o in _PROVEN_FAIL or o in _LANE_FAIL for o in evaluated)
    any_unknown = any(
        o not in _PROVEN_FAIL | _SATISFIES | _LANE_FAIL
        and o != CPU_LANE_OK for o in evaluated)

    # ---- axis: hardware_state ---------------------------------------
    if not device_outcomes and lane is None:
        hardware_state = "not_evaluated"
    elif any(a["fit_class"] == FIT_WITH_HEADROOM for a in device_outcomes) \
            or (lane is not None and lane["outcome"] == CPU_LANE_OK
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

    # ---- aggregation (F3/F4 — deterministic precedence, advisory) ----
    # Group 1: authoritative blockers — evidence-independent.
    if runtime_planned:
        advisory, block_reason = ADVISORY_BLOCKED, "runtime_planned"
    elif plan.policy_state != dep_resolve.POLICY_CLEAR:
        advisory = ADVISORY_BLOCKED
        block_reason = f"policy_{plan.policy_state}"
    elif plan.runtime_state == "candidate_blocked" \
            and runtime_blocked_authoritative:
        advisory, block_reason = ADVISORY_BLOCKED, "runtime_blocked"
    elif plan.structural_state == dep_resolve.STRUCT_CONFLICTED:
        advisory, block_reason = ADVISORY_BLOCKED, "dependency_conflicted"
    elif plan.capacity_state == dep_resolve.CAP_EXCEEDS:
        advisory, block_reason = ADVISORY_BLOCKED, "lora_capacity_exceeded"
    elif residency_error:
        advisory, block_reason = ADVISORY_BLOCKED, "residency_not_supported"
    elif lane is not None and lane["explicitly_requested"] \
            and lane["outcome"] == CPU_LANE_UNSUPPORTED:
        # Contract-level CPU refusal — a fact, not a measurement.
        advisory, block_reason = ADVISORY_BLOCKED, "cpu_unsupported"
    # Group 2: evidence-integrity gates — absence/staleness/simulation
    # can never prove a current physical failure NOR a readiness.
    elif freshness == "unavailable":
        advisory = ADVISORY_INCOMPLETE
        block_reason = "hardware_evidence_unavailable"
    elif simulated:
        advisory = ADVISORY_INCOMPLETE
        block_reason = "simulated_evidence"
    elif freshness == "stale":
        advisory = ADVISORY_INCOMPLETE
        block_reason = "stale_evidence"
    else:
        block_reason = None
        advisory = None

    if advisory is None:
        # Group 3: evidence-dependent blockers — fresh applicable
        # real evidence only.
        if ram_decision == "block":
            advisory, block_reason = ADVISORY_BLOCKED, "ram_insufficient"
        elif plan.runtime_state == "candidate_blocked":
            advisory, block_reason = ADVISORY_BLOCKED, "runtime_blocked"
        elif all_proven_fail:
            advisory, block_reason = ADVISORY_BLOCKED, "hardware_insufficient"
        elif not evaluated:
            advisory, block_reason = (ADVISORY_BLOCKED,
                                      "no_permitted_alternative")

    if advisory is None:
        # Group 4: incomplete — missing or insufficient evidence.
        if not reservations_captured:
            advisory = ADVISORY_INCOMPLETE
            block_reason = "reservation_evidence_unavailable"
        elif limits_exceeded:
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
        elif ram_decision == "unknown":
            advisory, block_reason = ADVISORY_INCOMPLETE, "ram_unknown"
        elif not ram_eval.get("fully_evaluated"):
            advisory, block_reason = (ADVISORY_INCOMPLETE,
                                      "ram_evidence_incomplete")
        elif not satisfies:
            advisory = ADVISORY_INCOMPLETE
            if lane is not None and lane["outcome"] == CPU_LANE_UNVERIFIED:
                block_reason = "cpu_lane_unverified"
            else:
                block_reason = ("insufficient_evidence" if any_unknown
                                else "no_permitted_alternative")
        else:
            # Group 5: READY — non-simulated fresh evidence with
            # reservations captured and RAM fully evaluated.
            advisory = ADVISORY_READY
    if advisory == ADVISORY_INCOMPLETE and all_proven_fail:
        reasons.append("insufficient capacity was indicated on "
                       f"{block_reason.replace('_', ' ')} — not a "
                       "current physical measurement")
    if block_reason and block_reason not in reasons:
        reasons.append(block_reason)

    # ---- limitations -------------------------------------------------
    if freshness == "stale":
        limitations.append("snapshot_stale")
    if freshness == "unavailable":
        limitations.append("hardware_evidence_unavailable")
    if simulated:
        limitations.append("simulated_hardware")
    if not reservations_captured:
        limitations.append("scheduler_reservations_unavailable")
    if any(a.get("reservation_overlap_possible") for a in alternatives):
        limitations.append("reservation_overlap_possible")
    if hardware_state == "fits_tight":
        limitations.append("fits_tight")
    if ram_decision == "warn":
        limitations.append("ram_warn")
    if not ram_known:
        limitations.append("ram_estimate_unknown")
    if ram_eval.get("decision") not in (None, "block") \
            and not ram_eval.get("fully_evaluated"):
        limitations.append("ram_evidence_incomplete")
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
    snapshot_id = snapshot.get("snapshot_id")
    fingerprint = _fingerprint(
        plan, snapshot_id, alternatives, lane, axes, request, margins,
        limits_exceeded, cpu_evidence, ram_eval, verdict_staleness)
    mem_ev = snapshot.get("memory") or {}
    result_fp = _result_fingerprint(
        fingerprint, snapshot_id, advisory, block_reason, axes,
        freshness, simulated, cpu_evidence, ram_eval,
        [mem_ev.get("total_bytes"), mem_ev.get("available_bytes"),
         mem_ev.get("commit_headroom_bytes")],
        reservations_captured, verdict_staleness, alternatives,
        limits_exceeded, truncated)

    return {
        "advisory": True,
        "advisory_state": advisory,
        "plan_fingerprint": fingerprint,
        "plan_result_fingerprint": result_fp,
        "artifact_id": plan.artifact_id,
        "workflow_key": workflow_key,
        "runtime_key": runtime_key,
        "dependency_plan_ref": plan.resolution_version,
        "snapshot": snapshot or None,
        "snapshot_stale": freshness == "stale",
        "snapshot_freshness": freshness,
        "snapshot_simulated": bool(simulated),
        "verdict_staleness": dict(verdict_staleness or {}),
        "ram_eval": ram_eval,
        "cpu_evidence": cpu_evidence,
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
    "CPU_ADAPTER_CONTRACT",
    "CPU_COMMAND_CONSISTENT",
    "CPU_DECLARED",
    "CPU_LANE_BLOCKED",
    "CPU_LANE_OK",
    "CPU_LANE_UNSUPPORTED",
    "CPU_LANE_UNVERIFIED",
    "CPU_OPERATIONAL",
    "CPU_UNSUPPORTED",
    "FIT_BACKEND_EVIDENCE_UNKNOWN",
    "FIT_BACKEND_MISMATCH",
    "FIT_ESTIMATE_UNKNOWN",
    "FIT_INSUFFICIENT",
    "FIT_INSUFFICIENT_EVIDENCE",
    "FIT_NOT_PRESENT",
    "FIT_PRESENCE_UNVERIFIED",
    "FIT_TIGHT",
    "FIT_UNMEASURABLE",
    "FIT_WITH_HEADROOM",
    "MAX_DEVICE_ALTERNATIVES",
    "PLAN_MAX_DEVICE_ALTERNATIVES",
    "PLAN_RESULT_VERSION",
    "PLAN_RULESET_VERSION",
    "RESIDENCY_MODES",
    "build_preview",
    "cpu_evidence_class",
    "cpu_lane",
    "evaluate_device",
    "residency_evidence",
    "residency_modes_for_runtime",
    "stage_estimates",
]
