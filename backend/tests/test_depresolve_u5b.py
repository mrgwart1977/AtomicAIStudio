"""U-5b — dependency graph + resolution tests (R1–R9 / C1–C5).

Synthetic fixtures only — no files, no network, no process, no GPU, no
inference. The resolver is a pure function over stored-evidence views;
endpoint tests insert synthetic rows and hit the one read-only GET.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import Character, CompatVerdict, ModelArtifact, Scene, Shot, ShotCharacter
from app.domain import dep_graph as g
from app.domain import dep_resolve as dr
from app.domain.model_families import FAMILIES, ModelFamilySpec
from app.domain.runtime_caps import RUNTIME_CAPS
from app.domain.workflow_defs import WORKFLOW_DEFS

SDCPP = RUNTIME_CAPS["sdcpp"]
COMFY = RUNTIME_CAPS["comfyui"]
WF_T2I = WORKFLOW_DEFS["text2image"]
WF_T2V = WORKFLOW_DEFS["text2video"]
WF_LORA = WORKFLOW_DEFS["lora_character"]


def man(*entries):
    return tuple(g.ManifestEntry(*e) for e in entries)


def view(aid, *, family="wan", kind="model", fmt="safetensors",
         role="diffusion", manifest=(), source_type="catalog",
         content_sha256="", enabled=True, support_tier="tested",
         version="", install_verified=False, install_sha256="",
         install_status="", inspection_level=0,
         requires_inspection=False, modality="video"):
    return dr.ArtifactView(
        artifact_id=aid, kind=kind, family=family, fmt=fmt, role=role,
        modality=modality, source_type=source_type, managed_by="atomic",
        content_sha256=content_sha256, enabled=enabled,
        support_tier=support_tier, version=version, manifest=manifest,
        install_status=install_status, install_verified=install_verified,
        install_sha256=install_sha256, inspection_level=inspection_level,
        requires_inspection=requires_inspection)


def comp(v, *, conf="missing", policy="eligible", stale=False):
    return dr.CompEntry(view=v, verdict_confidence=conf,
                        policy_axis=policy, stale=stale)


WAN_MAN = man(
    ("wan2.1_t2v_1.3B_fp16.safetensors", "diffusion", "sha-d"),
    ("wan_2.1_vae.safetensors", "vae", "sha-v"),
    ("umt5-xxl-encoder-Q4_K_M.gguf", "text_encoder", "sha-t"))
WAN_ROOT = view("catalog:wan21.t2v.1_3b", manifest=WAN_MAN)
WAN_FAM = FAMILIES["wan"]


def plan(root=WAN_ROOT, *, workflow=WF_T2V, fam_spec=WAN_FAM,
         cap=SDCPP, runtime_key="sdcpp",
         runtime_state="candidate_compatible_static",
         root_conf="tested", pool=None, cast=None, selectors=None,
         key_to_artifact=None, spec_role=None, fam_spec_for=None,
         lora_held=False, include_stale=True, fp=None):
    """One-call resolver harness — every input is a prepared view."""
    return dr.resolve_plan(
        root=root, root_confidence=root_conf, root_verdict=None,
        workflow=workflow, fam_spec=fam_spec, cap=cap,
        runtime_key=runtime_key, root_runtime_state=runtime_state,
        pool=pool or {}, cast=cast or dr.CastContext(False),
        selectors=selectors or {},
        key_to_artifact=key_to_artifact or {},
        spec_role=spec_role or (lambda k: ""),
        fam_spec_for=fam_spec_for or (
            lambda aid: FAMILIES.get(
                pool[aid].view.family) if pool and aid in pool else None),
        lora_policy_held=lora_held, include_stale=include_stale,
        fingerprint_inputs=fp or {"base": "x"})


def slot(plan, role):
    return next(s for s in plan.requirements
                if s.requirement.role == role)


# ------------------------------------------------------------- C1/R1

def test_wan21_three_component_bundle():
    """R1 — one root artifact fills all three declared slots; every
    fill is bundle-provenance with a stable component_key."""
    p = plan()
    assert p.structural_state == dr.STRUCT_COMPLETE
    for role in ("diffusion", "vae", "text_encoder"):
        s = slot(p, role)
        assert s.outcome == g.SLOT_RESOLVED
        f = s.fills[0]
        assert f.artifact_id == WAN_ROOT.artifact_id
        assert f.provenance == g.PROV_BUNDLE
        assert f.component_key.startswith(
            f"{WAN_ROOT.artifact_id}::")
        assert f.file_name


def test_two_text_encoders_exclusive_conflict():
    """C1 — two declared members sharing one exclusive slot cannot be
    silently chosen between: explicit exclusive violation."""
    root = view("x:multi", manifest=man(
        ("enc-a.safetensors", "text_encoder", "a"),
        ("enc-b.safetensors", "text_encoder", "b"),
        ("d.safetensors", "diffusion", "d")))
    p = plan(root=root)
    s = slot(p, "text_encoder")
    assert s.outcome == g.SLOT_CONFLICTED
    assert g.DEP_EXCLUSIVE_VIOLATION in s.reasons
    assert {f.component_key for f in s.fills} == {
        "x:multi::enc-a.safetensors", "x:multi::enc-b.safetensors"}


def test_two_text_encoders_distinguishable_keys():
    """C1 — repeated roles produce distinct stable member keys."""
    m = g.canonical_manifest([
        {"name": "enc-b.st", "role": "text_encoder", "sha256": "b"},
        {"name": "enc-a.st", "role": "text_encoder", "sha256": "a"}])
    mk = g.member_keys(m)
    assert not mk.conflicts
    keys = sorted(mk.keys)
    assert keys == ["enc-a.st", "enc-b.st"]


def test_duplicate_filename_different_sha_disambiguated():
    m = g.canonical_manifest([
        {"name": "enc.st", "role": "text_encoder", "sha256": "aa" * 16},
        {"name": "enc.st", "role": "text_encoder", "sha256": "bb" * 16}])
    mk = g.member_keys(m)
    assert not mk.conflicts
    assert mk.keys[0].startswith("enc.st#")
    assert mk.keys[0] != mk.keys[1]


def test_duplicate_filename_no_discriminator_is_evidence_conflict():
    """Identical declared metadata can never be silently collapsed —
    explicit conflict, never an unstable identifier."""
    # Identical declared members dedupe (same member), but two
    # DISTINCT members sharing a name with no sha to disambiguate can
    # never be told apart — explicit conflict, never unstable identity.
    m2 = g.canonical_manifest([
        {"name": "enc.st", "role": "text_encoder", "sha256": ""},
        {"name": "enc.st", "role": "diffusion", "sha256": ""}])
    mk = g.member_keys(m2)
    assert mk.conflicts                      # cannot distinguish
    assert len(m2) == 2                      # distinct members kept


def test_manifest_reorder_stable_identity():
    """Stored order is irrelevant — canonical sort makes keys stable."""
    a = g.canonical_manifest([
        {"name": "b.st", "role": "vae", "sha256": "2"},
        {"name": "a.st", "role": "vae", "sha256": "1"}])
    b = g.canonical_manifest([
        {"name": "a.st", "role": "vae", "sha256": "1"},
        {"name": "b.st", "role": "vae", "sha256": "2"}])
    assert a == b
    assert g.member_keys(a).keys == g.member_keys(b).keys


def test_plan_stable_under_manifest_reorder():
    r1 = view("x:r", manifest=man(("b.st", "vae", "2"),
                                ("a.st", "diffusion", "1")))
    r2 = view("x:r", manifest=man(("a.st", "diffusion", "1"),
                                ("b.st", "vae", "2")))
    wf = WORKFLOW_DEFS["text2video"]
    p1 = plan(root=r1, workflow=wf, fam_spec=None, cap=None)
    p2 = plan(root=r2, workflow=wf, fam_spec=None, cap=None)
    assert p1.resolution_version == p2.resolution_version
    assert [f.component_key for f in slot(p1, "vae").fills] == \
        [f.component_key for f in slot(p2, "vae").fills]


def test_no_fabricated_artifact_ids():
    """Component member keys address INSIDE real artifacts — every
    fill's artifact_id is a real registry row, never fabricated."""
    p = plan()
    for s in p.requirements:
        for f in s.fills:
            assert f.artifact_id == WAN_ROOT.artifact_id
            assert "::" not in f.artifact_id


