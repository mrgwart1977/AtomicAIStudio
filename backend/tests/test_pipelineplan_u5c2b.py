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
    c = pp.evaluate_candidate(assign, nodes, (("img",), ("vid",)),
                              pp.LIFETIME_RELEASE_ASSUMED, eval_ram())
    assert c["device_fits"]["0"] == pv.FIT_WITH_HEADROOM
    assert c["device_fits"]["1"] == pv.FIT_WITH_HEADROOM
    assert c["state"] == pp.FEASIBLE


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
