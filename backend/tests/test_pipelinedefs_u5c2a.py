"""U-5c-2a — declarative pipeline contracts.

Covers the accepted R1–R6 architecture plus safeguards S1–S6:
deep immutability, audited command contracts, typed ports/edges,
parameter bindings, graph validation/limits, static classifications,
concurrency derivation, both fingerprints, and read-only API guards.
"""

from __future__ import annotations

import dataclasses
import json

from app.domain import pipeline_defs as pd

# ------------------------------------------------------------------
# Fixtures — injected static evidence (no commands import needed here
# for most tests; the endpoint tests use the real app context).
# ------------------------------------------------------------------


_DEFAULT_WORKFLOWS = (
    pd.WorkflowEvidence("text2image", "implemented", "none",
                        ("diffusion",), ("lora", "image_encoder"), 1),
    pd.WorkflowEvidence("image2video", "implemented", "none",
                        ("diffusion", "vae", "text_encoder"),
                        ("image_encoder",), 1),
)


def _ctx(
    command_table=(("generate.image", "gen.image.sdcpp"),
                   ("generate.video", "gen.video.sdcpp")),
    adapter_profiles=(("gen.image.sdcpp", (("adapter_kind", "local"),
                                           ("notes", "subprocess"),)),
                      ("gen.video.sdcpp", (("adapter_kind", "local"),
                                           ("notes", "subprocess"),))),
    workflows=None,
    runtime_planned=(("sdcpp", False),),
):
    if workflows is None:
        workflows = _DEFAULT_WORKFLOWS
    return pd.EvidenceContext(
        command_table=tuple(command_table),
        adapter_profiles=tuple(adapter_profiles),
        workflows=tuple(workflows),
        runtime_planned=tuple(runtime_planned))


CTX = _ctx()
T1 = pd.IMG2VID_CHAIN


def _replace(defn, **kw):
    return dataclasses.replace(defn, **kw)


def _replace_node(defn, node_id, **kw):
    return _replace(defn, nodes=tuple(
        dataclasses.replace(n, **kw) if n.node_id == node_id else n
        for n in defn.nodes))


def _codes(findings):
    return sorted(f.code for f in findings)


def _valid(defn, ctx=CTX):
    structural, findings = pd.validate_pipeline(defn, ctx)
    return structural, _codes(findings)


# ==================================================================
# Registry / T1 happy path
# ==================================================================


def test_registry_has_only_t1():
    assert set(pd.PIPELINE_DEFS) == {"img2vid_chain"}
    assert pd.get_pipeline("img2vid_chain") is T1
    assert pd.get_pipeline("nope") is None
    assert pd.get_pipeline("") is None


def test_t1_validates_clean():
    structural, codes = _valid(T1)
    assert structural == "valid"
    assert codes == []


def test_t1_topology():
    img, vid = T1.nodes
    assert img.command_key == "generate.image"
    assert vid.command_key == "generate.video"
    assert img.workflow_key == "text2image"
    assert vid.workflow_key == "image2video"
    (e,) = T1.edges
    assert (e.src_node, e.src_port) == ("img", "result")
    assert (e.dst_node, e.dst_port) == ("vid", "init_image")
    assert e.dst_command_arg == "image_asset_id"
    assert e.ownership == "same_project"
    assert e.provenance == "generated"
    assert e.asset_role == "init_image"


def test_t1_batch_is_fixed_constant():
    img = T1.nodes[0]
    b = next(b for b in img.param_bindings if b.request_key == "batch")
    assert b.source == "constant" and b.constant == 1


def test_t1_params_independent():
    keys = {p.param_key for p in T1.external_params}
    assert "img_model_key" in keys and "vid_model_key" in keys
    img, vid = T1.nodes
    assert next(b for b in img.param_bindings
                if b.request_key == "model_key").external_key \
        == "img_model_key"
    assert next(b for b in vid.param_bindings
                if b.request_key == "model_key").external_key \
        == "vid_model_key"


# ==================================================================
# S1 — deep immutability
# ==================================================================


def test_dataclasses_frozen():
    for cls in (pd.ParamDecl, pd.ParamBinding, pd.PortDecl,
                pd.PipelineEdge, pd.PipelineNode, pd.PipelineDefinition,
                pd.Constraint, pd.CommandArg, pd.CommandContract):
        assert cls.__dataclass_params__.frozen, cls.__name__


def test_no_mutable_containers_in_t1():
    assert pd._check_deep_immutable(T1) == []


def test_mutable_field_detected():
    bad = dataclasses.replace(
        T1, policy_constraints={"a": 1})          # dict — mutable
    structural, codes = _valid(bad)
    assert structural == "invalid"
    assert "mutable_definition_field" in codes


def test_mutable_constraint_value_detected():
    p = pd.ParamDecl("x", "string", required=False,
                     constraints=(pd.Constraint("choices", ["a"]),))
    assert _constraint_codes(p) == ["invalid_constraint"]


def _constraint_codes(p):
    return [f.code for f in pd._constraint_findings("s", p.ptype,
                                                    p.constraints)]


# ==================================================================
# R1/R2 — ports, edges, parameter bindings
# ==================================================================


def test_in_port_names_real_command_arg():
    vid = T1.nodes[1]
    p = vid.input_ports[0]
    assert p.command_arg == "image_asset_id"
    carg = pd.COMMAND_CONTRACTS["generate.video"].arg("image_asset_id")
    assert carg is not None and carg.is_asset_arg


def test_out_port_primary_asset_contract():
    for n in T1.nodes:
        (p,) = n.output_ports
        assert p.output_contract == "primary_asset"


def test_edge_removed_invalidates():
    bad = _replace(T1, edges=())
    structural, codes = _valid(bad)
    assert structural == "invalid"
    assert "missing_pipeline_input" in codes
    assert "conflicting_binding_sources" in codes


def test_missing_mandatory_binding():
    img = T1.nodes[0]
    keep = tuple(b for b in img.param_bindings
                 if b.request_key != "model_key")
    bad = _replace_node(T1, "img", param_bindings=keep)
    _, codes = _valid(bad)
    assert "missing_mandatory_binding" in codes


