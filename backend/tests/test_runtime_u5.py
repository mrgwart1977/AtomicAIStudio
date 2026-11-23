"""U-5a — workflow contracts + runtime-candidate resolver tests.

Synthetic fixtures only — no files, no network, no process, no GPU, no
inference. The resolver is a pure function over stored-evidence views;
endpoint tests insert synthetic rows and hit the two read-only GETs.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import CompatVerdict, ModelArtifact
from app.domain import runtime_resolver as rr
from app.domain.runtime_caps import RUNTIME_CAPS
from app.domain.workflow_defs import WORKFLOW_DEFS

# ------------------------------------------------------------------
# fixture builders (resolver is pure — no DB needed for unit tests)

WF_T2I = WORKFLOW_DEFS["text2image"]
WF_T2V = WORKFLOW_DEFS["text2video"]

ADAPTERS = {
    "gen.image.sdcpp": rr.AdapterEvidence(
        "gen.image.sdcpp", True, ("text2image",)),
    "gen.image.sdcpp-server": rr.AdapterEvidence(
        "gen.image.sdcpp-server", True, ("text2image",)),
    "gen.video.sdcpp": rr.AdapterEvidence(
        "gen.video.sdcpp", True, ("text2video", "image2video")),
}


def vview(**kw):
    base = {"runtime_key": "sdcpp", "arch_axis": "supported",
            "arch_confidence": "tested", "runtime_axis": "operational",
            "hardware_axis": "fits", "policy_axis": "eligible",
            "confidence": "tested", "reasons": (), "missing": (),
            "stale": False,
            "domain_levels": {"inspection": 1, "family": 2,
                              "runtime_static": 3, "hardware": 4}}
    base.update(kw)
    return rr.VerdictView(**base)


def ctx(catalog=None, workflow=WF_T2I, adapters=None, inspection_ok=True):
    return rr.ResolverContext(
        decls=dict(RUNTIME_CAPS),
        adapters=dict(ADAPTERS) if adapters is None else adapters,
        catalog=catalog, workflow=workflow, inspection_ok=inspection_ok)


SDXL_CAT = rr.CatalogBinding(
    adapter_id="gen.image.sdcpp", runtime_key="sdcpp",
    compatible_adapters=("gen.image.sdcpp-server",))

WAN_CAT = rr.CatalogBinding(
    adapter_id="gen.video.sdcpp", runtime_key="sdcpp",
    compatible_adapters=())


def resolve(verdicts, catalog=SDXL_CAT, workflow=WF_T2I, fam="sdxl",
            kind="model", **ctx_kw):
    return rr.resolve_candidates(
        artifact_id="a1", family_id=fam, kind=kind, verdicts=verdicts,
        ctx=ctx(catalog=catalog, workflow=workflow, **ctx_kw))


def state_map(cands):
    return {(c.runtime_key, c.adapter_id): c for c in cands}


# --------------------------------------------------------------- C1

def test_fully_supported_static_candidate():
    c = resolve([vview()])[0]
    assert c.state == rr.COMPATIBLE_STATIC
    assert c.binding_kind == "catalog_default"
    assert c.reasons == ()
    assert c.has_verdict and not c.verdict_stale


def test_missing_verdict_never_compatible_static():
    cands = resolve([])
    for c in cands:
        assert c.state in (rr.UNKNOWN, rr.BLOCKED)
    sdcpp = state_map(cands)[("sdcpp", "gen.image.sdcpp")]
    assert sdcpp.state == rr.UNKNOWN
    assert "verdict_missing" in sdcpp.reasons


def test_policy_hold_is_hard_block():
    c = resolve([vview(policy_axis="blocked", reasons=("hold_gated",))])[0]
    assert c.state == rr.BLOCKED
    assert "hold_gated" in c.reasons


def test_license_restricted_blocks_despite_clean_axes():
    c = resolve([vview(policy_axis="restricted",
                       reasons=("license_restricted",))])[0]
    assert c.state == rr.BLOCKED
    assert "license_restricted" in c.reasons


def test_arch_unsupported_blocks():
    c = resolve([vview(arch_axis="unsupported",
                       reasons=("family_unsupported",))])[0]
    assert c.state == rr.BLOCKED
    assert "family_unsupported" in c.reasons


def test_arch_unknown_is_unknown_not_blocked():
    c = resolve([vview(arch_axis="unknown", arch_confidence="unverified",
                       reasons=("family_unrecognized",))])[0]
    assert c.state == rr.UNKNOWN
    assert "family_unrecognized" in c.reasons


def test_unknown_arch_plus_insufficient_hardware_is_blocked():
    """Hard block must surface THROUGH the unknown — independent
    evaluation then aggregation, never first-match."""
    c = resolve([vview(arch_axis="unknown",
                       arch_confidence="unverified",
                       hardware_axis="insufficient",
                       reasons=("family_unrecognized", "vram_below_min"))])[0]
    assert c.state == rr.BLOCKED
    assert "vram_below_min" in c.reasons
    assert "family_unrecognized" in c.reasons   # both retained


def test_runtime_planned_blocks():
    c = resolve([vview(runtime_axis="planned",
                       reasons=("runtime_planned",))])[0]
    assert c.state == rr.BLOCKED


def test_runtime_not_installed_blocks():
    c = resolve([vview(runtime_axis="not_installed",
                       reasons=("runtime_not_installed",))])[0]
    assert c.state == rr.BLOCKED


def test_installed_unverified_is_limited():
    c = resolve([vview(runtime_axis="installed_unverified",
                       reasons=("runtime_version_unverified",))])[0]
    assert c.state == rr.LIMITED
    assert "runtime_version_unverified" in c.reasons


def test_runtime_unknown_is_unknown():
    c = resolve([vview(runtime_axis="unknown")])[0]
    assert c.state == rr.UNKNOWN


def test_missing_verdict_plus_planned_runtime_is_blocked():
    """wan-family artifact, no verdicts: comfyui declares wan but is
    planned=True with adapter_ids=() → blocked on decl evidence alone.
    A missing verdict never softens a static hard block."""
    cands = resolve([], catalog=None, fam="wan")
    comfy = [c for c in cands if c.runtime_key == "comfyui"]
    assert comfy and all(c.state == rr.BLOCKED for c in comfy)
    assert all("runtime_planned" in c.reasons for c in comfy)
    assert all("adapter_missing" in c.reasons for c in comfy)


def test_adapter_missing_is_blocked():
    """Declared capability without a registered adapter is never
    operational — the C2 truth table."""
    cands = resolve([vview()], adapters={})
    assert all(c.state == rr.BLOCKED for c in cands)
    assert all("adapter_missing" in c.reasons for c in cands)


def test_mode_not_in_adapter_blocks():
    """Registered adapter, but the workflow mode is outside its tasks."""
    cands = resolve([vview()], workflow=WF_T2V)
    sdcpp_img = state_map(cands)[("sdcpp", "gen.image.sdcpp")]
    assert sdcpp_img.state == rr.BLOCKED
    assert "mode_not_in_adapter" in sdcpp_img.reasons


def test_installed_unverified_plus_missing_adapter_is_blocked():
    c = resolve([vview(runtime_axis="installed_unverified",
                       reasons=("runtime_version_unverified",))],
                adapters={})[0]
    assert c.state == rr.BLOCKED
    assert "adapter_missing" in c.reasons
    assert "runtime_version_unverified" in c.reasons   # limited retained


def test_hardware_insufficient_blocks():
    c = resolve([vview(hardware_axis="insufficient",
                       reasons=("vram_below_min",))])[0]
    assert c.state == rr.BLOCKED


def test_hardware_limited_is_limited():
    c = resolve([vview(hardware_axis="limited",
                       reasons=("vram_headroom_tight",))])[0]
    assert c.state == rr.LIMITED


def test_hardware_unknown_is_unknown():
    c = resolve([vview(hardware_axis="unknown",
                       reasons=("requirements_undeclared",))])[0]
    assert c.state == rr.UNKNOWN


def test_limited_hardware_plus_policy_hold_is_blocked():
    c = resolve([vview(hardware_axis="limited",
                       policy_axis="blocked",
                       reasons=("vram_headroom_tight", "hold_gated"))])[0]
    assert c.state == rr.BLOCKED
    assert "hold_gated" in c.reasons
    assert "vram_headroom_tight" in c.reasons


def test_documented_support_caps_at_limited():
    c = resolve([vview(arch_confidence="documented",
                       confidence="documented",
                       reasons=("runtime_path_unverified",))])[0]
    assert c.state == rr.LIMITED
    assert "runtime_path_unverified" in c.reasons


def test_stale_downgrades_only_optimistic_state():
    c = resolve([vview(stale=True)])[0]
    assert c.state == rr.LIMITED
    assert c.verdict_stale and "verdict_stale" in c.reasons


def test_stale_never_overrides_policy_block():
    c = resolve([vview(stale=True, policy_axis="blocked",
                       reasons=("hold_gated",))])[0]
    assert c.state == rr.BLOCKED          # block stays authoritative
    assert c.verdict_stale
    assert "verdict_stale" not in c.reasons  # flag only, no fake reason


def test_stale_does_not_change_blocked_or_unknown():
    for axis in ({"arch_axis": "unknown",
                  "arch_confidence": "unverified"},
                 {"hardware_axis": "insufficient"}):
        v = vview(stale=True, **axis)
        c = resolve([v])[0]
        assert c.state in (rr.BLOCKED, rr.UNKNOWN)
        assert c.verdict_stale


def test_alternate_runtime_is_advisory_limited():
    """wan is claimed by sdcpp (tested) AND comfyui (documented). With no
    catalog binding, sdcpp's declared adapters surface as advisory
    alternates — binding_conflict keeps them below a catalog default."""
    cands = resolve([vview()], catalog=None, fam="wan")
    by = state_map(cands)
    img_alt = by[("sdcpp", "gen.image.sdcpp")]
    assert img_alt.binding_kind == "alternate_runtime"
    assert img_alt.state == rr.LIMITED      # binding_conflict only
    assert "binding_conflict" in img_alt.reasons
    comfy = by[("comfyui", None)]
    assert comfy.state == rr.BLOCKED        # planned + adapter_missing


def test_alternate_runtime_carries_binding_conflict():
    cands = resolve([vview()], catalog=WAN_CAT, fam="wan",
                    workflow=WF_T2V)
    alt = [c for c in cands if c.binding_kind == "alternate_runtime"]
    assert alt
    assert all("binding_conflict" in c.reasons or
               "runtime_planned" in c.reasons for c in alt)


def test_compatible_adapter_is_contract_verified_not_tested():
    """compatible_adapters are contract-verified catalog metadata —
    never execution-tested (C2). The candidate is advisory-limited and
    ranks after the catalog default."""
    cands = resolve([vview()])
    alt = state_map(cands)[("sdcpp", "gen.image.sdcpp-server")]
    assert alt.binding_kind == "compatible_adapter"
    assert alt.state == rr.LIMITED
    assert "alternate_unverified_exec" in alt.reasons


def test_catalog_default_binding_unverified_when_undeclared():
    """Catalog binds runtime X; decl X makes NO claim for the family —
    the default binding is unverified, never silently supported."""
    cat = rr.CatalogBinding(adapter_id="gen.image.sdcpp",
                            runtime_key="sdcpp")
    cands = rr.resolve_candidates(
        artifact_id="a1", family_id="ltx", kind="model",
        verdicts=[vview()], ctx=ctx(catalog=cat))
    default = state_map(cands)[("sdcpp", "gen.image.sdcpp")]
    assert "binding_unverified" in default.reasons
    assert default.state == rr.LIMITED


def test_binding_kind_ordering():
    cands = resolve([vview()])
    kinds = [c.binding_kind for c in cands]
    assert kinds[:2] == ["catalog_default", "compatible_adapter"]
    assert all(k == "alternate_runtime" for k in kinds[2:])


def test_deterministic_repeated_resolution():
    a = resolve([vview()])
    b = resolve([vview()])
    assert [(c.runtime_key, c.adapter_id, c.state, c.reasons)
            for c in a] == [
               (c.runtime_key, c.adapter_id, c.state, c.reasons)
               for c in b]


def test_integrity_evidence_missing_limited_for_import():
    c = resolve([vview()], inspection_ok=False)[0]
    assert c.state == rr.LIMITED
    assert "integrity_evidence_missing" in c.reasons


def test_catalog_artifact_no_inspection_penalty():
    """Catalog rows legitimately score domain_levels.inspection=0 —
    that alone must never block a fully static candidate."""
    c = resolve([vview()], inspection_ok=True)[0]
    assert c.state == rr.COMPATIBLE_STATIC


def test_lora_policy_hold_via_verdict():
    c = resolve([vview(policy_axis="blocked", reasons=("hold_gated",))],
                kind="adapter", fam="sd1.5")[0]
    assert c.state == rr.BLOCKED


def test_missing_components_surface():
    c = resolve([vview(missing=("text_encoder", "vae"))])[0]
    assert "text_encoder" in c.missing and "vae" in c.missing


def test_resolver_pure_no_io(monkeypatch):
    """The resolver performs zero file, socket, or process access —
    it is pure stored-evidence math."""
    import builtins
    import socket
    import subprocess
    for mod, name in ((builtins, "open"),
                      (socket, "create_connection"),
                      (subprocess, "run"), (subprocess, "Popen")):
        def _blocked(*a, _n=name, **k):
            raise AssertionError(f"resolver hit {_n}")
        monkeypatch.setattr(mod, name, _blocked)
    cands = resolve([vview()])
    assert cands


# --------------------------------------------------------------- defs

def test_workflow_defs_cover_required_categories():
    keys = set(WORKFLOW_DEFS)
    for k in ("text2image", "image2image", "text2video", "image2video",
              "video2video", "character_consistency",
              "multi_character_scene", "lora_character", "upscale",
              "audio_generation", "voice_generation", "lipsync",
              "timeline_render", "keyframe_video"):
        assert k in keys


def test_workflow_status_values_are_declared_set():
    assert all(d.status in ("implemented", "partial", "missing", "held")
               for d in WORKFLOW_DEFS.values())


def test_comfyui_bound_workflows_are_not_implemented():
    assert WORKFLOW_DEFS["lipsync"].status == "missing"
    assert WORKFLOW_DEFS["video2video"].status == "missing"
    assert WORKFLOW_DEFS["upscale"].status == "missing"


def test_lora_workflow_is_held_not_implemented():
    assert WORKFLOW_DEFS["lora_character"].status == "held"
    assert WORKFLOW_DEFS["lora_character"].policy == "lora_hold"


# ------------------------------------------------------- API endpoints
# U-1 seeds catalog artifacts at app startup — "catalog:sdxl.base" and
# "catalog:wan21.t2v.1_3b" rows already exist in every test DB. Endpoint
# tests use them directly; only verdict rows are inserted.

SDXL_AID = "catalog:sdxl.base"
WAN_AID = "catalog:wan21.t2v.1_3b"


def _verdict(client, aid, runtime_key="sdcpp", stale=True, **kw):
    """Insert a synthetic verdict row. `stale=True` stores a bogus
    fingerprint; `stale=False` stores the live fingerprint computed
    exactly as the endpoint does — yielding a non-stale verdict."""
    base = {"arch_axis": "supported", "arch_confidence": "tested",
            "runtime_axis": "operational", "hardware_axis": "fits",
            "policy_axis": "eligible", "level": 4,
            "overall": "compatible", "confidence": "tested",
            "reasons_json": [], "missing_json": [], "warnings_json": [],
            "hardware_limits_json": [], "actions_json": [],
            "blocking_axes_json": [],
            "domain_levels_json": {"inspection": 1, "family": 2,
                                   "runtime_static": 3, "hardware": 4},
            "inputs_fingerprint": "fp", "ruleset_version": "u4:fam1.caps1"}
    base.update(kw)
    with client.app.state.session_factory() as s:
        if not stale:
            from app.domain import compat_verdicts, hardware
            row = s.get(ModelArtifact, aid)
            base["inputs_fingerprint"] = \
                compat_verdicts.fingerprint_for_runtime(
                    s, row, runtime_key, hardware.probe(),
                    compat_verdicts.gpu_prefs(s))
        s.add(CompatVerdict(artifact_id=aid, runtime_key=runtime_key,
                            **base))
        s.commit()


def test_defs_endpoint(client):
    r = client.get("/api/workflows/defs")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workflow_defs_version"] >= 1
    assert any(d["workflow_key"] == "text2image" for d in body["defs"])
    assert all(d["status"] in ("implemented", "partial", "missing",
                             "held") for d in body["defs"])


def test_defs_endpoint_is_read_only_and_repeatable(client):
    a = client.get("/api/workflows/defs").json()
    b = client.get("/api/workflows/defs").json()
    assert a == b


def test_candidates_404_unknown_artifact(client):
    r = client.get("/api/models/artifacts/nope:0/runtime-candidates")
    assert r.status_code == 404


def test_candidates_400_unknown_workflow(client):
    aid = SDXL_AID
    r = client.get(
        f"/api/models/artifacts/{aid}/runtime-candidates?workflow=nope")
    assert r.status_code == 400


def test_candidates_endpoint_shape_and_order(client):
    aid = SDXL_AID
    _verdict(client, aid, stale=False)
    r = client.get(f"/api/models/artifacts/{aid}/runtime-candidates")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["artifact_id"] == aid
    assert body["workflow_key"] == "text2image"
    cands = body["candidates"]
    assert cands and cands[0]["state"] == "candidate_compatible_static"
    assert cands[0]["binding_kind"] == "catalog_default"
    assert cands[0]["runtime_key"] == "sdcpp"
    assert cands[0]["adapter_id"] == "gen.image.sdcpp"
    assert cands[0]["verdict"]["runtime_key"] == "sdcpp"
    assert cands[0]["verdict_stale"] is False
    states = [c["state"] for c in cands]
    order = {"candidate_compatible_static": 0, "candidate_limited": 1,
             "candidate_unknown": 2, "candidate_blocked": 3}
    assert states == sorted(states, key=lambda s: order[s])
    assert all(c["state"] in order for c in cands)


def test_candidates_read_only_no_writes(client):
    aid = SDXL_AID
    _verdict(client, aid)
    client.get(f"/api/models/artifacts/{aid}/runtime-candidates")
    client.get(f"/api/models/artifacts/{aid}/runtime-candidates")
    with client.app.state.session_factory() as s:
        n = s.execute(
            select(func.count()).select_from(CompatVerdict)
            .where(CompatVerdict.artifact_id == aid)).scalar()
        assert n == 1


def test_include_stale_false_keeps_blocks_filters_optimism(client):
    """wan21.t2v artifact + text2video workflow:
    - catalog default (sdcpp/gen.video.sdcpp): clean verdict, stale fp
      → stale-downgraded to limited → filtered when include_stale=false
    - comfyui alternate: planned + no adapter → blocked; a stale block
      is NEVER hidden by the filter."""
    aid = WAN_AID
    _verdict(client, aid)                    # sdcpp — stale (fp differs)
    _verdict(client, aid, runtime_key="comfyui",
             runtime_axis="planned", overall="blocked",
             reasons_json=["runtime_planned"])
    url = (f"/api/models/artifacts/{aid}/runtime-candidates"
           "?workflow=text2video&include_stale=false")
    body = client.get(url).json()
    states = {(c["runtime_key"], c["state"]) for c in body["candidates"]}
    assert ("comfyui", "candidate_blocked") in states
    assert ("sdcpp", "candidate_limited") not in states
    assert ("sdcpp", "candidate_compatible_static") not in states
    # with include_stale=true the stale flag stays explicit
    url2 = (f"/api/models/artifacts/{aid}/runtime-candidates"
            "?workflow=text2video&include_stale=true")
    cands2 = client.get(url2).json()["candidates"]
    sdcpp = next(c for c in cands2 if c["runtime_key"] == "sdcpp")
    assert sdcpp["verdict_stale"] is True
    assert sdcpp["state"] == "candidate_limited"


def test_missing_verdict_endpoint_unknown_not_compatible(client):
    aid = SDXL_AID
    body = client.get(
        f"/api/models/artifacts/{aid}/runtime-candidates").json()
    sdcpp = next(c for c in body["candidates"]
                 if c["runtime_key"] == "sdcpp")
    assert sdcpp["state"] == "candidate_unknown"
    assert "verdict_missing" in sdcpp["reasons"]
    assert sdcpp["has_verdict"] is False


def test_no_paths_or_provider_data_in_candidates(client):
    aid = SDXL_AID
    _verdict(client, aid)
    import json as _json
    body = client.get(
        f"/api/models/artifacts/{aid}/runtime-candidates").text
    assert "source_uri" not in body
    assert "install_dir" not in body and "exe_path" not in body
    assert _json.loads(body)["candidates"]


def test_candidates_do_not_mutate_eligibility(client):
    aid = SDXL_AID
    _verdict(client, aid)
    with client.app.state.session_factory() as s:
        row = s.get(ModelArtifact, aid)
        before = (row.enabled, row.support_tier)
    client.get(f"/api/models/artifacts/{aid}/runtime-candidates")
    with client.app.state.session_factory() as s:
        row = s.get(ModelArtifact, aid)
        assert (row.enabled, row.support_tier) == before


def test_no_gpu_reservation_during_resolution(client, monkeypatch):
    sched = client.app.state.gpu_scheduler
    called = []
    orig = sched.acquire
    monkeypatch.setattr(sched, "acquire",
                        lambda *a, **k: called.append(1) or orig(*a, **k))
    aid = SDXL_AID
    r = client.get(f"/api/models/artifacts/{aid}/runtime-candidates")
    assert r.status_code == 200
    assert called == []


def test_candidates_deterministic_repeated(client):
    aid = SDXL_AID
    _verdict(client, aid)
    url = f"/api/models/artifacts/{aid}/runtime-candidates"
    a = client.get(url).json()["candidates"]
    b = client.get(url).json()["candidates"]
    assert [(c["runtime_key"], c["adapter_id"], c["state"],
             c["reasons"]) for c in a] == [
                (c["runtime_key"], c["adapter_id"], c["state"],
                 c["reasons"]) for c in b]
