"""U-5d-0b Phase beta — candidate physical-device binding (lcfg:v1).

Pure validation and candidate-configuration module. NO production
activation: nothing in this module launches, reserves, schedules or
observes anything, and no production caller may import it during the
qualification-foundation stage.

The lcfg:v1 record is a *candidate launch configuration*. It is:
- NOT an operational runtime mapping.
- NOT an execution authorization.
- NOT a scheduler reservation.
- NOT proof of actual GPU selection.

Authority model:

- Physical identity comes ONLY from the Phase alpha evidence snapshot
  (hardware.evidence_cached() shape: one coherent report+identity pair
  with a process-lifetime generation). probe_cached() and
  identity_cached() results are never combined here — only the paired
  evidence representation is accepted.
- The CUDA singleton contract: the candidate CUDA_VISIBLE_DEVICES
  value is exactly one full authoritative 'GPU-<8-4-4-4-12>' UUID —
  never an ordinal, prefix, MIG id or multi-device list. CUDA
  Programming Guide env-var semantics (L1) document UUID input forms;
  UUIDs are matched as opaque identity strings, so CUDA_DEVICE_ORDER
  does not affect UUID binding and is never used to infer ordinals.
- Backend ordinals: with a singleton visible set, the only CUDA-local
  ordinal is 0 — labeled EXPECTED, never observed. Source support for
  '--backend <mod>=cuda<ord>' / '--auto-fit off' / '--split-mode' is
  established at pinned sd.cpp 991f825 + ggml d803c238 (L3 class);
  packaged-binary behavior is L5 scope (held, beta-3).

Windows environment notes: environment keys are case-insensitive —
two differently-cased spellings of CUDA_VISIBLE_DEVICES can exist in
one env block and would produce an ambiguous duplicate-key block.
Collisions are detected BEFORE normalization; identical values may be
canonicalized deterministically (single canonical spelling retained),
conflicting values refuse. os.environ and caller dicts are never
mutated; candidate output contains only the managed key — inherited
unrelated variables are never copied into the candidate or into
diagnostics (only CUDA_/GGML_/NVIDIA_ key NAMES are recorded as
restriction evidence).
"""

from __future__ import annotations

import os
import re

from app.domain import hw_identity

LCFG_CONTRACT = "lcfg:v1"
CUDA_ENV_KEY = "CUDA_VISIBLE_DEVICES"
ORDER_ENV_KEY = "CUDA_DEVICE_ORDER"

FLAVOR_CUDA = "cuda"
FLAVOR_CPU = "cpu"
FLAVOR_VULKAN = "vulkan"
_BINDABLE_FLAVORS = {FLAVOR_CUDA}
_KNOWN_FLAVORS = {FLAVOR_CUDA, FLAVOR_CPU, FLAVOR_VULKAN}

# Documented CUDA_DEVICE_ORDER values. UUID-form binding is order
# independent, so both are tolerated and recorded; unknown values get
# a controlled refusal (never silently tolerated).
_KNOWN_DEVICE_ORDER = {"PCI_BUS_ID", "FASTEST_FIRST"}

_STATE_CANDIDATE = "candidate"
_STATE_REFUSED = "refused"

_MIG_RE = re.compile(r"^MIG-", re.IGNORECASE)
_ORDINAL_RE = re.compile(r"^\d+$")
# 'GPU-' followed by hex/dash only but failing the full canonical form
# — an abbreviated prefix can never be accepted: it is only unique
# "assuming no other GPU shares this prefix", unverifiable by
# construction.
_UUID_PREFIX_RE = re.compile(r"^GPU-[0-9A-Fa-f-]*$")
_CUDA_ORD_RE = re.compile(r"^cuda(\d*)$", re.IGNORECASE)

# Module names documented at the pinned sd.cpp revision (backend.md).
_KNOWN_MODULES = {
    "all", "default", "*", "diffusion", "te", "vae", "clipvision",
    "llm", "photo",
}

SCOPE_NOTES = (
    "candidate_not_operational_mapping",
    "not_execution_authorization",
    "not_scheduler_reservation",
    "not_gpu_selection_proof",
)