def test_conflicting_binding_sources():
    img = T1.nodes[0]
    bindings = img.param_bindings + (
        pd.ParamBinding("steps", "external", edge_id="img_to_vid_init"),)
    bad = _replace_node(T1, "img", param_bindings=bindings)
    _, codes = _valid(bad)
    assert "conflicting_binding_sources" in codes


def test_unsupported_command_arg():
    img = T1.nodes[0]
    bindings = img.param_bindings + (
        pd.ParamBinding("not_a_real_arg", "external",
                        external_key="img_prompt"),)
    bad = _replace_node(T1, "img", param_bindings=bindings)
    _, codes = _valid(bad)
    assert "unsupported_command_arg" in codes


def test_binding_to_computed_arg():
    img = T1.nodes[0]
    bindings = img.param_bindings + (
        pd.ParamBinding("est_vram_bytes", "constant", constant=1),)
    bad = _replace_node(T1, "img", param_bindings=bindings)
    _, codes = _valid(bad)
    assert "binding_to_computed_arg" in codes


def test_refused_arg_loras_on_video():
    vid = T1.nodes[1]
    bindings = vid.param_bindings + (
        pd.ParamBinding("loras", "constant", constant="x"),)
    bad = _replace_node(T1, "vid", param_bindings=bindings)
    _, codes = _valid(bad)
    assert "refused_arg" in codes


def test_missing_external_param():
    img = T1.nodes[0]
    bindings = tuple(
        dataclasses.replace(b, external_key="nope")
        if b.request_key == "model_key" else b
        for b in img.param_bindings)
    bad = _replace_node(T1, "img", param_bindings=bindings)
    _, codes = _valid(bad)
    assert "missing_external_param" in codes


def test_bad_command_arg_edge_port_mismatch():
    (e,) = T1.edges
    bad_e = dataclasses.replace(e, dst_command_arg="wrong_arg")
    bad = _replace(T1, edges=(bad_e,))
    _, codes = _valid(bad)
    assert "bad_command_arg" in codes


def test_constant_type_mismatch():
    img = T1.nodes[0]
    bindings = tuple(
        dataclasses.replace(b, constant="one")
        if b.request_key == "batch" else b
        for b in img.param_bindings)
    bad = _replace_node(T1, "img", param_bindings=bindings)
    _, codes = _valid(bad)
    assert "constant_type_mismatch" in codes


def test_default_on_required_external():
    p = pd.ParamDecl("m", "string", required=True,
                     has_default=True, default="x")
    bad = _replace(T1, external_params=T1.external_params + (p,))
    _, codes = _valid(bad)
    assert "default_on_required_external" in codes


def test_default_type_mismatch():
    p = pd.ParamDecl("n", "int", required=False,
                     has_default=True, default="two")
    bad = _replace(T1, external_params=T1.external_params + (p,))
    _, codes = _valid(bad)
    assert "default_type_mismatch" in codes


def test_invalid_ptype():
    p = pd.ParamDecl("z", "banana", required=True)
    bad = _replace(T1, external_params=T1.external_params + (p,))
    _, codes = _valid(bad)
    assert "invalid_ptype" in codes


def test_default_violates_constraint():
    p = pd.ParamDecl("n", "int", required=False,
                     has_default=True, default=99,
                     constraints=(pd.Constraint("max", 64),))
    bad = _replace(T1, external_params=T1.external_params + (p,))
    _, codes = _valid(bad)
    assert "constraint_violation" in codes


def test_constant_violates_constraint():
    img = T1.nodes[0]
    bindings = tuple(
        dataclasses.replace(
            b, constraints=(pd.Constraint("max", 0),))
        if b.request_key == "batch" else b
        for b in img.param_bindings)
    bad = _replace_node(T1, "img", param_bindings=bindings)
    _, codes = _valid(bad)
    assert "constraint_violation" in codes


def test_binding_constraint_invalid_kind():
    img = T1.nodes[0]
    bindings = tuple(
        dataclasses.replace(
            b, constraints=(pd.Constraint("bogus", 1),))
        if b.request_key == "batch" else b
        for b in img.param_bindings)
    bad = _replace_node(T1, "img", param_bindings=bindings)
    _, codes = _valid(bad)
    assert "invalid_constraint" in codes


def test_duplicate_external_param():
    dup = T1.external_params[0]
    bad = _replace(T1, external_params=T1.external_params + (dup,))
    _, codes = _valid(bad)
    assert "duplicate_external_param" in codes


def test_edge_direction_checks():
    (e,) = T1.edges
    # Swap ports: src->vid.result (out), dst stays vid.init_image —
    # src port belongs to vid here; use reverse edge instead.
    bad_e = dataclasses.replace(
        e, src_node="vid", src_port="init_image",
        dst_node="img", dst_port="result")
    bad = _replace(T1, edges=(bad_e,))
    _, codes = _valid(bad)
    assert "bad_direction" in codes


def test_kind_mismatch():
    vid = T1.nodes[1]
    ports = (dataclasses.replace(vid.input_ports[0],
                                 asset_kind="audio"),)
    bad = _replace_node(T1, "vid", input_ports=ports)
    _, codes = _valid(bad)
    assert "kind_mismatch" in codes


# ==================================================================
# D4/R6 — graph validation, cycles, connectivity, limits
# ==================================================================


def test_self_edge():
    (e,) = T1.edges
    bad_e = dataclasses.replace(e, src_node="img", src_port="result",
                                dst_node="img", dst_port="result")
    # img.result is an out-port; the edge also needs an in-port —
    # self_edge fires on node equality regardless.
    bad = _replace(T1, edges=(bad_e,))
    _, codes = _valid(bad)
    assert "self_edge" in codes


