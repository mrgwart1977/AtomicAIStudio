"""U-4a — model-family classification + runtime capability declarations.

All fixtures are synthetic stored-evidence dicts shaped exactly like the
records U-1/U-2 persist (ModelArtifact row fields, import_report_json).
No files, no network, no model content, no inference — classification
and capability declarations are pure functions of stored evidence.
"""

from __future__ import annotations

import builtins
import socket
from pathlib import Path
from typing import ClassVar

from app.domain import model_families, runtime_caps
from app.domain.model_families import ArtifactView, classify_artifact, view_from_row
from app.domain.runtime_caps import RUNTIME_CAPS, adapter_cap_key, claimed_families, get_decl, ruleset_version

# ---------------------------------------------------------------- fixtures

def _import_view(report: dict, **kw) -> ArtifactView:
    """An imported-artifact view: stored U-2 evidence only."""
    base = {
        "artifact_id": "import:" + "a" * 12, "source_type": "import",
        "managed_by": "external", "kind": "model", "arch": "unknown",
        "family": "", "modality": "image",
        "fmt": report.get("fmt", "unknown"), "role": "",
        "meta": {}, "import_report": report}
    base.update(kw)
    return ArtifactView(**base)


def _catalog_view(**kw) -> ArtifactView:
    base = {
        "artifact_id": "catalog:sdxl.base", "source_type": "catalog",
        "managed_by": "atomic", "kind": "model", "arch": "sdxl",
        "family": "sdxl", "modality": "image", "fmt": "safetensors",
        "role": "diffusion",
        "meta": {
            "runtime_key": "sdcpp", "adapter_id": "gen.image.sdcpp",
            "modes": ["text2image"],
            "requirements": {"min_vram_bytes": 8 * 1024**3},
            "files": [{"name": "sd_xl_base_1.0.safetensors",
                       "role": "diffusion", "size_bytes": 1, "sha256": "x"}],
            "capabilities": {"precision": ["fp16"]}},
        "import_report": None}
    base.update(kw)
    return ArtifactView(**base)


SDXL_REPORT = {
    "fmt": "safetensors", "suggested_arch": "sdxl", "suggested_role": "",
    "sha256": "b" * 64, "byte_size": 100, "tensor_count": 3,
    "metadata": {"modelspec.architecture": "stable-diffusion-xl-v1-base"},
    "evidence": {"tensor_keys_sample": [
        "model.diffusion_model.input_blocks.0.0.weight",
        "conditioner.embedders.1.transformer.text_model.encoder.layers.0.weight",
        "first_stage_model.encoder.conv_in.weight",
        "text_encoder_2.transformer.encoder.layer.0.weight"]},
}

SD15_REPORT = {
    "fmt": "safetensors", "suggested_arch": "sd1.5", "suggested_role": "",
    "sha256": "c" * 64, "byte_size": 90, "tensor_count": 3,
    "metadata": {},
    "evidence": {"tensor_keys_sample": [
        "model.diffusion_model.input_blocks.0.0.weight",
        "first_stage_model.encoder.conv_in.weight",
        "cond_stage_model.transformer.text_model.encoder.layers.0.weight"]},
}

GGUF_QWEN_REPORT = {
    "fmt": "gguf", "suggested_arch": "qwen", "suggested_role": "",
    "sha256": "d" * 64, "byte_size": 80, "tensor_count": 0,
    "metadata": {"architecture": "qwen3", "name": "Qwen3-8B"},
    "evidence": {"gguf_version": 3, "n_tensors": 400,
                 "kv": {"general.architecture": "qwen3",
                        "general.name": "Qwen3-8B"}},
}

LORA_REPORT = {
    "fmt": "safetensors", "suggested_arch": "sdxl", "suggested_role": "lora",
    "sha256": "e" * 64, "byte_size": 50, "tensor_count": 2,
    "metadata": {"ss_network_module": "networks.lora",
                 "ss_base_model_version": "sd_xl_base_1.0"},
    "evidence": {"tensor_keys_sample": [
        "lora_unet_down_blocks_0_attentions_0_to_q.lora_down.weight",
        "lora_te2_text_model_encoder_layers_0_mlp_fc1.lora_up.weight"]},
}

VAE_REPORT = {
    "fmt": "safetensors", "suggested_arch": "unknown", "suggested_role": "",
    "sha256": "f" * 64, "byte_size": 30, "tensor_count": 2,
    "metadata": {},
    "evidence": {"tensor_keys_sample": [
        "first_stage_model.encoder.conv_in.weight",
        "first_stage_model.decoder.conv_out.weight"]},
}


# ------------------------------------------------------- classification

def test_sdxl_import_classified_signature_and_metadata():
    c = classify_artifact(_import_view(SDXL_REPORT))
    assert c.family_id == "sdxl"
    assert c.kind == "model"
    assert c.fmt == "safetensors"
    assert c.confidence == "signature"          # signature outranks metadata
    fields = {e["field"] for e in c.evidence}
    assert "suggested_arch" in fields
    assert "modelspec.architecture" in fields
    assert "tensor_keys_sample" in fields
    assert not c.conflicts


