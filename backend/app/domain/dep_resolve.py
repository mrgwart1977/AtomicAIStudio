"""U-5b — deterministic dependency resolution (advisory, read-only).

Builds the ephemeral requirement DAG for one root artifact + workflow +
requested runtime, fills each slot by precedence

  explicit selector > root-bundle manifest > external registered
  artifacts > advisory suggestions

and reports five INDEPENDENT planning axes — structural, runtime,
policy, capacity, evidence — aggregated into `advisory_state` per the
C3 contract. A resolved plan is planning truth, never execution
authorization: `execution_authorized` is not an emittable value.

The resolver performs zero I/O — every input is a prepared,
stored-evidence view supplied by the caller. The LoRA admission gate
(`lora_resolve.check_loras`) is NOT invoked (it performs a filesystem
stat); this module projects its constants and rules conservatively and
labels unstatable evidence `file_stat_skipped`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from app.domain import dep_graph as g
from app.domain import lora_resolve
from app.domain.dep_graph import (
    BASE_MODEL,
    DEP_EVIDENCE_CONFLICT,
    DEP_EXCLUSIVE_VIOLATION,
    DEP_FAMILY_CONFLICT,
    DEP_MAX_CANDIDATES,
    DEP_MAX_CHARACTER_SLOTS,
    DEP_MAX_DEPTH,
    DEP_MAX_EDGES,
    DEP_MAX_NODES,
    DEP_VERSION_CONFLICT,
    LORA,
    LORA_IDENTITY,
    LORA_STYLE,
    MAX_DEPTH,
    PROV_BUNDLE,
    PROV_EXPLICIT,
    PROV_EXTERNAL,
    PROV_SUGGESTED,
    SLOT_AMBIGUOUS,
    SLOT_CONFLICTED,
    SLOT_LIMIT_EXCEEDED,
    SLOT_MISSING,
    SLOT_NEEDS_SHOT_CTX,
    SLOT_NO_FILL_EVIDENCE,
    SLOT_RESOLVED,
    SLOT_SELECTOR_REFUSED,
    SRC_CAP,
    SRC_CAST,
    SRC_FAMILY,
    SRC_WORKFLOW,
    Requirement,
    component_key,
    member_keys,
)

# --- plan axis values -------------------------------------------------
STRUCT_COMPLETE = "complete"
STRUCT_INCOMPLETE_MISSING = "incomplete_missing"
STRUCT_AMBIGUOUS = "ambiguous"
STRUCT_CONFLICTED = "conflicted"
STRUCT_UNKNOWN = "unknown"

POLICY_CLEAR = "clear"
POLICY_HELD = "held"
POLICY_RESTRICTED = "restricted"
POLICY_BLOCKED = "blocked"

CAP_WITHIN = "within_capacity"
CAP_EXCEEDS = "exceeds_capacity"
CAP_NA = "not_applicable"
CAP_UNKNOWN = "unknown"

EV_TESTED = "tested"
EV_DOCUMENTED = "documented"
EV_UNVERIFIED = "unverified"
EV_MISSING = "missing"

ADVISORY_COMPLETE = "plan_complete"
ADVISORY_INCOMPLETE = "plan_incomplete"
ADVISORY_BLOCKED = "plan_blocked"

# file_integrity labels — recorded facts only, never implied coverage.
FI_VERIFIED_INSTALL = "verified_at_install"
FI_RECORDED_IMPORT = "recorded_at_import"
FI_DECLARED = "declared"
FI_MISSING = "missing"

_CONF_ORDER = {EV_TESTED: 0, EV_DOCUMENTED: 1, EV_UNVERIFIED: 2,
               EV_MISSING: 3}
_PROV_ORDER = {PROV_EXPLICIT: 0, PROV_BUNDLE: 1, PROV_EXTERNAL: 2,
               PROV_SUGGESTED: 3}
_SRC_ORDER = {SRC_WORKFLOW: 0, SRC_FAMILY: 1, SRC_CAP: 2, SRC_CAST: 3,
              "request": 4}
_LORA_ROLES = (LORA, LORA_IDENTITY, LORA_STYLE)


@dataclass(frozen=True)
class ArtifactView:
    """Stored-evidence view of one model_artifacts row (+ install-row
    summary + U-4 inspection level). Prepared by the caller."""
    artifact_id: str
    kind: str
    family: str
    fmt: str
    role: str
    modality: str
    source_type: str
    managed_by: str
    content_sha256: str
    enabled: bool
    support_tier: str
    version: str
    manifest: tuple = ()                       # ManifestEntry tuple
    updated_at: str = ""
    install_status: str = ""                   # "" = no install row
    install_verified: bool = False
    install_sha256: str = ""                   # verified primary sha
    inspection_level: int = 0                  # U-4 domain_levels V1
    requires_inspection: bool = False


@dataclass(frozen=True)
class CompEntry:
    """A candidate component artifact for slot filling."""
    view: ArtifactView
    verdict_confidence: str = "missing"        # confidence chain grade
    verdict_reasons: tuple = ()
    policy_axis: str = "eligible"
    stale: bool = False


@dataclass(frozen=True)
class CastBinding:
    """One stored cast LoRA link (project-scoped evidence)."""
    character_id: str
    lora_key: str
    updated_at: str = ""
    digest: str = ""


@dataclass(frozen=True)
class CastContext:
    """Validated cast context supplied by the endpoint. `has_shot`
    distinguishes 'entire effective cast' from 'no cast context'."""
    has_shot: bool
    character_ids: tuple = ()
    bindings: tuple = ()                       # CastBinding tuple


@dataclass(frozen=True)
class Fill:
    component_key: str
    artifact_id: str
    role: str
    file_name: str | None
    provenance: str
    confidence: str
    file_integrity: str
    reasons: tuple = ()


@dataclass(frozen=True)
class SlotCandidate:
    artifact_id: str
    outcome: str                               # eligible|ineligible|ambiguous_member
    reasons: tuple = ()


@dataclass(frozen=True)
class SlotResult:
    requirement: Requirement
    outcome: str
    fills: tuple = ()
    candidates: tuple = ()
    reasons: tuple = ()
    limits: tuple = ()
    cycle_path: tuple = ()


@dataclass(frozen=True)
class DepPlan:
    artifact_id: str
    workflow_key: str
    runtime_key: str
    structural_state: str
    runtime_state: str
    policy_state: str
    capacity_state: str
    evidence_state: str
    advisory_state: str
    limitations: tuple
    reasons: tuple
    requirements: tuple                        # SlotResult tuple
    limits: dict
    limits_exceeded: tuple
    suggestions: tuple
    resolution_version: str
    advisory: bool = True                      # never execution authz


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _norm_role(view: ArtifactView) -> str:
    """Adapters store role as 'lora:<role>'; normalize to slot roles."""
    r = view.role or ""
    return r.replace("lora:", "lora_") if r.startswith("lora:") else r


def _view_roles(view: ArtifactView) -> set:
    return {_norm_role(view), *(m.role for m in view.manifest)}


def _file_integrity(member_sha: str, view: ArtifactView) -> str:
    """C2 — recorded facts only. `verified_at_install` requires the
    member's declared sha to BE the install row's verified primary —
    anything less proves coverage we cannot establish. Import rows get
    `recorded_at_import` (a hash of actual bytes read — NOT a
    comparison against an independently trusted expected digest)."""
    if view.source_type == "import":
        return FI_RECORDED_IMPORT if view.content_sha256 else FI_MISSING
    if not member_sha:
        return FI_MISSING
    if view.install_verified and member_sha == view.install_sha256:
        return FI_VERIFIED_INSTALL
    return FI_DECLARED


def _candidate_eligible(view: ArtifactView, comp: CompEntry,
                        req: Requirement,
                        root: ArtifactView) -> tuple:
    """Independent eligibility rules for one external candidate —
    every applicable reason retained, none short-circuited."""
    reasons: list[str] = []
    # "*" = explicitly cross-family (sub-component slots) — the role
    # binds, not the family. "" is treated the same; component slots
    # always set an explicit root family.
    fam = req.family_id
    is_lora = req.role in _LORA_ROLES
    if is_lora:
        if view.kind != "adapter":
            reasons.append("not_adapter")
        if req.base_family and view.family != req.base_family:
            reasons.append(DEP_FAMILY_CONFLICT)
    elif fam not in ("", "*") and view.family \
            and view.family != fam:
        reasons.append(DEP_FAMILY_CONFLICT)
    if (view.version and root.version and fam not in ("", "*")
            and view.family == fam
            and view.version != root.version):
        reasons.append(DEP_VERSION_CONFLICT)
    if req.fmts_accepted and view.fmt not in req.fmts_accepted:
        reasons.append("fmt_unaccepted")
    if not view.enabled:
        reasons.append("artifact_disabled")
    if view.support_tier == "planned":
        reasons.append("support_planned")
    if comp.policy_axis in ("blocked", "restricted"):
        reasons.append("policy_" + comp.policy_axis)
    if view.requires_inspection and view.inspection_level < 1:
        reasons.append("integrity_evidence_missing")
    return tuple(reasons)


def _component_requirements(root: ArtifactView, workflow, fam_spec,
                            cap_components, cap_fmts,
                            have_cast: bool) -> list[Requirement]:
    """Union of declared component slots — one slot per role, sources
    recorded. A generic optional `lora` slot is superseded once real
    cast bindings exist (they carry their own per-character slots)."""
    reqs: dict[str, Requirement] = {}

    def put(role: str, required: bool, source: str) -> None:
        if role not in g.COMPONENT_ROLES:
            return
        if role in reqs:
            ex = reqs[role]
            if required and not ex.required:
                reqs[role] = Requirement(
                    ex.requirement_id, role, True, ex.source,
                    ex.family_id, ex.base_family, ex.fmts_accepted,
                    ex.binds_character_id, ex.selected_artifact_id,
                    ex.exclusive, ex.depth)
            return
        reqs[role] = Requirement(
            requirement_id=f"{source}:{role}:{root.family}",
            role=role, required=required, source=source,
            family_id="" if role in _LORA_ROLES else root.family,
            base_family=root.family if role in _LORA_ROLES else "",
            fmts_accepted=tuple(cap_fmts or ()))

    for r in workflow.required_components:
        put(r, True, SRC_WORKFLOW)
    for r in workflow.optional_components:
        if r == LORA and have_cast:
            continue                 # cast slots supersede generic lora
        put(r, False, SRC_WORKFLOW)
    for r in getattr(fam_spec, "component_roles", ()) or ():
        put(r, True, SRC_FAMILY)
    for r in cap_components or ():
        put(r, True, SRC_CAP)
    return list(reqs.values())


def _lora_slots(cast: CastContext, base_family: str,
                key_to_artifact: dict,
                spec_role) -> tuple[list[Requirement], tuple[str, ...]]:
    """Cast-derived LoRA slots — one requirement per stored binding
    entry, pre-pinned by the cast link itself (explicit provenance).
    Identity vs style comes from the catalog spec's declared
    `lora_role`; an undeclared role resolves to the generic `lora`
    slot and is flagged, never guessed."""
    reqs: list[Requirement] = []
    limits: list[str] = []
    if len(cast.bindings) > g.MAX_CHARACTER_SLOTS:
        limits.append(DEP_MAX_CHARACTER_SLOTS)
    for i, b in enumerate(cast.bindings[:g.MAX_CHARACTER_SLOTS]):
        spec = spec_role(b.lora_key)
        role = LORA_IDENTITY if spec == "identity" else (
            LORA_STYLE if spec == "style" else LORA)
        reqs.append(Requirement(
            requirement_id=f"cast:{b.character_id}:{b.lora_key}:{i}",
            role=role, required=True, source=SRC_CAST,
            base_family=base_family,
            binds_character_id=b.character_id,
            selected_artifact_id=key_to_artifact.get(b.lora_key,
                                                   b.lora_key),
            exclusive=False))
    return reqs, tuple(limits)


def _slot_resolve(req: Requirement, root: ArtifactView,
                  root_confidence: str, pool: dict[str, CompEntry],
                  root_mk, include_stale: bool = True,
                  root_comp: CompEntry | None = None) -> SlotResult:
    """Fill one slot by precedence — explicit > bundle > external >
    suggestion. Ambiguity is a first-class outcome, never a pick."""
    reasons: list[str] = []
    candidates: list[SlotCandidate] = []
    fills: list[Fill] = []
    conflicts_set = set(root_mk.conflicts)

    # ---- base_model: the root model itself satisfies the slot -------
    if req.role == BASE_MODEL and root.kind == "model":
        fills.append(Fill(component_key(root.artifact_id),
                          root.artifact_id, req.role, None,
                          PROV_BUNDLE, root_confidence,
                          _file_integrity(root.content_sha256, root),
                          ()))
        return SlotResult(req, SLOT_RESOLVED, fills=tuple(fills),
                          reasons=reasons)

    # ---- 1. explicit selector (request or cast pin) -----------------
    if req.selected_artifact_id:
        sel = req.selected_artifact_id
        comp = root_comp if (sel == root.artifact_id
                             and root_comp is not None) \
            else pool.get(sel)
        if comp is None:
            return SlotResult(req, SLOT_SELECTOR_REFUSED,
                              reasons=("selector_unknown_artifact",))
        view = comp.view
        refuse = _candidate_eligible(view, comp, req, root)
        if req.role in _LORA_ROLES:
            if view.kind != "adapter":
                refuse = (*refuse, "selector_role_mismatch")
            nr = _norm_role(view)
            if req.role in (LORA_IDENTITY, LORA_STYLE) \
                    and nr != req.role:
                refuse = (*refuse, "selector_role_mismatch")
            if nr == LORA:
                refuse = (*refuse, "lora_role_undeclared")
        elif req.role not in _view_roles(view):
            refuse = (*refuse, "selector_role_mismatch")
        if refuse:
            return SlotResult(req, SLOT_SELECTOR_REFUSED,
                              candidates=(SlotCandidate(
                                  sel, "ineligible", refuse),),
                              reasons=refuse)
        integrity = _file_integrity(view.content_sha256, view)
        fills.append(Fill(component_key(sel), sel, req.role, None,
                          PROV_EXPLICIT, comp.verdict_confidence,
                          integrity,
                          ("file_stat_skipped",)
                          if req.role in _LORA_ROLES else ()))
        candidates.append(SlotCandidate(sel, "eligible", ()))
        # Explicit precedence is FINAL — an honored selector fills the
        # slot; bundle and external paths do not add competing fills.
        return SlotResult(req, SLOT_RESOLVED, fills=tuple(fills),
                          candidates=tuple(candidates), reasons=reasons)

    # ---- 2. root-bundle manifest members ----------------------------
    root_mk_map = root_mk or member_keys(())
    for i, m in enumerate(root.manifest):
        if m.role != req.role:
            continue
        if i in conflicts_set:
            reasons.append(DEP_EVIDENCE_CONFLICT)
            continue
        integrity = _file_integrity(m.sha256, root)
        fills.append(Fill(component_key(root.artifact_id,
                                        root_mk_map.keys[i]),
                          root.artifact_id, req.role, m.file_name,
                          PROV_BUNDLE, root_confidence, integrity, ()))

    if fills:
        if req.exclusive and len(fills) > 1:
            return SlotResult(req, SLOT_CONFLICTED, fills=tuple(fills),
                              candidates=tuple(candidates),
                              reasons=(*reasons,
                                       DEP_EXCLUSIVE_VIOLATION))
        return SlotResult(req, SLOT_RESOLVED, fills=tuple(fills),
                          candidates=tuple(candidates), reasons=reasons)

    # ---- 3. external registered candidates --------------------------
    eligible: list[tuple] = []
    for aid, comp in sorted(pool.items()):
        view = comp.view
        if aid in (root.artifact_id, req.selected_artifact_id):
            continue
        if req.role in _LORA_ROLES:
            if view.kind != "adapter":
                continue
            nr = _norm_role(view)
            if req.role != LORA and nr != req.role:
                continue
        elif req.role not in _view_roles(view):
            continue
        # Stale-verdict components mirror the U-5a filter: optimism is
        # filtered, blocks stay visible, and nothing is silently picked.
        if comp.stale and not include_stale \
                and comp.policy_axis != "blocked":
            candidates.append(SlotCandidate(
                aid, "ineligible", ("stale_verdict_filtered",)))
            continue
        refuse = _candidate_eligible(view, comp, req, root)
        if refuse:
            candidates.append(SlotCandidate(aid, "ineligible", refuse))
            continue
        candidates.append(SlotCandidate(aid, "eligible", ()))
        if view.manifest:
            mk = member_keys(view.manifest)
            for i, m in enumerate(view.manifest):
                if m.role != req.role or i in set(mk.conflicts):
                    continue
                eligible.append((aid, view, comp, m, mk.keys[i]))
        else:
            eligible.append((aid, view, comp, None, None))
    if len(eligible) > g.MAX_CANDIDATES_PER_SLOT:
        return SlotResult(req, SLOT_LIMIT_EXCEEDED,
                          candidates=tuple(candidates),
                          limits=(DEP_MAX_CANDIDATES,), reasons=reasons)
    if len(eligible) > 1:
        return SlotResult(req, SLOT_AMBIGUOUS,
                          candidates=tuple(
                              SlotCandidate(
                                  c.artifact_id,
                                  "ambiguous_member"
                                  if c.outcome == "eligible"
                                  else c.outcome, c.reasons)
                              for c in candidates),
                          reasons=reasons)
    if len(eligible) == 1:
        aid, view, comp, m, mk = eligible[0]
        integrity = _file_integrity(
            m.sha256 if m else view.content_sha256, view)
        fills.append(Fill(component_key(aid, mk), aid, req.role,
                          m.file_name if m else None, PROV_EXTERNAL,
                          comp.verdict_confidence, integrity, ()))
        return SlotResult(req, SLOT_RESOLVED, fills=tuple(fills),
                          candidates=tuple(candidates), reasons=reasons)

    if candidates:
        return SlotResult(req, SLOT_MISSING,
                          candidates=tuple(candidates), reasons=reasons)
    return SlotResult(req, SLOT_NO_FILL_EVIDENCE,
                      candidates=tuple(candidates), reasons=reasons)


def _sub_requirements(fill: Fill, req: Requirement,
                      fam_spec_for, depth: int) -> list[Requirement]:
    """A filled whole-artifact model may itself declare family
    component_roles its own manifest doesn't cover — emit bounded
    sub-requirements (cycle/depth guarded by the caller)."""
    out: list[Requirement] = []
    if fill.provenance not in (PROV_EXTERNAL, PROV_EXPLICIT) \
            or fill.file_name is not None:
        return out
    spec = fam_spec_for(fill.artifact_id)
    if spec is None:
        return out
    for role in getattr(spec, "component_roles", ()) or ():
        out.append(Requirement(
            requirement_id=f"sub:{req.requirement_id}:{role}",
            role=role, required=True, source=SRC_FAMILY,
            # cross-family-eligible: sub-components legitimately come
            # from aux_* families — the role, not the family, binds.
            family_id="*", depth=depth))
    return out


def resolve_plan(*, root: ArtifactView, root_confidence: str,
                 root_verdict, workflow, fam_spec, cap, runtime_key,
                 root_runtime_state, pool: dict[str, CompEntry],
                 cast: CastContext, selectors: dict[str, str],
                 key_to_artifact: dict, spec_role, fam_spec_for,
                 lora_policy_held: bool, include_stale: bool,
                 fingerprint_inputs: dict) -> DepPlan:
    """Deterministic advisory resolution — pure stored evidence."""
    cap_components = getattr(cap, "components_required", ()) if cap else ()
    cap_fmts: list[str] = []
    fam_cap = cap.families.get(root.family) if cap is not None else None
    if fam_cap is not None:
        cap_fmts = list(fam_cap.fmts_tested) + [
            f for f in fam_cap.fmts_documented
            if f not in fam_cap.fmts_tested]
        if getattr(fam_cap, "components_required", ()):
            cap_components = fam_cap.components_required

    lora_reqs, lora_limits = _lora_slots(
        cast, root.family, key_to_artifact, spec_role)
    reqs = _component_requirements(root, workflow, fam_spec,
                                   cap_components, cap_fmts,
                                   have_cast=bool(lora_reqs))
    # C5 — a character supplied WITHOUT shot context can never imply
    # identity-LoRA admission: emit a placeholder slot per character
    # that resolves to needs_shot_context, never a bound adapter.
    if cast.character_ids and not cast.has_shot:
        for cid in cast.character_ids:
            reqs.append(Requirement(
                requirement_id=f"cast:{cid}:unbound",
                role=LORA_IDENTITY, required=True, source=SRC_CAST,
                binds_character_id=cid, exclusive=False))
    reqs.extend(lora_reqs)
    for i, (role, aid) in enumerate(sorted(selectors.items())):
        # A selector pins the EXISTING slot for its role — it never
        # spawns a duplicate requirement. Only an unmatched role gets
        # a request-sourced slot of its own.
        target = next((r for r in reqs if r.role == role
                       and not r.selected_artifact_id), None)
        if target is not None:
            reqs[reqs.index(target)] = Requirement(
                target.requirement_id, target.role, target.required,
                target.source, target.family_id, target.base_family,
                target.fmts_accepted, target.binds_character_id, aid,
                target.exclusive, target.depth)
        else:
            reqs.append(Requirement(
                requirement_id=f"request:{role}:{i}", role=role,
                required=True, source="request", family_id=root.family,
                selected_artifact_id=aid))

    root_mk = member_keys(root.manifest)
    root_comp = CompEntry(
        root, root_confidence, (),
        getattr(root_verdict, "policy_axis", None) or "eligible")

    # ---- bounded iterative resolution --------------------------------
    slots: list[SlotResult] = []
    pending = list(reqs)
    spawn_chain: dict[str, tuple] = {}        # req_id -> ancestor artifacts
    edges: list[tuple[str, str]] = []
    depth_exceeded = False
    spawned_count = 0
    while pending:
        req = pending.pop(0)
        if len(reqs) + spawned_count > g.MAX_REQUIREMENTS:
            slots.append(SlotResult(req, SLOT_LIMIT_EXCEEDED,
                                    limits=(DEP_MAX_NODES,)))
            continue
        if req.source == SRC_CAST and req.role == LORA_IDENTITY \
                and not cast.has_shot:
            slots.append(SlotResult(
                req, SLOT_NEEDS_SHOT_CTX,
                reasons=("identity_binding_requires_shot_context",)))
            continue
        chain = spawn_chain.get(req.requirement_id, ())
        if root.artifact_id in chain:
            slots.append(SlotResult(
                req, SLOT_CONFLICTED, reasons=(g.DEP_CYCLE,),
                cycle_path=chain + (root.artifact_id,)))
            continue
        res = _slot_resolve(req, root, root_confidence, pool, root_mk,
                            include_stale, root_comp)
        slots.append(res)
        for f in res.fills:
            # artifact-chain cycle: the filler appears in this slot's
            # own ancestor chain → real back-edge, reported verbatim.
            if f.artifact_id in chain:
                slots.append(SlotResult(
                    Requirement(f"{req.requirement_id}:cycle",
                                req.role, True, SRC_FAMILY),
                    SLOT_CONFLICTED, reasons=(g.DEP_CYCLE,),
                    cycle_path=chain + (f.artifact_id,)))
                continue
            if req.depth + 1 >= MAX_DEPTH:
                depth_exceeded = True
                continue
            subs = _sub_requirements(f, req, fam_spec_for,
                                     req.depth + 1)
            for s in subs:
                edges.append((req.requirement_id, s.requirement_id))
                spawn_chain[s.requirement_id] = chain + (f.artifact_id,)
                pending.append(s)
                spawned_count += 1

    limits: list[str] = [*lora_limits]
    if len(reqs) + spawned_count > g.MAX_REQUIREMENTS:
        limits.append(DEP_MAX_NODES)
    if len(edges) > g.MAX_EDGES:
        limits.append(DEP_MAX_EDGES)
    if depth_exceeded:
        limits.append(DEP_MAX_DEPTH)
    for s in slots:
        limits.extend(s.limits)

    # ---- five independent axes ---------------------------------------
    missing_req = any(
        s.requirement.required and s.outcome in
        (SLOT_MISSING, SLOT_NO_FILL_EVIDENCE) for s in slots)
    ambiguous_req = any(s.outcome == SLOT_AMBIGUOUS for s in slots)
    conflicted = any(s.outcome == SLOT_CONFLICTED for s in slots)
    needs_ctx = any(s.outcome == SLOT_NEEDS_SHOT_CTX for s in slots)
    limit_hit = bool(set(limits))

    if conflicted:
        structural = STRUCT_CONFLICTED
    elif limit_hit:
        structural = STRUCT_UNKNOWN
    elif missing_req:
        structural = STRUCT_INCOMPLETE_MISSING
    elif ambiguous_req:
        structural = STRUCT_AMBIGUOUS
    elif needs_ctx:
        structural = STRUCT_UNKNOWN
    else:
        structural = STRUCT_COMPLETE

    # policy — LoRA holds gate the whole plan; candidate policy applies
    policy = POLICY_CLEAR
    if (lora_reqs and lora_policy_held) \
            or getattr(workflow, "policy", "") == "lora_hold":
        policy = POLICY_HELD
    for comp in pool.values():
        if comp.policy_axis == "blocked":
            policy = POLICY_BLOCKED
        elif comp.policy_axis == "restricted" \
                and policy == POLICY_CLEAR:
            policy = POLICY_RESTRICTED
    if getattr(root_verdict, "policy_axis", "eligible") == "blocked":
        policy = POLICY_BLOCKED
    elif getattr(root_verdict, "policy_axis", "eligible") \
            == "restricted" and policy == POLICY_CLEAR:
        policy = POLICY_RESTRICTED

    # capacity — admission constants projected, never weakened. Every
    # cast slot is one adapter APPLICATION; identity applications are
    # counted per binding entry, never deduped to leniency.
    lora_apps = [s for s in slots if s.requirement.role in _LORA_ROLES]
    unique_adapters = {r.selected_artifact_id for r in reqs
                       if r.role in _LORA_ROLES}
    identity_apps = sum(1 for s in lora_apps
                        if s.requirement.role == LORA_IDENTITY)
    if not lora_apps:
        capacity = CAP_NA
    else:
        capacity = CAP_EXCEEDS if (
            len(unique_adapters) > lora_resolve.LORA_MAX_PER_JOB
            or len(lora_apps) > lora_resolve.LORA_MAX_PER_JOB
            or identity_apps > 1) else CAP_WITHIN

    # evidence — worst-of confidence chain + integrity + inspection
    grades = [root_confidence]
    for s in slots:
        for f in s.fills:
            grades.append(f.confidence)
            if f.file_integrity == FI_MISSING:
                grades.append(EV_MISSING)
            elif f.file_integrity == FI_DECLARED:
                grades.append(EV_UNVERIFIED)
    evidence = max(grades, key=lambda x: _CONF_ORDER[x])

    runtime_state = root_runtime_state or "unevaluated"

    limitations: list[str] = []
    if runtime_state == "candidate_limited":
        limitations.append("runtime_candidate_limited")
    if evidence == EV_DOCUMENTED:
        limitations.append("documented_support")
    if evidence == EV_UNVERIFIED:
        limitations.append("unverified_evidence")
    if root.requires_inspection and root.inspection_level < 1:
        limitations.append("integrity_evidence_missing")
    for s in slots:
        for f in s.fills:
            if f.file_integrity in (FI_DECLARED, FI_MISSING):
                limitations.append(f"integrity_{f.file_integrity}")
            if "file_stat_skipped" in f.reasons:
                limitations.append("file_stat_skipped")

    # ---- C3 deterministic aggregation --------------------------------
    blocked = (policy != POLICY_CLEAR
               or runtime_state == "candidate_blocked"
               or structural == STRUCT_CONFLICTED
               or capacity == CAP_EXCEEDS)
    incomplete = (structural in (STRUCT_INCOMPLETE_MISSING,
                                 STRUCT_AMBIGUOUS, STRUCT_UNKNOWN)
                  or runtime_state in ("candidate_unknown",
                                       "unevaluated")
                  or (capacity == CAP_UNKNOWN and bool(lora_apps))
                  or evidence == EV_MISSING)
    advisory = (ADVISORY_BLOCKED if blocked else
                ADVISORY_INCOMPLETE if incomplete else
                ADVISORY_COMPLETE)

    reasons = list(dict.fromkeys(
        r for s in slots for r in s.reasons))

    suggestions = tuple(
        s.requirement.role for s in slots
        if not s.requirement.required and s.outcome in
        (SLOT_MISSING, SLOT_NO_FILL_EVIDENCE))

    # ---- C4 fingerprint — every evaluated input ----------------------
    fp = dict(fingerprint_inputs)
    fp["slots"] = [
        {"req": s.requirement.requirement_id,
         "role": s.requirement.role, "outcome": s.outcome,
         "fills": [{"ck": f.component_key, "prov": f.provenance,
                    "conf": f.confidence, "fi": f.file_integrity}
                   for f in s.fills],
         "cands": [{"aid": c.artifact_id, "out": c.outcome}
                   for c in s.candidates]}
        for s in slots]
    fp["limits"] = sorted(set(limits))
    fp["dep_ruleset"] = g.DEP_RULESET_VERSION
    resolution_version = "dep:v1:" + hashlib.sha256(
        _canon(fp).encode()).hexdigest()[:16]

    return DepPlan(
        artifact_id=root.artifact_id,
        workflow_key=workflow.workflow_key,
        runtime_key=runtime_key,
        structural_state=structural,
        runtime_state=runtime_state,
        policy_state=policy,
        capacity_state=capacity,
        evidence_state=evidence,
        advisory_state=advisory,
        limitations=tuple(dict.fromkeys(limitations)),
        reasons=tuple(reasons),
        requirements=tuple(slots),
        limits=dict(g.LIMITS),
        limits_exceeded=tuple(sorted(set(limits))),
        suggestions=suggestions,
        resolution_version=resolution_version)


def view_from_row(row, manifest, install_status="",
                  install_verified=False, install_sha256="",
                  inspection_level=0,
                  requires_inspection=False) -> ArtifactView:
    """Build the resolver's stored-evidence view of one row — the
    endpoint prepares these; the resolver never sees ORM objects."""
    return ArtifactView(
        artifact_id=row.artifact_id, kind=row.kind or "model",
        family=row.family or "", fmt=row.fmt or "", role=row.role or "",
        modality=row.modality or "", source_type=row.source_type or "",
        managed_by=row.managed_by or "",
        content_sha256=row.content_sha256 or "",
        enabled=bool(row.enabled), support_tier=row.support_tier or "",
        version=row.version or "", manifest=tuple(manifest),
        updated_at=str(row.updated_at or ""),
        install_status=install_status, install_verified=install_verified,
        install_sha256=install_sha256,
        inspection_level=inspection_level,
        requires_inspection=requires_inspection)


__all__ = [
    "ADVISORY_BLOCKED",
    "ADVISORY_COMPLETE",
    "ADVISORY_INCOMPLETE",
    "CAP_EXCEEDS",
    "CAP_NA",
    "CAP_UNKNOWN",
    "CAP_WITHIN",
    "EV_DOCUMENTED",
    "EV_MISSING",
    "EV_TESTED",
    "EV_UNVERIFIED",
    "FI_DECLARED",
    "FI_MISSING",
    "FI_RECORDED_IMPORT",
    "FI_VERIFIED_INSTALL",
    "POLICY_BLOCKED",
    "POLICY_CLEAR",
    "POLICY_HELD",
    "POLICY_RESTRICTED",
    "STRUCT_AMBIGUOUS",
    "STRUCT_COMPLETE",
    "STRUCT_CONFLICTED",
    "STRUCT_INCOMPLETE_MISSING",
    "STRUCT_UNKNOWN",
    "ArtifactView",
    "CastBinding",
    "CastContext",
    "CompEntry",
    "DepPlan",
    "Fill",
    "SlotCandidate",
    "SlotResult",
    "resolve_plan",
    "view_from_row",
]