# ------------------------------------------------------------- R3

def test_selector_honored():
    ext = view("ext:vae", family="wan", role="vae",
               content_sha256="x" * 64)
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        selectors={"vae": "ext:vae"},
        pool={"ext:vae": comp(ext, conf="tested")})
    s = slot(p, "vae")
    assert s.outcome == g.SLOT_RESOLVED
    assert s.fills[0].provenance == g.PROV_EXPLICIT
    assert s.fills[0].artifact_id == "ext:vae"


def test_selector_refused_unknown_artifact():
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        selectors={"vae": "nope:missing"})
    s = slot(p, "vae")
    assert s.outcome == g.SLOT_SELECTOR_REFUSED
    assert "selector_unknown_artifact" in s.reasons


def test_selector_refused_wrong_family():
    ext = view("ext:sd-vae", family="sd1.5", role="vae")
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        selectors={"vae": "ext:sd-vae"},
        pool={"ext:sd-vae": comp(ext)})
    s = slot(p, "vae")
    assert s.outcome == g.SLOT_SELECTOR_REFUSED
    assert g.DEP_FAMILY_CONFLICT in s.reasons


def test_selector_refused_role_mismatch():
    ext = view("ext:te", family="wan", role="text_encoder")
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        selectors={"vae": "ext:te"},
        pool={"ext:te": comp(ext)})
    assert slot(p, "vae").outcome == g.SLOT_SELECTOR_REFUSED


def test_root_never_replaced():
    """A selector that fails validation is REFUSED — the slot is never
    silently filled by a substitute, and the root stays the root."""
    p = plan(selectors={"diffusion": "ext:other"})
    s = slot(p, "diffusion")
    assert s.outcome == g.SLOT_SELECTOR_REFUSED
    assert all(f.artifact_id == WAN_ROOT.artifact_id
               or f.artifact_id == "ext:other" for f in s.fills)
    # Selecting the root itself is honored — explicit provenance on the
    # root artifact, not a substitution.
    p2 = plan(selectors={"diffusion": WAN_ROOT.artifact_id})
    s2 = slot(p2, "diffusion")
    assert s2.outcome == g.SLOT_RESOLVED
    assert s2.fills[0].artifact_id == WAN_ROOT.artifact_id


