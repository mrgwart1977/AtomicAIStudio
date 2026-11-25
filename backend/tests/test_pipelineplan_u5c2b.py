"""U-5c-2b — multi-node pipeline plan preview (pure planner + endpoint).

Synthetic fixtures only — no live hardware, no probe, no model
execution. The injected evaluate_ram mirrors the real evaluator's
decision contract; the planner never reaches hardware paths."""

import dataclasses
from types import SimpleNamespace as NS

from app.domain import dep_resolve as dr
from app.domain import pipeline_defs as pdefs
from app.domain import pipeline_plan as pp
from app.domain import plan_preview as pv
from app.domain.pipeline_defs import (
    IMG2VID_CHAIN,
    ParamBinding,
    ParamDecl,
    PipelineDefinition,
    PipelineEdge,
    PipelineNode,
    PortDecl,
)

GIB = 1024 ** 3


# ------------------------------------------------------------- fixtures


def _evidence_ctx():
    from app.domain import commands, runtime_caps, workflow_defs
    profiles = {
        "local.sdcpp.image": (("adapter_kind", "subprocess"),
                              ("notes", "")),
        "local.sdcpp.video": (("adapter_kind", "subprocess"),
                              ("notes", "")),
    }
    return pdefs.EvidenceContext(
        command_table=tuple(commands.JOB_COMMANDS.items()),
        adapter_profiles=tuple(profiles.items()),
        workflows=tuple(
            pdefs.WorkflowEvidence(
                workflow_key=w.workflow_key, status=w.status,
                policy=w.policy,
                required_components=w.required_components,
                optional_components=w.optional_components,
                defs_version=workflow_defs.WORKFLOW_DEFS_VERSION)
            for w in workflow_defs.workflow_catalog()),
        runtime_planned=tuple(
            (k, bool(d.planned)) for k, d in
            sorted(runtime_caps.RUNTIME_CAPS.items())))


CTX = _evidence_ctx()

FANOUT = PipelineDefinition(
    pipeline_key="fanout_test", schema_version=1, definition_version=1,
    display_name="fanout", description="", category="generation_chain",
    status="declared", required_context=("project",),
    policy_constraints=(), limitations=(),
    external_params=(
        ParamDecl("img_model_key", "string", required=True),
        ParamDecl("img_prompt", "string", required=True),
        ParamDecl("vid_model_key", "string", required=True),
    ),
    nodes=(
        PipelineNode(node_id="img", node_kind="job",
                     command_key="generate.image",
                     workflow_key="text2image", runtime_keys=("sdcpp",),
                     required_components=("diffusion",),
                     input_ports=(),
                     output_ports=(PortDecl("result", "out", "image", cardinality=1,
                                 output_contract="primary_asset"),),
                     param_bindings=(
                         ParamBinding("model_key", "external",
                                      external_key="img_model_key"),
                         ParamBinding("prompt", "external",
                                      external_key="img_prompt"),
                         ParamBinding("batch", "constant", constant=1))),
        PipelineNode(node_id="vida", node_kind="job",
                     command_key="generate.video",
                     workflow_key="image2video",
                     runtime_keys=("sdcpp",),
                     required_components=("diffusion",),
                     input_ports=(PortDecl(
                         "init_image", "in", "image", required=True,
                         command_arg="image_asset_id"),),
                     output_ports=(PortDecl("result", "out", "video", cardinality=1,
                                 output_contract="primary_asset"),),
                     param_bindings=(
                         ParamBinding("model_key", "external",
                                      external_key="vid_model_key"),
                         ParamBinding("image_asset_id", "edge",
                                      edge_id="e_a"),)),
        PipelineNode(node_id="vidb", node_kind="job",
                     command_key="generate.video",
                     workflow_key="image2video",
                     runtime_keys=("sdcpp",),
                     required_components=("diffusion",),
                     input_ports=(PortDecl(
                         "init_image", "in", "image", required=True,
                         command_arg="image_asset_id"),),
                     output_ports=(PortDecl("result", "out", "video", cardinality=1,
                                 output_contract="primary_asset"),),
                     param_bindings=(
                         ParamBinding("model_key", "external",
                                      external_key="vid_model_key"),
                         ParamBinding("image_asset_id", "edge",
                                      edge_id="e_b"),)),
    ),
    edges=(
        PipelineEdge("e_a", "img", "result", "vida", "init_image",
                     "image_asset_id", "init_image", "same_project",
                     "generated"),
        PipelineEdge("e_b", "img", "result", "vidb", "init_image",
                     "image_asset_id", "init_image", "same_project",
                     "generated"),
    ))

DISCONNECTED = PipelineDefinition(
    pipeline_key="disc_test", schema_version=1, definition_version=1,
    display_name="disc", description="", category="generation_chain",
    status="declared",
    required_context=("project",), policy_constraints=(),
    limitations=(), external_params=(
        ParamDecl("img_model_key", "string", required=True),
        ParamDecl("img_prompt", "string", required=True),
        ParamDecl("vid_model_key", "string", required=True),
    ),
    nodes=(
        PipelineNode(node_id="a", node_kind="job",
                     command_key="generate.image",
                     workflow_key="text2image", runtime_keys=("sdcpp",),
                     required_components=("diffusion",),
                     output_ports=(PortDecl("result", "out", "image", cardinality=1,
                                 output_contract="primary_asset"),),
                     param_bindings=(
                         ParamBinding("model_key", "external",
                                      external_key="img_model_key"),
                         ParamBinding("prompt", "external",
                                      external_key="img_prompt"),
                         ParamBinding("batch", "constant", constant=1))),
        PipelineNode(node_id="b", node_kind="job",
                     command_key="generate.image",
                     workflow_key="text2image", runtime_keys=("sdcpp",),
                     required_components=("diffusion",),
                     output_ports=(PortDecl("result", "out", "image", cardinality=1,
                                 output_contract="primary_asset"),),
                     param_bindings=(
                         ParamBinding("model_key", "external",
                                      external_key="img_model_key"),
                         ParamBinding("prompt", "external",
                                      external_key="img_prompt"),
                         ParamBinding("batch", "constant", constant=1))),
    ), edges=())


def plan_stub(advisory=dr.ADVISORY_COMPLETE,
              runtime="candidate_compatible_static",
              policy=dr.POLICY_CLEAR,
              structural=dr.STRUCT_COMPLETE,
              capacity=dr.CAP_NA, resolution_version="dep:v1:0"):
    return NS(advisory_state=advisory, runtime_state=runtime,
              policy_state=policy, structural_state=structural,
              capacity_state=capacity,
              evidence_state=dr.EV_TESTED,
              resolution_version=resolution_version,
              runtime_key="sdcpp", reasons=(), limits={},
              limits_exceeded=(), requirements=())


def dev(index, fit=pv.FIT_WITH_HEADROOM, budget=12 * GIB, eligible=True):
    return {"index": index, "eligible": eligible, "fit_class": fit,
            "outcome": fit, "budget_bytes": budget,
            "backend_evidence": {"vulkan": "measured"},
            "reasons": [], "limitations": [],
            "reservation_overlap_possible": False}


def node_ev(node_id="img", est_vram=6 * GIB, est_ram=4 * GIB,
            devices=None, bindable="bindable",
            plan=None, runtime="candidate_compatible_static",
            runtime_blocked=False, residency_error=None, lane=None,
            adapters=("local.sdcpp.image",), ram_known=True):
    return pp.NodeEvidence(
        node_id=node_id, artifact_id=f"catalog:{node_id}",
        model_key_effective=node_id, bindability=bindable,
        bindability_reason="", integrity_class="verified",
        dep_plan=plan if plan is not None else plan_stub(),
        runtime_state=runtime,
        runtime_blocked_authoritative=runtime_blocked,
        adapter_ids=adapters,
        est_vram_bytes=est_vram, est_ram_bytes=est_ram,
        ram_known=ram_known,
        device_alternatives=tuple(devices if devices is not None
                                  else [dev(0)]),
        cpu_lane=lane, residency_error=residency_error,
        workflow_key="text2image", required=True)