def _refuse(code: str, **ctx) -> dict:
    """Controlled refusal record — bounded context; env VALUES and
    absolute paths are never embedded in refusal detail."""
    return {"contract": LCFG_CONTRACT, "state": _STATE_REFUSED,
            "refusal": {"code": code,
                        **{k: v for k, v in ctx.items()
                           if v is not None}}}


def _env_entries(env: dict, canonical: str) -> list[tuple[str, str]]:
    """All entries whose key case-folds to the canonical name —
    Windows case-insensitive collision detection BEFORE normalize."""
    folded = canonical.casefold()
    return [(k, v) for k, v in env.items()
            if isinstance(k, str) and k.casefold() == folded]


def _classify_cvd_token(tok: str) -> str:
    if hw_identity.parse_nvidia_uuid(tok) is not None:
        return "full_uuid"
    if _MIG_RE.match(tok):
        return "mig"
    if _UUID_PREFIX_RE.match(tok):
        return "uuid_prefix"
    if _ORDINAL_RE.match(tok):
        return "ordinal"
    return "malformed"


def evaluate_device_order(env: dict) -> dict:
    """Independent CUDA_DEVICE_ORDER validation. Never infers UUIDs
    from ordinals; FASTEST_FIRST is restriction evidence, not a
    visibility restriction and not a refusal."""
    entries = _env_entries(env, ORDER_ENV_KEY)
    if not entries:
        return {"state": "unset", "value": None}
    vals = {v for _, v in entries}
    if len(vals) > 1:
        return {"state": "refuse",
                    "refusal": "conflicting_device_order_keys"}
    raw = entries[0][1]
    if not isinstance(raw, str) or not raw.strip():
        return {"state": "refuse", "refusal": "malformed_device_order"}
    val = raw.strip().upper()
    if val not in _KNOWN_DEVICE_ORDER:
        return {"state": "refuse", "refusal": "unknown_device_order",
                "value_class": "unrecognized"}
    return {"state": "recorded", "value": val,
            "note": ("ordering evidence only — never used for "
                     "ordinal-to-uuid inference")}


def evaluate_visibility(env: dict, target_uuid: str) -> dict:
    """Inherited CUDA_VISIBLE_DEVICES decision matrix. Returns
    {"action": "set_singleton", "mode": ...} or
    {"action": "refuse", "refusal": <code>}. The parent env is never
    mutated and restrictive inherited values are never removed — the
    candidate either narrows to the singleton or refuses."""
    entries = _env_entries(env, CUDA_ENV_KEY)
    if not entries:
        return {"action": "set_singleton", "mode": "parent_unset"}
    vals = {v for _, v in entries}
    if len(vals) > 1:
        # Same key under two spellings with DIFFERENT values — a
        # contradictory restriction; canonicalization may never pick.
        return {"action": "refuse",
                "refusal": "conflicting_env_key_representations"}
    raw = entries[0][1]
    if not isinstance(raw, str):
        return {"action": "refuse",
                "refusal": "malformed_inherited_visibility"}
    if raw.strip() == "":
        return {"action": "refuse",
                "refusal": "inherited_visibility_empty"}
    members = [m.strip() for m in raw.split(",")]
    if any(m == "" for m in members):
        return {"action": "refuse",
                "refusal": "empty_visibility_member"}
    classes = [_classify_cvd_token(m) for m in members]
    if "mig" in classes:
        return {"action": "refuse",
                "refusal": "unsupported_mig_identifier"}
    if "uuid_prefix" in classes:
        return {"action": "refuse", "refusal": "ambiguous_uuid_prefix"}
    if "malformed" in classes:
        return {"action": "refuse",
                "refusal": "malformed_inherited_visibility"}
    uuids = [hw_identity.parse_nvidia_uuid(m) for m in members]
    if "ordinal" in classes:
        # Ordinals cannot be authoritatively translated to UUIDs
        # without NVML evidence — refuse rather than guess.
        code = ("mixed_inherited_tokens" if any(uuids)
                else "inherited_ordinal_untranslatable")
        return {"action": "refuse", "refusal": code}
    if len(set(uuids)) != len(uuids):
        return {"action": "refuse",
                "refusal": "duplicate_inherited_token"}
    target = hw_identity.parse_nvidia_uuid(target_uuid)
    if target is None:
        return {"action": "refuse", "refusal": "invalid_target_uuid"}
    if target not in set(uuids):
        return {"action": "refuse",
                "refusal": "target_excluded_by_inherited_visibility"}
    return {"action": "set_singleton",
            "mode": ("parent_singleton_matching" if len(uuids) == 1
                     else "restricted_to_target_singleton")}