def test_sd15_import_classified():
    c = classify_artifact(_import_view(SD15_REPORT))
    assert c.family_id == "sd1.5"
    assert c.confidence == "signature"


def test_gguf_architecture_maps_to_llm_family():
    c = classify_artifact(_import_view(GGUF_QWEN_REPORT))
    assert c.family_id == "llm_qwen"
    assert c.modality == "text"
    assert c.kind == "model"


def test_unknown_architecture_stays_unknown():
    report = dict(SDXL_REPORT, suggested_arch="unknown",
                  metadata={},
                  evidence={"tensor_keys_sample": [
                      "unrecognized.block.0.weight",
                      "odd.prefix.weight"]})
    c = classify_artifact(_import_view(report))
    assert c.family_id is None
    assert c.arch == "unknown"
    assert c.confidence == "unknown"


def test_conflicting_metadata_and_signature_is_conservative():
    report = dict(SDXL_REPORT, suggested_arch="sd1.5",
                  metadata={"modelspec.architecture":
                            "stable-diffusion-xl-v1-base"})
    c = classify_artifact(_import_view(report))
    assert c.family_id is None                      # never silently picked
    assert c.confidence == "unverified"
    assert any(x["type"] == "arch_conflict" for x in c.conflicts)


def test_catalog_declaration_is_declared_provenance():
    c = classify_artifact(_catalog_view())
    assert c.family_id == "sdxl"
    assert c.confidence == "declared"
    assert all(e["source"] == "catalog" for e in c.evidence)
    assert c.precision == "fp16"                    # capabilities evidence


def test_import_provenance_is_labeled():
    c = classify_artifact(_import_view(SDXL_REPORT))
    confs = {e["confidence"] for e in c.evidence}
    assert confs <= {"signature", "metadata"}
    assert "declared" not in confs                  # metadata is untrusted


def test_lora_identified_with_base_family():
    c = classify_artifact(_import_view(LORA_REPORT))
    assert c.kind == "adapter"
    assert c.family_id == "sdxl"
    assert c.base_family == "sdxl"


def test_auxiliary_vae_classified():
    c = classify_artifact(_import_view(VAE_REPORT))
    assert c.family_id == "aux_vae"
    assert c.kind == "auxiliary"


def test_wan_catalog_components_required_and_present():
    c = classify_artifact(_catalog_view(
        artifact_id="catalog:wan21.t2v.1_3b", family="wan", arch="wan",
        modality="video",
        meta={"files": [{"role": "diffusion"}, {"role": "vae"},
                        {"role": "text_encoder"}],
              "requirements": {}, "capabilities": {}}))
    assert c.family_id == "wan"
    assert set(c.components_required) == {"diffusion", "vae", "text_encoder"}
    assert set(c.components_present) == {"diffusion", "vae", "text_encoder"}
    assert not set(c.components_required) - set(c.components_present)


def test_missing_components_surface_as_gap():
    c = classify_artifact(_catalog_view(
        family="wan", arch="wan", modality="video",
        meta={"files": [{"role": "diffusion"}], "capabilities": {}}))
    assert set(c.components_required) - set(c.components_present) == {
        "vae", "text_encoder"}


def test_import_precision_is_honestly_absent():
    # dtype census / GGUF file_type are not in the stored U-2 report —
    # precision must be None, never inferred.
    assert classify_artifact(_import_view(SDXL_REPORT)).precision is None
    assert classify_artifact(_import_view(GGUF_QWEN_REPORT)).precision is None


def test_import_without_report_is_unverified():
    c = classify_artifact(_import_view(None or {}, import_report=None))
    assert c.family_id is None
    assert c.confidence == "unverified"


def test_fmt_passthrough_no_refusal():
    # fmt is reported, not adjudicated here — runtime-fit decides in U-4b.
    report = dict(GGUF_QWEN_REPORT, fmt="gguf")
    c = classify_artifact(_import_view(report))
    assert c.fmt == "gguf"
    assert c.family_id == "llm_qwen"


def test_view_from_row_duck_typing():
    class Row:
        artifact_id = "import:" + "g" * 12
        source_type = "import"
        managed_by = "atomic"
        kind = "model"
        arch = "unknown"
        family = ""
        modality = "image"
        fmt = "safetensors"
        role = ""
        meta_json: ClassVar = {"suggestions": {"arch": "sdxl"}}
        import_report_json: ClassVar = SDXL_REPORT
    v = view_from_row(Row())
    c = classify_artifact(v)
    assert c.family_id == "sdxl"


def test_deterministic_same_input_same_output():
    v = _import_view(SDXL_REPORT)
    assert classify_artifact(v) == classify_artifact(v)


def test_catalog_unregistered_family_conflict():
    c = classify_artifact(_catalog_view(family="madeup", arch="madeup"))
    assert c.family_id is None
    assert any(x["type"] == "catalog_family_unregistered"
               for x in c.conflicts)