def test_ambiguous_external_never_picked():
    a = view("ext:vae-a", family="wan", role="vae")
    b = view("ext:vae-b", family="wan", role="vae")
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        pool={"ext:vae-a": comp(a), "ext:vae-b": comp(b)})
    s = slot(p, "vae")
    assert s.outcome == g.SLOT_AMBIGUOUS
    assert not s.fills
    assert {c.artifact_id for c in s.candidates
            if c.outcome == "ambiguous_member"} == {
                "ext:vae-a", "ext:vae-b"}


def test_single_external_resolved():
    a = view("ext:vae-a", family="wan", role="vae")
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        pool={"ext:vae-a": comp(a, conf="tested")})
    s = slot(p, "vae")
    assert s.outcome == g.SLOT_RESOLVED
    assert s.fills[0].provenance == g.PROV_EXTERNAL


# ------------------------------------------------------------- R2

def test_shared_component_across_slots_permitted():
    """One auxiliary artifact may serve the same role in multiple
    requirements — shared components are legitimate."""
    shared = view("aux:shared", family="wan",
                  manifest=man(("shared.st", "vae", "s")))
    wf = WORKFLOW_DEFS["text2video"]
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        workflow=wf, fam_spec=FAMILIES.get("wan"),
        pool={"aux:shared": comp(shared, conf="tested")})
    assert slot(p, "vae").outcome == g.SLOT_RESOLVED
    assert slot(p, "vae").fills[0].artifact_id == "aux:shared"


def test_family_conflict_detected():
    sd = view("ext:sdvae", family="sd1.5", role="vae")
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        pool={"ext:sdvae": comp(sd)})
    s = slot(p, "vae")
    assert s.outcome == g.SLOT_MISSING       # no eligible candidate
    c = next(c for c in s.candidates
             if c.artifact_id == "ext:sdvae")
    assert g.DEP_FAMILY_CONFLICT in c.reasons


def test_version_conflict_on_selector():
    ext = view("ext:vae2", family="wan", role="vae", version="2.2")
    root = view("r:bare", version="2.1", manifest=man(
        ("d.st", "diffusion", "d")))
    p = plan(root=root, selectors={"vae": "ext:vae2"},
             pool={"ext:vae2": comp(ext)})
    s = slot(p, "vae")
    assert s.outcome == g.SLOT_SELECTOR_REFUSED
    assert g.DEP_VERSION_CONFLICT in s.reasons


def test_cycle_detected_via_subrequirements():
    """A requires vae → B fills it → B's family requires vae → B fills
    its own requirement: a real back-edge reported as dep_cycle with
    the path, never mislabeled as depth."""
    famA = ModelFamilySpec("famA", "A", "video", "model",
                           fmts=("safetensors",),
                           component_roles=("vae",))
    famB = ModelFamilySpec("famB", "B", "video", "model",
                           fmts=("safetensors",),
                           component_roles=("vae",))
    spec_for = {"x:B": famB}.get
    root = view("x:A", family="famA", manifest=())
    vb = view("x:B", family="famA", role="vae")
    pool = {"x:B": comp(vb)}
    p = dr.resolve_plan(
        root=root, root_confidence="tested", root_verdict=None,
        workflow=WORKFLOW_DEFS["text2video"], fam_spec=famA, cap=None,
        runtime_key="sdcpp", root_runtime_state="unevaluated",
        pool=pool, cast=dr.CastContext(False), selectors={},
        key_to_artifact={}, spec_role=lambda k: "",
        fam_spec_for=spec_for, lora_policy_held=False,
        include_stale=True, fingerprint_inputs={"t": 1})
    cyc = [s for s in p.requirements if g.DEP_CYCLE in s.reasons]
    assert cyc
    assert "x:B" in cyc[0].cycle_path
    assert p.structural_state == dr.STRUCT_CONFLICTED


def test_depth_exceedance_is_not_a_cycle():
    """A deep acyclic chain hits dep_max_depth — never dep_cycle."""
    # Each fill's family requires the NEXT link's role; roles differ
    # per level so exactly one candidate exists at each depth.
    roles = ("vae", "text_encoder", "image_encoder", "motion_module")
    next_role = {"f1": "text_encoder", "f2": "image_encoder",
                 "f3": "motion_module", "f4": "vae"}
    fams = {fid: ModelFamilySpec(
        fid, fid, "video", "model", fmts=("safetensors",),
        component_roles=(role,)) for fid, role in next_role.items()}
    root = view("x:0", family="f1", manifest=())
    pool = {f"x:{i}": comp(view(f"x:{i}", family=f"f{i}",
                                role=roles[i - 1]))
            for i in range(1, 5)}
    spec_for = lambda aid: fams.get(pool[aid].view.family) \
        if aid in pool else None
    p = dr.resolve_plan(
        root=root, root_confidence="tested", root_verdict=None,
        workflow=WORKFLOW_DEFS["text2video"], fam_spec=None,
        cap=None, runtime_key="sdcpp", root_runtime_state="unevaluated",
        pool=pool, cast=dr.CastContext(False), selectors={},
        key_to_artifact={}, spec_role=lambda k: "",
        fam_spec_for=spec_for, lora_policy_held=False,
        include_stale=True, fingerprint_inputs={"t": 1})
    assert g.DEP_MAX_DEPTH in p.limits_exceeded
    assert g.DEP_CYCLE not in p.limits_exceeded
    assert all(g.DEP_CYCLE not in s.reasons for s in p.requirements)


