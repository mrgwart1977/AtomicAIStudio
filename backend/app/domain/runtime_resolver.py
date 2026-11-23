"""U-5a — runtime-candidate resolver (advisory, read-only).

Evaluates which runtimes could *statically* satisfy a workflow request
for a registered artifact. Architecture follows the accepted C1
correction: every applicable condition is evaluated INDEPENDENTLY into
a finding set, then aggregated by severity — an early unknown/limited
finding can never conceal a hard block.

  aggregation precedence:  block > unknown > limited > compatible_static

Staleness is a separate marker — it downgrades only the optimistic
state and can never weaken or override a block. A missing verdict can
never produce `candidate_compatible_static`.

This module computes NOTHING about execution: a candidate is a static
evidence claim for plan review. Execution authorization is a separate,
existing gate (user action + GPU admission + holds) that U-5 cannot
emit, mutate, or approach.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain import runtime_caps
from app.domain.runtime_caps import RuntimeCapabilityDecl
from app.domain.workflow_defs import WorkflowDef

# Candidate states — the ONLY four values ever emitted.
COMPATIBLE_STATIC = "candidate_compatible_static"
LIMITED = "candidate_limited"
BLOCKED = "candidate_blocked"
UNKNOWN = "candidate_unknown"

_STATE_ORDER = {COMPATIBLE_STATIC: 0, LIMITED: 1, UNKNOWN: 2, BLOCKED: 3}
_BINDING_ORDER = {"catalog_default": 0, "compatible_adapter": 1,
                  "alternate_runtime": 2}

_SEV_ORDER = {"block": 0, "unknown": 1, "limited": 2}


@dataclass(frozen=True)
class Finding:
    """One independently evaluated condition. `severity` feeds the
    aggregate; `reasons` are retained verbatim regardless of outcome."""
    severity: str                     # block | unknown | limited
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterEvidence:
    """What a registered adapter proves about itself (C2): identity via
    the registry, task coverage via its own profile. Family support is a
    DECLARED capability property (FamilyCap), never an adapter claim."""
    adapter_id: str
    registered: bool
    tasks: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogBinding:
    """The catalog ModelSpec binding for the artifact (None for
    import-sourced artifacts, which have no catalog default)."""
    adapter_id: str
    runtime_key: str
    compatible_adapters: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerdictView:
    """The stored U-4 verdict axes + live staleness for one
    (artifact, runtime_key). `stale` is computed by the caller (the
    same live fingerprint check GET /compat performs)."""
    runtime_key: str
    arch_axis: str
    arch_confidence: str | None
    runtime_axis: str
    hardware_axis: str
    policy_axis: str
    confidence: str
    reasons: tuple[str, ...]
    missing: tuple[str, ...]
    stale: bool
    domain_levels: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ResolverContext:
    """Prepared, stored-evidence inputs — the resolver never performs
    I/O, never queries the DB, never touches the registry globals."""
    decls: dict[str, RuntimeCapabilityDecl]
    adapters: dict[str, AdapterEvidence]          # registered only
    catalog: CatalogBinding | None
    workflow: WorkflowDef
    # C3 inspection evidence — the U-4 `domain_levels.inspection`
    # semantics verbatim. V1 = a completed U-2 import report with a
    # recognized container/format. It is RECOGNITION evidence only —
    # it is never a claim of cryptographic integrity verification, and
    # nothing in this module may assert one. Catalog rows legitimately
    # score 0 (their files were never inspected) and take no penalty;
    # only `requires_inspection` (import-sourced) rows with level 0
    # carry `integrity_evidence_missing` — the record that attests a
    # real file was opened and its format recognized is absent.
    inspection_level: int                         # 0 | 1 (U-4 V1 cap)
    requires_inspection: bool                     # source_type=="import"


@dataclass(frozen=True)
class Candidate:
    """One (runtime_key, adapter) candidacy — a static evidence claim,
    never an execution offer. `rank` is assigned after stable sort."""
    runtime_key: str
    adapter_id: str | None
    binding_kind: str                    # catalog_default |
    # compatible_adapter | alternate_runtime
    state: str                           # the four C1 states
    verdict_stale: bool
    reasons: tuple[str, ...]
    missing: tuple[str, ...]
    has_verdict: bool


def _findings(view_fam: str, view_kind: str, v: VerdictView | None,
              binding_kind: str, decl: RuntimeCapabilityDecl | None,
              adapter: AdapterEvidence | None, adapter_id: str | None,
              ctx: ResolverContext) -> tuple[list[Finding], tuple[str, ...]]:
    """Independent evaluation — every applicable condition runs."""
    f: list[Finding] = []
    missing: tuple[str, ...] = ()

    # F2 — missing verdict: unknown evidence, never support
    if v is None:
        f.append(Finding("unknown", ("verdict_missing",)))
    else:
        missing = v.missing
        # F3 — policy hold (authoritative block, independently evaluated)
        if v.policy_axis in ("blocked", "restricted"):
            pol = tuple(r for r in v.reasons
                        if r in ("hold_gated", "license_restricted")) \
                or ("policy_blocked",)
            f.append(Finding("block", pol))
        # F4/F5 — architecture
        if v.arch_axis == "unsupported":
            f.append(Finding("block",
                             tuple(r for r in v.reasons if r in (
                                 "family_unsupported", "fmt_unsupported",
                                 "mode_unsupported", "components_missing"))
                             or ("arch_unsupported",)))
        elif v.arch_axis == "unknown":
            f.append(Finding("unknown",
                             tuple(r for r in v.reasons if r in (
                                 "family_unrecognized", "arch_conflict",
                                 "dtype_unverified"))
                             or ("arch_unknown",)))
        # F6–F9 — runtime install/verification axis
        if v.runtime_axis == "planned":
            f.append(Finding("block", ("runtime_planned",)))
        elif v.runtime_axis == "not_installed":
            f.append(Finding("block", ("runtime_not_installed",)))
        elif v.runtime_axis == "installed_unverified":
            f.append(Finding("limited", ("runtime_version_unverified",)))
        elif v.runtime_axis == "unknown":
            f.append(Finding("unknown", ("runtime_unknown",)))
        # F12–F14 — hardware axis
        if v.hardware_axis == "insufficient":
            f.append(Finding("block",
                             tuple(r for r in v.reasons if r in (
                                 "vram_below_min", "no_backend",
                                 "ram_below_min", "cpu_only"))
                             or ("hardware_insufficient",)))
        elif v.hardware_axis == "limited":
            f.append(Finding("limited",
                             tuple(r for r in v.reasons if r in (
                                 "vram_headroom_tight", "vram_unmeasured"))
                             or ("hardware_limited",)))
        elif v.hardware_axis == "unknown":
            f.append(Finding("unknown",
                             tuple(r for r in v.reasons if r in (
                                 "requirements_undeclared",))
                             or ("hardware_unknown",)))
        # F15 — documented/unverified support caps at limited
        if v.arch_axis == "supported" and (
                v.arch_confidence in ("documented", "unverified")
                or v.confidence in ("documented", "unverified")):
            f.append(Finding("limited", ("runtime_path_unverified",)))

    # F6b — a PLANNED runtime is blocked on static decl evidence alone;
    # it does not need a verdict to be ruled out (covers the
    # missing-verdict + planned-runtime case).
    if decl is not None and decl.planned:
        f.append(Finding("block", ("runtime_planned",)))

    # C3 — inspection evidence is an independent artifact condition:
    # an import-sourced artifact whose V1 inspection record is absent
    # is limited regardless of verdict presence. Catalog rows score 0
    # legitimately and are never penalized for it. `inspection_level`
    # encodes format recognition only — never integrity verification.
    if ctx.requires_inspection and ctx.inspection_level < 1:
        f.append(Finding("limited", ("integrity_evidence_missing",)))

    # F10/F11 — adapter implementation evidence (independent of axes)
    if adapter_id is None or adapter is None or not adapter.registered:
        f.append(Finding("block", ("adapter_missing",)))
    elif ctx.workflow.required_modes and not set(
            ctx.workflow.required_modes) & set(adapter.tasks):
        f.append(Finding("block", ("mode_not_in_adapter",)))

    # F16/F17 — binding conflicts and unverified bindings
    if decl is not None and binding_kind == "catalog_default" \
            and not _decl_claims(decl, view_kind, view_fam):
        f.append(Finding("limited", ("binding_unverified",)))
    # compatible_adapters are contract-verified catalog metadata —
    # never execution-tested unless separate evidence exists (C2).
    if binding_kind == "compatible_adapter":
        f.append(Finding("limited", ("alternate_unverified_exec",)))
    if binding_kind == "alternate_runtime":
        f.append(Finding("limited", ("binding_conflict",)))

    return f, missing


def _decl_claims(decl: RuntimeCapabilityDecl, kind: str,
                 family_id: str) -> bool:
    return runtime_caps.adapter_cap_key(kind, family_id) in decl.families


def _aggregate(findings: list[Finding], stale: bool
               ) -> tuple[str, tuple[str, ...]]:
    """Deterministic severity aggregation — block > unknown > limited >
    compatible_static. ALL reasons retained in canonical order."""
    severities = {x.severity for x in findings}
    if "block" in severities:
        state = BLOCKED
    elif "unknown" in severities:
        state = UNKNOWN
    elif "limited" in severities:
        state = LIMITED
    else:
        state = COMPATIBLE_STATIC

    reasons = list(dict.fromkeys(
        r for x in findings for r in x.reasons))
    # Staleness is a separate marker: it only dampens the optimistic
    # state — never conceals, weakens, or upgrades a block/unknown.
    if stale and state == COMPATIBLE_STATIC:
        state = LIMITED
        reasons.append("verdict_stale")
    return state, tuple(reasons)


def resolve_candidates(
    *,
    artifact_id: str,
    family_id: str | None,
    kind: str,
    verdicts: list[VerdictView],
    ctx: ResolverContext,
) -> list[Candidate]:
    """Ranked candidates for one artifact + workflow. Deterministic —
    same stored inputs always produce the same ordered output."""
    fam = family_id or ""
    by_runtime = {v.runtime_key: v for v in verdicts}
    out: list[Candidate] = []

    def emit(runtime_key: str, adapter_id: str | None,
             binding_kind: str, decl: RuntimeCapabilityDecl | None) -> None:
        adapter = ctx.adapters.get(adapter_id) if adapter_id else None
        v = by_runtime.get(runtime_key)
        findings, missing = _findings(
            fam, kind, v, binding_kind, decl, adapter, adapter_id, ctx)
        state, reasons = _aggregate(findings, v.stale if v else False)
        out.append(Candidate(
            runtime_key=runtime_key, adapter_id=adapter_id,
            binding_kind=binding_kind, state=state,
            verdict_stale=bool(v.stale) if v else False,
            reasons=reasons, missing=missing, has_verdict=v is not None))

    # Catalog-default + contract-verified compatible adapters — the
    # catalog binding is the execution default and always ranks first.
    emitted_adapters: set[str] = set()
    cat = ctx.catalog
    if cat is not None:
        decl = ctx.decls.get(cat.runtime_key)
        emit(cat.runtime_key, cat.adapter_id, "catalog_default", decl)
        emitted_adapters.add(cat.adapter_id)
        for aid in cat.compatible_adapters:
            if aid not in emitted_adapters:
                emit(cat.runtime_key, aid, "compatible_adapter", decl)
                emitted_adapters.add(aid)

    # Alternate runtimes: every decl that claims this artifact's
    # family cap key contributes candidates for each declared adapter
    # that is not already emitted — advisory only, ranked last.
    cap_key = runtime_caps.adapter_cap_key(kind, fam)
    for key in sorted(ctx.decls):
        decl = ctx.decls[key]
        if cat is not None and key == cat.runtime_key:
            continue
        if cap_key not in decl.families:
            continue
        any_adapter = False
        for aid in decl.adapter_ids:
            if aid in emitted_adapters:
                continue
            emitted_adapters.add(aid)
            any_adapter = True
            emit(key, aid, "alternate_runtime", decl)
        if not any_adapter:
            # Declared capability with no adapter implementation —
            # adapter_missing, never implied operational.
            emit(key, None, "alternate_runtime", decl)

    out.sort(key=lambda c: (
        _STATE_ORDER[c.state],
        _BINDING_ORDER.get(c.binding_kind, 9),
        c.runtime_key, c.adapter_id or ""))
    return [
        Candidate(
            runtime_key=c.runtime_key, adapter_id=c.adapter_id,
            binding_kind=c.binding_kind, state=c.state,
            verdict_stale=c.verdict_stale, reasons=c.reasons,
            missing=c.missing, has_verdict=c.has_verdict)
        for c in out
    ]


__all__ = [
    "BLOCKED",
    "COMPATIBLE_STATIC",
    "LIMITED",
    "UNKNOWN",
    "AdapterEvidence",
    "Candidate",
    "CatalogBinding",
    "Finding",
    "ResolverContext",
    "VerdictView",
    "resolve_candidates",
]