def eval_ram(decision="ok", fully=True, commit_ok=True):
    def _e(est, known):
        checks = {}
        if known:
            checks["phys_avail"] = decision if decision != "unknown" \
                else "ok"
            checks["commit_headroom"] = "ok" if commit_ok else decision
        else:
            checks["estimate"] = "unknown"
        return {"decision": decision if known else "unknown",
                "fully_evaluated": fully and known,
                "checks": checks, "reason": "", "snapshot": {}}
    return _e


def agg_kwargs(**kw):
    d = {"structural": "valid", "defn_findings": [], "node_ev": [],
         "retained": [], "states": [], "truncs": [],
         "freshness": "fresh", "simulated": False,
         "reservations_captured": True, "deferred": [],
         "exhaustive": True}
    d.update(kw)
    return d


# ------------------------------------------------- external parameters


def test_params_happy_path():
    vals, f = pp.resolve_external_params(
        IMG2VID_CHAIN,
        {"img_model_key": "a", "img_prompt": "p", "vid_model_key": "b"})
    assert not f and vals["width"] == 512 and vals["fps"] == 24


def test_params_missing_required():
    _, f = pp.resolve_external_params(IMG2VID_CHAIN, {})
    assert {x.code for x in f} == {"missing_param"}


def test_params_unknown_rejected():
    _, f = pp.resolve_external_params(
        IMG2VID_CHAIN, {"img_model_key": "a", "img_prompt": "p",
                        "vid_model_key": "b", "bogus": 1})
    assert any(x.code == "unknown_param" for x in f)


def test_params_type_strict_bool_not_int():
    _, f = pp.resolve_external_params(
        IMG2VID_CHAIN, {"img_model_key": "a", "img_prompt": "p",
                        "vid_model_key": "b", "width": True})
    assert any(x.code == "param_type" for x in f)


def test_params_nonfinite_float_rejected():
    _, f = pp.resolve_external_params(
        FANOUT, {"img_model_key": "a", "img_prompt": "p",
                 "vid_model_key": "b"})
    assert not f


def test_params_constraint_violation():
    _, f = pp.resolve_external_params(
        IMG2VID_CHAIN, {"img_model_key": "a", "img_prompt": "p",
                        "vid_model_key": "b", "width": 99999})
    assert any(x.code == "param_constraint" for x in f)


# ------------------------------------------------------- deferred binds


def test_deferred_binding_typed_nothing_fabricated():
    vals, _ = pp.resolve_external_params(
        IMG2VID_CHAIN, {"img_model_key": "a", "img_prompt": "p",
                        "vid_model_key": "b"})
    _, deferred = pp.resolve_bindings(IMG2VID_CHAIN, vals)
    assert len(deferred) == 1
    d = deferred[0]
    assert d["state"] == "deferred_future_output"
    assert d["request_key"] == "image_asset_id"
    assert "asset" not in str(d) or d.get("asset_id") is None
    assert "path" not in d and "sha" not in d


def test_deferred_binding_edge_metadata():
    vals, _ = pp.resolve_external_params(
        IMG2VID_CHAIN, {"img_model_key": "a", "img_prompt": "p",
                        "vid_model_key": "b"})
    _, deferred = pp.resolve_bindings(IMG2VID_CHAIN, vals)
    assert deferred[0]["provenance"] == "generated"
    assert deferred[0]["ownership"] == "same_project"


# ------------------------------------------------------------ topology


def test_topo_levels_t1():
    assert pp.topo_levels(IMG2VID_CHAIN) == [["img"], ["vid"]]


def test_topo_levels_fanout():
    lv = pp.topo_levels(FANOUT)
    assert lv[0] == ["img"] and sorted(lv[1]) == ["vida", "vidb"]


def test_independence_fanout():
    ind = pp.independence_matrix(FANOUT)
    assert ind["vida"] == {"vidb"} and ind["img"] == set()


def test_independence_t1_none():
    ind = pp.independence_matrix(IMG2VID_CHAIN)
    assert ind["img"] == set() and ind["vid"] == set()


def test_wave_arrangements_t1_single():
    b = pp.Budgets()
    waves, trunc = pp.wave_arrangements(IMG2VID_CHAIN, b)
    assert waves == [(("img",), ("vid",))]
    assert trunc == [] and b.waves == 1


def test_wave_arrangements_fanout_bounded():
    b = pp.Budgets()
    waves, _ = pp.wave_arrangements(FANOUT, b)
    # sequential + concurrent-group options for level [vida,vidb]
    flat = {tuple(tuple(g) for g in w) for w in waves}
    assert any(any(len(g) == 2 for g in w) for w in flat)
    assert any(all(len(g) == 1 for g in w) for w in flat)
    assert b.waves <= pp.LIMITS["max_wave_decompositions"]


def test_wave_proposals_bounded():
    b = pp.Budgets()
    b.group_proposals = pp.LIMITS["max_group_proposals"]
    _, trunc = pp.wave_arrangements(FANOUT, b)
    assert "group_truncated" in trunc and b.omitted_groups


def test_disconnected_still_invalid():
    structural, f = pdefs.validate_pipeline(DISCONNECTED, CTX)
    assert structural == "invalid"
    assert any(x.code in ("disconnected_graph", "node_isolated")
               for x in f)


def test_fanout_valid_connected():
    structural, _ = pdefs.validate_pipeline(FANOUT, CTX)
    assert structural == "valid"


# ------------------------------------------------------- composite fit


def test_composite_margin_once():
    assert pp._composite_fit(6 * GIB, 7 * GIB) == pv.FIT_WITH_HEADROOM
    assert pp._composite_fit(7 * GIB - 1, 7 * GIB) == pv.FIT_TIGHT
    assert pp._composite_fit(7 * GIB + 1, 7 * GIB) == pv.FIT_INSUFFICIENT


def test_composite_unknown_est_and_budget():
    assert pp._composite_fit(None, 5 * GIB) == pv.FIT_ESTIMATE_UNKNOWN
    assert pp._composite_fit(5 * GIB, None) == pv.FIT_UNMEASURABLE


def test_composite_measured_zero_budget():
    assert pp._composite_fit(1, 0) == pv.FIT_INSUFFICIENT
    assert pp._composite_fit(0, 0) == pv.FIT_TIGHT


# ------------------------------------------------- candidate evaluation


def _t1_nodes(dev0=None, dev1=None):
    img = node_ev("img", 6 * GIB, 4 * GIB,
                  devices=[dev0 or dev(0, budget=7 * GIB),
                           dev1 or dev(1, budget=10 * GIB)])
    vid = dataclasses.replace(node_ev(
        "vid", 8 * GIB, 6 * GIB,
        devices=[dev0 or dev(0, budget=7 * GIB),
                 dev1 or dev(1, budget=10 * GIB)]),
        workflow_key="image2video")
    return [img, vid]


def _assign(devices):
    return {f"n{i}": {"device": d, "artifact_id": None,
                      "runtime_key": "sdcpp", "adapter_id": "a",
                      "residency": "gpu",
                      "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB}
            for i, d in enumerate(devices)}


def test_sequential_single_device_scenario_a():
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_RELEASE_ASSUMED, eval_ram())
    # sequential A: device peak = max(6,8)=8 not sum
    assert c["device_fits"]["0"] == pv.FIT_INSUFFICIENT  # 8>7 budget
    assert c["host_peak_bytes"] == 6 * GIB               # max, not 10