def inherited_accelerator_env_names(env: dict) -> list[str]:
    """Restriction evidence: NAMES ONLY of inherited CUDA_/GGML_/
    NVIDIA_ variables outside the two managed keys. Values are never
    recorded — diagnostics must not leak unrelated environment."""
    out = []
    managed = {CUDA_ENV_KEY.casefold(), ORDER_ENV_KEY.casefold()}
    for k in env:
        if not isinstance(k, str):
            continue
        f = k.casefold()
        if f in managed:
            continue
        if f.startswith(("cuda_", "ggml_", "nvidia_")):
            out.append(k)
    return sorted(set(out))


def assess_evidence(evidence: dict | None,
                    fresh: dict | None = None) -> dict:
    """Independent validation of ONE coherent Phase alpha evidence
    snapshot. Generation equality alone never proves freshness —
    identical generations with different content are contradictory
    evidence, and generation is process-lifetime scoped only."""
    if not isinstance(evidence, dict):
        return {"ok": False, "refusal": "missing_evidence"}
    for key in ("report", "identity", "generation",
                "probed_monotonic", "simulated"):
        if key not in evidence:
            return {"ok": False, "refusal": "missing_evidence",
                    "missing": key}
    if evidence["simulated"]:
        return {"ok": False, "refusal": "simulated_evidence"}
    if not isinstance(evidence["generation"], int) \
            or isinstance(evidence["generation"], bool) \
            or evidence["generation"] < 1:
        return {"ok": False, "refusal": "generation_invalid"}
    ident = evidence["identity"]
    if not isinstance(ident, dict) or not ident:
        return {"ok": False, "refusal": "missing_identity_evidence"}
    if ident.get("provenance") != "real":
        return {"ok": False,
                "refusal": "unqualified_identity_provenance",
                "provenance": ident.get("provenance")}
    coll = ident.get("collection") or {}
    if coll.get("probed_monotonic") != evidence["probed_monotonic"] \
            or bool(coll.get("simulated")) != bool(evidence["simulated"]):
        return {"ok": False, "refusal": "incoherent_evidence_pair"}
    records = ident.get("records")
    if not isinstance(records, list):
        return {"ok": False, "refusal": "incomplete_evidence"}
    comp = ident.get("completeness")
    if not isinstance(comp, dict):
        return {"ok": False, "refusal": "incomplete_evidence"}
    if comp.get("wmi_devices") is not None \
            and comp["wmi_devices"] != len(records):
        return {"ok": False, "refusal": "incomplete_evidence"}
    counted = sum(comp.get(k, 0) for k in
                  ("qualified", "ambiguous", "unavailable",
                   "unsupported"))
    if counted != len(records):
        return {"ok": False, "refusal": "incomplete_evidence"}
    recomputed = hw_identity.inventory_digest(
        records, ident.get("provenance"))
    if recomputed != ident.get("digest"):
        return {"ok": False, "refusal": "identity_digest_mismatch"}
    if fresh is not None:
        if not isinstance(fresh, dict) \
                or fresh.get("generation") != evidence["generation"]:
            return {"ok": False, "refusal": "stale_evidence"}
        if fresh.get("report") != evidence["report"] \
                or fresh.get("identity") != evidence["identity"]:
            # Same generation, different content — contradictory
            # evidence, never a favorable resolution.
            return {"ok": False, "refusal": "contradictory_evidence"}
    return {"ok": True, "identity": ident, "records": records}


def _find_record(records: list[dict], token: str) -> dict:
    hits = [r for r in records if r.get("token") == token]
    if not hits:
        return {"ok": False, "refusal": "missing_physical_identity"}
    if len(hits) > 1:
        return {"ok": False, "refusal": "duplicate_physical_identity"}
    rec = hits[0]
    if rec.get("correlation") != hw_identity.CORR_QUALIFIED:
        return {"ok": False,
                "refusal": "unqualified_identity_association",
                "correlation": rec.get("correlation")}
    uuid = hw_identity.parse_nvidia_uuid(rec.get("gpu_uuid"))
    if uuid is None:
        return {"ok": False, "refusal": "invalid_uuid_evidence"}
    if hw_identity.identity_token(uuid) != token:
        # Token/uuid association contradiction — never resolvable in
        # the caller's favor.
        return {"ok": False,
                "refusal": "physical_token_uuid_mismatch"}
    return {"ok": True, "record": rec, "uuid": uuid}


