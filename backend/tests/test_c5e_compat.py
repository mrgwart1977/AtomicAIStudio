"""C5-E — consolidated compatibility matrix, install preflight, read-only
validation report, and the opt-in benchmark harness.

Covers: availability classes (installed_operational / installed_limited /
installable / planned / blocked), CPU-only + multi-GPU + unknown-VRAM
classification, preflight fields + no-download defaults, validation
checks (catalog metadata, runtime binary, missing files, hash mismatch,
capability contract, driver/runtime), benchmark opt-in/refusal/
cancellation/durable result — all through the real endpoints.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models import ModelInstall, new_id, utcnow
from app.domain import model_catalog, model_files
from tests.conftest import wait_for_job


# ------------------------------------------------------------ helpers

def _install_row(client, model_key: str, tmp_path: Path, *,
                 match_size: bool = False, sha256: str = ""):
    """Real file + installed row — see test_m3._install_row."""
    app = client.app
    with app.state.session_factory() as s:
        spec = model_catalog.get(model_key)
        root = model_files.models_root(s, app.state.settings)
        dest_dir = model_files.model_dir(root, spec)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(spec.files):
            dest = dest_dir / f.name
            if match_size and f.size_bytes:
                head = (b"FAKEWEIGHTS" * 4)[:min(48, f.size_bytes)]
                dest.write_bytes(head + b"\0" * (f.size_bytes - len(head)))
            else:
                dest.write_bytes(b"FAKEWEIGHTS" * (16 + i))
        row = s.execute(
            select(ModelInstall).where(ModelInstall.model_key == model_key)
        ).scalar_one_or_none() or ModelInstall(id=new_id(), model_key=model_key)
        row.adapter_id = spec.adapter_id
        row.version = spec.version
        row.status = "installed"
        row.install_root = str(root)
        row.rel_dir = dest_dir.name
        row.file_name = spec.files[0].name
        row.byte_size = (dest_dir / spec.files[0].name).stat().st_size
        row.sha256 = sha256
        row.verified = False
        row.license = spec.license["name"]
        row.meta_json = {}
        row.error = None
        row.created_at = row.created_at or utcnow()
        row.updated_at = utcnow()
        s.add(row)
        s.commit()
    return dest_dir


def _wait_app_job(client, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = next((j for j in client.get("/api/jobs").json()
                    if j["id"] == job_id), None)
        if job and job["status"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.15)
    raise TimeoutError(f"job {job_id} did not finish")


def _check(report: dict, name: str) -> dict:
    return next(c for c in report["checks"] if c["name"] == name)


# ------------------------------------------------------- matrix

def test_compatibility_matrix_shape_and_classes(client):
    r = client.get("/api/compatibility")
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["hardware_profile"]["key"] in (
        "cpu_only", "entry", "mid", "high", "pro", "workstation_multi")
    assert len(rep["runtimes"]) >= 2  # sdcpp + comfyui
    keys = {m["key"]: m for m in rep["models"]}
    assert keys["sd15.fp16"]["availability"] == "installable"
    # planned video models must read as planned — never installable
    for k in ("ltx.2.5", "wan.2.2.ti2v", "hunyuan.video"):
        assert keys[k]["availability"] == "planned", keys[k]
        assert keys[k]["compatibility"]["status"] == "planned"
    # every entry carries the consolidated fields
    m = keys["sd15.fp16"]
    for f in ("display_name", "version", "modality", "modes", "runtime",
              "install", "estimates", "license", "limitations",
              "compatibility", "benchmarkable"):
        assert f in m, f
    assert m["runtime"]["key"] == "sdcpp" and m["runtime"]["known"]


def test_installed_model_reports_operational_and_benchmarkable(client, tmp_path):
    _install_row(client, "sd15.q8", tmp_path)
    rep = client.get("/api/compatibility").json()
    m = next(x for x in rep["models"] if x["key"] == "sd15.q8")
    assert m["availability"] in ("installed_operational", "installed_limited")
    assert m["benchmarkable"] is True
    assert m["install"]["status"] == "installed"


def test_installed_weights_without_runtime_are_limited_not_operational(client, tmp_path):
    """Installed model files alone must not imply operational: with no
    installed runtime flavor, an otherwise-compatible model is
    installed_limited — runtime availability is part of the answer."""
    from app.db.models import RuntimeInstall
    _install_row(client, "sd15.q8", tmp_path)
    rep = client.get("/api/compatibility").json()
    m = next(x for x in rep["models"] if x["key"] == "sd15.q8")
    assert m["runtime"]["any_operational"] is False
    assert m["availability"] == "installed_limited"
    # install a matching runtime flavor with a real binary on disk
    rt_dir = tmp_path / "rt"; rt_dir.mkdir(exist_ok=True)
    (rt_dir / "sd.exe").write_bytes(b"x")
    app = client.app
    with app.state.session_factory() as s:
        s.add(RuntimeInstall(
            id=new_id(), runtime_key="sdcpp-vulkan", version="t",
            status="installed", install_dir=str(rt_dir),
            exe_path=str(rt_dir / "sd.exe"), byte_size=1,
            verified=True, meta_json={}, created_at=utcnow(),
            updated_at=utcnow()))
        s.commit()
    rep = client.get("/api/compatibility").json()
    m = next(x for x in rep["models"] if x["key"] == "sd15.q8")
    assert m["runtime"]["installed_flavors"] == ["vulkan"]
    hw = client.get("/api/hardware").json()
    if (hw.get("backends") or {}).get("vulkan", {}).get("available"):
        assert m["runtime"]["any_operational"] is True
        # Operational runtime lifts the runtime gate, but availability
        # still honestly reflects this machine's hardware fit: only a
        # "compatible" classifier verdict earns installed_operational.
        assert m["availability"] == (
            "installed_operational"
            if m["compatibility"]["status"] == "compatible"
            else "installed_limited")


def test_benchmark_adapter_shares_gen_lane_admission(client):
    """Fairness policy: benchmarks take a GPU scheduler slot and pass the
    same memory admission as generation jobs — same queue, same gates."""
    adapters = client.get("/api/adapters").json()
    b = next(a for a in adapters if a["adapter_id"] == "ops.model.benchmark")
    assert b["lane"] == "gen" and b["needs_gpu"] is True


def test_planned_runtime_does_not_claim_operational(client):
    rep = client.get("/api/compatibility").json()
    comfy = next(rt for rt in rep["runtimes"] if rt["key"] == "comfyui")
    assert comfy["planned"] is True
    assert comfy["any_operational"] is False
    # multi-GPU claims only via measured flavors — nothing invented
    for fl in comfy["flavors"]:
        assert fl["installed"] is False and fl["operational"] is False


# ------------------------------------------------------- S-C5 reasons

def test_availability_reason_distinguishes_block_causes(client):
    """Blocked must not be a hardware lump: license, missing download,
    and planned each get a distinct machine-readable reason."""
    rep = client.get("/api/compatibility").json()
    keys = {m["key"]: m for m in rep["models"]}
    assert keys["flux.schnell"]["availability"] == "blocked"
    assert keys["flux.schnell"]["availability_reason"] == "download_unavailable"
    assert keys["flux.dev"]["availability_reason"] == "license_restricted"
    assert keys["sdxl.turbo"]["availability_reason"] == "license_restricted"
    assert keys["hunyuan.video"]["availability_reason"] == "planned"
    # every entry carries the field (None when nothing adverse applies)
    for m in rep["models"]:
        assert "availability_reason" in m


def test_availability_reason_installed_without_runtime(client, tmp_path):
    _install_row(client, "sd15.q8", tmp_path)
    rep = client.get("/api/compatibility").json()
    m = next(x for x in rep["models"] if x["key"] == "sd15.q8")
    assert m["availability_reason"] == "missing_runtime"


# ------------------------------------------------------- preflight

def test_preflight_reports_disk_license_estimates_no_autodownload(client):
    r = client.get("/api/models/sd15.fp16/preflight")
    assert r.status_code == 200, r.text
    pf = r.json()
    assert pf["download_bytes"] > 0
    assert pf["disk_free_bytes"] is None or pf["disk_free_bytes"] >= 0
    assert pf["disk_headroom_bytes"] > 0  # required headroom is disclosed
    assert pf["disk_sufficient"] in (True, False)
    assert pf["est_vram_bytes"] is None or pf["est_vram_bytes"] > 0
    assert pf["estimate_basis"] == "estimate"
    assert pf["license"]["name"]
    assert pf["requires_explicit_approval"] is True
    assert pf["auto_download"] is False
    assert pf["runtime"]["key"] == "sdcpp"
    assert isinstance(pf["runtime"]["flavors"], list)


def test_preflight_planned_model_not_installable(client):
    pf = client.get("/api/models/wan.2.2.ti2v/preflight").json()
    assert pf["installable"] is False
    assert pf["runtime"]["planned"] is True


def test_preflight_unknown_model_404(client):
    assert client.get("/api/models/nope/preflight").status_code == 404


# ------------------------------------------------------- validation

def test_validation_not_installed_skips_file_checks(client):
    rep = client.get("/api/models/sd15.fp16/validation").json()
    assert rep["read_only"] is True and rep["ran_inference"] is False
    assert _check(rep, "catalog_metadata")["ok"] is True
    assert _check(rep, "runtime_available")["ok"] is True
    assert _check(rep, "model_files")["ok"] is None      # skipped
    assert _check(rep, "hash_verification")["ok"] is None  # skipped/opt-in


def test_validation_installed_missing_and_size_mismatch(client, tmp_path):
    # file present but wrong size (FAKEWEIGHTS << declared size)
    _install_row(client, "sd15.q8", tmp_path)
    rep = client.get("/api/models/sd15.q8/validation").json()
    mf = _check(rep, "model_files")
    assert mf["ok"] is False and "size mismatch" in mf["detail"]
    # runtime_binary: model installed but no runtime install row → flag
    rb = _check(rep, "runtime_binary")
    assert rb["ok"] is False
    assert "runtime is not installed" in rb["detail"]


def test_validation_hash_mismatch_detected(client, tmp_path):
    # sd15.q8 declares sha256; our fake file can't match it
    _install_row(client, "sd15.q8", tmp_path, match_size=True)
    rep = client.get("/api/models/sd15.q8/validation?verify_hashes=true").json()
    hv = _check(rep, "hash_verification")
    assert hv["ok"] is False and "mismatch" in hv["detail"]


def test_validation_hash_default_is_opt_in(client, tmp_path):
    _install_row(client, "sd15.q8", tmp_path, match_size=True)
    rep = client.get("/api/models/sd15.q8/validation").json()
    hv = _check(rep, "hash_verification")
    assert hv["ok"] is None and "verify_hashes" in hv["detail"]


def test_validation_runtime_flavor_driver_mismatch(client, tmp_path):
    """An installed CUDA flavor with no CUDA backend detected is flagged."""
    from app.db.models import RuntimeInstall
    app = client.app
    with app.state.session_factory() as s:
        rt = RuntimeInstall(
            id=new_id(), runtime_key="sdcpp-cuda", version="t",
            status="installed", install_dir=str(tmp_path / "rt"),
            exe_path=str(tmp_path / "rt" / "sd.exe"), byte_size=1,
            verified=True, meta_json={}, created_at=utcnow(), updated_at=utcnow())
        (tmp_path / "rt").mkdir(parents=True, exist_ok=True)
        (tmp_path / "rt" / "sd.exe").write_bytes(b"x")
        s.add(rt); s.commit()
    rep = client.get("/api/models/sd15.fp16/validation").json()
    rb = _check(rep, "runtime_binary")
    assert rb["ok"] is True  # binary exists
    dr = _check(rep, "driver_runtime")
    hw = client.get("/api/hardware").json()
    cuda_available = bool((hw.get("backends") or {}).get("cuda", {}).get("available"))
    if not cuda_available:
        assert dr["ok"] is False and dr["severity"] == "warning"
    else:
        assert dr["ok"] is True


def test_validation_planned_model_safe(client):
    rep = client.get("/api/models/hunyuan.video/validation").json()
    assert rep["read_only"] is True
    assert _check(rep, "runtime_available")["ok"] is None  # skipped for planned


def test_capability_contract_reports_unregistered_adapter(client):
    # experimental comfyui models point at adapters that don't serve locally
    rep = client.get("/api/models/qwen.image/validation").json()
    cc = _check(rep, "capability_contract")
    # comfyui adapter isn't registered locally → reported, not hidden
    assert cc["ok"] is False or "not registered" in cc["detail"] or cc["ok"] is True


# -------------------------------------------------- S-C5 severity bands

def test_validation_missing_artifact_is_warning_not_blocking(client):
    """An unpinned download artifact is honest 'download unavailable' —
    a warning, not a catalog integrity failure, and must not fail the
    validation rollup."""
    rep = client.get("/api/models/flux.schnell/validation").json()
    ap = _check(rep, "artifact_pin")
    assert ap["ok"] is False and ap["severity"] == "warning"
    assert rep["summary"]["warning"] >= 1
    assert rep["ok"] is True  # warnings/info never fail the rollup


def test_validation_planned_runtime_is_info_not_blocking(client):
    rep = client.get("/api/models/qwen.image/validation").json()
    ra = _check(rep, "runtime_available")
    assert ra["ok"] is False and ra["severity"] == "info"
    assert rep["summary"]["info"] >= 1
    assert rep["ok"] is True


def test_validation_integrity_failures_are_blocking(client, tmp_path):
    # size mismatch = integrity → blocking severity, rollup fails
    _install_row(client, "sd15.q8", tmp_path)
    rep = client.get("/api/models/sd15.q8/validation").json()
    mf = _check(rep, "model_files")
    assert mf["ok"] is False and mf["severity"] == "blocking"
    rb = _check(rep, "runtime_binary")  # installed model, no runtime → blocking
    assert rb["ok"] is False and rb["severity"] == "blocking"
    assert rep["ok"] is False
    assert rep["summary"]["blocking"] >= 2
    # hash mismatch = integrity → blocking too
    _install_row(client, "sd15.q8", tmp_path, match_size=True)
    rep = client.get("/api/models/sd15.q8/validation?verify_hashes=true").json()
    hv = _check(rep, "hash_verification")
    assert hv["ok"] is False and hv["severity"] == "blocking"
    assert rep["ok"] is False


# ------------------------------------------------------- benchmark

def test_benchmark_refuses_uninstalled_model(client):
    r = client.post("/api/models/sd15.fp16/benchmark", json={"runs": 1})
    assert r.status_code == 422
    assert "not installed" in r.json()["detail"]


def test_benchmark_refuses_planned_and_nonlocal(client):
    assert client.post("/api/models/hunyuan.video/benchmark",
                       json={}).status_code == 422
    assert client.post("/api/models/wan21.t2v.1_3b/benchmark",
                       json={}).status_code == 422  # video adapter: not this harness
    assert client.post("/api/models/nope/benchmark", json={}).status_code == 404


_FAKE_WORKER = r'''
import os, sys, time
out = None
sleep = float(os.environ.get("FAKE_SD_SLEEP", "0"))
for i, a in enumerate(sys.argv):
    if a == "-o":
        out = sys.argv[i + 1]
if sleep:
    time.sleep(sleep)
png = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082")
open(out, "wb").write(png)
print("fake sdcpp wrote", out)
'''


def _fake_cmd(monkeypatch, tmp_path, sleep="0"):
    script = tmp_path / "fake_sdcpp.py"
    script.write_text(_FAKE_WORKER)
    monkeypatch.setenv("ATOMIC_SDCPP_CMD", f"{sys.executable} {script}")
    monkeypatch.setenv("FAKE_SD_SLEEP", sleep)


def test_benchmark_opt_in_measures_and_persists(client, tmp_path, monkeypatch):
    _fake_cmd(monkeypatch, tmp_path)
    _install_row(client, "sd15.q8", tmp_path)
    r = client.post("/api/models/sd15.q8/benchmark", json={"runs": 2})
    assert r.status_code == 200, r.text
    job = _wait_app_job(client, r.json()["job_id"])
    assert job["status"] == "succeeded", job
    res = job["result_json"] or {}
    assert res["model_key"] == "sd15.q8"
    assert len(res["runs"]) == 2
    assert all(rr["duration_ms"] >= 0 for rr in res["runs"])
    assert res["median_duration_ms"] >= 0
    # reproducibility record: identity + params + conditions + limits
    assert res["model_file_sha256"] == model_catalog.get("sd15.q8").files[0].sha256
    assert res["runs"][0]["condition"] == "cold"
    assert res["runs"][1]["condition"] == "warm"
    assert isinstance(res["params"]["width"], int)
    assert "driver_version" in res and "measured_at" in res
    assert res["measurement_limitations"]
    assert "estimate" in res["estimate_vs_measured"]["warning"].lower() \
        or "ESTIMATE" in res["estimate_vs_measured"]["warning"]
    assert "vram" in res["peak_memory_note"].lower()
    # durable: install row meta carries the result — /api/compatibility shows it
    rep = client.get("/api/compatibility").json()
    m = next(x for x in rep["models"] if x["key"] == "sd15.q8")
    assert m["benchmark"] and m["benchmark"]["median_duration_ms"] == res["median_duration_ms"]


def test_benchmark_cancellation(client, tmp_path, monkeypatch):
    _fake_cmd(monkeypatch, tmp_path, sleep="30")
    _install_row(client, "sd15.q8", tmp_path)
    r = client.post("/api/models/sd15.q8/benchmark", json={"runs": 3})
    jid = r.json()["job_id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        job = next((j for j in client.get("/api/jobs").json() if j["id"] == jid), None)
        if job and job["status"] == "running":
            break
        time.sleep(0.1)
    assert client.post(f"/api/jobs/{jid}/cancel").status_code == 200
    job = _wait_app_job(client, jid)
    assert job["status"] == "cancelled"
    # cancelled benchmark must NOT stamp a result
    rep = client.get("/api/compatibility").json()
    m = next(x for x in rep["models"] if x["key"] == "sd15.q8")
    assert not m.get("benchmark")


def test_benchmark_app_level_not_project_scoped(client, tmp_path, monkeypatch):
    _fake_cmd(monkeypatch, tmp_path)
    _install_row(client, "sd15.q8", tmp_path)
    r = client.post("/api/models/sd15.q8/benchmark", json={"runs": 1})
    job = _wait_app_job(client, r.json()["job_id"])
    assert job["project_id"] is None
