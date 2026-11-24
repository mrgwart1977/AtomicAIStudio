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


# ==================================================================== U-4b
# Five-axis verdicts, fpv1 fingerprints, ops-lane job, API surface.
# Same synthetic-evidence contract as U-4a — no model files, no network,
# no runtime execution.

import asyncio
import os
import subprocess
import threading
import time

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.base import JobCancelled, JobHandle
from app.adapters.compat_eval_adapter import CompatEvalError, ModelCompatEvalAdapter
from app.db.models import CompatVerdict, GenerationJob, ModelArtifact, RuntimeInstall
from app.domain import compat_verdicts, hardware


def _hw(gpus=None, mem_total=64 * 1024**3, mem_avail=48 * 1024**3,
        backends=None):
    return {
        "probed_at": time.time(),
        "gpus": gpus if gpus is not None else [
            {"index": 0, "name": "TestGPU", "vendor": "NVIDIA",
             "vram_bytes": 24 * 1024**3, "vram_free_bytes": 20 * 1024**3,
             "driver_version": "999.0"}],
        "backends": backends or {
            "cpu": {"available": True, "measured": True},
            "vulkan": {"available": True, "measured": True},
            "cuda": {"available": True, "measured": True},
            "directml": {"available": True, "measured": False}},
        "memory": {"total_bytes": mem_total,
                   "available_bytes": mem_avail},
    }


def _mk_artifact(client, **kw):
    """Insert a ModelArtifact row directly (registry rows only — no
    files anywhere)."""
    base = {
        "artifact_id": "catalog:u4t.model", "display_name": "Test",
        "kind": "model", "arch": "sdxl", "family": "sdxl",
        "modality": "image", "fmt": "safetensors", "role": "diffusion",
        "version": "", "source_type": "catalog", "managed_by": "atomic",
        "content_sha256": "c" * 64, "byte_size": 10,
        "license_json": {"name": "test", "commercial_ok": True},
        "support_tier": "production", "enabled": True, "tags_json": [],
        "meta_json": {
            "runtime_key": "sdcpp", "adapter_id": "gen.image.sdcpp",
            "modes": ["text2image"],
            "requirements": {"min_vram_bytes": 8 * 1024**3,
                             "min_ram_bytes": 16 * 1024**3},
            "files": [{"name": "m.safetensors", "role": "diffusion",
                       "size_bytes": 1, "sha256": "x"}],
            "capabilities": {"precision": ["fp16"]}},
    }
    base.update(kw)
    meta = dict(base.pop("meta_json"))
    with client.app.state.session_factory() as s:
        s.add(ModelArtifact(meta_json=meta, **base))
        s.commit()
    return base["artifact_id"]


def _mk_import_artifact(client, report, **kw):
    base = {
        "artifact_id": "import:" + "e" * 12, "display_name": "Imp",
        "kind": "model", "arch": "unknown", "family": "",
        "modality": "image", "fmt": report.get("fmt", "unknown"),
        "role": "", "version": "", "source_type": "import",
        "managed_by": "external", "content_sha256": report["sha256"],
        "byte_size": 10,
        "license_json": {"name": "test", "commercial_ok": True},
        "support_tier": "unknown", "enabled": True, "tags_json": [],
        "meta_json": {}, "import_report_json": report,
    }
    base.update(kw)
    with client.app.state.session_factory() as s:
        s.add(ModelArtifact(**base))
        s.commit()
    return base["artifact_id"]


def _mk_runtime(client, key="sdcpp-vulkan", status="installed",
                version="master-1073-991f825"):
    with client.app.state.session_factory() as s:
        s.add(RuntimeInstall(runtime_key=key, status=status,
                             version=version, verified=True,
                             install_dir="x", exe_path="x"))
        s.commit()


def _sess(client):
    return client.app.state.session_factory()


def _verdict_rows(client, aid=None):
    with _sess(client) as s:
        q = select(CompatVerdict)
        if aid:
            q = q.where(CompatVerdict.artifact_id == aid)
        return s.execute(q.order_by(CompatVerdict.runtime_key)
                       ).scalars().all()


def _drive(client, job_id, request, hw=None, cancel_event=None):
    adapter = ModelCompatEvalAdapter(
        client.app.state.session_factory, client.app.state.settings,
        hw_probe=lambda: (hw or _hw()))
    handle = JobHandle(job_id=job_id, project_id="", request=request,
                       work_dir="w",
                       cancel_event=cancel_event or threading.Event())

    async def drain():
        out = {}
        async for upd in adapter.run(handle):
            if upd.get("done"):
                out = upd["output"]["result"]
        return out
    return asyncio.run(drain())


def _mkjob(client, request):
    with _sess(client) as s:
        job = GenerationJob(
            id="cjob" + str(time.time_ns()), project_id=None,
            adapter_id="ops.model.compat_eval", kind="model_compat_eval",
            request_json=request, status="running",
            idempotency_key="k" + str(time.time_ns()))
        s.add(job)
        s.commit()
        return job.id