def _norm_module(name: str) -> str:
    return re.sub(r"[-_]", "", (name or "").strip().lower())


def _classify_device_token(tok: str) -> dict:
    """Resolve one backend device token under the singleton contract."""
    t = (tok or "").strip()
    if not t:
        return {"class": "malformed"}
    low = t.lower()
    m = _CUDA_ORD_RE.match(low)
    if m:
        ord_s = m.group(1)
        n = int(ord_s) if ord_s else 0  # bare 'cuda' := cuda0
        if n != 0:
            return {"class": "refuse",
                    "refusal": "device_ordinal_out_of_singleton_scope"}
        return {"class": "cuda0"}
    if low == "cpu":
        return {"class": "cpu"}
    if low == "disk":
        return {"class": "disk"}
    if low.startswith("vulkan"):
        return {"class": "refuse",
                "refusal": "vulkan_runtime_substitution"}
    return {"class": "refuse", "refusal": "unsupported_backend_device"}


def evaluate_backend(backend: dict | None) -> dict:
    """Validate a proposed backend/argument spec for the exact-device
    candidate. 'backend' is {"spec": "all=cuda0", "auto_fit": "off"|...
    , "split_mode": ..., "params_backend": ...}; None requests the
    recommended bounded-default spec (source-supported, not observed).
    Exact-build support exists at sd.cpp 991f825 (L3); packaged-binary
    behavior is unqualified (L5-held)."""
    recommended = backend is None
    b = {"spec": "all=cuda0", "auto_fit": "off", "split_mode": None,
         "params_backend": None} if recommended else dict(backend)
    flags = []
    af = b.get("auto_fit")
    if af is not None:
        if not isinstance(af, str) or af.strip().lower() not in (
                "on", "off"):
            return {"state": "refuse",
                    "refusal": "malformed_auto_fit"}
        if af.strip().lower() == "on":
            # Auto-fit may re-place modules by memory budget —
            # unqualified behavior, never silently accepted for an
            # exact-device candidate.
            return {"state": "refuse",
                    "refusal": "unqualified_autofit"}
    if b.get("split_mode") is not None:
        return {"state": "refuse",
                "refusal": "unsupported_multi_device_split"}

    def _check_spec(spec, allow_disk):
        if not isinstance(spec, str) or not spec.strip():
            return {"refusal": "malformed_backend_spec"}
        if "&" in spec:
            return {"refusal": "unsupported_multi_device_split"}
        assigns = []
        for part in spec.split(","):
            part = part.strip()
            if not part:
                return {"refusal": "malformed_backend_spec"}
            if "=" in part:
                mod, dev = part.split("=", 1)
                mod_n = _norm_module(mod)
                if mod_n not in _KNOWN_MODULES:
                    return {"refusal": "backend_config_conflict",
                            "module": mod.strip()}
                default = mod_n in ("all", "default", "*")
            else:
                mod_n, dev, default = "all", part, True
            cls = _classify_device_token(dev)
            if cls["class"] == "refuse":
                return {"refusal": cls["refusal"]}
            if cls["class"] == "malformed":
                return {"refusal": "malformed_backend_spec"}
            if cls["class"] == "cpu":
                if default:
                    # CPU as the module DEFAULT backend is runtime
                    # substitution, not offloading.
                    return {"refusal": "cpu_runtime_substitution"}
                flags.append("explicit_cpu_offload")
            if cls["class"] == "disk":
                if not allow_disk:
                    return {"refusal": "unsupported_backend_device"}
                flags.append("explicit_disk_params")
            assigns.append({"module": mod_n, "device": cls["class"]})
        return {"assignments": assigns}

    res = _check_spec(b.get("spec"), allow_disk=False)
    if "refusal" in res:
        return {"state": "refuse", **res}
    params = b.get("params_backend")
    if params is not None:
        pres = _check_spec(params, allow_disk=True)
        if "refusal" in pres:
            return {"state": "refuse", **pres}
    return {"state": "accepted",
            "spec": b["spec"],
            "auto_fit": "off",
            "assignments": res["assignments"],
            "flags": sorted(set(flags)),
            "recommended": recommended,
            "argv_fragment": ["--backend", b["spec"],
                              "--auto-fit", "off"],
            "assurance": "L3_source_supported_unqualified"}


