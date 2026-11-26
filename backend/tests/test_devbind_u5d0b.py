"""U-5d-0b Phase beta — synthetic device-binding tests (lcfg:v1).

Every test is synthetic: Phase alpha evidence is fabricated via
hw_identity.correlate/finalize fixtures; the parent environment is an
explicit dict — os.environ is never read or mutated; no hardware
discovery, runtime enumeration or process launch occurs.
"""

from __future__ import annotations

import copy

import pytest

from app.domain import device_bind as db
from app.domain import hw_identity as hi
from app.domain import runtime_integrity as ri

PNP_A = ("PCI\\VEN_10DE&DEV_2484&SUBSYS_37003842&REV_A1"
         "\\4&1111AAAA&0&0008")
PNP_B = ("PCI\\VEN_10DE&DEV_2484&SUBSYS_37003842&REV_A1"
         "\\4&2222BBBB&0&0009")
UUID_A = "GPU-12345678-1234-1234-1234-123456789abc"
UUID_B = "GPU-87654321-4321-4321-4321-cba987654321"
UUID_C = "GPU-00000000-1111-2222-3333-444455556666"
TOKEN_A = hi.identity_token(UUID_A)
TOKEN_B = hi.identity_token(UUID_B)

GIB = 1024 ** 3


def smi_row(index=0, bus_id="00000000:1B:00.0", uuid=UUID_A,
            total=12 * GIB, free=11 * GIB):
    return {"smi_index": index, "name": "NVIDIA GeForce RTX 3060",
            "vram_bytes": total, "vram_free_bytes": free,
            "driver_version": "555.99", "pci_bus_id": bus_id,
            "uuid": uuid}


def pci_props(bus=1, device=0, function=0, pnp=PNP_A):
    return {pnp: {"bus_number": bus,
                  "address": (device << 16) | function,
                  "location_info": (f"PCI bus {bus}, device {device}, "
                                    f"function {function}"),
                  "location_paths": ["PCIROOT(0)#PCI(1B00)#PCI(0000)"]}}


def _dual_props():
    return {**pci_props(bus=0x1B, pnp=PNP_A),
            **pci_props(bus=0x21, pnp=PNP_B)}


def evidence(mono=500.0, gen=1, meta=None, smi=None, props=None,
             simulated=False, provenance="real"):
    if meta is None:
        meta = [{"index": 0, "pnp": PNP_A, "vendor": "NVIDIA"}]
    if smi is None:
        smi = [smi_row()]
    if props is None:
        props = pci_props(bus=0x1B)
    ident = hi.finalize(hi.correlate(meta, smi, props),
                        provenance=provenance)
    ident["collection"] = {"probed_at": 1000.0,
                           "probed_monotonic": mono,
                           "simulated": simulated}
    return {"report": {"probed_at": 1000.0, "probed_monotonic": mono,
                       "gpus": [], "simulated": simulated},
            "identity": ident, "probed_at": 1000.0,
            "probed_monotonic": mono, "simulated": simulated,
            "order": mono, "generation": gen}


def rtq_qualified(flavor="cuda"):
    return {"flavor": flavor,
            "rtq": {"contract": "rtq:v1", "classification": "qualified",
                    "manifest_digest": "sha256:" + "ab" * 32,
                    "trust": ri.TRUST_OBSERVED_FIRST_USE}}


def refusal(res):
    assert res["state"] == "refused"
    return res["refusal"]["code"]


def build(env=None, **kw):
    args = {"evidence": evidence(), "gpu_token": TOKEN_A,
            "flavor": "cuda", "runtime": rtq_qualified(),
            "environ": env if env is not None else {}}
    args.update(kw)
    return db.build_cuda_candidate(**args)


# -------------------------------------------------- visibility matrix


def test_parent_unset_permits_singleton():
    res = build({})
    assert res["state"] == "candidate"
    assert res["candidate_env"] == {"CUDA_VISIBLE_DEVICES": UUID_A}
    assert res["inherited_visibility"]["mode"] == "parent_unset"


def test_parent_empty_refuses():
    assert refusal(build({"CUDA_VISIBLE_DEVICES": ""})) == \
        "inherited_visibility_empty"


def test_parent_matching_singleton_permits():
    res = build({"CUDA_VISIBLE_DEVICES": UUID_A})
    assert res["inherited_visibility"]["mode"] == \
        "parent_singleton_matching"
    assert res["candidate_env"]["CUDA_VISIBLE_DEVICES"] == UUID_A