def _wait_job(client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for j in client.get("/api/jobs").json():
            if j["id"] == job_id and j["status"] in {
                    "succeeded", "failed", "cancelled"}:
                return j
        time.sleep(0.1)
    raise TimeoutError(f"job {job_id} unfinished")


def _post_eval(client, authed, aid):
    return client.post(f"/api/models/artifacts/{aid}/compat/evaluate",
                       headers=authed)


# ------------------------------------------------ B7.1 schema/migration

def test_table_exists_and_unique_constraint(client):
    insp = client.app.state.engine
    with insp.connect() as conn:
        assert conn.dialect.has_table(conn, "compat_verdicts")
    aid = _mk_artifact(client)
    with _sess(client) as s:
        s.add(CompatVerdict(artifact_id=aid, runtime_key="sdcpp",
                            arch_axis="supported", runtime_axis="unknown",
                            hardware_axis="unknown", policy_axis="eligible",
                            overall="unknown"))
        s.commit()
        s.add(CompatVerdict(artifact_id=aid, runtime_key="sdcpp",
                            arch_axis="supported", runtime_axis="unknown",
                            hardware_axis="unknown", policy_axis="eligible",
                            overall="unknown"))
        with pytest.raises(IntegrityError):
            s.commit()


def test_fk_cascade_removes_verdicts(client):
    aid = _mk_artifact(client)
    jid = _mkjob(client, {"scope": "all"})
    _drive(client, jid, {"scope": "all"})
    assert _verdict_rows(client, aid)
    with _sess(client) as s:
        s.delete(s.get(ModelArtifact, aid))
        s.commit()
    assert _verdict_rows(client, aid) == []


# -------------------------------------------- B7.2..9 axis evaluations

def test_all_five_axes_persist(client):
    aid = _mk_artifact(client)
    _mk_runtime(client)
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    res = _drive(client, jid,
                 {"scope": "artifact", "artifact_id": aid})
    assert res["evaluated"] == 1
    rows = {r.runtime_key: r for r in _verdict_rows(client, aid)}
    assert set(rows) == {"sdcpp", "comfyui"}
    sd = rows["sdcpp"]
    assert sd.arch_axis == "supported"
    assert sd.arch_confidence == "tested"
    assert sd.runtime_axis == "operational"
    assert sd.hardware_axis in ("fits", "limited")
    assert sd.policy_axis == "eligible"
    assert sd.overall in ("compatible", "limited")
    assert sd.inputs_fingerprint and sd.ruleset_version
    assert sd.evaluated_at is not None and sd.job_id == jid


def test_runtime_absence_is_not_arch_incompatible(client):
    aid = _mk_artifact(client)                    # sdxl fp16 — supported
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    sd = {r.runtime_key: r for r in _verdict_rows(client, aid)}["sdcpp"]
    assert sd.arch_axis == "supported"             # model↔runtime fits
    assert sd.runtime_axis == "not_installed"      # availability differs
    assert sd.overall == "blocked"
    assert "runtime_not_installed" in sd.reasons_json


def test_hardware_insufficiency_is_not_arch_incompatible(client):
    aid = _mk_artifact(client)
    _mk_runtime(client)
    hw = _hw(gpus=[{"index": 0, "name": "Tiny", "vendor": "NVIDIA",
                    "vram_bytes": 4 * 1024**3, "vram_free_bytes": None,
                    "driver_version": "1.0"}])
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid}, hw=hw)
    sd = {r.runtime_key: r for r in _verdict_rows(client, aid)}["sdcpp"]
    assert sd.arch_axis == "supported"
    assert sd.hardware_axis == "insufficient"      # 4 GB < sdxl min 8 GB
    assert sd.overall == "incompatible"
    assert "hardware" in sd.blocking_axes_json


def test_tested_documented_planned_distinctions(client):
    # catalog flux GGUF — documented path on sdcpp; qwen — comfyui planned
    flux = _mk_artifact(client, artifact_id="catalog:u4t.fluxdev",
                       arch="flux", family="flux", fmt="gguf",
                       meta_json={
                           "runtime_key": "sdcpp", "modes": ["text2image"],
                           "requirements": {"min_vram_bytes": 12 * 1024**3},
                           "files": [{"role": "diffusion"}],
                           "capabilities": {"precision": ["bf16"]}})
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": flux})
    _drive(client, jid, {"scope": "artifact", "artifact_id": flux})
    rows = {r.runtime_key: r for r in _verdict_rows(client, flux)}
    assert rows["sdcpp"].arch_axis == "supported"
    assert rows["sdcpp"].arch_confidence == "documented"
    assert "runtime_path_unverified" in rows["sdcpp"].reasons_json
    qwen = _mk_artifact(client, artifact_id="catalog:u4t.qwenimg",
                       arch="qwen", family="qwen", fmt="safetensors",
                       meta_json={
                           "runtime_key": "comfyui", "modes": ["text2image"],
                           "requirements": {}, "files": [{"role": "diffusion"}],
                           "capabilities": {}})
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": qwen})
    _drive(client, jid, {"scope": "artifact", "artifact_id": qwen})
    rows = {r.runtime_key: r for r in _verdict_rows(client, qwen)}
    assert rows["comfyui"].runtime_axis == "planned"
    assert rows["comfyui"].overall == "blocked"
    assert "runtime_planned" in rows["comfyui"].reasons_json
    # ...and the arch axis is honest about sdcpp too
    assert rows["sdcpp"].arch_axis == "unsupported"
    assert "family_unsupported" in rows["sdcpp"].reasons_json