def test_cycle():
    a = pd.PipelineNode("a", "job", "generate.image", "text2image",
                        ("sdcpp",), ("diffusion",),
                        input_ports=(pd.PortDecl("i", "in", "image",
                                                 command_arg="model_key"),),
                        output_ports=(pd.PortDecl("o", "out", "image",
                                                  output_contract="primary_asset"),),
                        param_bindings=(
                            pd.ParamBinding("model_key", "external",
                                            external_key="img_model_key"),
                            pd.ParamBinding("prompt", "external",
                                            external_key="img_prompt"),
                            pd.ParamBinding("batch", "constant",
                                            constant=1)))
    b = dataclasses.replace(
        a, node_id="b",
        input_ports=(pd.PortDecl("i", "in", "image", required=True,
                                 command_arg="model_key"),),
        output_ports=(pd.PortDecl("o", "out", "image",
                                  output_contract="primary_asset"),),
        param_bindings=a.param_bindings)
    edges = (
        pd.PipelineEdge("e1", "a", "o", "b", "i",
                        dst_command_arg="model_key",
                        asset_role="init_image",
                        ownership="same_project",
                        provenance="generated"),
        pd.PipelineEdge("e2", "b", "o", "a", "i",
                        dst_command_arg="model_key",
                        asset_role="init_image",
                        ownership="same_project",
                        provenance="generated"),
    )
    bad = _replace(T1, nodes=(a, b), edges=edges)
    _, codes = _valid(bad)
    assert "cycle" in codes


def test_disconnected_graph_two_chains():
    """Two valid chains with no connecting edge = invalid (S6)."""
    extra = dataclasses.replace(
        T1.nodes[0], node_id="img2",
        output_ports=(pd.PortDecl("result", "out", "image",
                                  output_contract="primary_asset"),))
    extra2 = dataclasses.replace(
        T1.nodes[1], node_id="vid2",
        input_ports=(pd.PortDecl("init_image", "in", "image",
                                 required=True,
                                 command_arg="image_asset_id"),))
    e2 = dataclasses.replace(
        T1.edges[0], edge_id="chain2",
        src_node="img2", dst_node="vid2",
        src_port="result", dst_port="init_image")
    bad = _replace(T1, nodes=T1.nodes + (extra, extra2),
                   edges=T1.edges + (e2,))
    _, codes = _valid(bad)
    assert "disconnected_graph" in codes
    assert "node_isolated" not in codes  # every node HAS an edge


def test_node_isolated():
    extra = dataclasses.replace(
        T1.nodes[0], node_id="lone",
        output_ports=(pd.PortDecl("result", "out", "image",
                                  output_contract="primary_asset"),))
    bad = _replace(T1, nodes=T1.nodes + (extra,))
    _, codes = _valid(bad)
    assert "node_isolated" in codes
    assert "disconnected_graph" in codes


def test_duplicate_node_id():
    bad = _replace(T1, nodes=T1.nodes + (T1.nodes[0],))
    _, codes = _valid(bad)
    assert "duplicate_node_id" in codes


def test_duplicate_edge_id():
    e2 = dataclasses.replace(T1.edges[0])
    bad = _replace(T1, edges=T1.edges + (e2,))
    _, codes = _valid(bad)
    assert "duplicate_edge_id" in codes


def test_duplicate_port_key():
    img = T1.nodes[0]
    ports = img.output_ports + (img.output_ports[0],)
    bad = _replace_node(T1, "img", output_ports=ports)
    _, codes = _valid(bad)
    assert "duplicate_port_key" in codes


def test_bad_identifier():
    bad = _replace(T1, pipeline_key="Bad Key!")
    _, codes = _valid(bad)
    assert "bad_identifier" in codes


def test_unsupported_schema_version():
    bad = _replace(T1, schema_version=99)
    _, codes = _valid(bad)
    assert "unsupported_schema_version" in codes


def test_max_ports_combined():
    many = tuple(
        pd.PortDecl(f"p{i}", "out", "image",
                    output_contract="primary_asset")
        for i in range(9))
    bad = _replace_node(T1, "img", output_ports=many)
    _, codes = _valid(bad)
    assert "limit_exceeded" in codes


def test_max_nodes_limit():
    nodes = T1.nodes + tuple(
        dataclasses.replace(T1.nodes[0], node_id=f"n{i}")
        for i in range(31))
    bad = _replace(T1, nodes=nodes)
    _, codes = _valid(bad)
    assert "limit_exceeded" in codes


def test_unknown_command():
    bad = _replace_node(T1, "img", command_key="generate.nope")
    _, codes = _valid(bad)
    assert "unknown_command" in codes
    assert "missing_command_contract" in codes


def test_unknown_workflow():
    bad = _replace_node(T1, "img", workflow_key="not_a_workflow")
    _, codes = _valid(bad)
    assert "unknown_workflow" in codes


def test_workflow_not_served():
    bad = _replace_node(T1, "img", workflow_key="image2video")
    _, codes = _valid(bad)
    assert "workflow_not_served_by_command" in codes


def test_unknown_runtime():
    bad = _replace_node(T1, "img", runtime_keys=("comfyui",))
    _, codes = _valid(bad)
    assert "unknown_runtime" in codes


def test_planned_runtime_flagged_unknown_state():
    ctx = _ctx(runtime_planned=(("sdcpp", True),))
    d = pd.describe_pipeline(T1, ctx)
    assert d["nodes"][0]["runtime_declared"]["sdcpp"] == "planned"


def test_component_not_declared():
    bad = _replace_node(T1, "img",
                        required_components=("diffusion", "warpdrive"))
    _, codes = _valid(bad)
    assert "component_not_declared" in codes


# ==================================================================
# Concurrency
# ==================================================================


def test_t1_no_concurrent_pairs():
    assert pd.concurrency_pairs(T1) == []


def test_concurrency_independent_branches():
    c = dataclasses.replace(
        T1.nodes[0], node_id="img2",
        output_ports=(pd.PortDecl("result", "out", "image",
                                  output_contract="primary_asset"),))
    # Two chains sharing NO edge: invalid graph, but concurrency math
    # still derives correctly on the node set.
    d = _replace(T1, nodes=T1.nodes + (c,))
    assert pd.concurrency_pairs(d) == [["img", "img2"],
                                       ["img2", "vid"]]