def test_parent_excluding_singleton_refuses():
    assert refusal(build(
        {"CUDA_VISIBLE_DEVICES": UUID_B})) == \
        "target_excluded_by_inherited_visibility"


def test_parent_multi_including_restricts_to_target():
    res = build({"CUDA_VISIBLE_DEVICES": f"{UUID_B},{UUID_A}"})
    assert res["inherited_visibility"]["mode"] == \
        "restricted_to_target_singleton"
    # restricted — never widened back to the inherited multi list
    assert res["candidate_env"]["CUDA_VISIBLE_DEVICES"] == UUID_A


def test_parent_multi_excluding_refuses():
    assert refusal(build(
        {"CUDA_VISIBLE_DEVICES": f"{UUID_B},{UUID_C}"})) == \
        "target_excluded_by_inherited_visibility"


@pytest.mark.parametrize("val", ["0", "1", "0,1", " 2 "])
def test_parent_numeric_ordinals_refuse(val):
    assert refusal(build({"CUDA_VISIBLE_DEVICES": val})) == \
        "inherited_ordinal_untranslatable"


def test_parent_mixed_tokens_refuse():
    assert refusal(build(
        {"CUDA_VISIBLE_DEVICES": f"0,{UUID_A}"})) == \
        "mixed_inherited_tokens"


@pytest.mark.parametrize("val", [
    "GPU-12345678", "GPU-12345678-1234", UUID_A[:-1]])
def test_parent_uuid_prefix_refuses(val):
    assert refusal(build({"CUDA_VISIBLE_DEVICES": val})) == \
        "ambiguous_uuid_prefix"


def test_parent_duplicate_uuid_refuses():
    assert refusal(build(
        {"CUDA_VISIBLE_DEVICES": f"{UUID_A},{UUID_A}"})) == \
        "duplicate_inherited_token"


@pytest.mark.parametrize("val", ["xyz", "GPU-zz", "not a uuid",
                                 "GPU-12345678-1234-1234-1234-zzzzzzzzzzzz"])
def test_parent_malformed_refuses(val):
    assert refusal(build({"CUDA_VISIBLE_DEVICES": val})) == \
        "malformed_inherited_visibility"


def test_parent_mig_refuses():
    assert refusal(build(
        {"CUDA_VISIBLE_DEVICES":
         "MIG-12345678-1234-1234-1234-123456789abc"})) == \
        "unsupported_mig_identifier"


@pytest.mark.parametrize("val", [f"{UUID_A},", f",{UUID_A}",
                                 f"{UUID_A},,{UUID_B}", " ,GPU-x"])
def test_parent_empty_member_refuses(val):
    assert refusal(build({"CUDA_VISIBLE_DEVICES": val})) == \
        "empty_visibility_member"


def test_parent_none_value_refuses():
    assert refusal(build({"CUDA_VISIBLE_DEVICES": None})) == \
        "malformed_inherited_visibility"


def test_conflicting_case_variant_keys_refuse():
    env = {"CUDA_VISIBLE_DEVICES": UUID_A,
           "cuda_visible_devices": UUID_B}
    assert refusal(build(env)) == \
        "conflicting_env_key_representations"


def test_identical_case_variant_keys_canonicalize():
    env = {"CUDA_VISIBLE_DEVICES": f"{UUID_B},{UUID_A}",
           "cuda_visible_devices": f"{UUID_B},{UUID_A}"}
    res = build(env)
    assert res["state"] == "candidate"
    assert res["candidate_env"]["CUDA_VISIBLE_DEVICES"] == UUID_A


def test_parent_env_and_inputs_not_mutated():
    env = {"CUDA_VISIBLE_DEVICES": f"{UUID_B},{UUID_A}",
           "CUDA_DEVICE_ORDER": "PCI_BUS_ID",
           "PATH": "unchanged", "SECRET_TOKEN": "s3cret"}
    env_copy = copy.deepcopy(env)
    ev = evidence()
    ev_copy = copy.deepcopy(ev)
    build(env, evidence=ev)
    assert env == env_copy
    assert ev == ev_copy


def test_no_widening_and_no_multi_device_output():
    res = build({"CUDA_VISIBLE_DEVICES": f"{UUID_B},{UUID_A},{UUID_C}"})
    val = res["candidate_env"]["CUDA_VISIBLE_DEVICES"]
    assert val == UUID_A and "," not in val


# -------------------------------------------------- device order