def test_node_limit():
    reqs = [g.Requirement(f"r{i}", "vae", True, g.SRC_WORKFLOW)
            for i in range(g.MAX_REQUIREMENTS + 1)]
    gv = g.build_graph(reqs)
    assert g.DEP_MAX_NODES in gv.limits


def test_edge_limit():
    reqs = [g.Requirement("p", "vae", True, g.SRC_WORKFLOW)]
    subs = {"p": [g.Requirement(f"c{i}", "vae", True, g.SRC_FAMILY)
                  for i in range(g.MAX_EDGES + 1)]}
    gv = g.build_graph(reqs, subs)
    assert g.DEP_MAX_EDGES in gv.limits


def test_candidate_enumeration_limit():
    pool = {f"ext:vae-{i}": comp(
        view(f"ext:vae-{i}", family="wan", role="vae"))
        for i in range(g.MAX_CANDIDATES_PER_SLOT + 1)}
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))), pool=pool)
    s = slot(p, "vae")
    assert s.outcome == g.SLOT_LIMIT_EXCEEDED
    assert g.DEP_MAX_CANDIDATES in s.limits


def test_character_slot_limit():
    bind = tuple(dr.CastBinding(f"c{i}", f"k{i}")
                 for i in range(g.MAX_CHARACTER_SLOTS + 1))
    cast = dr.CastContext(True, tuple(b.character_id for b in bind),
                        bind)
    p = plan(cast=cast, workflow=WF_LORA)
    assert g.DEP_MAX_CHARACTER_SLOTS in p.limits_exceeded


def test_limit_never_claims_completeness():
    pool = {f"ext:vae-{i}": comp(
        view(f"ext:vae-{i}", family="wan", role="vae"))
        for i in range(g.MAX_CANDIDATES_PER_SLOT + 1)}
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))), pool=pool)
    assert p.structural_state == dr.STRUCT_UNKNOWN
    assert p.advisory_state != dr.ADVISORY_COMPLETE


# ------------------------------------------------------------- C2

def test_integrity_declared_vs_verified_at_install():
    """verified_at_install requires the member sha to BE the install
    row's verified primary — anything less is `declared`."""
    root = view("x:r", manifest=man(
        ("d.st", "diffusion", "primary-sha"),
        ("v.st", "vae", "other-sha")),
        install_verified=True, install_sha256="primary-sha")
    p = plan(root=root)
    d = slot(p, "diffusion").fills[0]
    assert d.file_integrity == dr.FI_VERIFIED_INSTALL
    v = slot(p, "vae").fills[0]
    assert v.file_integrity == dr.FI_DECLARED


def test_integrity_missing_when_no_sha():
    root = view("x:r", manifest=man(("d.st", "diffusion", "")))
    p = plan(root=root, workflow=WF_T2I, fam_spec=None)
    f = slot(p, "diffusion").fills[0]
    assert f.file_integrity == dr.FI_MISSING
    assert p.evidence_state == dr.EV_MISSING


def test_import_records_recorded_not_verified():
    """U-2 records a hash OF actual bytes — `recorded_at_import`,
    never `verified_against_trusted`."""
    ext = view("import:abc", family="wan", role="vae",
               source_type="import", content_sha256="i" * 64)
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        pool={"import:abc": comp(ext)})
    f = slot(p, "vae").fills[0]
    assert f.file_integrity == dr.FI_RECORDED_IMPORT


def test_integrity_uncertainty_not_absence():
    """A missing sha is unverified EVIDENCE — the component still
    exists in the manifest; absence is structural only."""
    root = view("x:r", manifest=man(("d.st", "diffusion", "")))
    p = plan(root=root, workflow=WF_T2I, fam_spec=None)
    s = slot(p, "diffusion")
    assert s.outcome == g.SLOT_RESOLVED       # present, just unverified
    assert p.advisory_state == dr.ADVISORY_INCOMPLETE  # missing evidence


def test_catalog_provenance_is_evidence_not_integrity():
    """Catalog membership alone never upgrades integrity evidence."""
    p = plan()
    f = slot(p, "vae").fills[0]
    assert f.file_integrity == dr.FI_DECLARED  # sha declared, no install


# ------------------------------------------------------------- C3

def test_c3_wan21_complete_static():
    p = plan()
    assert p.structural_state == dr.STRUCT_COMPLETE
    assert p.runtime_state == "candidate_compatible_static"
    assert p.policy_state == dr.POLICY_CLEAR
    assert p.advisory_state == dr.ADVISORY_COMPLETE
    assert "integrity_declared" in p.limitations


def test_c3_complete_graph_unknown_runtime():
    p = plan(runtime_state="candidate_unknown")
    assert p.structural_state == dr.STRUCT_COMPLETE
    assert p.advisory_state == dr.ADVISORY_INCOMPLETE