def classify_drift(held_evidence: dict, fresh_evidence: dict | None,
                   token: str) -> dict:
    """Drift decision for a held candidate's physical identity against
    a fresh coherent snapshot. A candidate refresh is NEVER claimed to
    be an actual runtime mapping refresh."""
    if not isinstance(fresh_evidence, dict):
        return {"decision": "refuse_missing_identity",
                "reason": "fresh_evidence_absent"}
    fresh_ident = fresh_evidence.get("identity") or {}
    held_ident = (held_evidence or {}).get("identity") or {}
    f_records = fresh_ident.get("records") or []
    h_rec = next((r for r in (held_ident.get("records") or [])
                  if r.get("token") == token), None)
    f_rec = next((r for r in f_records if r.get("token") == token),
                 None)
    if f_rec is None:
        slot_taken = any(
            r.get("correlation") == hw_identity.CORR_QUALIFIED
            and h_rec is not None
            and (r.get("index") == h_rec.get("index")
                 or (r.get("pnp_device_id")
                     and r.get("pnp_device_id")
                     == h_rec.get("pnp_device_id")))
            for r in f_records)
        if slot_taken:
            return {"decision":
                    "refuse_physical_replacement_new_approval_required",
                    "reason": "different_qualified_token_at_slot"}
        return {"decision": "refuse_missing_identity",
                "reason": "token_absent_in_fresh"}
    if f_rec.get("correlation") != hw_identity.CORR_QUALIFIED:
        return {"decision": "refuse_contradictory_identity",
                "reason": "fresh_record_not_qualified"}
    if h_rec is None:
        return {"decision": "mapping_refresh",
                "reason": "token_newly_qualified"}
    h_pci = tuple((h_rec.get("pci") or {}).get(k)
                  for k in ("segment", "bus", "device", "function"))
    f_pci = tuple((f_rec.get("pci") or {}).get(k)
                  for k in ("segment", "bus", "device", "function"))
    if h_pci != f_pci:
        return {"decision":
                "identity_revalidation_and_mapping_refresh",
                "reason": "pci_association_changed"}
    if f_rec.get("index") != h_rec.get("index") \
            or f_rec.get("smi_index") != h_rec.get("smi_index"):
        return {"decision": "mapping_refresh",
                "reason": "namespace_index_changed"}
    if f_rec.get("smi_vram_bytes") != h_rec.get("smi_vram_bytes") \
            or f_rec.get("smi_vram_free_bytes") \
            != h_rec.get("smi_vram_free_bytes"):
        return {"decision": "resource_drift_not_replacement",
                "reason": "volatile_resource_change"}
    if fresh_ident.get("digest") != held_ident.get("digest"):
        return {"decision": "identity_revalidation_required",
                "reason": "material_inventory_drift"}
    return {"decision": "unchanged", "reason": "identity_stable"}