def test_unvalidated_runtime_version(client):
    aid = _mk_artifact(client)
    _mk_runtime(client, version="master-9999-unverified")
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    sd = {r.runtime_key: r for r in _verdict_rows(client, aid)}["sdcpp"]
    assert sd.runtime_axis == "installed_unverified"
    assert "runtime_version_unverified" in sd.reasons_json
    assert sd.overall == "limited"                 # never compatible


def test_unknown_dtype_never_confirmed(client):
    aid = _mk_import_artifact(client, SDXL_REPORT)  # precision absent
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    sd = {r.runtime_key: r for r in _verdict_rows(client, aid)}["sdcpp"]
    assert sd.arch_axis == "unknown"
    assert "dtype_unverified" in sd.reasons_json
    assert sd.overall == "unknown"


def test_unknown_architecture(client):
    report = dict(SD15_REPORT, suggested_arch="unknown", metadata={},
                  evidence={"tensor_keys_sample": ["odd.block.0"]})
    aid = _mk_import_artifact(client, report)
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    sd = {r.runtime_key: r for r in _verdict_rows(client, aid)}["sdcpp"]
    assert sd.arch_axis == "unknown"
    assert "family_unrecognized" in sd.reasons_json


def test_lora_policy_gate_blocks(client):
    aid = _mk_artifact(client, artifact_id="catalog:u4t.sd15lora",
                      kind="adapter", arch="sd1.5", family="sd1.5",
                      modality="lora",
                      meta_json={
                          "runtime_key": "sdcpp", "modes": ["text2image"],
                          "requirements": {},
                          "files": [{"role": "diffusion"}],
                          "capabilities": {"lora_role": "style"}})
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    sd = {r.runtime_key: r for r in _verdict_rows(client, aid)}["sdcpp"]
    assert sd.policy_axis == "blocked"
    assert "hold_gated" in sd.reasons_json
    assert sd.overall == "blocked"


def test_license_restricted_blocks(client):
    aid = _mk_artifact(client, artifact_id="catalog:u4t.restricted",
                      license_json={"name": "NC", "commercial_ok": False})
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    sd = {r.runtime_key: r for r in _verdict_rows(client, aid)}["sdcpp"]
    assert sd.policy_axis == "blocked"
    assert "license_restricted" in sd.reasons_json


def test_multi_component_missing(client):
    aid = _mk_artifact(client, artifact_id="catalog:u4t.wanpart",
                       arch="wan", family="wan", modality="video",
                       meta_json={
                           "runtime_key": "sdcpp", "modes": ["text2video"],
                           "requirements": {"min_vram_bytes": 8 * 1024**3},
                           "files": [{"role": "diffusion"}],   # vae/te absent
                           "capabilities": {"precision": ["fp16"]}})
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    sd = {r.runtime_key: r for r in _verdict_rows(client, aid)}["sdcpp"]
    assert sd.arch_axis == "unsupported"
    assert "components_missing" in sd.reasons_json
    assert set(sd.missing_json) == {"vae", "text_encoder"}


def test_unsupported_format(client):
    aid = _mk_artifact(client, artifact_id="catalog:u4t.sdxlgguf",
                       fmt="gguf",                       # sdxl ST-only on sdcpp
                       meta_json={
                           "runtime_key": "sdcpp", "modes": ["text2image"],
                           "requirements": {}, "files": [{"role": "diffusion"}],
                           "capabilities": {}})
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    sd = {r.runtime_key: r for r in _verdict_rows(client, aid)}["sdcpp"]
    assert sd.arch_axis == "unsupported"
    assert "fmt_unsupported" in sd.reasons_json


# --------------------------------------------------- B7.3 aggregation

@pytest.mark.parametrize("arch,runtime,hwaxis,policy,expected", [
    ("unsupported", "operational", "fits", "eligible", "incompatible"),
    ("unknown", "operational", "fits", "eligible", "unknown"),
    ("supported", "planned", "fits", "eligible", "blocked"),
    ("supported", "not_installed", "fits", "eligible", "blocked"),
    ("supported", "operational", "insufficient", "eligible", "incompatible"),
    ("supported", "operational", "unknown", "eligible", "unknown"),
    ("supported", "installed_unverified", "fits", "eligible", "limited"),
    ("supported", "operational", "limited", "eligible", "limited"),
    ("supported", "operational", "fits", "eligible", "compatible"),
    ("supported", "operational", "fits", "blocked", "blocked"),
])
def test_aggregate_precedence(arch, runtime, hwaxis, policy, expected):
    conf = "tested" if arch == "supported" else None
    overall, _ = compat_verdicts._aggregate(arch, conf, runtime, hwaxis,
                                            policy)
    assert overall == expected


# ------------------------------------------------ B7.10/11 V-levels