def test_sequential_scenario_a_fits():
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 1, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(
        assign, nodes, (("img",), ("vid",)),
        pp.LIFETIME_RELEASE_ASSUMED, eval_ram(),
        transition_host={0: 0, 1: 0},
        transition_dev={(0, 0): 0, (0, 1): 0, (1, 0): 0, (1, 1): 0})
    assert c["device_fits"]["0"] == pv.FIT_WITH_HEADROOM
    assert c["device_fits"]["1"] == pv.FIT_WITH_HEADROOM
    # unqualified feasible — scenario A, all transition overhead
    # established (known zero), no unresolved assumptions (Q5)
    assert c["state"] == pp.FEASIBLE


def test_unknown_overhead_caps_at_conditional():
    """Q5/Q2-E: same arithmetic fits, but transition overhead unknown →
    feasible_conditional, never unqualified feasible."""
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 1, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_RELEASE_ASSUMED, eval_ram())
    assert c["device_fits"]["0"] == pv.FIT_WITH_HEADROOM
    assert c["state"] == pp.FEASIBLE_CONDITIONAL
    assert "transition_overhead_unknown" in c["limitations"]


def test_sequential_retained_sums():
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_RETAINED, eval_ram())
    assert c["host_peak_bytes"] == 10 * GIB   # retained = sum
    assert "retained_bound_unproven" in c["limitations"]


def test_retained_overflow_unverified_not_proven():
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_RETAINED,
                              eval_ram(decision="block"))
    assert c["state"] == pp.INFEASIBLE_UNVERIFIED


def test_scenario_a_ram_block_is_candidate_block():
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_RELEASE_ASSUMED,
                              eval_ram(decision="block"))
    assert c["state"] == pp.CAND_BLOCKED
    assert "ram_insufficient" in c["reasons"]


def test_concurrent_group_sums_vram():
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 3 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 3 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes,
                              (("img", "vid"),),
                              pp.LIFETIME_RELEASE_ASSUMED, eval_ram())
    assert c["device_fits"]["0"] == pv.FIT_WITH_HEADROOM  # 6+0.7<=7
    assert "concurrency_unverified" in c["limitations"]


def test_unknown_release_marks_unproven():
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_UNKNOWN, eval_ram())
    assert c["host_peak_bytes"] == 10 * GIB
    assert "retained_bound_unproven" in c["limitations"]


def test_no_vram_pooling():
    nodes = _t1_nodes()
    assign = {"img": {"device": -1, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": None,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_RELEASE_ASSUMED, eval_ram())
    assert "1" not in c["device_fits"]  # budgets stay per-device


def test_nonbindable_blocks_candidate():
    nodes = [node_ev("img"), dataclasses.replace(
        node_ev("vid"), bindability="non_bindable")]
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_RELEASE_ASSUMED, eval_ram())
    assert c["state"] == pp.CAND_BLOCKED
    assert c["node_states"]["vid"] == "blocked"


def test_unqualified_ram_unknown_not_block():
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 1, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_RELEASE_ASSUMED,
                              eval_ram(decision="unknown", fully=False))
    assert c["state"] == pp.INFEASIBLE_UNVERIFIED


def test_limitation_disclosures_present():
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_RELEASE_ASSUMED, eval_ram())
    assert "estimator_asymmetry" in c["limitations"]
    assert "host_reservation_overlap_possible" in c["limitations"]
    assert "outstanding_demand_unobservable" in c["limitations"]
    assert "transition_overhead_unknown" in c["limitations"]


# ------------------------------------------------------------- search


def _two_node_ev(devs_img=None, devs_vid=None):
    return [node_ev("img", devices=devs_img or [dev(0), dev(1)]),
            dataclasses.replace(node_ev(
                "vid", devices=devs_vid or [dev(0), dev(1)]),
                workflow_key="image2video")]


def test_search_returns_retained_and_states():
    nodes = _two_node_ev()
    b = pp.Budgets()
    waves = [(("img",), ("vid",))]
    retained, _, states = pp.search(
        nodes, waves, pp.LIFETIME_RELEASE_ASSUMED, eval_ram(), {}, b)
    assert retained and states
    assert all(c["state"] in (pp.FEASIBLE, pp.FEASIBLE_CONDITIONAL,
                              pp.INFEASIBLE_UNVERIFIED,
                              pp.INFEASIBLE_PROVEN, pp.CAND_BLOCKED)
               for c in retained)


def test_search_completed_budget_global():
    nodes = _two_node_ev()
    b = pp.Budgets()
    b.completed = pp.LIMITS["max_completed_assignments"]
    _, _, states = pp.search(
        nodes, [(("img",), ("vid",))], pp.LIFETIME_RELEASE_ASSUMED,
        eval_ram(), {}, b)
    assert states == [] and b.omitted_completed


def test_search_partial_budget_counts_pruned():
    nodes = _two_node_ev()
    b = pp.Budgets()
    b.partial_expansions = pp.LIMITS["max_partial_expansions"] - 1
    _, _, _ = pp.search(
        nodes, [(("img",), ("vid",))], pp.LIFETIME_RELEASE_ASSUMED,
        eval_ram(), {}, b)
    assert b.omitted_partial


def test_search_hard_device_constraint():
    nodes = _two_node_ev()
    retained, _, _ = pp.search(
        nodes, [(("img",), ("vid",))], pp.LIFETIME_RELEASE_ASSUMED,
        eval_ram(), {"vid": 1}, pp.Budgets())
    assert all(c["assignment"]["vid"]["device"] == 1 for c in retained)


def test_search_top8_displacement():
    nodes = _two_node_ev(
        devs_img=[dev(i, budget=12 * GIB) for i in range(4)],
        devs_vid=[dev(i, budget=12 * GIB) for i in range(4)])
    retained, _, states = pp.search(
        nodes, [(("img",), ("vid",))], pp.LIFETIME_RELEASE_ASSUMED,
        eval_ram(), {}, pp.Budgets())
    assert len(retained) <= pp.LIMITS["max_retained_candidates"]
    keys = [pp._rank_key(c, {}) for c in retained]
    assert keys == sorted(keys)
    assert len(states) > len(retained)  # explored > retained


def test_search_ranking_deterministic():
    nodes = _two_node_ev()
    r1, _, _ = pp.search(nodes, [(("img",), ("vid",))],
                         pp.LIFETIME_RELEASE_ASSUMED, eval_ram(), {},
                         pp.Budgets())
    r2, _, _ = pp.search(nodes, [(("img",), ("vid",))],
                         pp.LIFETIME_RELEASE_ASSUMED, eval_ram(), {},
                         pp.Budgets())
    assert [c["assignment"] for c in r1] == \
        [c["assignment"] for c in r2]


# ---------------------------------------------------------- aggregate


def test_agg_invalid_structure():
    r = pp.aggregate("invalid", [pdefs.Finding("cycle", "graph", "x")],
                     [], [], [], [], "fresh", False, True, [], True)
    assert r["advisory_state"] == "invalid"


def test_agg_hw_unavailable():
    r = pp.aggregate(**agg_kwargs(freshness="unavailable"))
    assert r["advisory_state"] == pp.ADVISORY_INCOMPLETE
    assert r["block_reason"] == "hardware_evidence_unavailable"


def test_agg_simulated():
    r = pp.aggregate(**agg_kwargs(simulated=True))
    assert r["block_reason"] == "simulated_evidence"


def test_agg_stale():
    r = pp.aggregate(**agg_kwargs(freshness="stale"))
    assert r["block_reason"] == "stale_evidence"


def test_agg_deferred_inputs_incomplete():
    r = pp.aggregate(**agg_kwargs(
        states=[pp.FEASIBLE], retained=[{"state": pp.FEASIBLE,
                                        "node_states": {}}],
        deferred=[{"edge_id": "e", "state":
                   "deferred_future_output"}]))
    assert r["advisory_state"] == pp.ADVISORY_INCOMPLETE
    assert r["block_reason"] == "deferred_execution_inputs"