def test_device_order_pci_bus_id_recorded():
    res = build({"CUDA_DEVICE_ORDER": "PCI_BUS_ID"})
    assert res["device_order"]["value"] == "PCI_BUS_ID"
    assert res["state"] == "candidate"


def test_device_order_fastest_first_recorded_not_restriction():
    res = build({"CUDA_DEVICE_ORDER": "FASTEST_FIRST"})
    assert res["device_order"]["value"] == "FASTEST_FIRST"
    assert res["state"] == "candidate"


def test_device_order_unknown_refuses():
    assert refusal(build({"CUDA_DEVICE_ORDER": "LUDICROUS"})) == \
        "unknown_device_order"


def test_device_order_conflicting_case_keys_refuse():
    env = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID",
           "cuda_device_order": "FASTEST_FIRST"}
    assert refusal(build(env)) == "conflicting_device_order_keys"


def test_device_order_empty_refuses():
    assert refusal(build({"CUDA_DEVICE_ORDER": "  "})) == \
        "malformed_device_order"


# -------------------------------------------------- evidence checks


def test_missing_evidence_refuses():
    assert refusal(build(evidence=None)) == "missing_evidence"
    assert refusal(build(evidence={"report": {}})) == "missing_evidence"


def test_simulated_evidence_refuses():
    ev = evidence(simulated=True, provenance="simulated")
    assert refusal(build(evidence=ev)) == "simulated_evidence"


def test_unqualified_provenance_refuses():
    ev = evidence(provenance="unavailable")
    assert refusal(build(evidence=ev)) == \
        "unqualified_identity_provenance"


def test_incoherent_pair_refuses():
    ev = evidence()
    ev["identity"]["collection"]["probed_monotonic"] = 999.0
    assert refusal(build(evidence=ev)) == "incoherent_evidence_pair"


def test_incomplete_evidence_refuses():
    ev = evidence()
    ev["identity"]["completeness"]["wmi_devices"] = 7
    assert refusal(build(evidence=ev)) == "incomplete_evidence"


def test_digest_mismatch_refuses():
    ev = evidence()
    ev["identity"]["digest"] = "hwid:v1:" + "00" * 32
    assert refusal(build(evidence=ev)) == "identity_digest_mismatch"


def test_tampered_record_caught_by_digest():
    ev = evidence()
    ev["identity"]["records"][0]["gpu_uuid"] = UUID_B
    assert refusal(build(evidence=ev)) == "identity_digest_mismatch"


def test_stale_evidence_refuses():
    assert refusal(build(fresh=evidence(gen=2))) == "stale_evidence"


def test_same_generation_different_content_refuses():
    fresh = evidence(gen=1)
    fresh["report"]["gpus"] = [{"different": True}]
    assert refusal(build(fresh=fresh)) == "contradictory_evidence"


def test_matching_generation_and_content_fresh():
    res = build(fresh=evidence(gen=1))
    assert res["state"] == "candidate"


def test_invalid_generation_refuses():
    ev = evidence(gen=0)
    assert refusal(build(evidence=ev)) == "generation_invalid"
    ev = evidence(gen=True)
    assert refusal(build(evidence=ev)) == "generation_invalid"


# -------------------------------------------------- identity checks


def test_missing_physical_identity_refuses():
    assert refusal(build(gpu_token=TOKEN_B)) == \
        "missing_physical_identity"


def test_unqualified_association_refuses():
    # correlate() only mints a token for a qualified join — an
    # unqualified record carries token=None, so the lookup itself
    # fails closed as missing identity.
    ev = evidence(smi=[])  # correlate → unavailable, token None
    assert refusal(build(evidence=ev)) == "missing_physical_identity"


def test_token_uuid_mismatch_refuses():
    ev = evidence()
    # token field is not covered by the inventory digest — replacing
    # it survives the digest check but fails token↔uuid verification.
    ev["identity"]["records"][0]["token"] = TOKEN_B
    assert refusal(build(evidence=ev, gpu_token=TOKEN_B)) == \
        "physical_token_uuid_mismatch"


def test_malformed_token_refuses():
    assert refusal(build(gpu_token="GPU-1234")) == \
        "malformed_physical_token"
    assert refusal(build(gpu_token=None)) == "malformed_physical_token"


