"""U-4b — five-axis compatibility verdicts + fpv1 fingerprints.

Pure evaluation over STORED evidence only: a classified ArtifactView
(U-4a), runtime capability declarations (U-4a), runtime_installs state,
a hardware probe dict, and residency settings. Nothing here opens,
hashes, or parses a model file, executes a runtime, touches a
discovery candidate, or grants inference eligibility.

Axes (independent by contract):
  arch_axis     supported | unsupported | unknown
  runtime_axis  operational | installed_unverified | not_installed |
                planned | unknown
  hardware_axis fits | limited | insufficient | unknown
  policy_axis   eligible | restricted | blocked
  verification  domain_levels_json {inspection ≤1, family ≤2,
                runtime_static ≤3, hardware ≤4} — level is the max,
                never a bare "verified"; V5 does not exist.

A missing runtime never becomes an architecture incompatibility;
insufficient VRAM never becomes unsupported architecture; unknown
precision or missing evidence never becomes confirmed support.

Volatility contract: verdict staleness is derived ONLY from
inputs_fingerprint (stable configuration). vram_free_bytes /
memory.available_bytes / probed_at are point-in-time samples — they
feed advisory warnings and evidence_json, never the fingerprint.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass

from app.domain import model_families
from app.domain.model_families import ArtifactView, Classification
from app.domain.runtime_caps import (
    RUNTIME_CAPS,
    RuntimeCapabilityDecl,
    adapter_cap_key,
    get_decl,
    ruleset_version,
)

# Bump on ANY verdict semantics change — feeds the fingerprint so a
# ruleset change invalidates stored verdicts.
VERDICT_SCHEMA_VERSION = 1

# V5 (runtime smoke test) is deferred — levels can never exceed 4.
MAX_LEVEL = 4

ARCH_AXES = ("supported", "unsupported", "unknown")
RUNTIME_AXES = ("operational", "installed_unverified", "not_installed",
                "planned", "unknown")
HARDWARE_AXES = ("fits", "limited", "insufficient", "unknown")
POLICY_AXES = ("eligible", "restricted", "blocked")
OVERALLS = ("compatible", "limited", "blocked", "incompatible", "unknown")

# Fraction of measured total VRAM treated as usable when no fresh free
# measurement exists — matches the catalog compatibility engine's rule.
_USABLE_VRAM_FRACTION = 0.85
# A free-VRAM sample is "fresh" inside the probe's own cache lifetime.
_SAMPLE_FRESH_S = 300
# Stability headroom a "fits" verdict keeps free at default settings.
_DEFAULT_HEADROOM_BYTES = int(0.7 * 1024**3)

_DTYPE_NORMALIZE = {
    "fp16": "F16", "f16": "F16", "float16": "F16",
    "fp32": "F32", "f32": "F32", "float32": "F32",
    "bf16": "BF16", "bfloat16": "BF16",
    "fp8": "FP8", "fp8_e4m3": "FP8", "fp8_e5m2": "FP8",
    "q8_0": "Q8_0", "q8": "Q8_0", "q4_0": "Q4_0", "q4_k": "Q4_K",
    "q5_0": "Q5_0", "q5_k": "Q5_K", "q6_k": "Q6_K",
    "q2_k": "Q2_K", "q3_k": "Q3_K",
}


def _norm_dtype(raw: str | None) -> str | None:
    if not raw:
        return None
    return _DTYPE_NORMALIZE.get(raw.strip().lower())


# ------------------------------------------------------------ input ctx

@dataclass(frozen=True)
class RuntimeState:
    """Installation state of a declared runtime, observed from
    runtime_installs rows (flavors are "<key>-<backend>")."""

    runtime_key: str
    installed: bool
    version: str | None          # version of the selected install row
    verified: bool
    flavors: tuple[str, ...] = ()  # installed flavor keys, sorted


@dataclass(frozen=True)
class Verdict:
    """One deterministic verdict for (artifact, runtime_key)."""

    artifact_id: str
    runtime_key: str
    arch_axis: str
    arch_confidence: str | None
    runtime_axis: str
    hardware_axis: str
    policy_axis: str
    level: int
    domain_levels: dict[str, int]
    overall: str
    blocking_axes: tuple[str, ...]
    confidence: str                       # classification confidence
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    missing: tuple[str, ...]
    hardware_limits: tuple[str, ...]
    actions: tuple[str, ...]
    inputs_fingerprint: str
    ruleset_version: str


# ------------------------------------------------------------ state view

def runtime_state_for_rows(decl_key: str,
                           rows: list) -> RuntimeState:
    """Install state for one declared runtime over RuntimeInstall rows.

    Flavor keys are "<runtime_key>-<backend>" — any flavor row with
    status "installed" counts; the selected version is deterministic
    (max by version string). Version validation happens against the
    decl's validated_versions downstream, not here.
    """
    installed = [r for r in rows if getattr(r, "status", "") == "installed"]
    installed.sort(key=lambda r: (getattr(r, "version", "") or ""))
    if not installed:
        return RuntimeState(runtime_key=decl_key, installed=False,
                            version=None, verified=False, flavors=())
    return RuntimeState(
        runtime_key=decl_key, installed=True,
        version=getattr(installed[-1], "version", None) or None,
        verified=any(bool(getattr(r, "verified", False))
                     for r in installed),
        flavors=tuple(sorted(getattr(r, "runtime_key", "")
                             for r in installed)),
    )


# ---------------------------------------------------------- fingerprint

def inputs_fingerprint(*, view: ArtifactView, cls: Classification,
                       content_sha256: str | None,
                       decl: RuntimeCapabilityDecl,
                       rstate: RuntimeState, hw: dict,
                       gpu_prefs: dict) -> str:
    """fpv1 — deterministic fingerprint of everything a verdict depends
    on. Stable configuration ONLY: volatile samples (vram_free_bytes,
    memory.available_bytes, probed_at) are excluded by construction.

    Change ANY input (artifact evidence, runtime version/state, caps or
    ruleset version, GPU set/driver/backends/total RAM, residency prefs,
    required components) and the fingerprint changes — the stored verdict
    then reports stale on read instead of silently agreeing.
    """
    gpus = []
    for g in (hw.get("gpus") or []):
        gpus.append({
            "name": g.get("name"), "vendor": g.get("vendor"),
            "vram_bytes": g.get("vram_bytes"),
            "driver_version": g.get("driver_version"),
        })
    gpus.sort(key=lambda g: (str(g["name"]), str(g["vram_bytes"])))
    backends = {k: bool(v.get("available"))
                for k, v in (hw.get("backends") or {}).items()}
    caps_meta = (view.meta or {}).get("capabilities") or {}
    meta_req = (view.meta or {}).get("requirements") or {}
    payload = {
        "v": "fpv1",
        "schema": VERDICT_SCHEMA_VERSION,
        "ruleset": ruleset_version(),
        "artifact": {
            "id": view.artifact_id,
            "sha256": content_sha256,
            "source_type": view.source_type,
            "family": cls.family_id,
            "kind": cls.kind,
            "fmt": cls.fmt,
            "precision": cls.precision,
            "base_family": cls.base_family,
            "modes": sorted(
                (view.meta or {}).get("modes") or []),
            "requirements": {
                k: meta_req[k] for k in sorted(meta_req)
                if k in ("min_vram_bytes", "rec_vram_bytes",
                         "min_ram_bytes", "backends")
            },
            "offload": bool(caps_meta.get("weight_offload_cpu")),
            "components_required": sorted(cls.components_required),
        },
        "runtime": {
            "key": decl.runtime_key,
            "caps_version": decl.caps_version,
            "planned": decl.planned,
            "installed": (
                {"version": rstate.version,
                 "flavors": sorted(rstate.flavors)}
                if rstate.installed else None),
        },
        "hardware": {
            "gpus": gpus,
            "backends": backends,
            "mem_total_bytes":
                (hw.get("memory") or {}).get("total_bytes"),
        },
        "policy": {
            "gpu_mode": (gpu_prefs or {}).get("mode"),
            "preferred_gpu": (gpu_prefs or {}).get("preferred_gpu"),
            "vram_headroom_bytes":
                (gpu_prefs or {}).get("vram_headroom_bytes") or 0,
        },
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- eval axes

def _declared_modes(view: ArtifactView) -> tuple[str, ...]:
    modes = (view.meta or {}).get("modes") or []
    return tuple(m for m in modes if isinstance(m, str) and m)


def _arch_eval(view: ArtifactView, cls: Classification,
               cap) -> tuple[str, str | None, list[str],
                             list[str]]:
    """(arch_axis, arch_confidence, reasons, missing_components)."""
    if cls.family_id is None:
        code = "arch_conflict" if cls.conflicts else "family_unrecognized"
        return "unknown", "unverified", [code], []
    if cap is None:
        return "unsupported", None, ["family_unsupported"], []
    fmt = cls.fmt
    if fmt in cap.fmts_tested:
        tested_fmt = True
    elif fmt in cap.fmts_documented:
        tested_fmt = False
    else:
        return "unsupported", None, ["fmt_unsupported"], []
    modes = _declared_modes(view)
    if modes and cap.modes and not (set(modes) & set(cap.modes)):
        return "unsupported", None, ["mode_unsupported"], []
    if modes and not cap.modes and not cap.policy_gated:
        # Runtime claims the family but declares no serving mode at all —
        # nothing usable is claimed.
        return "unsupported", None, ["mode_unsupported"], []
    missing = [c for c in cap.components_required
               if c not in cls.components_present]
    if missing:
        return "unsupported", None, ["components_missing"], missing
    if not tested_fmt:
        # Documented path: nothing about this combo is verified — dtype
        # cannot downgrade it further.
        return "supported", "documented", ["runtime_path_unverified"], []
    # Tested fmt — dtype evidence gates confirmation. Missing precision
    # or an untested dtype means the combo can never be confirmed.
    precision = _norm_dtype(cls.precision)
    if precision is None or not cap.dtypes_tested \
            or precision not in cap.dtypes_tested:
        return "unknown", "unverified", ["dtype_unverified"], []
    return "supported", "tested", [], []


def _runtime_eval(decl: RuntimeCapabilityDecl,
                  rstate: RuntimeState) -> tuple[str, list[str], list[str]]:
    """(runtime_axis, reasons, actions)."""
    if decl.planned:
        return "planned", ["runtime_planned"], []
    if not rstate.installed:
        return "not_installed", ["runtime_not_installed"], [
            f"install_runtime:{decl.runtime_key}"]
    if decl.validated_versions and rstate.version in decl.validated_versions:
        return "operational", [], []
    return "installed_unverified", ["runtime_version_unverified"], []


def _requirements(view: ArtifactView, cls: Classification) -> dict:
    req = dict((view.meta or {}).get("requirements") or {})
    fam = model_families.get_family(cls.family_id)
    if fam:
        for k, v in (fam.requirements_profile or {}).items():
            req.setdefault(k, v)
    return req


def _usable_vram(g: dict, *, fresh_sample: bool) -> int | None:
    free = g.get("vram_free_bytes")
    if free is not None and fresh_sample:
        # A measured zero is real evidence — only an ABSENT sample may
        # fall back to the total-VRAM estimate (CD-9 parity).
        return int(free)
    total = g.get("vram_bytes")
    if total:
        return int(total * _USABLE_VRAM_FRACTION)
    return None


def _hardware_eval(view: ArtifactView, cls: Classification,
                   decl: RuntimeCapabilityDecl, hw: dict,
                   gpu_prefs: dict) -> tuple[str, list[str], list[str],
                                            list[str]]:
    """(hardware_axis, reasons, warnings, limits).

    Static fit comes from stable capacity (measured VRAM total, total
    RAM) against declared requirements. Volatile free-VRAM/available-RAM
    samples may tighten the usable estimate when fresh and add warnings —
    they never independently establish compatibility.
    """
    req = _requirements(view, cls)
    min_vram = req.get("min_vram_bytes") or 0
    rec_vram = req.get("rec_vram_bytes") or 0
    min_ram = req.get("min_ram_bytes") or 0
    warnings: list[str] = []
    limits: list[str] = []

    if not min_vram and not min_ram:
        return "unknown", ["requirements_undeclared"], warnings, limits

    gpus = hw.get("gpus") or []
    backends_needed = set(req.get("backends") or decl.backends or ())
    hw_backends = {k for k, v in (hw.get("backends") or {}).items()
                   if v.get("available")}
    probed_at = hw.get("probed_at")
    fresh_sample = bool(probed_at) and \
        (time.time() - float(probed_at)) <= _SAMPLE_FRESH_S

    if backends_needed and not (backends_needed & hw_backends):
        return "insufficient", ["no_backend"], warnings, \
            [f"no usable backend of {sorted(backends_needed)}"]

    axis = "fits"
    headroom = int((gpu_prefs or {}).get("vram_headroom_bytes")
                   or _DEFAULT_HEADROOM_BYTES)
    if min_vram:
        if not gpus:
            if "cpu" in backends_needed:
                axis = "limited"
                limits.append("cpu_only")
            else:
                return "insufficient", ["vram_below_min"], warnings, \
                    ["no GPU present and no CPU path"]
        elif not any(g.get("vram_bytes") or g.get("vram_free_bytes")
                     for g in gpus):
            axis = "unknown"
            limits.append("vram_unmeasured")
        else:
            usable = max((_usable_vram(g, fresh_sample=fresh_sample) or 0)
                         for g in gpus)
            if usable < min_vram:
                return "insufficient", ["vram_below_min"], warnings, [
                    f"needs >= {min_vram} VRAM; best usable {usable}"]
            target = rec_vram or min_vram
            if usable < target + headroom:
                axis = "limited"
                limits.append("vram_headroom_tight")
            if fresh_sample:
                # Zero is a real minimum — only absent samples are
                # excluded from the aggregation (CD-9 parity).
                free_min = min(
                    (g["vram_free_bytes"] for g in gpus
                     if g.get("vram_free_bytes") is not None),
                    default=None)
                if free_min is not None and free_min < min_vram:
                    warnings.append("vram_tight")
    total_ram = (hw.get("memory") or {}).get("total_bytes")
    if min_ram and total_ram and total_ram < min_ram:
        if axis == "fits":
            axis = "limited"
        limits.append("ram_below_min")
    avail = (hw.get("memory") or {}).get("available_bytes")
    if avail is not None and min_ram and avail < min_ram:
        # Same zero-versus-None class: a measured 0-byte availability
        # is a real ram_tight warning, not missing evidence.
        warnings.append("ram_tight")
    return axis, [], warnings, limits


def _policy_eval(cls: Classification, license_json: dict | None,
                 cap) -> tuple[str, list[str]]:
    # kind=adapter (LoRA) is hold-gated while G1/B-2/B-3e-2 remain active;
    # a policy_gated FamilyCap asserts the same hold on the runtime side.
    if cls.kind == "adapter" or (cap is not None and cap.policy_gated):
        return "blocked", ["hold_gated"]
    if (license_json or {}).get("commercial_ok") is False:
        return "blocked", ["license_restricted"]
    return "eligible", []


def _domain_levels(view: ArtifactView, cls: Classification) -> dict:
    """Non-cumulative per-domain evidence levels.

    inspection — V1: container/format inspection evidence exists (a
        completed U-2 report with a recognized fmt). Catalog rows are
        0 — their files were never inspected, and family identification
        does NOT imply file-integrity verification.
    family     — V2: architecture identified from trusted declaration
        or stored inspection evidence.
    runtime_static — V3: this verdict ran the static model↔runtime
        evaluation (always true once a verdict exists).
    hardware   — V4: this verdict ran the hardware evaluation.
    """
    report = view.import_report or {}
    inspected = bool(
        view.source_type == "import" and report
        and report.get("fmt") not in (None, "", "unknown"))
    return {
        "inspection": 1 if inspected else 0,
        "family": 2 if cls.family_id else 0,
        "runtime_static": 3,
        "hardware": 4,
    }


def _aggregate(arch: str, arch_conf: str | None, runtime: str,
               hardware: str, policy: str) -> tuple[str, tuple[str, ...]]:
    """Deterministic precedence (frozen F3 contract). `blocking_axes`
    lists every non-clean axis — nothing is hidden by the aggregate."""
    blocking: list[str] = []
    if arch != "supported":
        blocking.append("arch")
    if runtime not in ("operational",):
        blocking.append("runtime")
    if hardware != "fits":
        blocking.append("hardware")
    if policy != "eligible":
        blocking.append("policy")

    if arch == "unsupported":
        return "incompatible", tuple(blocking)
    if arch == "unknown":
        return "unknown", tuple(blocking)
    if policy in ("blocked", "restricted") \
            or runtime in ("planned", "not_installed"):
        return "blocked", tuple(blocking)
    if hardware == "insufficient":
        return "incompatible", tuple(blocking)
    if hardware == "unknown" or runtime == "unknown":
        return "unknown", tuple(blocking)
    if runtime == "installed_unverified" or hardware == "limited" \
            or arch_conf == "documented":
        return "limited", tuple(blocking)
    return "compatible", tuple(blocking)


# ------------------------------------------------------------- evaluate

def evaluate_for_runtime(view: ArtifactView, cls: Classification,
                         *, content_sha256: str | None,
                         license_json: dict | None,
                         decl: RuntimeCapabilityDecl,
                         rstate: RuntimeState, hw: dict,
                         gpu_prefs: dict) -> Verdict:
    cap_key = adapter_cap_key(
        cls.kind, cls.base_family or cls.family_id or "")
    cap = decl.families.get(cap_key)

    arch, arch_conf, arch_reasons, missing = _arch_eval(
        view, cls, cap)
    runtime, rt_reasons, actions = _runtime_eval(decl, rstate)
    hardware, hw_reasons, warnings, limits = _hardware_eval(
        view, cls, decl, hw, gpu_prefs)
    policy, pol_reasons = _policy_eval(cls, license_json, cap)

    reasons = [*arch_reasons, *rt_reasons, *hw_reasons, *pol_reasons]
    if cap is None and cls.family_id is not None \
            and cls.family_id not in _claimed_family_ids():
        reasons.append("no_declared_runtime")
    overall, blocking = _aggregate(
        arch, arch_conf, runtime, hardware, policy)

    domain = _domain_levels(view, cls)
    return Verdict(
        artifact_id=view.artifact_id,
        runtime_key=decl.runtime_key,
        arch_axis=arch,
        arch_confidence=arch_conf,
        runtime_axis=runtime,
        hardware_axis=hardware,
        policy_axis=policy,
        level=min(max(domain.values()), MAX_LEVEL),
        domain_levels=domain,
        overall=overall,
        blocking_axes=blocking,
        confidence=cls.confidence,
        reasons=tuple(dict.fromkeys(reasons)),
        warnings=tuple(dict.fromkeys(warnings)),
        missing=tuple(missing),
        hardware_limits=tuple(dict.fromkeys(limits)),
        actions=tuple(dict.fromkeys(actions)),
        inputs_fingerprint=inputs_fingerprint(
            view=view, cls=cls, content_sha256=content_sha256,
            decl=decl, rstate=rstate, hw=hw, gpu_prefs=gpu_prefs),
        ruleset_version=ruleset_version(),
    )


def evaluate_artifact(view: ArtifactView, cls: Classification,
                      *, content_sha256: str | None,
                      license_json: dict | None,
                      runtime_states: dict[str, RuntimeState],
                      hw: dict,
                      gpu_prefs: dict) -> list[Verdict]:
    """One verdict per declared runtime, deterministic order."""
    out = []
    for key in sorted(RUNTIME_CAPS):
        decl = RUNTIME_CAPS[key]
        rstate = runtime_states.get(key) or RuntimeState(
            runtime_key=key, installed=False, version=None,
            verified=False)
        out.append(evaluate_for_runtime(
            view, cls, content_sha256=content_sha256,
            license_json=license_json, decl=decl,
            rstate=rstate, hw=hw, gpu_prefs=gpu_prefs))
    return out


def _claimed_family_ids() -> frozenset[str]:
    from app.domain.runtime_caps import claimed_families
    return frozenset(claimed_families())


# -------------------------------------------------- staleness (GET path)

def artifact_fingerprint(session, row, hw: dict,
                         gpu_prefs: dict) -> str | None:
    """One fingerprint covering every declared runtime for an artifact —
    used for POST idempotency keys and GET stale checks."""
    view = model_families.view_from_row(row)
    cls = model_families.classify_artifact(view)
    states = runtime_states_by_decl(session)
    fps = sorted(
        inputs_fingerprint(view=view, cls=cls,
                           content_sha256=row.content_sha256,
                           decl=get_decl(k), rstate=states[k],
                           hw=hw, gpu_prefs=gpu_prefs)
        for k in sorted(RUNTIME_CAPS))
    return hashlib.sha256(
        "|".join(fps).encode("utf-8")).hexdigest()


def fingerprint_for_runtime(session, row, runtime_key: str, hw: dict,
                            gpu_prefs: dict) -> str | None:
    """Live fingerprint for one stored verdict's inputs — a stored
    verdict is stale exactly when this differs from inputs_fingerprint."""
    decl = get_decl(runtime_key)
    if decl is None:
        return None
    view = model_families.view_from_row(row)
    cls = model_families.classify_artifact(view)
    states = runtime_states_by_decl(session)
    return inputs_fingerprint(
        view=view, cls=cls, content_sha256=row.content_sha256,
        decl=decl, rstate=states.get(runtime_key) or RuntimeState(
            runtime_key=runtime_key, installed=False, version=None,
            verified=False),
        hw=hw, gpu_prefs=gpu_prefs)


def runtime_states_by_decl(session) -> dict[str, RuntimeState]:
    """Decl key -> RuntimeState, from live runtime_installs rows."""
    from sqlalchemy import select

    from app.db.models import RuntimeInstall
    rows = session.execute(select(RuntimeInstall)).scalars().all()
    out: dict[str, RuntimeState] = {}
    for key in RUNTIME_CAPS:
        mine = [r for r in rows
                if r.runtime_key == key
                or r.runtime_key.startswith(key + "-")]
        out[key] = runtime_state_for_rows(key, mine)
    return out


def gpu_prefs(session) -> dict:
    """Residency policy inputs — part of the fingerprint (a mode/pref
    change invalidates verdicts)."""
    from app.domain.model_files import _get_setting
    vram_hr = _get_setting(session, "res_vram_headroom_gib")
    return {
        "mode": _get_setting(session, "gpu_mode") or "auto",
        "preferred_gpu": _get_setting(session, "preferred_gpu"),
        "vram_headroom_bytes": int(float(vram_hr) * (1024**3))
        if vram_hr else 0,
    }


def evidence_payload(decl: RuntimeCapabilityDecl, rstate: RuntimeState,
                     hw: dict, cls: Classification,
                     evaluated_at) -> dict:
    """Point-in-time evaluation evidence — samples recorded for display,
    never re-consulted for staleness."""
    return {
        "evaluated_at": evaluated_at.isoformat() if evaluated_at else None,
        "probed_at": hw.get("probed_at"),
        "sample_basis": "probe sample at evaluation time",
        "gpus": [{"name": g.get("name"),
                  "vram_bytes": g.get("vram_bytes"),
                  "vram_free_bytes": g.get("vram_free_bytes")}
                 for g in (hw.get("gpus") or [])],
        "memory": {
            "total_bytes": (hw.get("memory") or {}).get("total_bytes"),
            "available_bytes":
                (hw.get("memory") or {}).get("available_bytes"),
        },
        "runtime_version": rstate.version,
        "runtime_installed": rstate.installed,
        "caps_version": decl.caps_version,
        "family_evidence": [e["claim"] for e in cls.evidence][:8],
        "conflicts": [c["type"] for c in cls.conflicts],
    }
