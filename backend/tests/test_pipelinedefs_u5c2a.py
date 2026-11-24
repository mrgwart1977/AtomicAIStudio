"""U-5c-2a — declarative pipeline contracts.

Covers the accepted R1–R6 architecture plus safeguards S1–S6:
deep immutability, audited command contracts, typed ports/edges,
parameter bindings, graph validation/limits, static classifications,
concurrency derivation, both fingerprints, and read-only API guards.
"""

from __future__ import annotations

import dataclasses

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