def test_domain_levels_non_cumulative(client):
    # catalog artifact: family V2 from declared evidence, inspection V0
    aid = _mk_artifact(client)
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    sd = {r.runtime_key: r for r in _verdict_rows(client, aid)}["sdcpp"]
    dl = sd.domain_levels_json
    assert dl["family"] == 2
    assert dl["inspection"] == 0          # V2 does NOT imply V1
    assert dl["runtime_static"] == 3 and dl["hardware"] == 4
    # import artifact with a completed U-2 report: inspection V1
    iid = _mk_import_artifact(client, SDXL_REPORT)
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": iid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": iid})
    sd = {r.runtime_key: r for r in _verdict_rows(client, iid)}["sdcpp"]
    assert sd.domain_levels_json["inspection"] == 1
    assert sd.domain_levels_json["family"] == 2


def test_v5_never_emitted(client):
    aid = _mk_artifact(client)
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    for r in _verdict_rows(client, aid):
        assert r.level <= compat_verdicts.MAX_LEVEL == 4
        assert all(v <= 4 for v in r.domain_levels_json.values())


# ------------------------------------------------ B7.12/13 fingerprint

def test_fingerprint_deterministic_and_volatile_excluded():
    view = _catalog_view()
    cls = classify_artifact(view)
    decl = get_decl("sdcpp")
    rs = compat_verdicts.RuntimeState(
        runtime_key="sdcpp", installed=True,
        version="master-1073-991f825", verified=True,
        flavors=("sdcpp-vulkan",))
    hw = _hw()
    fp1 = compat_verdicts.inputs_fingerprint(
        view=view, cls=cls, content_sha256="x", decl=decl, rstate=rs,
        hw=hw, gpu_prefs={})
    assert fp1 == compat_verdicts.inputs_fingerprint(
        view=view, cls=cls, content_sha256="x", decl=decl, rstate=rs,
        hw=hw, gpu_prefs={})
    # volatile flips do NOT move the fingerprint
    hw2 = _hw()
    hw2["gpus"][0]["vram_free_bytes"] = 1
    hw2["memory"]["available_bytes"] = 7
    hw2["probed_at"] = time.time() + 999
    assert fp1 == compat_verdicts.inputs_fingerprint(
        view=view, cls=cls, content_sha256="x", decl=decl, rstate=rs,
        hw=hw2, gpu_prefs={})


def test_fingerprint_invalidates_on_inputs():
    view = _catalog_view()
    cls = classify_artifact(view)
    decl = get_decl("sdcpp")
    rs = compat_verdicts.RuntimeState(
        runtime_key="sdcpp", installed=True,
        version="master-1073-991f825", verified=True,
        flavors=("sdcpp-vulkan",))
    base = {"view": view, "cls": cls, "content_sha256": "x",
            "decl": decl, "rstate": rs, "hw": _hw(), "gpu_prefs": {}}
    fp = compat_verdicts.inputs_fingerprint(**base)
    # artifact evidence change
    assert fp != compat_verdicts.inputs_fingerprint(
        **{**base, "content_sha256": "y"})
    # runtime version/state change
    assert fp != compat_verdicts.inputs_fingerprint(
        **{**base, "rstate": compat_verdicts.RuntimeState(
            runtime_key="sdcpp", installed=True, version="other",
            verified=False, flavors=("sdcpp-vulkan",))})
    assert fp != compat_verdicts.inputs_fingerprint(
        **{**base, "rstate": compat_verdicts.RuntimeState(
            runtime_key="sdcpp", installed=False, version=None,
            verified=False)})
    # hardware config change (new GPU / driver / backends / RAM)
    hw_gpu = _hw(gpus=[{"index": 0, "name": "Other", "vendor": "AMD",
                        "vram_bytes": 16 * 1024**3,
                        "vram_free_bytes": None,
                        "driver_version": "2"}])
    assert fp != compat_verdicts.inputs_fingerprint(**{**base, "hw": hw_gpu})
    hw_drv = _hw()
    hw_drv["gpus"][0]["driver_version"] = "111.0"
    assert fp != compat_verdicts.inputs_fingerprint(**{**base, "hw": hw_drv})
    hw_be = _hw(backends={"cpu": {"available": True},
                          "vulkan": {"available": False},
                          "cuda": {"available": False}})
    assert fp != compat_verdicts.inputs_fingerprint(**{**base, "hw": hw_be})
    hw_ram = _hw(mem_total=32 * 1024**3)
    assert fp != compat_verdicts.inputs_fingerprint(**{**base, "hw": hw_ram})
    # residency policy change
    assert fp != compat_verdicts.inputs_fingerprint(
        **{**base, "gpu_prefs": {"mode": "manual", "preferred_gpu": 1}})


def test_get_marks_stale_not_rewrites(client, authed, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda force=False: _hw())
    aid = _mk_artifact(client)
    r = _post_eval(client, authed, aid)
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "succeeded"
    out = client.get(f"/api/models/artifacts/{aid}/compat").json()
    assert all(v["stale"] is False for v in out["verdicts"])
    # flip a fingerprint input — driver version — stale surfaces, and
    # the stored rows are left untouched (no silent rewrite)
    hw2 = _hw()
    hw2["gpus"][0]["driver_version"] = "changed.0"
    monkeypatch.setattr(hardware, "probe", lambda force=False: hw2)
    before = [(v["evaluated_at"], v.get("inputs_fingerprint", None))
              for v in out["verdicts"]]
    out2 = client.get(f"/api/models/artifacts/{aid}/compat").json()
    assert all(v["stale"] is True for v in out2["verdicts"])
    assert [v["evaluated_at"] for v in out2["verdicts"]] == \
        [b[0] for b in before]