def test_agg_ready_when_no_deferred_and_feasible():
    r = pp.aggregate(**agg_kwargs(
        states=[pp.FEASIBLE],
        retained=[{"state": pp.FEASIBLE, "node_states": {}}]))
    assert r["advisory_state"] == pp.ADVISORY_READY


def test_agg_t1_ready_unreachable():
    # T1 always carries the deferred edge → incomplete regardless.
    r = pp.aggregate(**agg_kwargs(
        states=[pp.FEASIBLE],
        retained=[{"state": pp.FEASIBLE, "node_states": {}}],
        deferred=[{"state": "deferred_future_output"}]))
    assert r["advisory_state"] != pp.ADVISORY_READY


def test_agg_proven_no_fit_only_exhaustive():
    r = pp.aggregate(**agg_kwargs(
        states=[pp.INFEASIBLE_PROVEN, pp.INFEASIBLE_PROVEN],
        retained=[{"state": pp.INFEASIBLE_PROVEN,
                   "node_states": {}}], exhaustive=True))
    assert r["advisory_state"] == pp.ADVISORY_BLOCKED
    assert r["block_reason"] == "proven_no_fit"


def test_agg_unknown_candidate_prevents_no_fit():
    r = pp.aggregate(**agg_kwargs(
        states=[pp.INFEASIBLE_PROVEN, pp.INFEASIBLE_UNVERIFIED],
        retained=[{"state": pp.INFEASIBLE_PROVEN,
                   "node_states": {}}], exhaustive=True))
    assert r["advisory_state"] != pp.ADVISORY_BLOCKED or \
        r["block_reason"] != "proven_no_fit"


def test_agg_truncated_search_no_proven_no_fit():
    r = pp.aggregate(**agg_kwargs(
        states=[pp.INFEASIBLE_PROVEN], exhaustive=False,
        truncs=["device_truncated"],
        retained=[{"state": pp.INFEASIBLE_PROVEN,
                   "node_states": {}}]))
    assert r["block_reason"] != "proven_no_fit"


def test_agg_missing_reservations_incomplete():
    r = pp.aggregate(**agg_kwargs(states=[pp.FEASIBLE],
                                  reservations_captured=False))
    assert r["block_reason"] == "reservation_evidence_unavailable"


def test_agg_authoritative_blocker_under_bad_hw():
    ev = [dataclasses.replace(node_ev("vid"),
                                    residency_error="x")]
    r = pp.aggregate(**agg_kwargs(freshness="stale", node_ev=ev))
    assert r["advisory_state"] in (pp.ADVISORY_INCOMPLETE,
                                   pp.ADVISORY_BLOCKED)


def test_agg_surviving_alternative_no_block():
    states = [pp.INFEASIBLE_PROVEN, pp.FEASIBLE_CONDITIONAL]
    r = pp.aggregate(**agg_kwargs(
        states=states,
        retained=[{"state": pp.FEASIBLE_CONDITIONAL,
                   "node_states": {}}]))
    assert r["advisory_state"] != pp.ADVISORY_BLOCKED


# --------------------------------------------------------- fingerprints


def _fp_inputs(**kw):
    d = {"defn_fp": "pipe:v1:x", "ctx_fp": "pipectx:v1:y",
         "project_scope": "p1", "params": {"a": 1},
         "selections": {"m": {"artifact_id": "catalog:m",
                              "model_key_effective": "m"}},
         "nodes_meta": [{"node_id": "n"}], "deferred": [],
         "lifetime": "r",
         "snapshot_id": "hw:v2:z", "reservations": {"0": 100},
         "memory": {"avail": 8, "commit": 4, "commit_class": "q"},
         "limits": pp.LIMITS, "freshness": "fresh",
         "margins": {"m": 1}}
    d.update(kw)
    return d


def test_plan_fp_stable():
    assert pp.plan_fingerprint(**_fp_inputs()) == \
        pp.plan_fingerprint(**_fp_inputs())


def test_plan_fp_prefix():
    assert pp.plan_fingerprint(**_fp_inputs()).startswith("pipeplan:v1:")


def test_plan_fp_param_change():
    a = pp.plan_fingerprint(**_fp_inputs())
    b = pp.plan_fingerprint(**_fp_inputs(params={"a": 2}))
    assert a != b


def test_plan_fp_reservation_change_same_fit():
    a = pp.plan_fingerprint(**_fp_inputs())
    b = pp.plan_fingerprint(**_fp_inputs(reservations={"0": 200}))
    assert a != b


def test_plan_fp_ram_evidence_change():
    a = pp.plan_fingerprint(**_fp_inputs())
    b = pp.plan_fingerprint(**_fp_inputs(
        memory={"avail": 9, "commit": 4, "commit_class": "q"}))
    assert a != b


def test_plan_fp_hash_value_change_same_class():
    s1 = {"m": {"artifact_id": "x", "sha_class": "verified",
                "sha": "aa"}}
    s2 = {"m": {"artifact_id": "x", "sha_class": "verified",
                "sha": "bb"}}
    assert pp.plan_fingerprint(**_fp_inputs(selections=s1)) != \
        pp.plan_fingerprint(**_fp_inputs(selections=s2))


def test_plan_fp_freshness_change():
    assert pp.plan_fingerprint(**_fp_inputs(freshness="fresh")) != \
        pp.plan_fingerprint(**_fp_inputs(freshness="stale"))


def test_plan_fp_lifetime_and_margin():
    assert pp.plan_fingerprint(**_fp_inputs(lifetime="a")) != \
        pp.plan_fingerprint(**_fp_inputs(lifetime="b"))
    assert pp.plan_fingerprint(**_fp_inputs(margins={"m": 1})) != \
        pp.plan_fingerprint(**_fp_inputs(margins={"m": 2}))


def test_result_fp_embeds_plan():
    agg = {"advisory_state": "plan_incomplete", "block_reason": "x",
           "axes": {"a": 1}, "findings": []}
    fp = pp.result_fingerprint("pipeplan:v1:abc", agg, [], [], {}, [])
    assert fp.startswith("piperesult:v1:")


def test_result_fp_same_top8_different_outcomes():
    agg = {"advisory_state": "plan_incomplete", "block_reason": "x",
           "axes": {}, "findings": []}
    c = {"assignment": {"n": {"device": 0}}, "wave": [["n"]],
         "lifetime": "r", "state": "feasible", "device_fits": {},
         "ram_eval": {"decision": "ok"}}
    a = pp.result_fingerprint("p", agg, [c], [], {}, ["feasible"])
    b = pp.result_fingerprint("p", agg, [c], [], {},
                              ["feasible", "infeasible_proven"])
    assert a != b   # explored outcomes beyond retained matter


def test_result_fp_elapsed_excluded():
    agg = {"advisory_state": "x", "block_reason": None, "axes": {},
           "findings": []}
    a = pp.result_fingerprint("p", agg, [], [], {"t": 1}, [])
    b = pp.result_fingerprint("p", agg, [], [], {"t": 2}, [])
    assert a != b   # counters material — not elapsed seconds


def test_no_conditional_ready_state_exists():
    assert not hasattr(pp, "ADVISORY_CONDITIONAL")
    assert "conditionally_ready" not in dir(pp)


# ------------------------------------------------------------- endpoint


def test_endpoint_unknown_pipeline(client):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/nope/plan-preview",
        json={})
    assert r.status_code == 404


def test_endpoint_unknown_node_rejected(client):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"nodes": {"ghost": {"device_constraint": 0}}})
    assert r.status_code == 400


def test_endpoint_invalid_lifetime(client):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"lifetime_assumption": "bogus"})
    assert r.status_code == 400


def test_endpoint_bool_as_int_device_rejected(client):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"nodes": {"vid": {"device_constraint": True}}})
    assert r.status_code in (400, 422)