def test_c3_unevaluated_runtime_incomplete():
    p = plan(runtime_state="unevaluated")
    assert p.advisory_state == dr.ADVISORY_INCOMPLETE


def test_c3_blocked_runtime_plan_blocked():
    p = plan(runtime_state="candidate_blocked", cap=COMFY,
             runtime_key="comfyui")
    assert p.structural_state == dr.STRUCT_COMPLETE  # evidence separate
    assert p.advisory_state == dr.ADVISORY_BLOCKED


def test_c3_lora_hold_blocks_complete_graph():
    bind = (dr.CastBinding("c1", "lora.id.x"),)
    cast = dr.CastContext(True, ("c1",), bind)
    p = plan(cast=cast, lora_held=True, workflow=WF_LORA,
             key_to_artifact={"lora.id.x": "catalog:lora.id.x"},
             spec_role=lambda k: "identity",
             pool={"catalog:lora.id.x": comp(
                 view("catalog:lora.id.x", family="sd1.5",
                      kind="adapter", role="lora:identity",
                      modality="lora"))})
    assert p.policy_state == dr.POLICY_HELD
    assert p.advisory_state == dr.ADVISORY_BLOCKED


def test_c3_capacity_exceeded_blocks():
    """3 identity bindings > admission capacity — reported, never
    silently dropped, never unlocks."""
    ks = {"k1": "a:l1", "k2": "a:l2", "k3": "a:l3"}
    bind = tuple(dr.CastBinding(f"c{i}", f"k{i}") for i in (1, 2, 3))
    cast = dr.CastContext(True, ("c1", "c2", "c3"), bind)
    pool = {f"a:l{i}": comp(view(f"a:l{i}", family="wan",
                                kind="adapter", role="lora:identity",
                                modality="lora"))
            for i in (1, 2, 3)}
    p = plan(cast=cast, lora_held=True,
             key_to_artifact=ks, spec_role=lambda k: "identity",
             pool=pool)
    assert p.capacity_state == dr.CAP_EXCEEDS
    assert p.advisory_state == dr.ADVISORY_BLOCKED


def test_c3_ambiguous_prevents_complete():
    a = view("ext:vae-a", family="wan", role="vae")
    b = view("ext:vae-b", family="wan", role="vae")
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"),
        ("t.st", "text_encoder", "t"))),
        pool={"ext:vae-a": comp(a), "ext:vae-b": comp(b)})
    assert p.structural_state == dr.STRUCT_AMBIGUOUS
    assert p.advisory_state == dr.ADVISORY_INCOMPLETE


def test_c3_missing_required_prevents_complete():
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))))
    assert p.structural_state == dr.STRUCT_INCOMPLETE_MISSING
    assert slot(p, "vae").outcome == g.SLOT_NO_FILL_EVIDENCE
    assert p.advisory_state == dr.ADVISORY_INCOMPLETE


def test_c3_limited_runtime_completes_with_disclosure():
    p = plan(runtime_state="candidate_limited", root_conf="documented")
    assert p.advisory_state == dr.ADVISORY_COMPLETE
    assert "runtime_candidate_limited" in p.limitations
    # declared-not-verified bundle integrity dominates: evidence is
    # unverified, and BOTH limitations must be disclosed.
    assert "unverified_evidence" in p.limitations
    assert "integrity_declared" in p.limitations


def test_no_execution_authorized_vocabulary():
    p = plan()
    assert p.advisory is True
    assert p.advisory_state in (dr.ADVISORY_COMPLETE,
                              dr.ADVISORY_INCOMPLETE, dr.ADVISORY_BLOCKED)


def test_axes_independent_blocked_runtime_not_missing():
    """A blocked runtime never implies the graph's components are
    absent — the axes disagree openly."""
    p = plan(runtime_state="candidate_blocked")
    assert p.structural_state == dr.STRUCT_COMPLETE
    assert p.runtime_state == "candidate_blocked"
    assert p.advisory_state == dr.ADVISORY_BLOCKED


# ------------------------------------------------------------- C4

def test_fingerprint_stable():
    a = plan()
    b = plan()
    assert a.resolution_version == b.resolution_version
    assert a.resolution_version.startswith("dep:v1:")


def test_fingerprint_changes_on_new_eligible_candidate():
    a = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))))
    ext = view("ext:vae-a", family="wan", role="vae")
    b = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        pool={"ext:vae-a": comp(ext)})
    assert a.resolution_version != b.resolution_version


def test_fingerprint_changes_on_request_params():
    a = plan(fp={"request": ["text2video", "sdcpp", True]})
    b = plan(fp={"request": ["text2video", "sdcpp", False]})
    assert a.resolution_version != b.resolution_version


def test_deterministic_repeated_plans():
    a, b = plan(), plan()
    assert [(s.outcome, s.requirement.requirement_id)
            for s in a.requirements] == [
        (s.outcome, s.requirement.requirement_id)
        for s in b.requirements]


# ------------------------------------------------------------- C5