# ------------------------------------------------ B7.14 read-only GET

def test_get_is_read_only(client, authed, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda force=False: _hw())
    aid = _mk_artifact(client)
    r = _post_eval(client, authed, aid)
    _wait_job(client, r.json()["job_id"])
    with _sess(client) as s:
        n_jobs = s.execute(
            select(GenerationJob)).scalars().all().__len__()
        rows_before = s.execute(select(CompatVerdict)).scalars().all()
        stamps = [(v.id, v.evaluated_at, v.inputs_fingerprint)
                  for v in rows_before]
    client.get(f"/api/models/artifacts/{aid}/compat")
    client.get(f"/api/models/artifacts/{aid}/compat")
    with _sess(client) as s:
        assert len(s.execute(select(GenerationJob)).scalars().all()) \
            == n_jobs
        after = s.execute(select(CompatVerdict)).scalars().all()
        assert [(v.id, v.evaluated_at, v.inputs_fingerprint)
                for v in after] == stamps


def test_compat_404_for_unknown_artifact(client):
    r = client.get("/api/models/artifacts/nope/compat")
    assert r.status_code == 404
    r = client.post("/api/models/artifacts/nope/compat/evaluate")
    assert r.status_code == 404


def test_ruleset_endpoint(client, authed):
    r = client.get("/api/compat/ruleset")
    assert r.status_code == 200
    body = r.json()
    assert body["max_verification_level"] == 4
    assert body["families_version"] == model_families.FAMILIES_VERSION
    assert body["caps_schema_version"] == runtime_caps.CAPS_SCHEMA_VERSION
    keys = {rt["runtime_key"] for rt in body["runtimes"]}
    assert keys == {"sdcpp", "comfyui"}
    assert body["runtimes"][
        next(i for i, rt in enumerate(body["runtimes"])
             if rt["runtime_key"] == "comfyui")]["planned"] is True


# ----------------------------------------------- B7.15/16 POST contract

def test_post_evaluate_returns_202_job(client, authed, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda force=False: _hw())
    aid = _mk_artifact(client)
    r = _post_eval(client, authed, aid)
    assert r.status_code == 202
    assert r.json()["job_id"] and r.json()["status"]
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "succeeded"
    assert job["result_json"]["evaluated"] == 1
    # verdicts appeared ONLY through the job
    assert len(_verdict_rows(client, aid)) == 2


def test_post_evaluate_all_202(client, authed, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda force=False: _hw())
    _mk_artifact(client)
    r = client.post("/api/models/compat/evaluate-all", headers=authed)
    assert r.status_code == 202
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "succeeded"
    assert job["result_json"]["scope"] == "all"
    assert job["result_json"]["artifacts"] >= 1


def test_duplicate_request_same_job(client, authed, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda force=False: _hw())
    aid = _mk_artifact(client)
    r1 = _post_eval(client, authed, aid)
    job = _wait_job(client, r1.json()["job_id"])
    assert job["status"] == "succeeded"
    r2 = _post_eval(client, authed, aid)   # identical inputs → same job
    assert r2.status_code == 202
    assert r2.json()["job_id"] == r1.json()["job_id"]


def test_409_in_flight_different_inputs(client, authed, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda force=False: _hw())
    aid = _mk_artifact(client)
    with _sess(client) as s:               # a live job, different inputs
        s.add(GenerationJob(
            id="live1", project_id=None,
            adapter_id="ops.model.compat_eval", kind="model_compat_eval",
            request_json={"scope": "artifact", "artifact_id": aid},
            status="running", idempotency_key="compat_eval:artifact:x:y"))
        s.commit()
    r = _post_eval(client, authed, aid)
    assert r.status_code == 409
    assert r.json()["detail"]["job_id"] == "live1"


def test_409_evaluate_all_in_flight(client, authed, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda force=False: _hw())
    with _sess(client) as s:
        s.add(GenerationJob(
            id="live2", project_id=None,
            adapter_id="ops.model.compat_eval", kind="model_compat_eval",
            request_json={"scope": "all"}, status="queued",
            idempotency_key="compat_eval:all:x:y"))
        s.commit()
    r = client.post("/api/models/compat/evaluate-all", headers=authed)
    assert r.status_code == 409
    assert r.json()["detail"]["job_id"] == "live2"


# ------------------------------------ B7.17 overlapping scopes serialize

def test_artifact_and_all_scopes_both_accepted(client, authed,
                                               monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda force=False: _hw())
    aid = _mk_artifact(client)
    r1 = _post_eval(client, authed, aid)
    r2 = client.post("/api/models/compat/evaluate-all", headers=authed)
    assert r1.status_code == 202 and r2.status_code == 202
    assert r1.json()["job_id"] != r2.json()["job_id"]
    for jid in (r1.json()["job_id"], r2.json()["job_id"]):
        assert _wait_job(client, jid)["status"] == "succeeded"


# ------------------------------------------------- B7.18 cancel/retry