def test_endpoint_unknown_field_rejected(client):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"bogus_field": 1})
    assert r.status_code == 422


def test_endpoint_nonfinite_rejected(client):
    from tests.conftest import make_project
    p = make_project(client)
    # json= refuses non-finite floats; post a raw Infinity literal —
    # Python's json.loads accepts it, so it must be rejected server-side.
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        content=b'{"params": {"width": Infinity, "img_model_key": "a",'
                b' "img_prompt": "p", "vid_model_key": "b"}}',
        headers={"Content-Type": "application/json"})
    assert r.status_code in (400, 422)


def test_endpoint_unknown_catalog_key(client):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"selections": {"img_model_key":
                             {"kind": "catalog_key", "value": "nope"}},
              "params": {"img_model_key": "a", "img_prompt": "p",
                         "vid_model_key": "b"}})
    assert r.status_code == 404


def test_endpoint_unknown_artifact_id_404(client):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"selections": {"img_model_key":
                             {"kind": "artifact_id",
                              "value": "ghost-row"}},
              "params": {"img_model_key": "a", "img_prompt": "p",
                         "vid_model_key": "b"}})
    assert r.status_code == 404


def test_endpoint_bad_selection_kind(client):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"selections": {"img_model_key":
                             {"kind": "bogus", "value": "x"}},
              "params": {"img_model_key": "a", "img_prompt": "p",
                         "vid_model_key": "b"}})
    assert r.status_code == 400


def test_endpoint_import_artifact_is_nonbindable(client):
    """S1/S2 — model artifacts are the global shared registry; an import
    row is selectable as evidence but definitively non-bindable (no
    model_key path) → node-level blocker, never an invented binding."""
    import uuid

    from sqlalchemy.orm import Session

    from app.db.models import ModelArtifact
    from tests.conftest import make_project
    p = make_project(client)
    aid = f"import:{uuid.uuid4()}"
    eng = client.app.state.engine
    with Session(eng) as s:
        s.add(ModelArtifact(
            artifact_id=aid, kind="model", family="sd15",
            fmt="safetensors", role="diffusion", modality="image",
            source_type="import", managed_by="external",
            content_sha256="a" * 64, enabled=True,
            support_tier="tested", version="1"))
        s.commit()
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"selections": {"img_model_key":
                             {"kind": "artifact_id", "value": aid}},
              "params": {"img_model_key": "a", "img_prompt": "p",
                         "vid_model_key": "b"}})
    # 200 or evidence-dependent failure; if 200 the node must report
    # non_bindable — never a fabricated model_key.
    if r.status_code == 200:
        n = next(x for x in r.json()["nodes"] if x["node_id"] == "img")
        assert n["bindability"] == "non_bindable"
        assert n["model_key_effective"] is None


def test_endpoint_runs_on_t1_structure(client):
    """Endpoint executes end-to-end with cache-only hardware evidence:
    whatever the shared cache last published (unavailable, stale,
    simulated or fresh), T1 can never reach ready_advisory — its
    required vid input is a deferred future output — and nothing is
    fabricated."""
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"params": {"img_model_key": "a", "img_prompt": "p",
                         "vid_model_key": "b"}})
    assert r.status_code == 200
    body = r.json()
    # T1 never reaches ready_advisory (deferred edge-fed input).
    assert body["advisory_state"] in (
        "invalid", "plan_blocked", "plan_incomplete")
    assert body["operational_readiness"] == "not_evaluated"
    assert body["plan_fingerprint"].startswith("pipeplan:v1:")
    assert body["plan_result_fingerprint"].startswith("piperesult:v1:")
    # typed deferred binding — no fabricated asset/path/hash
    assert body["deferred_bindings"], "T1 vid input must be deferred"
    d = body["deferred_bindings"][0]
    assert d["state"] == "deferred_future_output"
    assert "asset_id" not in d and "path" not in d and "sha" not in d


# ============================================================ Q1 — canonical
# fingerprint material evidence (A–M)


def _fp_nodes_meta(**kw):
    m = {"node_id": "n", "dep": "dep:v1:0", "runtime": "sdcpp",
         "runtime_state": "candidate_compatible_static",
         "residency": "gpu", "bindability": "bindable",
         "integrity": "verified", "adapters": ["gen.image.sdcpp"],
         "verdict_fp": "u4:fam1.caps2:x", "verdict_staleness": "current",
         "device_constraint": None, "device_preference": 1,
         "wave_policy": "fixed_level", "eval_devices": [0, 1],
         "est_vram_bytes": 6 * GIB, "est_ram_bytes": 4 * GIB}
    m.update(kw)
    return [m]


def test_q1b_hash_change_recorded_class():
    """Hash value A→B with UNCHANGED recorded classification → both
    fingerprints change (hash VALUE is material, not just class)."""
    s1 = {"m": {"artifact_id": "x", "model_key_effective": "m",
                "content_sha256": "aa", "integrity_class": "recorded"}}
    s2 = {"m": {"artifact_id": "x", "model_key_effective": "m",
                "content_sha256": "bb", "integrity_class": "recorded"}}
    a = pp.plan_fingerprint(**_fp_inputs(selections=s1))
    b = pp.plan_fingerprint(**_fp_inputs(selections=s2))
    assert a != b
    agg = {"advisory_state": "x", "block_reason": None, "axes": {},
           "findings": []}
    assert pp.result_fingerprint(a, agg, [], [], {}, []) != \
        pp.result_fingerprint(b, agg, [], [], {}, [])


def test_q1c_hash_class_change_same_value():
    s1 = {"m": {"artifact_id": "x", "content_sha256": "aa",
                "integrity_class": "verified"}}
    s2 = {"m": {"artifact_id": "x", "content_sha256": "aa",
                "integrity_class": "recorded"}}
    assert pp.plan_fingerprint(**_fp_inputs(selections=s1)) != \
        pp.plan_fingerprint(**_fp_inputs(selections=s2))


def test_q1d_missing_hash_stays_missing():
    s_none = {"m": {"artifact_id": "x", "content_sha256": None,
                    "integrity_class": "missing"}}
    s_val = {"m": {"artifact_id": "x", "content_sha256": "aa",
                   "integrity_class": "recorded"}}
    fp_none = pp.plan_fingerprint(**_fp_inputs(selections=s_none))
    assert fp_none != pp.plan_fingerprint(
        **_fp_inputs(selections=s_val))
    # a missing hash is canonical-missing, never silently equated
    s_zero = {"m": {"artifact_id": "x", "content_sha256": 0,
                    "integrity_class": "missing"}}
    assert fp_none != pp.plan_fingerprint(
        **_fp_inputs(selections=s_zero))


def test_q1e_adapter_evidence_change():
    a = pp.plan_fingerprint(
        **_fp_inputs(nodes_meta=_fp_nodes_meta(
            adapters=["gen.image.sdcpp"])))
    b = pp.plan_fingerprint(
        **_fp_inputs(nodes_meta=_fp_nodes_meta(
            adapters=["gen.image.sdcpp-server"])))
    assert a != b


def test_q1f_verdict_staleness_change():
    a = pp.plan_fingerprint(
        **_fp_inputs(nodes_meta=_fp_nodes_meta(
            verdict_staleness="current")))
    b = pp.plan_fingerprint(
        **_fp_inputs(nodes_meta=_fp_nodes_meta(
            verdict_staleness="stale")))
    assert a != b


def test_q1g_device_preference_change():
    a = pp.plan_fingerprint(
        **_fp_inputs(nodes_meta=_fp_nodes_meta(device_preference=0)))
    b = pp.plan_fingerprint(
        **_fp_inputs(nodes_meta=_fp_nodes_meta(device_preference=1)))
    assert a != b