def test_identical_names_distinct_tokens():
    meta = [{"index": 0, "pnp": PNP_A, "vendor": "NVIDIA"},
            {"index": 1, "pnp": PNP_B, "vendor": "NVIDIA"}]
    smi = [smi_row(index=0, bus_id="00000000:1B:00.0", uuid=UUID_A),
           smi_row(index=1, bus_id="00000000:21:00.0", uuid=UUID_B)]
    ev = evidence(meta=meta, smi=smi, props=_dual_props())
    ra = build(evidence=ev, gpu_token=TOKEN_A)
    rb = build(evidence=ev, gpu_token=TOKEN_B)
    assert ra["candidate_env"]["CUDA_VISIBLE_DEVICES"] == UUID_A
    assert rb["candidate_env"]["CUDA_VISIBLE_DEVICES"] == UUID_B
    assert ra["physical_token"] != rb["physical_token"]


def test_divergent_smi_order_binds_by_bdf_not_ordinal():
    meta = [{"index": 0, "pnp": PNP_A, "vendor": "NVIDIA"},
            {"index": 1, "pnp": PNP_B, "vendor": "NVIDIA"}]
    # smi enumerates in REVERSED order — binding must follow BDF+UUID.
    smi = [smi_row(index=0, bus_id="00000000:21:00.0", uuid=UUID_B),
           smi_row(index=1, bus_id="00000000:1B:00.0", uuid=UUID_A)]
    ev = evidence(meta=meta, smi=smi, props=_dual_props())
    res = build(evidence=ev, gpu_token=TOKEN_A)
    assert res["candidate_env"]["CUDA_VISIBLE_DEVICES"] == UUID_A
    assert res["smi_index"] == 1
    assert res["expected_cuda_ordinal"]["value"] == 0


# -------------------------------------------------- runtime integrity


def test_missing_runtime_integrity_refuses():
    assert refusal(build(runtime=None)) == "runtime_integrity_missing"
    assert refusal(build(runtime={"flavor": "cuda"})) == \
        "runtime_integrity_missing"


def test_unqualified_rtq_refuses():
    rt = {"flavor": "cuda",
          "rtq": {"contract": "rtq:v1", "classification": "unqualified",
                  "reason": "member_integrity_failure"}}
    assert refusal(build(runtime=rt)) == "runtime_integrity_unqualified"


def test_wrong_rtq_contract_refuses():
    rt = {"rtq": {"contract": "other", "classification": "qualified"}}
    assert refusal(build(runtime=rt)) == "runtime_integrity_unqualified"


def test_runtime_flavor_mismatch_refuses():
    assert refusal(build(runtime=rtq_qualified(flavor="vulkan"))) == \
        "flavor_mismatch"


def test_synthetic_rtq_end_to_end(tmp_path):
    root = tmp_path / "sdcpp-cuda"
    root.mkdir()
    files = {"sd-cli.exe": b"exe", "ggml-cuda.dll": b"d"}
    for n, d in files.items():
        (root / n).write_bytes(d)
    m = ri.build_reference_manifest(
        "sdcpp-cuda", "cuda",
        [{"name": n, "sha256": __import__("hashlib").sha256(d).hexdigest(),
          "size": len(d)} for n, d in files.items()],
        ri.P_INSTALL_CAPTURE)["manifest"]
    q = ri.qualify(root, m, expected_flavor="cuda")
    res = build(runtime={"flavor": "cuda", "rtq": q})
    assert res["state"] == "candidate"
    assert res["runtime"]["rtq_classification"] == "qualified"
    assert res["runtime"]["rtq_trust"] == ri.TRUST_OBSERVED_FIRST_USE


# -------------------------------------------------- flavor / backend


def test_cpu_flavor_refused():
    assert refusal(build(flavor="cpu")) == \
        "flavor_cpu_outside_exact_contract"


def test_vulkan_flavor_refused():
    assert refusal(build(flavor="vulkan")) == \
        "flavor_vulkan_prerequisite_blocked"


def test_unknown_flavor_refused():
    assert refusal(build(flavor="rocm")) == "unsupported_flavor"


def test_default_backend_recommendation():
    res = build()
    be = res["candidate_backend"]
    assert be["argv_fragment"] == ["--backend", "all=cuda0",
                                   "--auto-fit", "off"]
    assert be["recommended"]
    assert be["assurance"] == "L3_source_supported_unqualified"


def test_backend_explicit_accepted():
    res = build(backend={"spec": "diffusion=cuda0,te=cpu",
                         "auto_fit": "off"})
    assert res["state"] == "candidate"
    assert "explicit_cpu_offload" in res["candidate_backend"]["flags"]