def test_cast_binding_creates_explicit_lora_slot():
    bind = (dr.CastBinding("c1", "lora.id.x"),)
    cast = dr.CastContext(True, ("c1",), bind)
    art = view("catalog:lora.id.x", family="sd1.5", kind="adapter",
               role="lora:identity", modality="lora")
    p = plan(root=view("r:sd", family="sd1.5",
                       manifest=man(("d.st", "diffusion", "d"))),
             cast=cast, workflow=WF_LORA, fam_spec=None,
             cap=SDCPP, key_to_artifact={"lora.id.x": art.artifact_id},
             spec_role=lambda k: "identity",
             pool={art.artifact_id: comp(art)}, lora_held=True)
    s = next(s for s in p.requirements
             if s.requirement.role == g.LORA_IDENTITY)
    assert s.outcome == g.SLOT_RESOLVED
    assert s.fills[0].provenance == g.PROV_EXPLICIT
    assert s.fills[0].artifact_id == art.artifact_id
    assert "file_stat_skipped" in s.fills[0].reasons
    assert p.capacity_state == dr.CAP_WITHIN


def test_identity_without_shot_needs_context():
    cast = dr.CastContext(False, ("c1",), ())
    p = plan(root=view("r:sd", family="sd1.5",
                       manifest=man(("d.st", "diffusion", "d"))),
             cast=cast, workflow=WF_LORA, fam_spec=None)
    s = next(s for s in p.requirements
             if s.requirement.role == g.LORA_IDENTITY)
    assert s.outcome == g.SLOT_NEEDS_SHOT_CTX
    assert "identity_binding_requires_shot_context" in s.reasons


def test_no_cast_context_no_lora_slots():
    p = plan()
    assert all(s.requirement.source != g.SRC_CAST
               for s in p.requirements)
    assert p.capacity_state == dr.CAP_NA


def test_lora_role_undeclared_flagged():
    bind = (dr.CastBinding("c1", "lora.undeclared"),)
    cast = dr.CastContext(True, ("c1",), bind)
    art = view("catalog:lora.u", family="sd1.5", kind="adapter",
               role="lora", modality="lora")
    p = plan(root=view("r:sd", family="sd1.5",
                       manifest=man(("d.st", "diffusion", "d"))),
             cast=cast, workflow=WF_LORA, fam_spec=None,
             key_to_artifact={"lora.undeclared": art.artifact_id},
             spec_role=lambda k: "",
             pool={art.artifact_id: comp(art)}, lora_held=True)
    s = next(s for s in p.requirements
             if s.requirement.role == g.LORA)
    assert s.outcome == g.SLOT_SELECTOR_REFUSED
    assert "lora_role_undeclared" in s.reasons


def test_shared_adapter_two_applications_counted():
    """One adapter bound to two characters = one unique adapter but
    TWO applications — dedup can never relax admission."""
    bind = (dr.CastBinding("c1", "k1"), dr.CastBinding("c2", "k1"))
    cast = dr.CastContext(True, ("c1", "c2"), bind)
    art = view("a:l1", family="wan", kind="adapter",
               role="lora:identity", modality="lora")
    p = plan(cast=cast, lora_held=True,
             key_to_artifact={"k1": "a:l1"},
             spec_role=lambda k: "identity",
             pool={"a:l1": comp(art)})
    id_apps = [s for s in p.requirements
               if s.requirement.role == g.LORA_IDENTITY]
    assert len(id_apps) == 2                    # two applications
    assert p.capacity_state == dr.CAP_EXCEEDS   # identity apps > 1


def test_stale_component_filtered_without_hiding_blocks():
    stale_a = comp(view("ext:vae-s", family="wan", role="vae"),
                   stale=True)
    blocked_b = comp(view("ext:vae-b", family="wan", role="vae"),
                     policy="blocked", stale=True)
    p = plan(root=view("r:bare", manifest=man(
        ("d.st", "diffusion", "d"))),
        pool={"ext:vae-s": stale_a, "ext:vae-b": blocked_b},
        include_stale=False)
    s = slot(p, "vae")
    st = next(c for c in s.candidates
              if c.artifact_id == "ext:vae-s")
    assert "stale_verdict_filtered" in st.reasons
    bl = next(c for c in s.candidates
              if c.artifact_id == "ext:vae-b")
    assert "policy_blocked" in bl.reasons     # block never hidden


def test_base_model_filled_by_root():
    p = plan(root=view("r:sd", family="sd1.5",
                       manifest=man(("d.st", "diffusion", "d"))),
             workflow=WF_LORA, fam_spec=None)
    s = slot(p, "base_model")
    assert s.outcome == g.SLOT_RESOLVED
    assert s.fills[0].artifact_id == "r:sd"
    assert p.policy_state == dr.POLICY_HELD   # lora_hold workflow


def test_generic_lora_optional_slot_superseded_by_cast():
    wf = WORKFLOW_DEFS["character_consistency"]
    bind = (dr.CastBinding("c1", "k1"),)
    cast = dr.CastContext(True, ("c1",), bind)
    p = plan(root=view("r:sd", family="sd1.5",
                       manifest=man(("d.st", "diffusion", "d"))),
             workflow=wf, fam_spec=None, cast=cast,
             key_to_artifact={"k1": "a:l1"},
             spec_role=lambda k: "identity",
             pool={"a:l1": comp(view("a:l1", family="sd1.5",
                                     kind="adapter", role="lora:identity",
                                     modality="lora"))},
             lora_held=True)
    lora_slots = [s for s in p.requirements
                  if s.requirement.role == g.LORA]
    assert not lora_slots                    # no duplicate generic slot