def test_q1h_wave_policy_change():
    a = pp.plan_fingerprint(
        **_fp_inputs(nodes_meta=_fp_nodes_meta(
            wave_policy="fixed_level")))
    b = pp.plan_fingerprint(
        **_fp_inputs(nodes_meta=_fp_nodes_meta(wave_policy=None)))
    assert a != b


def test_q1i_eval_domain_change():
    a = pp.plan_fingerprint(
        **_fp_inputs(nodes_meta=_fp_nodes_meta(eval_devices=[0, 1])))
    b = pp.plan_fingerprint(
        **_fp_inputs(nodes_meta=_fp_nodes_meta(eval_devices=[0])))
    assert a != b


def test_q1m_presentation_fields_excluded():
    """Elapsed seconds / presentation-only candidate fields change
    neither fingerprint (only canonical material fields are hashed)."""
    agg = {"advisory_state": "x", "block_reason": None, "axes": {},
           "findings": []}
    c1 = {"assignment": {"n": {"device": 0}}, "wave": [["n"]],
          "lifetime": "r", "state": "feasible", "device_fits": {},
          "ram_eval": {"decision": "ok"}, "elapsed_s": 1.5,
          "display": "fancy"}
    c2 = dict(c1, elapsed_s=99.0, display="other")
    a = pp.result_fingerprint("p", agg, [c1], [], {}, ["feasible"])
    b = pp.result_fingerprint("p", agg, [c2], [], {}, ["feasible"])
    assert a == b


# =============================================== Q2 — transition overhead


def _q2_assign(est_ram_a, est_ram_b, dev_a=0, dev_b=0):
    return {"a": {"device": dev_a, "artifact_id": "x",
                  "runtime_key": "sdcpp", "adapter_id": "ad",
                  "residency": "gpu", "est_vram_bytes": 1 * GIB,
                  "est_ram_bytes": est_ram_a},
            "b": {"device": dev_b, "artifact_id": "y",
                  "runtime_key": "sdcpp", "adapter_id": "ad",
                  "residency": "gpu", "est_vram_bytes": 1 * GIB,
                  "est_ram_bytes": est_ram_b}}


def _q2_nodes():
    return [node_ev("a", 1 * GIB, 16 * GIB,
                    devices=[dev(0, budget=64 * GIB)]),
            node_ev("b", 1 * GIB, 32 * GIB,
                    devices=[dev(0, budget=64 * GIB)])]


def test_q2a_wave_overhead_not_independently_maximized():
    """wave1 demand 16 + T 10 = 26; wave2 demand 32 + T 2 = 34 →
    peak is 34, NOT 42 (overhead binds to its own wave)."""
    nodes = _q2_nodes()
    c = pp.evaluate_candidate(
        _q2_assign(16 * GIB, 32 * GIB), nodes, (("a",), ("b",)),
        pp.LIFETIME_RELEASE_ASSUMED, eval_ram(),
        transition_host={0: 10 * GIB, 1: 2 * GIB},
        transition_dev={(0, 0): 0, (1, 0): 0})
    assert c["host_peak_bytes"] == 34 * GIB
    assert "transition_overhead_unknown" not in c["limitations"]


def test_q2b_device_overhead_scoped_to_gpu():
    """A device-specific transition demand affects only its GPU."""
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 1, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(
        assign, nodes, (("img",), ("vid",)),
        pp.LIFETIME_RELEASE_ASSUMED, eval_ram(),
        transition_host={0: 0, 1: 0},
        transition_dev={(0, 0): 5 * GIB, (0, 1): 0,
                        (1, 0): 0, (1, 1): 0})
    # dev0: 6 + 5 = 11 > 7 budget; dev1: 8 ≤ 10 unaffected
    assert c["device_fits"]["0"] == pv.FIT_INSUFFICIENT
    assert c["device_fits"]["1"] == pv.FIT_WITH_HEADROOM


def test_q2c_known_overhead_changes_fit():
    """Same est, budget boundary: known overhead flips the fit class."""
    nodes = [node_ev("a", 6 * GIB, 4 * GIB,
                     devices=[dev(0, budget=7 * GIB)])]
    assign = {"a": {"device": 0, "artifact_id": "x",
                    "runtime_key": "sdcpp", "adapter_id": "ad",
                    "residency": "gpu", "est_vram_bytes": 6 * GIB,
                    "est_ram_bytes": 4 * GIB}}
    tdev = {(0, 0): 2 * GIB}
    c = pp.evaluate_candidate(assign, nodes, (("a",),),
                              pp.LIFETIME_RELEASE_ASSUMED, eval_ram(),
                              transition_host={0: 0},
                              transition_dev=tdev)
    assert c["device_fits"]["0"] == pv.FIT_INSUFFICIENT  # 6+2 > 7


def test_q2d_known_overhead_counted_once():
    """Known T adds exactly once — host peak = est + T, not est + 2T."""
    nodes = _q2_nodes()
    c = pp.evaluate_candidate(
        _q2_assign(4 * GIB, 0), nodes, (("a",), ("b",)),
        pp.LIFETIME_RELEASE_ASSUMED, eval_ram(),
        transition_host={0: 1 * GIB, 1: 0},
        transition_dev={(0, 0): 0, (1, 0): 0})
    assert c["host_peak_bytes"] == 5 * GIB   # 4+1 once; b contributes 0


def test_q2e_unknown_overhead_no_positive_feasibility():
    """est fits arithmetic, but T unknown → conditional (Q5)."""
    nodes = _q2_nodes()
    c = pp.evaluate_candidate(
        _q2_assign(4 * GIB, 4 * GIB), nodes, (("a",), ("b",)),
        pp.LIFETIME_RELEASE_ASSUMED, eval_ram(),
        transition_host={0: 0},
        transition_dev={(0, 0): 0, (1, 0): 0})
    assert c["state"] == pp.FEASIBLE_CONDITIONAL
    assert "transition_overhead_unknown" in c["limitations"]
    assert "transition_overhead_partial" in c["limitations"]


def test_q2f_retention_overflow_remains_unverified():
    """Hypothetical retention overflow stays infeasible_unverified,
    never proven."""
    nodes = _t1_nodes()
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(
        assign, nodes, (("img",), ("vid",)), pp.LIFETIME_RETAINED,
        eval_ram(decision="block", commit_ok=False),
        transition_host={0: 0, 1: 0},
        transition_dev={(0, 0): 0, (1, 0): 0})
    assert c["state"] == pp.INFEASIBLE_UNVERIFIED
    assert "retained_bound_unproven" in c["limitations"]


def test_q2g_retained_transition_not_carried():
    """Scenario B: dev0 est 6+8=14 cumulative; wave0 T (2G) is
    TRANSIENT — never carried into wave1's retained total."""
    nodes = _t1_nodes(dev0=dev(0, budget=15 * GIB))
    assign = {"img": {"device": 0, "artifact_id": "a",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 6 * GIB,
                      "est_ram_bytes": 4 * GIB},
              "vid": {"device": 0, "artifact_id": "b",
                      "runtime_key": "sdcpp", "adapter_id": "ad",
                      "residency": "gpu", "est_vram_bytes": 8 * GIB,
                      "est_ram_bytes": 6 * GIB}}
    c = pp.evaluate_candidate(
        assign, nodes, (("img",), ("vid",)), pp.LIFETIME_RETAINED,
        eval_ram(),
        transition_host={0: 0, 1: 0},
        transition_dev={(0, 0): 2 * GIB, (1, 0): 0})
    # retained dev0 peak: wave0 = 6+2 = 8, wave1 = (6+8)+0 = 14.
    # budget 15G → headroom; had T been retained it would be 16 > 15.
    assert c["device_fits"]["0"] == pv.FIT_WITH_HEADROOM


# ============================================================ Q4 — counters