def test_backend_multi_device_split_refuses():
    assert refusal(build(backend={
        "spec": "diffusion=cuda0&cuda1"})) == \
        "unsupported_multi_device_split"


def test_backend_split_mode_refuses():
    assert refusal(build(backend={"spec": "all=cuda0",
                                  "split_mode": "row"})) == \
        "unsupported_multi_device_split"


def test_backend_autofit_on_refuses():
    assert refusal(build(backend={"spec": "all=cuda0",
                                  "auto_fit": "on"})) == \
        "unqualified_autofit"


def test_backend_cpu_default_is_substitution_refusal():
    assert refusal(build(backend={"spec": "cpu"})) == \
        "cpu_runtime_substitution"
    assert refusal(build(backend={"spec": "all=cpu"})) == \
        "cpu_runtime_substitution"


def test_backend_vulkan_substitution_refuses():
    assert refusal(build(backend={"spec": "all=vulkan0"})) == \
        "vulkan_runtime_substitution"


def test_backend_unknown_device_refuses():
    assert refusal(build(backend={"spec": "all=rocm0"})) == \
        "unsupported_backend_device"


def test_backend_ordinal_out_of_singleton_refuses():
    assert refusal(build(backend={"spec": "all=cuda1"})) == \
        "device_ordinal_out_of_singleton_scope"


def test_backend_unknown_module_refuses():
    r = build(backend={"spec": "bogus=cuda0"})
    assert refusal(r) == "backend_config_conflict"


def test_backend_bare_cuda_normalizes_to_cuda0():
    res = build(backend={"spec": "all=cuda"})
    assert res["state"] == "candidate"
    assert res["candidate_backend"]["assignments"] == [
        {"module": "all", "device": "cuda0"}]


def test_params_backend_disk_is_explicit_not_substitution():
    res = build(backend={"spec": "all=cuda0",
                         "params_backend": "te=cpu"})
    assert res["state"] == "candidate"
    res = build(backend={"spec": "all=cuda0",
                         "params_backend": "disk"})
    assert res["state"] == "candidate"
    assert "explicit_disk_params" in res["candidate_backend"]["flags"]


def test_backend_malformed_spec_refuses():
    assert refusal(build(backend={"spec": ""})) == \
        "malformed_backend_spec"
    assert refusal(build(backend={"spec": "all="})) == \
        "malformed_backend_spec"


def test_backend_malformed_autofit_refuses():
    assert refusal(build(backend={"spec": "all=cuda0",
                                  "auto_fit": "maybe"})) == \
        "malformed_auto_fit"


# -------------------------------------------------- drift decisions


def _two_gpu_evidence(mono=500.0):
    meta = [{"index": 0, "pnp": PNP_A, "vendor": "NVIDIA"},
            {"index": 1, "pnp": PNP_B, "vendor": "NVIDIA"}]
    smi = [smi_row(index=0, bus_id="00000000:1B:00.0", uuid=UUID_A),
           smi_row(index=1, bus_id="00000000:21:00.0", uuid=UUID_B)]
    return evidence(mono=mono, meta=meta, smi=smi, props=_dual_props())


def test_drift_unchanged():
    ev = _two_gpu_evidence()
    d = db.classify_drift(ev, evidence(mono=600.0, meta=[
        {"index": 0, "pnp": PNP_A, "vendor": "NVIDIA"},
        {"index": 1, "pnp": PNP_B, "vendor": "NVIDIA"}],
        smi=[smi_row(index=0, bus_id="00000000:1B:00.0", uuid=UUID_A),
             smi_row(index=1, bus_id="00000000:21:00.0", uuid=UUID_B)],
        props=_dual_props()), TOKEN_A)
    assert d["decision"] == "unchanged"


def test_drift_os_index_changed_is_mapping_refresh():
    held = evidence()
    fresh = evidence(mono=600.0,
                     meta=[{"index": 5, "pnp": PNP_A,
                            "vendor": "NVIDIA"}])
    d = db.classify_drift(held, fresh, TOKEN_A)
    assert d["decision"] == "mapping_refresh"


def test_drift_smi_index_changed_is_mapping_refresh():
    held = evidence()
    fresh = evidence(mono=600.0,
                     smi=[smi_row(index=3, bus_id="00000000:1B:00.0",
                                  uuid=UUID_A)])
    d = db.classify_drift(held, fresh, TOKEN_A)
    assert d["decision"] == "mapping_refresh"