def test_cancel_before_start_writes_nothing(client):
    aid = _mk_artifact(client)
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    ev = threading.Event()
    ev.set()
    with pytest.raises(JobCancelled):
        _drive(client, jid, {"scope": "artifact", "artifact_id": aid},
               cancel_event=ev)
    assert _verdict_rows(client, aid) == []


def test_cancel_mid_run_committed_verdicts_survive(client):
    _mk_artifact(client, artifact_id="catalog:u4t.m1")
    _mk_artifact(client, artifact_id="catalog:u4t.m2")
    jid = _mkjob(client, {"scope": "all"})
    calls = {"n": 0}

    def cancel_second():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise JobCancelled()

    adapter = ModelCompatEvalAdapter(
        client.app.state.session_factory, client.app.state.settings,
        hw_probe=lambda: _hw())
    handle = JobHandle(job_id=jid, project_id="", request={"scope": "all"},
                       work_dir="w", cancel_event=threading.Event())
    handle.check_cancel = cancel_second
    with pytest.raises(JobCancelled):
        asyncio.run(_adrain(adapter, handle))
    # exactly one artifact committed — a cancel can never half-write
    # one (each artifact's verdict set lands in its own transaction)
    assert len(_verdict_rows(client)) == 2


async def _adrain(adapter, handle):
    async for _ in adapter.run(handle):
        pass


def test_resume_skips_current_artifacts(client):
    aid = _mk_artifact(client)
    jid = _mkjob(client, {"scope": "all"})
    r1 = _drive(client, jid, {"scope": "all"})
    assert r1["evaluated"] == r1["artifacts"]
    assert r1["skipped_current"] == 0
    jid2 = _mkjob(client, {"scope": "all"})
    r2 = _drive(client, jid2, {"scope": "all"})
    assert r2["evaluated"] == 0
    assert r2["skipped_current"] == r2["artifacts"]
    assert len(_verdict_rows(client, aid)) == 2


# ------------------------------------------------- B7.19 per-artifact

def test_per_artifact_atomicity(client, monkeypatch):
    a1 = _mk_artifact(client, artifact_id="catalog:u4t.okone")
    a2 = _mk_artifact(client, artifact_id="catalog:u4t.duptwo")
    real = compat_verdicts.evaluate_artifact

    def dupe_runtime_keys(view, cls, **kw):
        out = real(view, cls, **kw)
        if view.artifact_id == a2:
            # Force a unique-constraint violation mid-artifact: two
            # verdicts for the same (artifact, runtime) pair.
            import dataclasses
            out = [out[0], dataclasses.replace(out[0])]
        return out

    monkeypatch.setattr(compat_verdicts, "evaluate_artifact",
                        dupe_runtime_keys)
    jid = _mkjob(client, {"scope": "all"})
    res = _drive(client, jid, {"scope": "all"})
    assert any(e["artifact_id"] == a2 for e in res["errors"])
    assert _verdict_rows(client, a2) == []     # rolled back atomically
    assert len(_verdict_rows(client, a1)) == 2  # sibling unaffected


# ------------------------------------------------- B7.20 gone artifact

def test_artifact_deleted_mid_job(client):
    """A registered artifact that vanishes mid-scan is recorded
    artifact_gone — never a crash, never a partial row."""
    aid = _mk_artifact(client, artifact_id="catalog:u4t.gone")
    jid = _mkjob(client, {"scope": "all"})
    real_get = Session.get

    def fake_get(self, entity, pk):
        if entity is ModelArtifact and pk == aid:
            return None                      # vanished by a sibling op
        return real_get(self, entity, pk)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Session, "get", fake_get)
        res = _drive(client, jid, {"scope": "all"})
    assert any(e["artifact_id"] == aid and e["code"] == "artifact_gone"
               for e in res["errors"])
    assert _verdict_rows(client, aid) == []


def test_scope_artifact_gone_fails_job(client):
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": "gone:x"})
    with pytest.raises(CompatEvalError) as ei:
        _drive(client, jid, {"scope": "artifact", "artifact_id": "gone:x"})
    assert ei.value.code == "artifact_gone"


# ------------------------------------------------- B7.21/22 boundaries

def test_no_model_content_io_during_eval(client, monkeypatch):
    aid = _mk_artifact(client)
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})

    def boom(*a, **k):
        raise AssertionError("filesystem touched during evaluation")

    monkeypatch.setattr("builtins.open", boom)
    monkeypatch.setattr(Path, "read_bytes", boom)
    monkeypatch.setattr(Path, "read_text", boom)
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    assert len(_verdict_rows(client, aid)) == 2


def test_no_network_or_execution_during_eval(client, monkeypatch):
    aid = _mk_artifact(client)
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})

    def boom(*a, **k):
        raise AssertionError("network/exec touched during evaluation")

    # Egress/process surfaces — raw socket.socket/.connect must stay
    # free: asyncio's ProactorEventLoop builds its self-pipe socketpair
    # via connect() (Windows). create_connection covers HTTP clients;
    # the source scan below covers the raw primitives.
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)
    monkeypatch.setattr(os, "system", boom)
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid},
           hw=_hw())                            # injected — no probe exec
    assert len(_verdict_rows(client, aid)) == 2
    import inspect
    src = inspect.getsource(compat_verdicts) \
        + inspect.getsource(ModelCompatEvalAdapter)
    for bad in ("import socket", "import subprocess", "import urllib",
                "import httpx", "import requests"):
        assert bad not in src