def test_concurrency_diamond():
    a = T1.nodes[0]
    # a->b, a->c gives b||c
    b = dataclasses.replace(
        T1.nodes[1], node_id="b",
        input_ports=(pd.PortDecl("init_image", "in", "image",
                                 required=True,
                                 command_arg="image_asset_id"),))
    c = dataclasses.replace(
        T1.nodes[1], node_id="c",
        input_ports=(pd.PortDecl("init_image", "in", "image",
                                 required=True,
                                 command_arg="image_asset_id"),))
    e1 = dataclasses.replace(T1.edges[0], edge_id="e1",
                             dst_node="b", dst_port="init_image")
    e2 = dataclasses.replace(T1.edges[0], edge_id="e2",
                             dst_node="c", dst_port="init_image")
    d = _replace(T1, nodes=(a, b, c), edges=(e1, e2))
    assert pd.concurrency_pairs(d) == [["b", "c"]]


# ==================================================================
# S5 — static classifications
# ==================================================================


def test_describe_classification_axes():
    d = pd.describe_pipeline(T1, CTX)
    n = d["nodes"][0]
    assert n["command_contract"] == "exists"
    assert n["adapter_class"] == "local"
    assert n["execution_class"] == "implemented"
    assert n["policy_state"] == "notes_only"
    assert n["operational_readiness"] == "not_evaluated"
    assert d["operational_readiness"] == "not_evaluated"
    assert d["static_assessment"] == "implemented"


def test_missing_adapter_classified():
    ctx = _ctx(command_table=(("generate.image", "gen.image.sdcpp"),))
    d = pd.describe_pipeline(T1, ctx)
    vid = d["nodes"][1]
    assert vid["execution_class"] == "missing"
    assert vid["adapter_class"] == "unknown"
    assert d["static_assessment"] == "missing"


def test_mock_adapter_simulated():
    ctx = _ctx(adapter_profiles=(
        ("gen.image.sdcpp", (("adapter_kind", "mock"), ("notes", ""),)),
        ("gen.video.sdcpp", (("adapter_kind", "local"), ("notes", ""),))))
    d = pd.describe_pipeline(T1, ctx)
    assert d["nodes"][0]["execution_class"] == "simulated"
    assert d["static_assessment"] == "simulated"


def test_experimental_adapter():
    ctx = _ctx(adapter_profiles=(
        ("gen.image.sdcpp", (("adapter_kind", "local"),
                             ("notes", "EXPERIMENTAL build"),)),
        ("gen.video.sdcpp", (("adapter_kind", "local"), ("notes", ""),))))
    d = pd.describe_pipeline(T1, ctx)
    assert d["nodes"][0]["execution_class"] == "experimental"


def test_held_workflow():
    ctx = _ctx(workflows=(
        pd.WorkflowEvidence("text2image", "held", "lora_hold",
                            ("diffusion",), (), 1),
        pd.WorkflowEvidence("image2video", "implemented", "none",
                            ("diffusion", "vae", "text_encoder"), (), 1)))
    d = pd.describe_pipeline(T1, ctx)
    assert d["nodes"][0]["execution_class"] == "held"
    assert d["nodes"][0]["policy_state"] == "held"


def test_policy_state_never_clear():
    d = pd.describe_pipeline(T1, CTX)
    states = {n["policy_state"] for n in d["nodes"]}
    assert "clear" not in states


def test_no_ready_or_executable_strings():
    d = pd.describe_pipeline(T1, CTX)
    import json as _j
    blob = _j.dumps(d)
    for banned in ('"ready"', '"executable"', '"runnable"',
                   '"runtime_operational":true'):
        assert banned not in blob


# ==================================================================
# Fingerprints
# ==================================================================


def test_definition_fingerprint_stable():
    assert pd.definition_fingerprint(T1) == \
        pd.definition_fingerprint(T1)
    assert pd.definition_fingerprint(T1).startswith("pipe:v1:")


def test_definition_fingerprint_material_change():
    bad = _replace(T1, definition_version=2)
    assert pd.definition_fingerprint(bad) != \
        pd.definition_fingerprint(T1)


def test_fingerprint_policy_notes_material():
    bad = _replace_node(T1, "img",
                        policy_notes=("other_note",))
    assert pd.definition_fingerprint(bad) != \
        pd.definition_fingerprint(T1)


def test_fingerprint_display_name_excluded():
    bad = _replace(T1, display_name="Renamed",
                   description="Different text")
    assert pd.definition_fingerprint(bad) == \
        pd.definition_fingerprint(T1)


def test_contract_evidence_fingerprint():
    fp1 = pd.contract_evidence_fingerprint(T1, CTX)
    assert fp1.startswith("pipectx:v1:")
    # Drift in adapter classification flips ctx fp, not def fp.
    ctx2 = _ctx(adapter_profiles=(
        ("gen.image.sdcpp", (("adapter_kind", "mock"), ("notes", ""),)),
        ("gen.video.sdcpp", (("adapter_kind", "local"), ("notes", ""),))))
    assert pd.contract_evidence_fingerprint(T1, ctx2) != fp1
    assert pd.definition_fingerprint(T1) == \
        pd.definition_fingerprint(T1)


def test_ctx_fingerprint_command_table_drift():
    ctx2 = _ctx(command_table=(
        ("generate.image", "gen.image.other"),
        ("generate.video", "gen.video.sdcpp")))
    assert pd.contract_evidence_fingerprint(T1, ctx2) != \
        pd.contract_evidence_fingerprint(T1, CTX)


def test_ctx_fingerprint_time_independent():
    assert pd.contract_evidence_fingerprint(T1, CTX) == \
        pd.contract_evidence_fingerprint(T1, CTX)


# ==================================================================
# API endpoints (real app context, real JOB_COMMANDS table)
# ==================================================================