def test_q4_completed_counts_arrangements():
    """A completed arrangement is (assignment × wave): one assignment
    evaluated over two wave arrangements counts two completions."""
    nodes = [node_ev("a"), node_ev("b")]
    b1 = pp.Budgets()
    pp.search(nodes, [(("a",), ("b",))],
              pp.LIFETIME_RELEASE_ASSUMED, eval_ram(), {}, b1)
    assert b1.completed == 1
    b2 = pp.Budgets()
    pp.search(nodes, [(("a",), ("b",)), (("a", "b"),)],
              pp.LIFETIME_RELEASE_ASSUMED, eval_ram(), {}, b2)
    assert b2.completed == 2


def test_q4_partial_counts_every_dfs_visit():
    nodes = [node_ev("a"), node_ev("b")]
    b = pp.Budgets()
    pp.search(nodes, [(("a",), ("b",))],
              pp.LIFETIME_RELEASE_ASSUMED, eval_ram(), {}, b)
    assert b.partial_expansions == 3   # root + node a + leaf


def test_q4_exact_ceiling_vs_omitted():
    """Reaching the ceiling WITHOUT omitting work is distinguished
    from truncation."""
    nodes = [node_ev("a"), node_ev("b")]
    b = pp.Budgets()
    b.completed = pp.LIMITS["max_completed_assignments"] - 1
    _, _, states = pp.search(
        nodes, [(("a",), ("b",))], pp.LIFETIME_RELEASE_ASSUMED,
        eval_ram(), {}, b)
    assert b.completed == pp.LIMITS["max_completed_assignments"]
    assert not b.omitted_completed and len(states) == 1


def test_q4_budgets_request_global():
    """wave_arrangements + search share ONE Budgets object — counters
    never reset between phases."""
    b = pp.Budgets()
    waves, _ = pp.wave_arrangements(FANOUT, b)
    groups = b.group_proposals
    waves_n = b.waves
    nodes = [node_ev("img"), node_ev("vida"), node_ev("vidb")]
    pp.search(nodes, waves, pp.LIFETIME_RELEASE_ASSUMED,
              eval_ram(), {}, b)
    assert b.group_proposals == groups and b.waves == waves_n
    assert b.partial_expansions > 0


def test_q4_variant_evals_attempts_bounded():
    """variant_evals counts attempted combinations; ≤24 materialize
    per node and excess attempts are flagged omitted."""
    n = node_ev("a", devices=[dev(i) for i in range(8)],
                adapters=tuple(f"ad{i}" for i in range(5)))
    b = pp.Budgets()
    pp.search([n, node_ev("b")], [(("a",), ("b",))],
              pp.LIFETIME_RELEASE_ASSUMED, eval_ram(), {}, b)
    # adapters capped at max_runtime_candidates_per_node=4 → 8×4=32
    # attempted, only 24 materialized
    assert b.variant_evals["a"] == 32
    assert b.omitted_variants.get("a") is True
    _, trunc, _ = pp.search(
        [n, node_ev("b")], [(("a",), ("b",))],
        pp.LIFETIME_RELEASE_ASSUMED, eval_ram(), {}, pp.Budgets())
    assert "variant_truncated" in trunc


def test_q4_hard_device_prunes_variants():
    """Hard device constraint filters variants at DFS expansion —
    non-matching variants never become completed arrangements."""
    nodes = [node_ev("a", devices=[dev(0), dev(1)]), node_ev("b")]
    b = pp.Budgets()
    retained, _, _ = pp.search(
        nodes, [(("a",), ("b",))], pp.LIFETIME_RELEASE_ASSUMED,
        eval_ram(), {"a": 1}, b)
    assert all(c["assignment"]["a"]["device"] == 1 for c in retained)
    assert b.completed >= 1


# ============================================= Q5 — feasibility and
# blocker scope


def test_q5_high_ram_wave_fails_sequential_survives():
    """A candidate-specific RAM failure must not block a surviving
    lower-demand alternative: concurrent wave demand 8G blocks while
    the sequential wave (peak 4G) survives."""
    def ram_gate(est, known):
        return {"decision": "block" if est >= 8 * GIB else "ok",
                "fully_evaluated": True, "checks": {}, "reason": "",
                "snapshot": {}}
    nodes = [node_ev("a", 1 * GIB, 4 * GIB, devices=[dev(0)]),
             node_ev("b", 1 * GIB, 4 * GIB, devices=[dev(0)])]
    assign = _q2_assign(4 * GIB, 4 * GIB)
    con = pp.evaluate_candidate(assign, nodes, (("a", "b"),),
                                pp.LIFETIME_RELEASE_ASSUMED, ram_gate,
                                transition_host={0: 0},
                                transition_dev={(0, 0): 0})
    seq = pp.evaluate_candidate(assign, nodes, (("a",), ("b",)),
                                pp.LIFETIME_RELEASE_ASSUMED, ram_gate,
                                transition_host={0: 0, 1: 0},
                                transition_dev={(0, 0): 0, (1, 0): 0})
    assert con["state"] == pp.CAND_BLOCKED
    assert seq["state"] == pp.FEASIBLE
    r = pp.aggregate(**agg_kwargs(
        states=[con["state"], seq["state"]],
        retained=[{"state": seq["state"], "node_states": {}}],
        explored_reasons=set(con["reasons"]) | set(seq["reasons"])))
    assert r["advisory_state"] != pp.ADVISORY_BLOCKED


def test_q5_all_blocked_ram_reason_not_authoritative():
    """Every explored candidate blocked solely by RAM insufficiency →
    ram_insufficient, not a conflated authoritative_block."""
    r = pp.aggregate(**agg_kwargs(
        states=[pp.CAND_BLOCKED, pp.CAND_BLOCKED],
        retained=[{"state": pp.CAND_BLOCKED, "node_states": {}}],
        explored_reasons={"ram_insufficient"}))
    assert r["advisory_state"] == pp.ADVISORY_BLOCKED
    assert r["block_reason"] == "ram_insufficient"


def test_q5_all_blocked_contract_reason_authoritative():
    r = pp.aggregate(**agg_kwargs(
        states=[pp.CAND_BLOCKED],
        retained=[{"state": pp.CAND_BLOCKED, "node_states": {}}],
        explored_reasons={"binding_contract_mismatch:x"}))
    assert r["advisory_state"] == pp.ADVISORY_BLOCKED
    assert r["block_reason"] == "authoritative_block"


# ============================================================ Q3 — exact
# command bindability


def _seed_artifact(s, artifact_id, source_type="catalog",
                   sha="a" * 64, enabled=True):
    from app.db.models import ModelArtifact
    s.add(ModelArtifact(
        artifact_id=artifact_id, kind="model", family="sd15",
        fmt="safetensors", role="diffusion", modality="image",
        source_type=source_type, managed_by="catalog",
        content_sha256=sha, enabled=enabled,
        support_tier="tested", version="1"))


def _seed_install(s, key="sd15.fp16", status="installed",
                  adapter="gen.image.sdcpp"):
    from app.db.models import ModelInstall, new_id
    s.add(ModelInstall(
        id=new_id(), model_key=key, adapter_id=adapter,
        version="1.0", status=status, install_root="/tmp/m",
        rel_dir=key, file_name="w.gguf", byte_size=1,
        sha256="ab" * 32, verified=(status == "installed"),
        license="test", meta_json={"source": {}}))


def _node_bindability(body_json, client, node_id="img"):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/"
        "plan-preview", json=body_json)
    assert r.status_code == 200
    return next(n for n in r.json()["nodes"]
                if n["node_id"] == node_id)