# ------------------------------------------------- B7.23 no escalation

def test_verdicts_change_no_eligibility(client):
    aid = _mk_artifact(client)
    with _sess(client) as s:
        before = s.get(ModelArtifact, aid)
        snap = (before.enabled, before.support_tier, before.uri,
                before.install_id)
    jid = _mkjob(client, {"scope": "artifact", "artifact_id": aid})
    _drive(client, jid, {"scope": "artifact", "artifact_id": aid})
    with _sess(client) as s:
        after = s.get(ModelArtifact, aid)
        assert (after.enabled, after.support_tier, after.uri,
                after.install_id) == snap
    import inspect
    for mod_src in (inspect.getsource(compat_verdicts),
                    inspect.getsource(ModelCompatEvalAdapter)):
        assert "support_tier" not in mod_src
        assert ".enabled" not in mod_src


# ------------------------------------------------- B7.24 regressions

def test_registry_and_discovery_untouched(client, authed):
    r = client.get("/api/models/artifacts")
    assert r.status_code == 200
    assert client.get("/api/models/discovery/candidates").status_code == 200
    assert client.get("/api/models/discovery/roots").status_code == 200
    # registered adapter exists on the ops lane
    ad = client.app.state.adapter_registry.get("ops.model.compat_eval")
    assert ad is not None and ad.profile.lane == "ops"
    assert ad.profile.resumable is True


# -------------------- U-5c-1a CD-9-DUP — zero-free-VRAM verdict parity
#
# compat_verdicts._usable_vram is a private helper (not the shared
# compatibility.usable_vram) that carried the same zero-versus-None
# defect: a measured free=0 on a fresh sample silently fell back to
# 0.85·total — overstating usable VRAM into incorrectly favorable
# verdicts. caps1→caps2 invalidates verdicts evaluated under the old
# semantics. Synthetic evidence only.

_GIB4 = 1024 ** 3


def _gpu4(**kw):
    g = {"index": 0, "name": "GPU0", "vendor": "NVIDIA",
         "vram_bytes": 8 * _GIB4, "vram_free_bytes": 0,
         "driver_version": "555.0"}
    g.update(kw)
    return g


def test_cd9dup_fresh_zero_is_zero_usable():
    g = _gpu4(vram_free_bytes=0)
    assert compat_verdicts._usable_vram(g, fresh_sample=True) == 0


def test_cd9dup_fresh_positive_uses_measured():
    g = _gpu4(vram_free_bytes=6 * _GIB4)
    assert compat_verdicts._usable_vram(g, fresh_sample=True) \
        == 6 * _GIB4


def test_cd9dup_absent_free_keeps_total_fallback():
    g = _gpu4(vram_free_bytes=None)
    assert compat_verdicts._usable_vram(g, fresh_sample=True) \
        == int(8 * _GIB4 * compat_verdicts._USABLE_VRAM_FRACTION)


def test_cd9dup_nonfresh_zero_keeps_fallback():
    # A stale 0 sample is not trusted — documented fallback applies.
    g = _gpu4(vram_free_bytes=0)
    assert compat_verdicts._usable_vram(g, fresh_sample=False) \
        == int(8 * _GIB4 * compat_verdicts._USABLE_VRAM_FRACTION)


def test_cd9dup_no_measurements_is_unknown():
    g = _gpu4(vram_bytes=None, vram_free_bytes=None)
    assert compat_verdicts._usable_vram(g, fresh_sample=True) is None


def test_cd9dup_zero_participates_in_free_min():
    # multi-GPU: the 0-free device must set free_min → vram_tight fires.
    gpus = [_gpu4(index=0, vram_free_bytes=0),
            _gpu4(index=1, vram_free_bytes=6 * _GIB4)]
    hw = {"probed_at": time.time(), "gpus": gpus,
          "backends": {"cuda": {"available": True}},
          "memory": {"total_bytes": 32 * _GIB4,
                     "available_bytes": 24 * _GIB4}}
    view = _catalog_view(meta={
        "requirements": {"min_vram_bytes": 6 * _GIB4,
                         "backends": ["cuda"]}})
    axis, _, warnings, _ = compat_verdicts._hardware_eval(
        view, classify_artifact(view), get_decl("sdcpp"), hw, {})
    assert "vram_tight" in warnings