def test_api_list(client):
    r = client.get("/api/pipelines")
    assert r.status_code == 200
    body = r.json()
    assert len(body["pipelines"]) == 1
    p = body["pipelines"][0]
    assert p["pipeline_key"] == "img2vid_chain"
    assert p["structural"] == "valid"
    assert p["definition_fingerprint"].startswith("pipe:v1:")


def test_api_describe(client):
    r = client.get("/api/pipelines/img2vid_chain")
    assert r.status_code == 200
    d = r.json()
    assert d["pipeline_key"] == "img2vid_chain"
    assert d["validation"]["structural"] == "valid"
    assert d["operational_readiness"] == "not_evaluated"
    assert d["definition_fingerprint"].startswith("pipe:v1:")
    assert d["contract_evidence_fingerprint"].startswith("pipectx:v1:")
    nodes = {n["node_id"]: n for n in d["nodes"]}
    assert nodes["img"]["adapter_id"] == "gen.image.sdcpp"
    assert nodes["vid"]["adapter_id"] == "gen.video.sdcpp"
    assert nodes["vid"]["input_ports"][0]["command_arg"] \
        == "image_asset_id"
    assert d["concurrency_pairs"] == []
    for n in d["nodes"]:
        assert n["operational_readiness"] == "not_evaluated"


def test_api_describe_404(client):
    r = client.get("/api/pipelines/nope")
    assert r.status_code == 404


def test_api_no_write_methods(client):
    for method in ("post", "put", "patch", "delete"):
        r = getattr(client, method)("/api/pipelines")
        assert r.status_code in (404, 405)
        r = getattr(client, method)("/api/pipelines/img2vid_chain")
        assert r.status_code in (404, 405)


# ==================================================================
# Read-only guards — no probe, no scheduler, no jobs, no mutation
# ==================================================================


def test_no_probe_or_scheduler_calls(client, monkeypatch):
    calls = {"probe": 0, "acquire": 0}
    from app.domain import gpu_sched, hardware
    monkeypatch.setattr(hardware, "probe",
                        lambda **kw: calls.__setitem__(
                            "probe", calls["probe"] + 1) or {})
    monkeypatch.setattr(gpu_sched.GpuScheduler, "acquire",
                        lambda *a, **kw: calls.__setitem__(
                            "acquire", calls["acquire"] + 1) or -1)
    assert client.get("/api/pipelines").status_code == 200
    assert client.get("/api/pipelines/img2vid_chain").status_code == 200
    assert calls == {"probe": 0, "acquire": 0}


def test_no_jobs_created(client):
    from sqlalchemy import func, select

    from app.db.models import GenerationJob
    with client.app.state.session_factory() as s:
        before = s.execute(select(func.count(GenerationJob.id))).scalar()
    client.get("/api/pipelines/img2vid_chain")
    client.get("/api/pipelines")
    with client.app.state.session_factory() as s:
        after = s.execute(select(func.count(GenerationJob.id))).scalar()
    assert after == before


def test_module_has_no_forbidden_imports():
    import inspect

    import app.domain.pipeline_defs as m
    src = inspect.getsource(m)
    for banned in ("import commands", "from app.domain import commands",
                   "gpu_sched", "JobEngine", "adapter_registry",
                   "acquire(", "probe(", "enqueue("):
        assert banned not in src, banned


def test_audit_fields_present():
    for c in pd.COMMAND_CONTRACTS.values():
        assert c.audited_from
        assert c.output_kind in pd.ASSET_KINDS
        assert c.output_origin


# ==================================================================
# D1 — fixed batch=1 pipeline pin (scoped to the pipeline contract,
#      the real generate.image batch range 1-4 stays intact)
# ==================================================================


def _img_bindings(*, batch_binding=None, drop_batch=False):
    keep = [b for b in T1.nodes[0].param_bindings
            if b.request_key != "batch" or not drop_batch]
    if batch_binding is not None:
        keep = [b for b in keep if b.request_key != "batch"]
        keep.append(batch_binding)
    return tuple(keep)


def test_d1_command_batch_range_preserved():
    """The audited command contract still documents the REAL batch
    range — the pin is a pipeline rule, not a command redefinition."""
    a = pd.COMMAND_CONTRACTS["generate.image"].arg("batch")
    mm = {c.kind: c.value for c in a.constraints}
    assert mm == {"min": 1, "max": 4}
    assert pd.COMMAND_CONTRACTS["generate.image"].output_count_arg \
        == "batch"


def test_d1_shipped_t1_remains_valid():
    structural, codes = _valid(T1)
    assert structural == "valid" and codes == []


def test_d1_external_batch_default_1_rejected():
    p = pd.ParamDecl("img_batch", "int", required=False,
                     has_default=True, default=1)
    b = pd.ParamBinding("batch", "external", external_key="img_batch")
    bad = _replace(_replace_node(T1, "img",
                                 param_bindings=_img_bindings(
                                     batch_binding=b)),
                   external_params=T1.external_params + (p,))
    structural, codes = _valid(bad)
    assert structural == "invalid"
    assert "cardinality_pin_violation" in codes


def test_d1_external_batch_choices_1_rejected():
    p = pd.ParamDecl("img_batch", "int", required=False,
                     has_default=True, default=1,
                     constraints=(pd.Constraint("choices", (1,)),))
    b = pd.ParamBinding("batch", "external", external_key="img_batch")
    bad = _replace(_replace_node(T1, "img",
                                 param_bindings=_img_bindings(
                                     batch_binding=b)),
                   external_params=T1.external_params + (p,))
    _, codes = _valid(bad)
    assert "cardinality_pin_violation" in codes


def test_d1_external_batch_range_1_4_rejected():
    p = pd.ParamDecl("img_batch", "int", required=False,
                     has_default=True, default=1,
                     constraints=(pd.Constraint("min", 1),
                                  pd.Constraint("max", 4)))
    b = pd.ParamBinding("batch", "external", external_key="img_batch")
    bad = _replace(_replace_node(T1, "img",
                                 param_bindings=_img_bindings(
                                     batch_binding=b)),
                   external_params=T1.external_params + (p,))
    _, codes = _valid(bad)
    assert "cardinality_pin_violation" in codes