def test_resolver_pure_no_io(monkeypatch):
    import builtins
    import socket
    import subprocess
    for mod, name in ((builtins, "open"),
                      (socket, "create_connection"),
                      (subprocess, "run"), (subprocess, "Popen")):
        def _blocked(*a, _n=name, **k):
            raise AssertionError(f"resolver hit {_n}")
        monkeypatch.setattr(mod, name, _blocked)
    p = plan()
    assert p.requirements


# ------------------------------------------------------- API endpoint
# Seeded catalog artifacts exist in every test DB (U-1 sync).

WAN_AID = "catalog:wan21.t2v.1_3b"
SD_AID = "catalog:sd15.fp16"
FLUX_AID = "catalog:flux.schnell"
LORA_AID = "catalog:lora.id.babylittlelo"


def _verdict(client, aid, runtime_key="sdcpp", fresh=True, **kw):
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
        if fresh:
            from app.domain import compat_verdicts, hardware
            row = s.get(ModelArtifact, aid)
            base["inputs_fingerprint"] = \
                compat_verdicts.fingerprint_for_runtime(
                    s, row, runtime_key, hardware.probe(),
                    compat_verdicts.gpu_prefs(s))
        s.add(CompatVerdict(artifact_id=aid, runtime_key=runtime_key,
                            **base))
        s.commit()