def build_cuda_candidate(*, evidence: dict | None, gpu_token: str,
                         flavor: str, runtime: dict | None = None,
                         environ: dict | None = None,
                         backend: dict | None = None,
                         fresh: dict | None = None) -> dict:
    """Build the lcfg:v1 candidate for exact single-physical-device
    CUDA binding, or a controlled refusal. Pure: no IO, no launch, no
    reservation, no env mutation."""
    if flavor == FLAVOR_CPU:
        return _refuse("flavor_cpu_outside_exact_contract")
    if flavor == FLAVOR_VULKAN:
        return _refuse("flavor_vulkan_prerequisite_blocked")
    if flavor not in _KNOWN_FLAVORS or flavor not in _BINDABLE_FLAVORS:
        return _refuse("unsupported_flavor", flavor=str(flavor))
    if not isinstance(gpu_token, str) \
            or not gpu_token.startswith("hwid:nv:"):
        return _refuse("malformed_physical_token")

    ev = assess_evidence(evidence, fresh=fresh)
    if not ev["ok"]:
        return _refuse(ev["refusal"],
                       **{k: v for k, v in ev.items()
                          if k not in ("ok", "refusal")})
    found = _find_record(ev["records"], gpu_token)
    if not found["ok"]:
        return _refuse(found["refusal"],
                       correlation=found.get("correlation"))
    rec, uuid = found["record"], found["uuid"]

    rt = dict(runtime or {})
    rtq = rt.get("rtq")
    if rtq is None:
        return _refuse("runtime_integrity_missing")
    if rt.get("flavor") is not None and rt.get("flavor") != flavor:
        return _refuse("flavor_mismatch",
                       runtime_flavor=str(rt.get("flavor")))
    if not isinstance(rtq, dict) or rtq.get("contract") != "rtq:v1" \
            or rtq.get("classification") != "qualified":
        return _refuse("runtime_integrity_unqualified",
                       rtq_classification=(
                           rtq.get("classification")
                           if isinstance(rtq, dict) else "absent"))

    env_src = os.environ if environ is None else environ
    order = evaluate_device_order(env_src)
    if order["state"] == "refuse":
        return _refuse(order["refusal"])
    vis = evaluate_visibility(env_src, uuid)
    if vis["action"] == "refuse":
        return _refuse(vis["refusal"])
    be = evaluate_backend(backend)
    if be["state"] == "refuse":
        return _refuse(be["refusal"],
                       module=be.get("module"))

    pci = rec.get("pci") or {}
    return {
        "contract": LCFG_CONTRACT,
        "state": _STATE_CANDIDATE,
        "physical_token": gpu_token,
        "gpu_uuid": uuid,
        "os_index": rec.get("index"),
        "smi_index": rec.get("smi_index"),
        "pci": [pci.get("segment"), pci.get("bus"),
                pci.get("device"), pci.get("function")] if pci else None,
        "hwid_digest": ev["identity"].get("digest"),
        "hw_generation": evidence["generation"],
        "generation_scope": "process_lifetime_only_not_durable",
        "probed_monotonic": evidence["probed_monotonic"],
        "expected_cuda_ordinal": {
            "value": 0, "class": "expected_not_observed"},
        "candidate_env": {CUDA_ENV_KEY: uuid},
        "inherited_visibility": {
            "mode": vis["mode"],
            "restriction_honored": True},
        "device_order": order,
        "inherited_accelerator_env_names":
            inherited_accelerator_env_names(env_src),
        "candidate_backend": {
            "spec": be["spec"], "auto_fit": be["auto_fit"],
            "assignments": be["assignments"], "flags": be["flags"],
            "recommended": be["recommended"],
            "argv_fragment": be["argv_fragment"],
            "assurance": be["assurance"]},
        "runtime": {
            "flavor": flavor,
            "rtq_classification": rtq["classification"],
            "rtq_manifest_digest": rtq.get("manifest_digest"),
            "rtq_trust": rtq.get("trust")},
        "assurance": {
            "L1_documented_semantics": {
                "state": "established",
                "basis": "cuda_env_var_uuid_form"},
            "L2_static_candidate": {"state": "validated"},
            "L3_exact_build_source": {
                "state": "source_supported",
                "revisions": {
                    "stable_diffusion_cpp":
                        "991f82558366b35e16ffabf376fa2cba298c12d3",
                    "ggml":
                        "d803c238f67915fb825a9aa4df27b99356d51597"}},
            "L4_integrity_identity": {
                "state": ("verified_members"
                          if rtq["classification"] == "qualified"
                          else "unestablished")},
            "L5_observed_behavior": {"state": "unestablished"},
            "L6_enforced_selection": {"state": "unestablished"}},
        "scope_notes": list(SCOPE_NOTES),
    }


__all__ = [
    "CUDA_ENV_KEY", "FLAVOR_CPU", "FLAVOR_CUDA", "FLAVOR_VULKAN",
    "LCFG_CONTRACT", "ORDER_ENV_KEY", "SCOPE_NOTES",
    "assess_evidence", "build_cuda_candidate", "classify_drift",
    "evaluate_backend", "evaluate_device_order", "evaluate_visibility",
    "inherited_accelerator_env_names",
]