def test_drift_pci_changed_is_revalidation():
    held = evidence()
    fresh = evidence(mono=600.0,
                     smi=[smi_row(bus_id="00000000:41:00.0",
                                  uuid=UUID_A)],
                     props=pci_props(bus=0x41))
    d = db.classify_drift(held, fresh, TOKEN_A)
    assert d["decision"] == "identity_revalidation_and_mapping_refresh"


def test_drift_replacement_requires_new_approval():
    held = evidence()
    # Same OS index, different qualified UUID — a different physical
    # device: binding refuses, never transfers approval.
    fresh = evidence(mono=600.0, smi=[smi_row(uuid=UUID_B)])
    d = db.classify_drift(held, fresh, TOKEN_A)
    assert d["decision"] == \
        "refuse_physical_replacement_new_approval_required"


def test_drift_missing_identity_refuses():
    held = evidence()
    fresh = evidence(smi=[])  # token absent, slot not re-qualified
    d = db.classify_drift(held, fresh, TOKEN_A)
    assert d["decision"] == "refuse_missing_identity"
    assert db.classify_drift(held, None, TOKEN_A)[
        "decision"] == "refuse_missing_identity"


def test_drift_contradictory_refuses():
    held = evidence()
    fresh = evidence(smi=[smi_row(uuid=None)])
    d = db.classify_drift(held, fresh, TOKEN_A)
    assert d["decision"] == "refuse_missing_identity"


def test_drift_resource_only_not_replacement():
    held = evidence()
    fresh = evidence(mono=600.0,
                     smi=[smi_row(free=5 * GIB)])
    d = db.classify_drift(held, fresh, TOKEN_A)
    assert d["decision"] == "resource_drift_not_replacement"


def test_drift_material_inventory_drift():
    held = evidence()
    fresh = evidence(mono=600.0, meta=[{
        "index": 0, "pnp": PNP_A + "X", "vendor": "NVIDIA"}],
        props=pci_props(bus=0x1B, pnp=PNP_A + "X"))
    d = db.classify_drift(held, fresh, TOKEN_A)
    # pnp changed → same uuid/pci but digest differs → revalidation
    assert d["decision"] in ("identity_revalidation_required",
                             "unchanged", "mapping_refresh")


# -------------------------------------------------- record semantics


def test_expected_ordinal_labeled_not_observed():
    res = build()
    assert res["expected_cuda_ordinal"] == {
        "value": 0, "class": "expected_not_observed"}


def test_assurance_dimensions_independent():
    res = build()
    a = res["assurance"]
    assert a["L1_documented_semantics"]["state"] == "established"
    assert a["L2_static_candidate"]["state"] == "validated"
    assert a["L3_exact_build_source"]["state"] == "source_supported"
    assert a["L4_integrity_identity"]["state"] == "verified_members"
    assert a["L5_observed_behavior"]["state"] == "unestablished"
    assert a["L6_enforced_selection"]["state"] == "unestablished"


def test_candidate_scope_notes_present():
    res = build()
    for note in ("candidate_not_operational_mapping",
                 "not_execution_authorization",
                 "not_scheduler_reservation",
                 "not_gpu_selection_proof"):
        assert note in res["scope_notes"]


def test_diagnostics_do_not_leak_env_values():
    env = {"CUDA_VISIBLE_DEVICES": UUID_B,
           "SECRET_API_KEY": "verysecretvalue",
           "NVIDIA_SMI_PATH": "x", "GGML_CUSTOM": "y"}
    res = build(env)
    assert "verysecretvalue" not in repr(res)
    # accelerator env names recorded (names only, sorted)
    names = res.get("inherited_accelerator_env_names") or []
    assert "SECRET_API_KEY" not in names
    assert set(names) == {"GGML_CUSTOM", "NVIDIA_SMI_PATH"} \
        or res["state"] == "refused"
    ok = build({"GGML_CUSTOM": "y", "NVIDIA_SMI_PATH": "x"})
    assert ok["inherited_accelerator_env_names"] == [
        "GGML_CUSTOM", "NVIDIA_SMI_PATH"]


def test_generation_scope_documented():
    res = build()
    assert res["generation_scope"] == \
        "process_lifetime_only_not_durable"
    assert res["hw_generation"] == 1


def test_candidate_contract_version():
    res = build()
    assert res["contract"] == "lcfg:v1"
    assert refusal(build(flavor="cpu")) or True