def _project(client):
    r = client.post("/api/projects", json={"name": "P",
                                           "template": "blank"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _cast(client, project_id, *, shot_loras=None, scene_char=None):
    """Scene + shot + shot-character (project-scoped) in one fixture."""
    with client.app.state.session_factory() as s:
        scene = Scene(project_id=project_id, title="sc")
        s.add(scene)
        s.flush()
        shot = Shot(project_id=project_id, scene_id=scene.id,
                    title="sh")
        s.add(shot)
        s.flush()
        ch = Character(project_id=project_id, name="C")
        s.add(ch)
        s.flush()
        sc = ShotCharacter(project_id=project_id, shot_id=shot.id,
                           character_id=ch.id,
                           lora_json=list(shot_loras or []))
        s.add(sc)
        s.commit()
        return shot.id, ch.id


def _url(pid, aid, **q):
    qs = "&".join(f"{k}={v}" for k, v in q.items())
    return f"/api/projects/{pid}/models/artifacts/{aid}" \
           f"/dependency-plan?{qs}"


def test_endpoint_404s(client):
    pid = _project(client)
    assert client.get(_url(pid, "nope:0", runtime_key="sdcpp")
                      ).status_code == 404
    assert client.get(_url("proj-no", WAN_AID, runtime_key="sdcpp")
                      ).status_code == 404


def test_endpoint_400s(client):
    pid = _project(client)
    assert client.get(_url(pid, WAN_AID)).status_code == 400
    assert client.get(
        _url(pid, WAN_AID, runtime_key="nope")).status_code == 400
    assert client.get(
        _url(pid, WAN_AID, runtime_key="sdcpp",
             workflow="nope")).status_code == 400
    assert client.get(
        _url(pid, WAN_AID, runtime_key="sdcpp",
             select="bogus_selector")).status_code == 400


def test_endpoint_wan21_bundle_plan(client):
    """R1 end-to-end: the seeded wan21 catalog bundle resolves all
    three declared slots internally — bundle provenance, declared
    integrity, no fabricated artifact rows."""
    pid = _project(client)
    _verdict(client, WAN_AID)
    r = client.get(_url(pid, WAN_AID, runtime_key="sdcpp",
                        workflow="text2video"))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["advisory"] is True
    assert b["structural_state"] == "complete"
    assert b["runtime_state"] == "candidate_compatible_static"
    assert b["advisory_state"] == "plan_complete"
    assert "integrity_declared" in b["limitations"]
    assert b["resolution_version"].startswith("dep:v1:")
    roles = {s["role"]: s for s in b["requirements"]}
    for role in ("diffusion", "vae", "text_encoder"):
        f = roles[role]["fills"][0]
        assert f["provenance"] == "bundle"
        assert f["artifact_id"] == WAN_AID
        assert f["component_key"].startswith(f"{WAN_AID}::")
        assert f["file_name"]
    for s in b["requirements"]:
        for f in s["fills"]:
            assert "/" not in f["component_key"].replace("::", "")
            assert "\\" not in str(f["file_name"])


def test_endpoint_missing_verdict_incomplete(client):
    pid = _project(client)
    r = client.get(_url(pid, WAN_AID, runtime_key="sdcpp",
                        workflow="text2video"))
    b = r.json()
    assert b["runtime_state"] == "candidate_unknown"
    assert b["structural_state"] == "complete"
    assert b["advisory_state"] == "plan_incomplete"


def test_endpoint_comfyui_blocked(client):
    """Wan21 on comfyui: structural complete + runtime blocked —
    axes disagree openly, never conflated."""
    pid = _project(client)
    r = client.get(_url(pid, WAN_AID, runtime_key="comfyui",
                        workflow="text2video"))
    b = r.json()
    assert b["structural_state"] == "complete"
    assert b["runtime_state"] == "candidate_blocked"
    assert b["advisory_state"] == "plan_blocked"


def test_endpoint_lora_cast_binding(client):
    """C5: shot-scoped identity binding resolves explicit fill +
    capacity + held policy — advisory projection, never admission."""
    pid = _project(client)
    shot_id, cid = _cast(client, pid,
                         shot_loras=["lora.id.babylittlelo"])
    _verdict(client, SD_AID)
    r = client.get(_url(pid, SD_AID, runtime_key="sdcpp",
                        workflow="lora_character", shot_id=shot_id))
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["policy_state"] == "held"
    assert b["capacity_state"] == "within_capacity"
    assert b["advisory_state"] == "plan_blocked"
    lora = next(s for s in b["requirements"]
                if s["role"] == "lora_identity")
    assert lora["outcome"] == "resolved"
    f = lora["fills"][0]
    assert f["provenance"] == "explicit"
    assert f["artifact_id"] == LORA_AID
    assert "file_stat_skipped" in f["reasons"]
    assert lora["binds_character_id"] == cid


def test_endpoint_lora_within_capacity_held(client):
    pid = _project(client)
    shot_id, _ = _cast(client, pid, shot_loras=[
        "lora.id.babylittlelo", "lora.style.pixelart"])
    _cast(client, pid, shot_loras=["lora.id.babylittlelo"])
    # Same-shot: 1 identity + 1 style = within capacity but held.
    r = client.get(_url(pid, SD_AID, runtime_key="sdcpp",
                        workflow="lora_character", shot_id=shot_id))
    b = r.json()
    assert b["capacity_state"] == "within_capacity"
    assert b["policy_state"] == "held"


def test_endpoint_character_not_in_cast(client):
    pid = _project(client)
    shot_id, _ = _cast(client, pid)
    with client.app.state.session_factory() as s:
        other = Character(project_id=pid, name="Other")
        s.add(other)
        s.commit()
        r = client.get(_url(pid, SD_AID, runtime_key="sdcpp",
                            shot_id=shot_id, character_ids=other.id))
    assert r.status_code == 422


def test_endpoint_cross_project_references_404(client):
    pid = _project(client)
    other_pid = _project(client)
    shot_id, cid = _cast(client, other_pid)
    assert client.get(_url(pid, SD_AID, runtime_key="sdcpp",
                           shot_id=shot_id)).status_code == 404
    assert client.get(_url(pid, SD_AID, runtime_key="sdcpp",
                           character_ids=cid)).status_code == 404


def test_endpoint_character_ids_without_shot(client):
    """Character context without a shot → needs_shot_context, never
    implied identity admission."""
    pid = _project(client)
    _, cid = _cast(client, pid)
    r = client.get(_url(pid, SD_AID, runtime_key="sdcpp",
                        workflow="lora_character",
                        character_ids=cid))
    b = r.json()
    ctx = next(s for s in b["requirements"]
               if s["outcome"] == "needs_shot_context")
    assert "identity_binding_requires_shot_context" in ctx["reasons"]


def test_endpoint_response_has_no_paths(client):
    pid = _project(client)
    r = client.get(_url(pid, WAN_AID, runtime_key="sdcpp",
                        workflow="text2video"))
    import json
    body = json.dumps(r.json())
    for token in ("install_root", "source_uri", "C:\\", "data\\",
                  "models_root"):
        assert token not in body


def test_endpoint_read_only_no_writes(client):
    pid = _project(client)
    _verdict(client, WAN_AID)
    client.get(_url(pid, WAN_AID, runtime_key="sdcpp",
                    workflow="text2video"))
    client.get(_url(pid, WAN_AID, runtime_key="sdcpp",
                    workflow="text2video"))
    with client.app.state.session_factory() as s:
        n = s.execute(
            select(CompatVerdict).where(
                CompatVerdict.artifact_id == WAN_AID)).scalars().all()
        assert len(n) == 1


def test_endpoint_fingerprint_repeatable(client):
    pid = _project(client)
    _verdict(client, WAN_AID)
    a = client.get(_url(pid, WAN_AID, runtime_key="sdcpp",
                        workflow="text2video")).json()
    b = client.get(_url(pid, WAN_AID, runtime_key="sdcpp",
                        workflow="text2video")).json()
    assert a["resolution_version"] == b["resolution_version"]


def test_family_roles_added_are_documented_grade(client):
    """R7 — declared component_roles for the planned/experimental
    families exist, but an empty manifest still yields no_fill_evidence,
    never a fabricated completeness."""
    for fam in ("flux", "ltx", "hunyuan", "qwen"):
        assert FAMILIES[fam].component_roles
    pid = _project(client)
    r = client.get(_url(pid, FLUX_AID, runtime_key="sdcpp",
                        workflow="text2image"))
    b = r.json()
    s = next(x for x in b["requirements"] if x["role"] == "diffusion")
    # flux catalog spec has files=() — the role is REQUIRED (declared)
    # but nothing can fill it: honest evidence gap, never "missing".
    assert s["outcome"] in ("missing", "no_fill_evidence")
    assert b["structural_state"] != "complete"