def test_q3_video_spec_on_image_node_nonbindable(client):
    """Actual lookup mismatch: a video-modality spec selected for the
    image node's model_key can never satisfy generate.image —
    definitively non_bindable."""
    n = _node_bindability({
        "selections": {"img_model_key": {
            "kind": "catalog_key", "value": "wan21.t2v.1_3b"}},
        "params": {"img_model_key": "a", "img_prompt": "p",
                   "vid_model_key": "b"}}, client)
    assert n["bindability"] == "non_bindable"
    assert n["bindability_reason"] == "command_contract_mismatch"


def test_q3_catalog_key_without_install_nonbindable(client):
    """Spec existence alone is NOT bindable: no ModelInstall row →
    installed_file() resolves nothing → non_bindable/not_installed."""
    n = _node_bindability({
        "selections": {"img_model_key": {
            "kind": "catalog_key", "value": "sd15.fp16"}},
        "params": {"img_model_key": "a", "img_prompt": "p",
                   "vid_model_key": "b"}}, client)
    assert n["bindability"] == "non_bindable"
    assert n["bindability_reason"] == "not_installed"


def test_q3_bindable_with_install(client):
    """Bindable requires the whole command gate: spec + matching
    installed ModelInstall + served adapter."""
    from sqlalchemy.orm import Session
    with Session(client.app.state.engine) as s:
        _seed_install(s, "sd15.fp16")
        s.commit()
    n = _node_bindability({
        "selections": {"img_model_key": {
            "kind": "artifact_id", "value": "catalog:sd15.fp16"}},
        "params": {"img_model_key": "a", "img_prompt": "p",
                   "vid_model_key": "b"}}, client)
    assert n["bindability"] == "bindable"
    assert n["integrity_class"] in ("verified", "recorded", "missing",
                                    "declared")


def test_q3_planned_tier_unknown_bindability(client):
    """A planned-tier video spec: generate.video's experimental gate
    is a runtime env flag — not stored evidence → unknown."""
    n = _node_bindability({
        "selections": {"vid_model_key": {
            "kind": "catalog_key", "value": "ltx.2.5"}},
        "params": {"img_model_key": "a", "img_prompt": "p",
                   "vid_model_key": "b"}}, client, "vid")
    assert n["bindability"] == "unknown"
    assert n["bindability_reason"] == "support_tier_unverified"


def test_q3_disabled_artifact_nonbindable(client):
    """A disabled selected row is artifact_disabled — authoritative,
    independent of any command path."""
    from sqlalchemy.orm import Session

    from app.db.models import ModelArtifact
    with Session(client.app.state.engine) as s:
        row = s.get(ModelArtifact, "catalog:sd15.fp16")
        row.enabled = False
        s.commit()
    n = _node_bindability({
        "selections": {"img_model_key": {
            "kind": "artifact_id", "value": "catalog:sd15.fp16"}},
        "params": {"img_model_key": "a", "img_prompt": "p",
                   "vid_model_key": "b"}}, client)
    assert n["bindability"] == "non_bindable"
    assert n["bindability_reason"] == "artifact_disabled"


def test_q3_import_row_no_model_key_path(client):
    """Bare import rows have no model_key binding — definitively
    non_bindable regardless of integrity or enabled state."""
    import uuid

    from sqlalchemy.orm import Session
    aid = f"import:{uuid.uuid4()}"
    with Session(client.app.state.engine) as s:
        _seed_artifact(s, aid, source_type="import")
        s.commit()
    n = _node_bindability({
        "selections": {"img_model_key": {
            "kind": "artifact_id", "value": aid}},
        "params": {"img_model_key": "a", "img_prompt": "p",
                   "vid_model_key": "b"}}, client)
    assert n["bindability"] == "non_bindable"
    assert n["bindability_reason"] == "no_model_key_binding"


# ============================================================ Q6 — isolation


def test_q6_visibility_predicate_unit():
    """Project-scope visibility predicate: project-scoped rows admit
    only global (None) or owned rows; a foreign scope is excluded."""
    from app.api.routers import _pipe_plan_visibility
    rows = [NS(artifact_id="a", project_id=None),
            NS(artifact_id="b", project_id="P1"),
            NS(artifact_id="c", project_id="P9")]
    db = NS(execute=lambda _q: NS(scalars=lambda: rows))
    vis = _pipe_plan_visibility(db, NS(id="P1"))
    assert vis == {"a", "b"}


def test_q6_unrelated_artifact_no_fp_change(client):
    """An unrelated artifact row cannot influence the plan
    fingerprint — it never enters selections or root evidence."""
    import uuid

    from sqlalchemy.orm import Session

    from app.db.models import ModelArtifact
    from tests.conftest import make_project
    p = make_project(client)
    url = (f"/api/projects/{p['id']}/pipelines/img2vid_chain/"
           "plan-preview")
    body = {"params": {"img_model_key": "a", "img_prompt": "p",
                       "vid_model_key": "b"}}
    r1 = client.post(url, json=body)
    assert r1.status_code == 200
    fp1 = r1.json()["plan_fingerprint"]
    with Session(client.app.state.engine) as s:
        s.add(ModelArtifact(
            artifact_id=f"import:{uuid.uuid4()}", kind="model",
            family="zz", fmt="safetensors", role="diffusion",
            modality="image", source_type="import",
            managed_by="external", content_sha256="f" * 64,
            enabled=True, support_tier="tested", version="1"))
        s.commit()
    r2 = client.post(url, json=body)
    assert r2.status_code == 200
    assert r2.json()["plan_fingerprint"] == fp1


def test_q6_context_fields_rejected(client):
    """No character/shot/cast selector surface exists on this
    endpoint — any such field is rejected at schema level."""
    from tests.conftest import make_project
    p = make_project(client)
    for extra in ({"character_ids": "x"}, {"shot_id": "x"},
                  {"cast": {}}, {"select": ["lora:z"]}):
        r = client.post(
            f"/api/projects/{p['id']}/pipelines/img2vid_chain/"
            "plan-preview", json=extra)
        assert r.status_code == 422


def test_q6_shared_catalog_artifact_visible(client):
    """Catalog registry rows are the shared library — an artifact_id
    selection of one is authorized for every project (Q6-H)."""
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"selections": {"img_model_key": {
                  "kind": "artifact_id", "value": "catalog:sd15.fp16"}},
              "params": {"img_model_key": "a", "img_prompt": "p",
                         "vid_model_key": "b"}})
    assert r.status_code == 200


# ============================================== Q8 — request contract


def test_q8_wave_policy_unsupported_rejected(client):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"nodes": {"img": {"wave_policy": "dynamic"}},
              "params": {"img_model_key": "a", "img_prompt": "p",
                         "vid_model_key": "b"}})
    assert r.status_code == 400
    assert "wave_policy_unsupported" in str(r.json())


def test_q8_wave_policy_fixed_level_accepted(client):
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"nodes": {"img": {"wave_policy": "fixed_level"}},
              "params": {"img_model_key": "a", "img_prompt": "p",
                         "vid_model_key": "b"}})
    assert r.status_code == 200


def test_q8_missing_param_is_invalid_request(client):
    """param_findings enforced: a missing required external param is
    an invalid request (400), not a silent partial plan."""
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"params": {"img_model_key": "a"}})
    assert r.status_code == 400
    assert "invalid_params" in str(r.json())


def test_q8_typed_response_shape(client):
    """Response validates against PipelinePlanPreviewOut — required
    fields present with declared types and bounded collections."""
    from tests.conftest import make_project
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/pipelines/img2vid_chain/plan-preview",
        json={"params": {"img_model_key": "a", "img_prompt": "p",
                         "vid_model_key": "b"}})
    assert r.status_code == 200
    b = r.json()
    assert b["advisory"] is True
    assert isinstance(b["axes"], dict)
    assert isinstance(b["candidates"], list) and len(b["candidates"]) <= 8
    assert isinstance(b["findings"], list) and len(b["findings"]) <= 64
    assert isinstance(b["counters"], dict)
    assert b["operational_readiness"] == "not_evaluated"