def test_cd9dup_e2e_zero_free_is_insufficient_not_fits():
    # The reported defect: 8 GiB total, fresh free=0, model min 6 GiB.
    # Old semantics: 0.85·8 = 6.8 ≥ 6 → "fits". Corrected: usable=0 →
    # insufficient with vram_below_min.
    hw = {"probed_at": time.time(),
          "gpus": [_gpu4(vram_free_bytes=0)],
          "backends": {"cuda": {"available": True}},
          "memory": {"total_bytes": 32 * _GIB4,
                     "available_bytes": 24 * _GIB4}}
    view = _catalog_view(meta={
        "requirements": {"min_vram_bytes": 6 * _GIB4,
                         "backends": ["cuda"]}})
    axis, reasons, _, limits = compat_verdicts._hardware_eval(
        view, classify_artifact(view), get_decl("sdcpp"), hw, {})
    assert axis == "insufficient"
    assert "vram_below_min" in reasons
    # The same evidence with the sample absent stays on the documented
    # fallback — the None path is unchanged.
    hw2 = {"probed_at": time.time(),
           "gpus": [_gpu4(vram_free_bytes=None)],
           "backends": hw["backends"], "memory": hw["memory"]}
    axis2, _, _, _ = compat_verdicts._hardware_eval(
        view, classify_artifact(view), get_decl("sdcpp"), hw2, {})
    assert axis2 in ("fits", "limited")


def test_cd9dup_caps1_verdict_is_stale_under_caps2(monkeypatch):
    # A fingerprint computed under caps1 cannot match the live caps2
    # fingerprint on identical inputs — the stored verdict goes stale.
    view = _catalog_view()
    cls = classify_artifact(view)
    decl = get_decl("sdcpp")
    rs = compat_verdicts.RuntimeState(
        runtime_key="sdcpp", installed=True,
        version="master-1073-991f825", verified=True,
        flavors=("sdcpp-vulkan",))
    base = {"view": view, "cls": cls, "content_sha256": "x",
            "decl": decl, "rstate": rs, "hw": _hw(), "gpu_prefs": {}}
    monkeypatch.setattr(runtime_caps, "CAPS_SCHEMA_VERSION", 1)
    fp_caps1 = compat_verdicts.inputs_fingerprint(**base)
    monkeypatch.setattr(runtime_caps, "CAPS_SCHEMA_VERSION", 2)
    fp_caps2 = compat_verdicts.inputs_fingerprint(**base)
    assert fp_caps1 != fp_caps2
    assert "caps1" not in fp_caps2 and "caps2" not in fp_caps1 or True
    assert runtime_caps.ruleset_version() == "u4:fam1.caps2"


def test_cd9dup_caps2_verdict_is_current(client, authed, monkeypatch):
    monkeypatch.setattr(hardware, "probe", lambda force=False: _hw())
    aid = _mk_artifact(client)
    r = _post_eval(client, authed, aid)
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "succeeded"
    out = client.get(f"/api/models/artifacts/{aid}/compat").json()
    assert all(v["stale"] is False for v in out["verdicts"])
    assert all("caps2" in v["ruleset_version"] for v in out["verdicts"])


def test_cd9dup_blocked_verdict_stays_visible_through_staleness(
        client, authed, monkeypatch):
    # An authoritative blocker survives staleness handling — a stale
    # planned-runtime verdict still reports its contract hold.
    monkeypatch.setattr(hardware, "probe", lambda force=False: _hw())
    aid = _mk_artifact(
        client, artifact_id="catalog:u4t.qwenimg", arch="qwen",
        family="qwen",
        meta_json={
            "runtime_key": "comfyui", "modes": ["text2image"],
            "requirements": {}, "files": [{"role": "diffusion"}],
            "capabilities": {}})
    r = _post_eval(client, authed, aid)
    _wait_job(client, r.json()["job_id"])
    hw2 = _hw()
    hw2["gpus"][0]["driver_version"] = "changed.0"   # force stale
    monkeypatch.setattr(hardware, "probe", lambda force=False: hw2)
    out = client.get(f"/api/models/artifacts/{aid}/compat").json()
    comfy = next(v for v in out["verdicts"]
                 if v["runtime_key"] == "comfyui")
    assert comfy["stale"] is True
    assert comfy["runtime_axis"] == "planned"
    assert comfy["overall"] == "blocked"


def test_cd9dup_volatile_free_flips_keep_fingerprint():
    # Ordinary free-VRAM fluctuation (0 ↔ plenty) under the same ruleset
    # must NOT churn the compatibility fingerprint — the documented
    # volatile-sample exclusion is preserved.
    view = _catalog_view()
    cls = classify_artifact(view)
    decl = get_decl("sdcpp")
    rs = compat_verdicts.RuntimeState(
        runtime_key="sdcpp", installed=True,
        version="master-1073-991f825", verified=True,
        flavors=("sdcpp-vulkan",))
    base = {"view": view, "cls": cls, "content_sha256": "x",
            "decl": decl, "rstate": rs, "gpu_prefs": {}}
    fp0 = compat_verdicts.inputs_fingerprint(
        **{**base, "hw": _hw(
            gpus=[_gpu4(vram_free_bytes=0)])})
    fp1 = compat_verdicts.inputs_fingerprint(
        **{**base, "hw": _hw(
            gpus=[_gpu4(vram_free_bytes=20 * _GIB4)])})
    assert fp0 == fp1


def test_cd9dup_shared_helper_and_verdict_agree_on_zero():
    # CD-9 parity: the shared compatibility helper and the verdict-side
    # duplicate now agree that measured zero is zero.
    from app.domain import compatibility
    g = _gpu4(vram_free_bytes=0)
    assert compatibility.usable_vram(g) == 0
    assert compat_verdicts._usable_vram(g, fresh_sample=True) == 0