# --------------------------------------------------- runtime capability

def test_sdcpp_tested_families_match_catalog_evidence():
    decl = get_decl("sdcpp")
    assert decl is not None and decl.planned is False
    assert decl.families["sd1.5"].grade == "tested"
    assert set(decl.families["sd1.5"].fmts_tested) == {"safetensors", "gguf"}
    assert decl.families["sdxl"].grade == "tested"
    assert "gguf" not in decl.families["sdxl"].fmts_tested  # unverified
    assert decl.families["wan"].grade == "tested"
    assert set(decl.families["wan"].components_required) == {
        "diffusion", "vae", "text_encoder"}
    assert decl.families["flux"].grade == "documented"


def test_sdcpp_does_not_overclaim():
    decl = get_decl("sdcpp")
    fams = decl.families
    assert "qwen" not in fams          # sdcpp cannot run Qwen-Image
    assert "ltx" not in fams           # no LTX claim
    assert "hunyuan" not in fams
    assert "llm_llama" not in fams     # no LLM claim on a diffusion runtime
    assert fams["sd1.5"].dtypes_tested == ("F16", "F32", "Q8_0")


def test_lora_capability_is_documented_and_policy_gated():
    cap = RUNTIME_CAPS["sdcpp"].families["adapter:sd1.5"]
    assert cap.grade == "documented"
    assert cap.policy_gated is True


def test_comfyui_decl_is_planned_never_verified():
    decl = get_decl("comfyui")
    assert decl is not None and decl.planned is True
    assert decl.validated_versions == ()
    assert all(f.grade == "documented" for f in decl.families.values())
    assert {"qwen", "ltx", "wan", "hunyuan"} <= set(decl.families)


def test_validated_versions_are_exact_pins():
    decl = get_decl("sdcpp")
    assert decl.validated_versions == ("master-1073-991f825",)
    assert "master-9999-abc" not in decl.validated_versions


def test_no_future_runtimes_declared():
    assert set(RUNTIME_CAPS) == {"sdcpp", "comfyui"}


def test_adapter_cap_key_contract():
    assert adapter_cap_key("adapter", "sd1.5") == "adapter:sd1.5"
    assert adapter_cap_key("model", "sdxl") == "sdxl"


def test_claimed_families_coverage():
    claimed = claimed_families()
    assert {"sd1.5", "sdxl", "wan", "flux", "qwen", "ltx", "hunyuan"} \
        <= claimed
    assert "llm_llama" not in claimed     # → no_declared_runtime coverage


def test_ruleset_version_tracks_both_registries():
    v = ruleset_version()
    assert f"fam{model_families.FAMILIES_VERSION}" in v
    assert f"caps{runtime_caps.CAPS_SCHEMA_VERSION}" in v


# ---------------------------------------------- trust-boundary guards

def test_no_model_content_io(monkeypatch):
    """Classification must never open or read a model file."""
    def _boom(*a, **k):
        raise AssertionError("filesystem touched during classification")
    monkeypatch.setattr(builtins, "open", _boom)
    monkeypatch.setattr(Path, "read_bytes", _boom)
    monkeypatch.setattr(Path, "read_text", _boom)
    monkeypatch.setattr(Path, "is_file", _boom)
    monkeypatch.setattr(Path, "is_dir", _boom)
    for view in (_import_view(SDXL_REPORT), _import_view(GGUF_QWEN_REPORT),
                 _import_view(LORA_REPORT), _catalog_view()):
        classify_artifact(view)          # no exception ⇒ no I/O


def test_no_network(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("network touched during classification")
    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    for view in (_import_view(SDXL_REPORT), _catalog_view()):
        classify_artifact(view)


def test_metadata_is_inert_data_never_code():
    """Hostile metadata strings stay strings — nothing is evaluated."""
    hostile = dict(SDXL_REPORT, metadata={
        "modelspec.architecture": "stable-diffusion-xl-v1-base",
        "ss_base_model_version": "__import__('os').system('x')",
        "payload": "cos\nsystem\n(S'echo hi'\ntRp1\n.",
    })
    c = classify_artifact(_import_view(hostile))
    assert c.family_id == "sdxl"      # classification unaffected
    # The hostile string can only ever appear as inert evidence data:
    assert isinstance(c.base_family, type(None) | str)


def test_no_inference_eligibility_in_modules():
    """The U-4a modules grant nothing — no adapter imports, no profiles."""
    import inspect
    for mod in (model_families, runtime_caps):
        src = inspect.getsource(mod)
        assert "app.adapters" not in src
        assert "CapabilityProfile" not in src
        for name in ("enabled", "support_tier", "install"):
            assert f"set_{name}" not in src


def test_classification_has_no_eligibility_fields():
    c = classify_artifact(_import_view(SDXL_REPORT))
    fields = set(type(c).__dataclass_fields__)
    assert not ({"enabled", "support_tier", "eligible", "runnable",
                 "installable"} & fields)
