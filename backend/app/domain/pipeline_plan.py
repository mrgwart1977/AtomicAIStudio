"""U-5c-2b — multi-node hardware-aware pipeline plan preview (pure).

Dedicated advisory planner for connected whole-job pipelines: composes
the U-5c-2a declarative DAG, per-node U-5b dependency plans and the
U-5c-1 single-stage evaluation primitives into bounded, deterministic
placement candidates. Advisory only — never execution authorization,
never a probe, never a reservation, never a write.

Every input is caller-prepared stored/cache-only evidence. No I/O.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass, field

from app.domain import compatibility, dep_resolve, plan_preview
from app.domain.pipeline_defs import PipelineDefinition, _ptype_ok, _value_meets, concurrency_pairs

PIPELINE_PLAN_SCHEMA_VERSION = 1
PIPELINE_PLAN_VERSION = 1
PIPELINE_SEARCH_VERSION = 1
PIPELINE_RANKING_VERSION = 1

# Approved ceilings (D3) — computational bounds, not feasibility limits.
LIMITS = {
    "max_nodes": 32,
    "max_device_alternatives": 8,
    "max_runtime_candidates_per_node": 4,
    "max_residency_alternatives": 4,
    "max_node_variants": 24,
    "max_partial_expansions": 65536,
    "max_completed_assignments": 4096,
    "max_group_proposals": 256,
    "max_group_size": 4,
    "max_wave_decompositions": 64,
    "max_retained_candidates": 8,
    "max_findings": 64,
}

ADVISORY_INVALID = "invalid"
ADVISORY_BLOCKED = "plan_blocked"
ADVISORY_INCOMPLETE = "plan_incomplete"
ADVISORY_READY = "ready_advisory"

# Candidate feasibility
FEASIBLE = "feasible"
FEASIBLE_CONDITIONAL = "feasible_conditional"
INFEASIBLE_PROVEN = "infeasible_proven"
INFEASIBLE_UNVERIFIED = "infeasible_unverified"
CAND_BLOCKED = "blocked"

LIFETIME_RELEASE_ASSUMED = "release_assumed"     # scenario A (explicit)
LIFETIME_RETAINED = "retained_model"             # scenario B
LIFETIME_UNKNOWN = "unknown"                     # scenario C
LIFETIME_POLICIES = (LIFETIME_RELEASE_ASSUMED, LIFETIME_RETAINED,
                     LIFETIME_UNKNOWN)

# Truncation classes (distinct — F4)
TRUNC_VARIANT = "variant_truncated"
TRUNC_DEVICE = "device_truncated"
TRUNC_GROUP = "group_truncated"
TRUNC_WAVE = "wave_truncated"
TRUNC_PARTIAL = "partial_truncated"
TRUNC_COMPLETED = "completed_truncated"
TRUNC_RESPONSE = "response_truncated"

_SATISFYING_FITS = {plan_preview.FIT_WITH_HEADROOM, plan_preview.FIT_TIGHT}
_PROVEN_FITS = {plan_preview.FIT_INSUFFICIENT,
                plan_preview.FIT_BACKEND_MISMATCH,
                plan_preview.FIT_NOT_PRESENT}
_LANE_OK = plan_preview.CPU_LANE_OK


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      default=str)


def _hash16(payload) -> str:
    return hashlib.sha256(_canon(payload).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Finding:
    code: str
    subject: str
    detail: str
    severity: str = "error"


@dataclass(frozen=True)
class NodeEvidence:
    """Per-node evaluated evidence — prepared by the endpoint."""
    node_id: str
    artifact_id: str | None
    model_key_effective: str | None
    bindability: str            # bindable|non_bindable|unknown|unresolved
    bindability_reason: str
    integrity_class: str        # verified|recorded|declared|missing|na
    dep_plan: object | None     # DepPlan or None when unresolvable
    runtime_state: str          # candidate_* | unevaluated
    runtime_blocked_authoritative: bool
    adapter_ids: tuple          # admissible adapter candidates
    est_vram_bytes: int | None
    est_ram_bytes: int | None
    ram_known: bool
    device_alternatives: tuple  # evaluate_device dicts (bounded)
    cpu_lane: dict | None
    residency_error: str | None
    workflow_key: str
    required: bool = True


@dataclass
class Budgets:
    """Request-GLOBAL budgets — never reset per branch/scenario (F4)."""
    partial_expansions: int = 0
    completed: int = 0
    group_proposals: int = 0
    waves: int = 0
    variant_evals: dict = field(default_factory=dict)
    omitted_partial: bool = False
    omitted_completed: bool = False
    omitted_groups: bool = False
    omitted_waves: bool = False
    omitted_variants: dict = field(default_factory=dict)


# ------------------------------------------------------------------
# External parameter resolution + deferred bindings
# ------------------------------------------------------------------


def resolve_external_params(defn: PipelineDefinition,
                            supplied: dict) -> tuple[dict, list[Finding]]:
    """Strict external-param resolution against ParamDecls — reuses the
    pipeline_defs type/domain machinery, no duplication."""
    findings: list[Finding] = []
    values: dict = {}
    decl = {p.param_key: p for p in defn.external_params}
    for k in sorted(supplied):
        if k not in decl:
            findings.append(Finding("unknown_param", k, "not declared"))
    for p in defn.external_params:
        if p.param_key in supplied:
            v = supplied[p.param_key]
            if not _ptype_ok(p.ptype, v):
                findings.append(Finding(
                    "param_type", p.param_key,
                    f"value {v!r} is not {p.ptype}"))
                continue
            if p.constraints and not _value_meets(p.ptype, v,
                                                  p.constraints):
                findings.append(Finding(
                    "param_constraint", p.param_key,
                    f"value {v!r} violates declared constraints"))
                continue
            values[p.param_key] = v
        elif p.required:
            findings.append(Finding(
                "missing_param", p.param_key,
                "required external param not supplied"))
        elif p.has_default:
            values[p.param_key] = p.default
        else:
            values[p.param_key] = None
    return values, findings


def resolve_bindings(defn: PipelineDefinition,
                     values: dict) -> tuple[dict, list[dict]]:
    """Per-node effective request args + typed deferred bindings."""
    deferred: list[dict] = []
    resolved: dict[str, dict] = {}
    edge_by_id = {e.edge_id: e for e in defn.edges}
    for n in defn.nodes:
        args: dict = {}
        for b in n.param_bindings:
            if b.source == "constant":
                args[b.request_key] = b.constant
            elif b.source == "external":
                args[b.request_key] = values.get(b.external_key or "")
            elif b.source == "edge":
                e = edge_by_id.get(b.edge_id or "")
                # A future generated output: structurally bound, never an
                # existing asset. Nothing is fabricated (D1/F-no-id).
                deferred.append({
                    "node_id": n.node_id, "request_key": b.request_key,
                    "edge_id": b.edge_id, "state": "deferred_future_output",
                    "asset_kind": e.asset_role if e else None,
                    "ownership": e.ownership if e else None,
                    "provenance": e.provenance if e else None})
                args[b.request_key] = None
        resolved[n.node_id] = args
    return resolved, deferred


# ------------------------------------------------------------------
# Topo levels, independence, waves
# ------------------------------------------------------------------


def topo_levels(defn: PipelineDefinition) -> list[list[str]]:
    """Deterministic topological levels — fixed-level wave strategy."""
    adj = {n.node_id: set() for n in defn.nodes}
    indeg = {n.node_id: 0 for n in defn.nodes}
    for e in defn.edges:
        if e.src_node in adj and e.dst_node in adj:
            adj[e.src_node].add(e.dst_node)
            indeg[e.dst_node] += 1
    frontier = sorted(k for k, d in indeg.items() if d == 0)
    levels: list[list[str]] = []
    seen: set[str] = set()
    while frontier:
        levels.append(frontier)
        seen.update(frontier)
        # deterministic: a node joins the next frontier once all of
        # its predecessors have been seen
        levels_next = []
        for d in sorted({d for s in frontier for d in adj[s]}):
            if d in seen:
                continue
            preds = {s for s in adj if d in adj[s]}
            if preds <= seen:
                levels_next.append(d)
        frontier = sorted(levels_next)
    return levels


def independence_matrix(defn: PipelineDefinition) -> dict[str, set]:
    pairs = concurrency_pairs(defn)
    ind: dict[str, set] = {n.node_id: set() for n in defn.nodes}
    for a, b in pairs:
        ind[a].add(b)
        ind[b].add(a)
    return ind


def wave_arrangements(defn: PipelineDefinition,
                      budgets: Budgets) -> tuple[list, list[str]]:
    """Bounded wave decompositions: per level, either maximal concurrent
    (all pairwise-independent nodes grouped) or fully sequential.
    Enumerated deterministically, request-global budget (F4)."""
    levels = topo_levels(defn)
    ind = independence_matrix(defn)
    trunc: list[str] = []
    per_level: list[list[list[str]]] = []
    for lvl in levels:
        if len(lvl) == 1:
            per_level.append([[tuple(lvl)]])
            continue
        choices = [[(x,) for x in lvl]]                # sequential
        members = set(lvl)
        groups = set()
        for r in range(2, min(len(lvl), LIMITS["max_group_size"]) + 1):
            for comb in itertools.combinations(sorted(lvl), r):
                budgets.group_proposals += 1
                if budgets.group_proposals > LIMITS["max_group_proposals"]:
                    budgets.omitted_groups = True
                    break
                if all(b in ind[a] for i, a in enumerate(comb)
                       for b in comb[i + 1:]):
                    groups.add(tuple(comb))
            else:
                continue
            break
        for g in sorted(groups):
            rest = sorted(members - set(g))
            choices.append([g] + [(x,) for x in rest])
        per_level.append(choices)
    # Compose per-level choices into wave arrangements (bounded).
    combos = 1
    for c in per_level:
        combos *= len(c)
    arrangements = []
    for pick in itertools.product(*per_level):
        budgets.waves += 1
        if budgets.waves > LIMITS["max_wave_decompositions"]:
            budgets.omitted_waves = True
            break
        arrangements.append(tuple(tuple(g) for grp in pick for g in grp))
    if budgets.omitted_groups and TRUNC_GROUP not in trunc:
        trunc.append(TRUNC_GROUP)
    if budgets.omitted_waves and TRUNC_WAVE not in trunc:
        trunc.append(TRUNC_WAVE)
    return arrangements, trunc


# ------------------------------------------------------------------
# Candidate evaluation
# ------------------------------------------------------------------


def _composite_fit(est_sum: int | None, cpb: int | None) -> str:
    """Margin applied ONCE at the composite boundary (F6)."""
    if est_sum is None:
        return plan_preview.FIT_ESTIMATE_UNKNOWN
    if cpb is None:
        return plan_preview.FIT_UNMEASURABLE
    if est_sum > cpb:
        return plan_preview.FIT_INSUFFICIENT
    if est_sum + compatibility._HEADROOM_BYTES <= cpb:
        return plan_preview.FIT_WITH_HEADROOM
    return plan_preview.FIT_TIGHT


def evaluate_candidate(assign: dict, nodes: list[NodeEvidence],
                       wave: tuple, lifetime: str,
                       evaluate_ram, transition_host=None,
                       transition_dev=None) -> dict:
    """One complete assignment: device/residency/adapter per node + wave
    arrangement + lifetime scenario. Every required node exactly once.

    Transition overhead (Q2): `transition_host` maps wave index → known
    additional host bytes for that wave; `transition_dev` maps
    (wave, device) → known additional device bytes. Only established
    values are summed — an absent key stays UNKNOWN (never a measured
    zero) and is disclosed via transition_overhead_unknown /
    _partial. Overhead already inside stage_estimates is never
    re-added: callers pass only ADDITIONAL evidenced bytes."""
    transition_host = transition_host or {}
    transition_dev = transition_dev or {}
    reasons: list[str] = []
    limitations: list[str] = []
    node_by_id = {n.node_id: n for n in nodes}
    # Per-device demand per wave; per-wave host demand.
    dev_peak: dict[int, list] = {}
    host_by_wave: list[int] = []
    unknown_wave_host = set()
    dev_used: set = set()
    for w, grp in enumerate(wave):
        host_sum = 0
        dev_sum: dict[int, int] = {}
        for nid in grp:
            n = node_by_id[nid]
            a = assign[nid]
            if a["device"] in (-1, None):
                pass                       # cpu lane / no-variant — no
                # device VRAM demand (no-variant marks the node
                # infeasible through its own fit evidence, not a 0 fit)
            else:
                dev_sum[a["device"]] = \
                    dev_sum.get(a["device"], 0) + \
                    (a["est_vram_bytes"] or 0)
            if a["est_ram_bytes"] is None:
                unknown_wave_host.add(w)
            else:
                host_sum += a["est_ram_bytes"]
        host_by_wave.append(host_sum)
        for d, s in dev_sum.items():
            dev_peak.setdefault(d, []).append((w, s))
            dev_used.add((w, d))
    # Retained scenario adds all earlier waves to each wave's demand.
    if lifetime in (LIFETIME_RETAINED, LIFETIME_UNKNOWN):
        for d, entries in dev_peak.items():
            running = 0
            dev_peak[d] = [(w, running := running + s)
                           for w, s in sorted(entries)]
        running = 0
        host_by_wave = [(running := running + h) for h in host_by_wave]
    # Known transition overhead joins ITS wave only — applied after any
    # retained-accumulation so transient bytes are never carried into a
    # later wave's retained totals. Absence stays unknown, not zero.
    t_unknown_waves = {w for w in range(len(wave))
                       if w not in transition_host}
    t_unknown_dev = dev_used - set(transition_dev)
    t_unknown = bool(t_unknown_waves or t_unknown_dev)
    t_known_any = bool(
        any(w in transition_host for w in range(len(wave))) or
        any((w, d) in transition_dev for w, d in dev_used))
    host_by_wave = [h + transition_host.get(w, 0)
                    for w, h in enumerate(host_by_wave)]
    for d, entries in dev_peak.items():
        dev_peak[d] = [(w, s + transition_dev.get((w, d), 0))
                       for w, s in entries]
    host_peak = max(host_by_wave) if host_by_wave else 0
    host_known = all(n.ram_known for n in nodes) \
        and not unknown_wave_host
    if lifetime != LIFETIME_RELEASE_ASSUMED:
        limitations.append("retained_bound_unproven")
    if t_unknown:
        limitations.append("transition_overhead_unknown")
        if t_known_any:
            limitations.append("transition_overhead_partial")
    # Device fits (composite per device, margin once).
    dev_fits: dict[int, str] = {}
    dev_budgets: dict[int, int | None] = {}
    any_unknown = False
    any_fail = False
    for d, entries in dev_peak.items():
        peak = max(s for _, s in entries)
        cpb = None
        for n in nodes:
            hit = next((x for x in n.device_alternatives
                        if x.get("index") == d), None)
            if hit is not None:
                cpb = hit.get("budget_bytes")
                break
        dev_budgets[d] = cpb
        fit = _composite_fit(peak, cpb)
        dev_fits[d] = fit
        if fit in _PROVEN_FITS:
            any_fail = True
        elif fit not in _SATISFYING_FITS:
            any_unknown = True
    # Shared host-memory gate — ONE evaluate_ram per candidate (F3).
    ram_eval = evaluate_ram(host_peak, host_known)
    ram_decision = ram_eval.get("decision")
    # Node-level evidence rollup.
    node_states = {}
    blocked = False
    incomplete = False
    for n in nodes:
        a = assign[n.node_id]
        if n.bindability == "non_bindable":
            node_states[n.node_id] = "blocked"
            blocked = True
            reasons.append("binding_contract_mismatch:" + n.node_id)
            continue
        st = "ok"
        if n.bindability in ("unknown", "unresolved"):
            st = "incomplete"
        if n.dep_plan is not None:
            if n.dep_plan.advisory_state == dep_resolve.ADVISORY_BLOCKED:
                st, blocked = "blocked", True
            elif n.dep_plan.advisory_state == \
                    dep_resolve.ADVISORY_INCOMPLETE:
                st = "incomplete"
        if n.runtime_state in ("candidate_blocked",) \
                and n.runtime_blocked_authoritative:
            st, blocked = "blocked", True
            reasons.append("runtime_blocked:" + n.node_id)
        elif n.runtime_state in ("candidate_unknown", "unevaluated",
                                 "candidate_blocked"):
            st = "incomplete"
        if n.residency_error:
            st, blocked = "blocked", True
            reasons.append("residency_not_supported:" + n.node_id)
        lane = n.cpu_lane
        if a["device"] == -1 and lane is not None:
            if lane["outcome"] == plan_preview.CPU_LANE_UNSUPPORTED:
                st, blocked = "blocked", True
            elif lane["outcome"] != _LANE_OK:
                st = "incomplete"
                limitations.append("cpu_lane_unverified")
        elif a["device"] != -1:
            alt = next((x for x in n.device_alternatives
                        if x.get("index") == a["device"]), None)
            if alt is not None and not alt.get("eligible"):
                st = "incomplete" if alt["fit_class"] not in _PROVEN_FITS \
                    else "blocked"
        node_states[n.node_id] = st
        if st == "incomplete":
            incomplete = True
    if any(len(g) > 1 for g in wave):
        limitations.append("concurrency_unverified")
        incomplete = True
    limitations.append("host_reservation_overlap_possible")
    limitations.append("outstanding_demand_unobservable")
    limitations.append("estimator_asymmetry")
    if ram_decision == "block" and lifetime == LIFETIME_RELEASE_ASSUMED:
        cand_state, blocked = CAND_BLOCKED, True
        reasons.append("ram_insufficient")
    elif ram_decision == "block":
        cand_state = INFEASIBLE_UNVERIFIED
        reasons.append("ram_insufficient_conditional")
    elif blocked:
        cand_state = CAND_BLOCKED
    elif any_fail:
        cand_state = INFEASIBLE_PROVEN
        reasons.append("hardware_insufficient")
    elif any_unknown or ram_decision == "unknown" \
            or not ram_eval.get("fully_evaluated"):
        cand_state = INFEASIBLE_UNVERIFIED
    elif incomplete or t_unknown:
        # Placement depends on unresolved assumptions (deferred inputs,
        # unknown transition overhead, unverified concurrency) —
        # conditional, never unqualified feasible (Q5).
        cand_state = FEASIBLE_CONDITIONAL
    else:
        cand_state = (FEASIBLE_CONDITIONAL
                      if lifetime != LIFETIME_RELEASE_ASSUMED
                      else FEASIBLE)
    return {
        "assignment": {nid: {k: a[k] for k in
                             ("artifact_id", "runtime_key", "adapter_id",
                              "device", "residency")}
                       for nid, a in assign.items()},
        "wave": [list(g) for g in wave],
        "lifetime": lifetime,
        "state": cand_state,
        "device_fits": {str(d): f for d, f in dev_fits.items()},
        "device_budgets": {str(d): b for d, b in dev_budgets.items()},
        "host_peak_bytes": host_peak,
        "ram_eval": ram_eval,
        "node_states": node_states,
        "reasons": reasons,
        "limitations": list(dict.fromkeys(limitations)),
    }


# ------------------------------------------------------------------
# Bounded search
# ------------------------------------------------------------------


def _rank_key(cand: dict, prefs: dict) -> tuple:
    """Deterministic comparator — evidence quality, never performance."""
    state_rank = {FEASIBLE: 0, FEASIBLE_CONDITIONAL: 1,
                  INFEASIBLE_UNVERIFIED: 2, INFEASIBLE_PROVEN: 3,
                  CAND_BLOCKED: 4}[cand["state"]]
    tight = sum(1 for f in cand["device_fits"].values()
                if f == plan_preview.FIT_TIGHT)
    pref_hits = sum(1 for nid, a in cand["assignment"].items()
                    if nid in prefs
                    and prefs[nid] in (a["device"], "auto"))
    devices = {a["device"] for a in cand["assignment"].values()} - {-1}
    unc = sum(1 for l in cand["limitations"]
              if l in ("cpu_lane_unverified", "concurrency_unverified",
                       "retained_bound_unproven"))
    return (state_rank, tight, -pref_hits, len(devices), unc,
            _canon(cand["assignment"]), _canon(cand["wave"]))


def search(nodes: list[NodeEvidence], waves: list,
           lifetime: str, evaluate_ram, prefs: dict,
           budgets: Budgets, reason_sink: set | None = None,
           transition_host=None, transition_dev=None,
           ) -> tuple[list, list[str], list]:
    """Bounded DFS over per-node variants. Returns (retained, truncations,
    all_states) — every explored candidate feeds aggregate feasibility.
    `reason_sink` (optional set) collects every explored candidate's
    reason codes so aggregate can classify an all-blocked outcome by
    cause; `transition_*` forward to evaluate_candidate."""
    trunc: list[str] = []
    retained: list[dict] = []
    states: list[str] = []
    node_order = sorted(nodes, key=lambda n: n.node_id)

    def variants(n: NodeEvidence) -> list:
        out = []
        devs = [a for a in n.device_alternatives][:LIMITS[
            "max_device_alternatives"]]
        if len(n.device_alternatives) > LIMITS["max_device_alternatives"]:
            trunc.append(TRUNC_DEVICE)
        for alt in devs:
            dev = alt.get("index")
            for ad in n.adapter_ids[:LIMITS[
                    "max_runtime_candidates_per_node"]]:
                for res in ("gpu",)[:LIMITS["max_residency_alternatives"]]:
                    budgets.variant_evals[n.node_id] = \
                        budgets.variant_evals.get(n.node_id, 0) + 1
                    if budgets.variant_evals[n.node_id] > LIMITS[
                            "max_node_variants"]:
                        budgets.omitted_variants[n.node_id] = True
                        continue
                    out.append({"node_id": n.node_id, "device": dev,
                                "adapter_id": ad,
                                "runtime_key": n.dep_plan.runtime_key
                                if n.dep_plan else "sdcpp",
                                "residency": res,
                                "artifact_id": n.artifact_id,
                                "est_vram_bytes": n.est_vram_bytes,
                                "est_ram_bytes": n.est_ram_bytes})
        if n.cpu_lane is not None and n.cpu_lane.get("permitted"):
            out.append({"node_id": n.node_id, "device": -1,
                        "adapter_id": n.adapter_ids[0]
                        if n.adapter_ids else None,
                        "runtime_key": n.dep_plan.runtime_key
                        if n.dep_plan else "sdcpp",
                        "residency": "gpu",
                        "artifact_id": n.artifact_id,
                        "est_vram_bytes": None,
                        "est_ram_bytes": n.est_ram_bytes})
        if not out:      # node has no variants — still emits the marker
            out.append({"node_id": n.node_id, "device": None,
                        "adapter_id": None, "runtime_key": "sdcpp",
                        "residency": "gpu", "artifact_id": n.artifact_id,
                        "est_vram_bytes": n.est_vram_bytes,
                        "est_ram_bytes": n.est_ram_bytes})
        return out

    per_node = {n.node_id: variants(n) for n in node_order}
    assign: dict = {}

    def dfs(i: int) -> None:
        if budgets.partial_expansions >= LIMITS["max_partial_expansions"]:
            budgets.omitted_partial = True
            return
        budgets.partial_expansions += 1          # pruned states count too
        if i == len(node_order):
            for wave in waves:
                # A completed arrangement is (assignment × wave ×
                # lifetime) — counted per materialized arrangement,
                # not per assignment (Q4).
                if budgets.completed >= \
                        LIMITS["max_completed_assignments"]:
                    budgets.omitted_completed = True
                    break
                budgets.completed += 1
                cand = evaluate_candidate(
                    dict(assign), nodes, wave, lifetime, evaluate_ram,
                    transition_host, transition_dev)
                states.append(cand["state"])
                if reason_sink is not None:
                    reason_sink.update(cand["reasons"])
                if len(retained) < LIMITS["max_retained_candidates"]:
                    retained.append(cand)
                    retained.sort(key=lambda c: _rank_key(c, prefs))
                elif _rank_key(cand, prefs) < _rank_key(
                        retained[-1], prefs):
                    retained[-1] = cand
                    retained.sort(key=lambda c: _rank_key(c, prefs))
            return
        nid = node_order[i].node_id
        pref = prefs.get(nid)
        for v in per_node[nid]:
            if pref is not None and pref != "auto" and \
                    v["device"] != pref:
                continue                        # hard constraint (D2)
            assign[nid] = v
            dfs(i + 1)
        assign.pop(nid, None)

    dfs(0)
    if budgets.omitted_variants and TRUNC_VARIANT not in trunc:
        trunc.append(TRUNC_VARIANT)
    if budgets.omitted_partial:
        trunc.append(TRUNC_PARTIAL)
    if budgets.omitted_completed:
        trunc.append(TRUNC_COMPLETED)
    return retained, list(dict.fromkeys(trunc)), states


# ------------------------------------------------------------------
# Aggregation + fingerprints
# ------------------------------------------------------------------


def aggregate(structural: str, defn_findings: list,
              node_ev: list[NodeEvidence], retained: list,
              states: list, truncs: list, freshness: str,
              simulated: bool, reservations_captured: bool,
              deferred: list, exhaustive: bool,
              explored_reasons: set | frozenset = frozenset()) -> dict:
    """F1 precedence — blockers at correct scope."""
    findings = [{"code": f.code, "subject": f.subject,
                 "detail": f.detail} for f in defn_findings]
    axes = {"structure": structural,
            "resource_evidence": ("qualified" if freshness == "fresh"
                                  and not simulated else freshness),
            "execution_input_availability": (
                "deferred_future_output" if deferred else
                "all_available")}
    if structural != "valid":
        return {"advisory_state": ADVISORY_INVALID,
                "block_reason": "invalid_pipeline",
                "axes": axes, "findings": findings}
    # Rank 2 — authoritative blockers (required-node-universal or
    # pipeline-wide), visible even under bad hw evidence.
    auth = [n for n in node_ev if n.bindability == "non_bindable"
            or n.residency_error
            or (n.dep_plan is not None and
                n.dep_plan.advisory_state == dep_resolve.ADVISORY_BLOCKED)
            or (n.runtime_state == "candidate_blocked"
                and n.runtime_blocked_authoritative)]
    req_blocked = [n for n in auth if n.required]
    if req_blocked and all(
            any(c["state"] == CAND_BLOCKED or
                c["node_states"].get(n.node_id) == "blocked"
                for c in retained) or not retained
            for n in req_blocked) and (
            all(n.bindability == "non_bindable" or n.residency_error
                or (n.dep_plan is not None and
                    n.dep_plan.policy_state != dep_resolve.POLICY_CLEAR)
                for n in req_blocked) or
            any(n.dep_plan is not None and
                n.dep_plan.policy_state != dep_resolve.POLICY_CLEAR
                for n in req_blocked)):
        return {"advisory_state": ADVISORY_BLOCKED,
                "block_reason": "authoritative_block",
                "axes": axes, "findings": findings}
    # Rank 3 — evidence integrity.
    if freshness == "unavailable":
        return {"advisory_state": ADVISORY_INCOMPLETE,
                "block_reason": "hardware_evidence_unavailable",
                "axes": axes, "findings": findings}
    if simulated:
        return {"advisory_state": ADVISORY_INCOMPLETE,
                "block_reason": "simulated_evidence",
                "axes": axes, "findings": findings}
    if freshness == "stale":
        return {"advisory_state": ADVISORY_INCOMPLETE,
                "block_reason": "stale_evidence",
                "axes": axes, "findings": findings}
    # Rank 4 — evidence-dependent blockers: qualified ram block on
    # required (scenario-A) workload, or proven no-fit. A RAM-driven
    # all-blocked outcome is labelled by its actual cause; a
    # contract-driven one stays authoritative_block (Q5).
    blocked_states = [s for s in states]
    if retained and all(s == CAND_BLOCKED for s in blocked_states) \
            and exhaustive:
        cause = ("ram_insufficient" if explored_reasons and
                 explored_reasons <= {"ram_insufficient"}
                 else "authoritative_block")
        return {"advisory_state": ADVISORY_BLOCKED,
                "block_reason": cause,
                "axes": axes, "findings": findings}
    if not reservations_captured:
        return {"advisory_state": ADVISORY_INCOMPLETE,
                "block_reason": "reservation_evidence_unavailable",
                "axes": axes, "findings": findings}
    if exhaustive and states and all(
            s == INFEASIBLE_PROVEN for s in states):
        return {"advisory_state": ADVISORY_BLOCKED,
                "block_reason": "proven_no_fit",
                "axes": axes, "findings": findings}
    # Rank 5 — incomplete.
    reason = None
    if deferred:
        reason = "deferred_execution_inputs"
    elif truncs:
        reason = "search_truncated"
    elif any(s == INFEASIBLE_UNVERIFIED for s in states) or not states:
        reason = "insufficient_evidence"
    elif any(s == FEASIBLE_CONDITIONAL for s in states):
        reason = "conditional_inputs"
    if any(s == FEASIBLE for s in states) and reason is None:
        return {"advisory_state": ADVISORY_READY,
                "block_reason": None, "axes": axes,
                "findings": findings}
    return {"advisory_state": ADVISORY_INCOMPLETE,
            "block_reason": reason or "insufficient_evidence",
            "axes": axes, "findings": findings}


def plan_fingerprint(defn_fp: str, ctx_fp: str, project_scope: str,
                     params: dict, selections: dict, nodes_meta: list,
                     deferred: list, lifetime: str, snapshot_id,
                     reservations, memory, limits: dict,
                     freshness: str, margins: dict) -> str:
    return "pipeplan:v1:" + _hash16({
        "ruleset": [PIPELINE_PLAN_SCHEMA_VERSION, PIPELINE_PLAN_VERSION,
                    PIPELINE_SEARCH_VERSION, PIPELINE_RANKING_VERSION],
        "definition_fp": defn_fp, "ctx_fp": ctx_fp,
        "project": project_scope, "params": params,
        "selections": selections, "nodes": nodes_meta,
        "deferred": deferred, "lifetime": lifetime,
        "snapshot": snapshot_id or "unavailable",
        "reservations": reservations or "unavailable",
        "memory": memory, "limits": limits,
        "freshness": freshness, "margins": margins})


def result_fingerprint(plan_fp: str, aggregate: dict,
                       candidates: list, truncs: list,
                       counters: dict, states: list) -> str:
    return "piperesult:v1:" + _hash16({
        "v": PIPELINE_PLAN_VERSION, "pipeplan": plan_fp,
        "advisory": aggregate["advisory_state"],
        "block_reason": aggregate["block_reason"],
        "axes": aggregate["axes"],
        "candidates": [[_canon(c["assignment"]), _canon(c["wave"]),
                        c["lifetime"], c["state"],
                        sorted(c["device_fits"].items()),
                        c["ram_eval"].get("decision")]
                       for c in candidates],
        "explored_states": sorted(states),
        "truncations": truncs, "counters": counters,
        "findings": aggregate["findings"]})


__all__ = [
    "ADVISORY_BLOCKED",
    "ADVISORY_INCOMPLETE",
    "ADVISORY_INVALID",
    "ADVISORY_READY",
    "CAND_BLOCKED",
    "FEASIBLE",
    "FEASIBLE_CONDITIONAL",
    "INFEASIBLE_PROVEN",
    "INFEASIBLE_UNVERIFIED",
    "LIFETIME_POLICIES",
    "LIFETIME_RELEASE_ASSUMED",
    "LIFETIME_RETAINED",
    "LIFETIME_UNKNOWN",
    "LIMITS",
    "PIPELINE_PLAN_SCHEMA_VERSION",
    "PIPELINE_PLAN_VERSION",
    "PIPELINE_RANKING_VERSION",
    "PIPELINE_SEARCH_VERSION",
    "Budgets",
    "Finding",
    "NodeEvidence",
    "aggregate",
    "evaluate_candidate",
    "independence_matrix",
    "plan_fingerprint",
    "resolve_bindings",
    "resolve_external_params",
    "result_fingerprint",
    "search",
    "topo_levels",
    "wave_arrangements",
]