def test_d1_constant_batch_2_rejected():
    b = pd.ParamBinding("batch", "constant", constant=2)
    bad = _replace_node(T1, "img",
                        param_bindings=_img_bindings(batch_binding=b))
    _, codes = _valid(bad)
    assert "cardinality_pin_violation" in codes


def test_d1_constant_batch_4_rejected():
    b = pd.ParamBinding("batch", "constant", constant=4)
    bad = _replace_node(T1, "img",
                        param_bindings=_img_bindings(batch_binding=b))
    _, codes = _valid(bad)
    assert "cardinality_pin_violation" in codes


def test_d1_constant_batch_true_rejected():
    b = pd.ParamBinding("batch", "constant", constant=True)
    bad = _replace_node(T1, "img",
                        param_bindings=_img_bindings(batch_binding=b))
    _, codes = _valid(bad)
    assert "cardinality_pin_violation" in codes


def test_d1_missing_batch_binding_rejected():
    bad = _replace_node(T1, "img",
                        param_bindings=_img_bindings(drop_batch=True))
    _, codes = _valid(bad)
    assert "cardinality_pin_violation" in codes


def test_d1_duplicate_batch_bindings_rejected():
    keep = list(T1.nodes[0].param_bindings)
    keep.append(pd.ParamBinding("batch", "constant", constant=1))
    bad = _replace_node(T1, "img", param_bindings=tuple(keep))
    structural, codes = _valid(bad)
    assert structural == "invalid"
    # Phase-A duplicate-key check fires first — the definition is
    # rejected before Phase-B pin analysis ever runs (bounds before
    # traversal).
    assert "conflicting_bindings" in codes


# ==================================================================
# D2 — non-finite numeric rejection
# ==================================================================


def test_d2_nonfinite_external_defaults_rejected():
    for bad_val in (float("nan"), float("inf"), float("-inf")):
        p = pd.ParamDecl("x", "float", required=False,
                         has_default=True, default=bad_val)
        bad = _replace(T1,
                       external_params=T1.external_params + (p,))
        structural, codes = _valid(bad)
        assert structural == "invalid", bad_val
        assert "default_type_mismatch" in codes


def test_d2_nonfinite_constants_rejected():
    for bad_val in (float("nan"), float("inf"), float("-inf")):
        b = pd.ParamBinding("cfg", "constant", constant=bad_val)
        bindings = T1.nodes[0].param_bindings + (b,)
        bad = _replace_node(T1, "img", param_bindings=bindings)
        structural, codes = _valid(bad)
        assert structural == "invalid", bad_val
        assert "constant_type_mismatch" in codes


def test_d2_nonfinite_constraint_values_rejected():
    for kind in ("min", "max"):
        for bad_val in (float("nan"), float("inf"), float("-inf")):
            p = pd.ParamDecl("x", "float", required=False,
                             constraints=(pd.Constraint(kind,
                                                        bad_val),))
            assert "invalid_constraint" in _constraint_codes(p), \
                (kind, bad_val)


def test_d2_nan_in_choices_rejected():
    p = pd.ParamDecl("x", "float", required=False,
                     constraints=(pd.Constraint(
                         "choices", (1.0, float("nan"))),))
    assert "invalid_constraint" in _constraint_codes(p)


def test_d2_bool_still_not_int():
    b = pd.ParamBinding("cfg", "constant", constant=True)
    bindings = T1.nodes[0].param_bindings + (b,)
    bad = _replace_node(T1, "img", param_bindings=bindings)
    _, codes = _valid(bad)
    assert "constant_type_mismatch" in codes


def test_d2_finite_values_still_valid():
    p = pd.ParamDecl("ok_f", "float", required=False,
                     has_default=True, default=0.5,
                     constraints=(pd.Constraint("min", 0.0),
                                  pd.Constraint("max", 1.0)))
    good = _replace(T1, external_params=T1.external_params + (p,))
    structural, codes = _valid(good)
    assert structural == "valid" and codes == []


def test_d2_describe_never_emits_nonfinite_json():
    p = pd.ParamDecl("x", "float", required=False,
                     has_default=True, default=float("nan"))
    bad = _replace(T1, external_params=T1.external_params + (p,))
    d = pd.describe_pipeline(bad, CTX)
    raw = json.dumps(d, allow_nan=False)   # strict — raises on NaN
    assert "NaN" not in raw
    xp = next(x for x in d["external_params"]
              if x["param_key"] == "x")
    assert xp["default"] is None
    assert d["validation"]["structural"] == "invalid"


def test_d2_fingerprint_deterministic_on_nonfinite():
    p = pd.ParamDecl("x", "float", required=False,
                     has_default=True, default=float("nan"))
    bad = _replace(T1, external_params=T1.external_params + (p,))
    fp1 = pd.definition_fingerprint(bad)
    fp2 = pd.definition_fingerprint(bad)
    assert fp1 == fp2 and fp1.startswith("pipe:v1:")


# ==================================================================
# D3 — literal persisted origin evidence
# ==================================================================


def test_d3_contract_origin_is_persisted_literal():
    """Adapters emit origin='generation' (jobs.py 'generated' is only
    the fallback default); edge provenance stays the semantic label."""
    assert pd.COMMAND_CONTRACTS["generate.image"].output_origin \
        == "generation"
    assert pd.COMMAND_CONTRACTS["generate.video"].output_origin \
        == "generation"
    assert T1.edges[0].provenance == "generated"   # semantic axis


def test_d3_edge_still_compatible():
    structural, codes = _valid(T1)
    assert structural == "valid" and codes == []


# ==================================================================
# D4 / D5 — binding_support classifications
# ==================================================================


def test_d4_loras_binding_unsupported_on_image():
    b = pd.ParamBinding("loras", "constant", constant="x:1.0")
    bindings = T1.nodes[0].param_bindings + (b,)
    bad = _replace_node(T1, "img", param_bindings=bindings)
    structural, codes = _valid(bad)
    assert structural == "invalid"
    assert "unsupported_binding_target" in codes


def test_d4_device_partial_still_bindable_as_string():
    b = pd.ParamBinding("device", "constant", constant="cpu")
    bindings = T1.nodes[0].param_bindings + (b,)
    good = _replace_node(T1, "img", param_bindings=bindings)
    structural, codes = _valid(good)
    assert structural == "valid" and codes == []


def test_d4_arg_classifications():
    c_img = pd.COMMAND_CONTRACTS["generate.image"]
    assert c_img.arg("loras").binding_support == "unsupported"
    assert c_img.arg("device").binding_support == "partial"
    rps = c_img.arg("runtime_prompt_sha256")
    assert rps.computed and "passthrough" in rps.note
    c_vid = pd.COMMAND_CONTRACTS["generate.video"]
    assert c_vid.arg("loras").refused


def test_d5_video_dims_not_bindable():
    for arg in ("width", "height"):
        b = pd.ParamBinding(arg, "constant", constant=512)
        bindings = T1.nodes[1].param_bindings + (b,)
        bad = _replace_node(T1, "vid", param_bindings=bindings)
        structural, codes = _valid(bad)
        assert structural == "invalid", arg
        assert "unsupported_binding_target" in codes
    c = pd.COMMAND_CONTRACTS["generate.video"]
    assert c.arg("width").binding_support == "unsupported"
    assert "paired" in c.arg("width").note


# ==================================================================
# D6 — duplicate registry detection before deduplication
# ==================================================================


def test_d6_unique_valid_declarations():
    s, f = pd.pipeline_registry(CTX, declarations=(T1,))
    assert s == "valid" and f == []


def test_d6_duplicate_identical_defs_flagged():
    s, f = pd.pipeline_registry(CTX, declarations=(T1, T1))
    assert s == "invalid"
    assert "duplicate_pipeline_key" in _codes(f)


def test_d6_duplicate_different_defs_flagged():
    twin = _replace(T1, display_name="Twin", description="other")
    s, f = pd.pipeline_registry(CTX, declarations=(T1, twin))
    assert s == "invalid"
    assert "duplicate_pipeline_key" in _codes(f)


def test_d6_registry_over_64_declarations():
    decls = tuple(_replace(T1, pipeline_key=f"pipe_{i:02d}")
                  for i in range(65))
    s, f = pd.pipeline_registry(CTX, declarations=decls)
    assert s == "invalid"
    assert "limit_exceeded" in _codes(f)


def test_d6_duplicate_detected_before_dedup():
    """The ordered source keeps BOTH declarations — the finding fires
    even though _build_registry would first-wins-collapse them."""
    decls = (T1, _replace(T1, definition_version=9))
    assert len(decls) == 2
    _, f = pd.pipeline_registry(CTX, declarations=decls)
    assert "duplicate_pipeline_key" in _codes(f)
    reg = pd._build_registry(decls)
    assert reg["img2vid_chain"].definition_version == 1


# ==================================================================
# D7 — concurrency analysis gated on valid bounded graphs
# ==================================================================


def _broken_defs():
    too_many = _replace(T1, nodes=tuple(
        dataclasses.replace(T1.nodes[0], node_id=f"n{i}")
        for i in range(33)))
    too_many_edges = _replace(T1, edges=tuple(
        dataclasses.replace(T1.edges[0], edge_id=f"e{i}")
        for i in range(65)))
    missing_ref = _replace(T1, edges=(
        dataclasses.replace(T1.edges[0], dst_node="ghost"),))
    no_edge = _replace(T1, edges=())
    return (too_many, too_many_edges, missing_ref, no_edge)


def test_d7_concurrency_skipped_on_invalid_defs(monkeypatch):
    calls = {"n": 0}

    def counting(defn):
        calls["n"] += 1
        return []

    monkeypatch.setattr(pd, "concurrency_pairs", counting)
    for bad in _broken_defs():
        d = pd.describe_pipeline(bad, CTX)
        assert d["validation"]["structural"] == "invalid"
        assert d["concurrency_pairs"] == []
    assert calls["n"] == 0


def test_d7_concurrency_runs_on_valid_t1(monkeypatch):
    calls = {"n": 0}
    real = pd.concurrency_pairs

    def counting(defn):
        calls["n"] += 1
        return real(defn)

    monkeypatch.setattr(pd, "concurrency_pairs", counting)
    d = pd.describe_pipeline(T1, CTX)
    assert calls["n"] == 1
    assert d["concurrency_pairs"] == []


# ==================================================================
# D8 — constraint declarations validated independent of defaults
# ==================================================================


def test_d8_min_gt_max_without_default_rejected():
    p = pd.ParamDecl("x", "int", required=False,
                     constraints=(pd.Constraint("min", 10),
                                  pd.Constraint("max", 5)))
    bad = _replace(T1, external_params=T1.external_params + (p,))
    structural, codes = _valid(bad)
    assert structural == "invalid"
    assert "invalid_constraint" in codes


def test_d8_min_gt_max_with_default_rejected():
    p = pd.ParamDecl("x", "int", required=False, has_default=True,
                     default=7,
                     constraints=(pd.Constraint("min", 10),
                                  pd.Constraint("max", 5)))
    bad = _replace(T1, external_params=T1.external_params + (p,))
    _, codes = _valid(bad)
    assert "invalid_constraint" in codes


def test_d8_duplicate_constraint_kinds_rejected():
    p = pd.ParamDecl("x", "int", required=False,
                     constraints=(pd.Constraint("min", 5),
                                  pd.Constraint("min", 9)))
    assert "invalid_constraint" in _constraint_codes(p)


def test_d8_len_bounds_contradiction_rejected():
    p = pd.ParamDecl("x", "string", required=False,
                     constraints=(pd.Constraint("min_len", 5),
                                  pd.Constraint("max_len", 2)))
    assert "invalid_constraint" in _constraint_codes(p)


def test_d8_bad_pattern_rejected():
    p = pd.ParamDecl("x", "string", required=False,
                     constraints=(pd.Constraint("pattern", "(["),))
    assert "invalid_constraint" in _constraint_codes(p)


def test_d8_choices_element_type_mismatch_rejected():
    p = pd.ParamDecl("x", "int", required=False,
                     constraints=(pd.Constraint("choices",
                                                (1, "two")),))
    assert "invalid_constraint" in _constraint_codes(p)


def test_d8_binding_vs_contract_empty_domain():
    keep = [b for b in T1.nodes[0].param_bindings
            if b.request_key != "width"]
    keep.append(pd.ParamBinding(
        "width", "external", external_key="width",
        constraints=(pd.Constraint("min", 5000),)))
    bad = _replace_node(T1, "img", param_bindings=tuple(keep))
    structural, codes = _valid(bad)
    assert structural == "invalid"
    assert "conflicting_constraints" in codes


def test_d8_external_vs_contract_empty_domain():
    p = pd.ParamDecl("width_hi", "int", required=True,
                     constraints=(pd.Constraint("min", 5000),))
    keep = [b for b in T1.nodes[0].param_bindings
            if b.request_key != "width"]
    keep.append(pd.ParamBinding("width", "external",
                                external_key="width_hi"))
    bad = _replace(_replace_node(T1, "img",
                                 param_bindings=tuple(keep)),
                   external_params=T1.external_params + (p,))
    _, codes = _valid(bad)
    assert "conflicting_constraints" in codes


def test_d8_disjoint_choices_empty_domain():
    """External-param choices vs binding choices — the intersection is
    empty, so NO value can ever satisfy the bound argument."""
    p = pd.ParamDecl("w", "int", required=True,
                     constraints=(pd.Constraint("choices",
                                                (512, 768)),))
    keep = [b for b in T1.nodes[0].param_bindings
            if b.request_key != "width"]
    keep.append(pd.ParamBinding("width", "external", external_key="w",
                                constraints=(pd.Constraint(
                                    "choices", (64, 128)),)))
    bad = _replace(_replace_node(T1, "img",
                                 param_bindings=tuple(keep)),
                   external_params=T1.external_params + (p,))
    structural, codes = _valid(bad)
    assert structural == "invalid"
    assert "conflicting_constraints" in codes


def test_d8_overlapping_choices_pass():
    p = pd.ParamDecl("w", "int", required=True,
                     constraints=(pd.Constraint("choices",
                                                (512, 768)),))
    keep = [b for b in T1.nodes[0].param_bindings
            if b.request_key != "width"]
    keep.append(pd.ParamBinding("width", "external", external_key="w",
                                constraints=(pd.Constraint(
                                    "choices", (512, 1024)),)))
    good = _replace(_replace_node(T1, "img",
                                  param_bindings=tuple(keep)),
                    external_params=T1.external_params + (p,))
    _, codes = _valid(good)
    assert "conflicting_constraints" not in codes


def test_d8_valid_compatible_ranges_pass():
    p = pd.ParamDecl("x", "int", required=False,
                     constraints=(pd.Constraint("min", 1),
                                  pd.Constraint("max", 10)))
    good = _replace(T1, external_params=T1.external_params + (p,))
    structural, codes = _valid(good)
    assert structural == "valid" and codes == []


# ==================================================================
# Gate 9 — fingerprint material-change coverage
# ==================================================================


def test_g9_ctx_fp_contract_arg_type_change(monkeypatch):
    fp1 = pd.contract_evidence_fingerprint(T1, CTX)
    c = pd.COMMAND_CONTRACTS["generate.image"]
    mod = dataclasses.replace(
        c, args=tuple(dataclasses.replace(a, ptype="float")
                      if a.name == "width" else a for a in c.args))
    monkeypatch.setitem(pd.COMMAND_CONTRACTS, "generate.image", mod)
    assert pd.contract_evidence_fingerprint(T1, CTX) != fp1


def test_g9_ctx_fp_required_arg_change(monkeypatch):
    fp1 = pd.contract_evidence_fingerprint(T1, CTX)
    c = pd.COMMAND_CONTRACTS["generate.video"]
    mod = dataclasses.replace(
        c, args=tuple(dataclasses.replace(a, required=False)
                      if a.name == "model_key" else a
                      for a in c.args))
    monkeypatch.setitem(pd.COMMAND_CONTRACTS, "generate.video", mod)
    assert pd.contract_evidence_fingerprint(T1, CTX) != fp1


def test_g9_ctx_fp_runtime_state_change():
    fp1 = pd.contract_evidence_fingerprint(T1, CTX)
    ctx2 = _ctx(runtime_planned=(("sdcpp", True),))
    assert pd.contract_evidence_fingerprint(T1, ctx2) != fp1


def test_g9_ctx_fp_workflow_policy_change():
    fp1 = pd.contract_evidence_fingerprint(T1, CTX)
    ctx2 = _ctx(workflows=(
        pd.WorkflowEvidence("text2image", "implemented", "lora_hold",
                            ("diffusion",), ("lora",), 1),
        _DEFAULT_WORKFLOWS[1]))
    assert pd.contract_evidence_fingerprint(T1, ctx2) != fp1


def test_g9_def_fp_fixed_constant_change():
    bad = _replace_node(T1, "img", param_bindings=_img_bindings(
        batch_binding=pd.ParamBinding("batch", "constant",
                                      constant=2)))
    assert pd.definition_fingerprint(bad) != \
        pd.definition_fingerprint(T1)


def test_g9_def_fp_param_constraint_change():
    p = pd.ParamDecl("width", "int", required=False,
                     has_default=True, default=512,
                     constraints=(pd.Constraint("min", 1),
                                  pd.Constraint("max", 2048)),
                     description="image width")
    bad = _replace(T1, external_params=tuple(
        p if x.param_key == "width" else x
        for x in T1.external_params))
    assert pd.definition_fingerprint(bad) != \
        pd.definition_fingerprint(T1)
