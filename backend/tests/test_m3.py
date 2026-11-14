"""M3 coverage: model catalog integrity, hardware/compatibility classification,
VRAM estimation, hardware profiles, GPU scheduler (incl. simulated multi-GPU),
model-file storage/removal, generate.image command validation, the sd.cpp
adapter end-to-end against a fake worker, app-level model-download jobs,
storyboard-thumbnail apply_command, and migration 0006."""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from tests.conftest import make_project, wait_for_job

GIB = 1024 ** 3


# ------------------------------------------------------------------ helpers

def _commit(client, pid, command_type, payload, key, expect=200):
    rev = client.get(f"/api/projects/{pid}").json()["revision"]
    r = client.post(
        f"/api/projects/{pid}/commands/commit",
        json={
            "command_type": command_type,
            "payload": payload,
            "scope": "project",
            "origin": "test",
            "expected_revision": rev,
            "idempotency_key": key if len(key) >= 8 else key + "-pad-0000",
        },
    )
    assert r.status_code == expect, r.text
    return r.json() if expect == 200 else r


def _hw(vram_gib=None, count=1, vendor="nvidia", vulkan=True, cuda=True):
    gpus = [] if vram_gib is None else [
        {
            "index": i, "name": f"Fake GPU {i}", "vendor": vendor,
            "vram_bytes": int(vram_gib[i] * GIB) if isinstance(vram_gib, list) else int(vram_gib * GIB),
            "vram_free_bytes": None, "vram_capped_at_4gb": False,
            "driver_version": "1.0", "inference_capable": True,
        }
        for i in range(count if isinstance(vram_gib, list) else count)
    ]
    if isinstance(vram_gib, list):
        gpus = [
            {
                "index": i, "name": f"Fake GPU {i}", "vendor": vendor,
                "vram_bytes": int(v * GIB), "vram_free_bytes": None,
                "vram_capped_at_4gb": False, "driver_version": "1.0",
                "inference_capable": True,
            }
            for i, v in enumerate(vram_gib)
        ]
    return {
        "gpus": gpus,
        "memory": {"total_bytes": 32 * GIB, "available_bytes": 24 * GIB},
        "disks": [{"mount": "C:\\", "fstype": "NTFS", "total_bytes": 512 * GIB, "free_bytes": 400 * GIB}],
        "backends": {"vulkan": {"available": vulkan}, "cuda": {"available": cuda}, "directml": {"available": False}},
    }


def _install_row(client, model_key: str, tmp_path: Path):
    """Write a real file into the models root + a matching installed row —
    the honest 'already downloaded' state without any network."""
    from app.db.models import ModelInstall, new_id, utcnow
    from app.domain import model_catalog, model_files

    app = client.app
    with app.state.session_factory() as s:
        spec = model_catalog.get(model_key)
        root = model_files.models_root(s, app.state.settings)
        dest_dir = model_files.model_dir(root, spec)
        dest_dir.mkdir(parents=True, exist_ok=True)
        f = spec.files[0]
        dest = dest_dir / f.name
        dest.write_bytes(b"FAKEWEIGHTS" * 16)
        row = s.execute(
            __import__("sqlalchemy").select(ModelInstall).where(ModelInstall.model_key == model_key)
        ).scalar_one_or_none() or ModelInstall(id=new_id(), model_key=model_key)
        row.adapter_id = spec.adapter_id
        row.version = spec.version
        row.status = "installed"
        row.install_root = str(root)
        row.rel_dir = dest_dir.name
        row.file_name = f.name
        row.byte_size = dest.stat().st_size
        row.sha256 = ""
        row.verified = False
        row.license = spec.license["name"]
        row.meta_json = {}
        row.error = None
        row.created_at = row.created_at or utcnow()
        row.updated_at = utcnow()
        s.add(row)
        s.commit()
    return dest


# -------------------------------------------------------------- 1. catalog

def test_catalog_integrity():
    from app.domain import model_catalog
    keys = [m.key for m in model_catalog.catalog()]
    assert len(keys) == len(set(keys)) >= 8  # image + video + experimental entries
    for m in model_catalog.catalog():
        assert m.license["name"] and m.license["url"]
        assert m.requirements["min_vram_bytes"] > 0
        assert m.support in ("production", "tested", "experimental", "planned")
        assert m.modality in ("image", "video", "audio", "multimodal")
        assert m.modes, "every entry declares at least one generation mode"
        assert set(m.modes) & set(m.capabilities.get("tasks", [])) or not m.capabilities.get("tasks")
        # a model can never be installable AND planned
        assert not (m.support == "planned" and m.files)
        for f in m.files:
            assert f.size_bytes > 0 and f.url.startswith("https://")
    runtimes = model_catalog.RUNTIMES
    assert "sdcpp" in runtimes and "vulkan" in runtimes["sdcpp"].backends
    assert "comfyui" in runtimes  # planned runtime for non-sd.cpp models


def test_catalog_video_candidates_are_planned_not_supported():
    """LTX/Wan/Hunyuan appear in the catalog but are never installable or
    generatable — catalog presence must not imply local support."""
    from app.domain import model_catalog, compatibility
    video = {k: s for k, s in model_catalog.MODELS.items() if s.modality == "video"}
    for required in ("ltx.2.5", "wan.2.2.ti2v", "hunyuan.video"):
        assert required in video, f"{required} missing from catalog"
    for key, spec in video.items():
        # M4.0 Phase 2: wan21.t2v.1_3b ran a real validated clip end-to-end
        # (Vulkan + CPU offload, RTX 3060 Ti) → 'tested' with 3 real files.
        # Every other video entry stays 'planned' with no files — nothing
        # else video may be generatable.
        if key == "wan21.t2v.1_3b":
            assert spec.support == "tested" and len(spec.files) == 3
        else:
            assert spec.support == "planned"
            assert spec.files == ()
        c = compatibility.classify_model(spec, _hw(vram_gib=8))
        # 'planned' entries classify as planned; the tested Wan candidate
        # classifies 'compatible' (real requirements, fits 8 GB).
        assert c["status"] == ("planned" if spec.support == "planned" else "compatible")
        assert c["modality"] == "video"
        assert set(c["modes"]) & {"text2video", "image2video", "audio2video"}


def test_catalog_image_candidates_metadata():
    """Qwen-Image + FLUX.1-dev carry honest license/runtime metadata."""
    from app.domain import model_catalog
    qwen = model_catalog.get("qwen.image")
    assert qwen is not None
    assert qwen.license["commercial_ok"] is True  # Apache 2.0
    assert qwen.support == "experimental"
    assert qwen.runtime_key == "comfyui"  # not an sd.cpp architecture
    assert qwen.modes == ("text2image",)
    dev = model_catalog.get("flux.dev")
    assert dev is not None
    assert dev.license["commercial_ok"] is False  # non-commercial license


def test_planned_model_install_rejected(client):
    from app.domain import model_catalog
    spec = model_catalog.get("ltx.2.5")
    assert spec is not None
    r = client.post(f"/api/models/{spec.key}/install", json={})
    assert r.status_code == 422
    assert "planned" in r.json()["detail"].lower()


def test_planned_and_experimental_models_cannot_generate(client):
    pid = make_project(client)["id"]
    for key, needle in (("ltx.2.5", "video"), ("qwen.image", "experimental")):
        r = _commit(client, pid, "generate.image",
                    {"model_key": key, "prompt": "x"}, f"gen-{key}-0001", expect=422)
        assert needle in r.json()["detail"]


# ------------------------------------------------------- 2. compatibility

def test_compat_no_gpu_and_unavailable():
    from app.domain import compatibility, model_catalog
    spec = model_catalog.get("sd15.fp16")
    c = compatibility.classify_model(spec, _hw(vram_gib=None), None)
    assert c["status"] in {"compatible", "limited"}  # cpu fallback exists
    flux = compatibility.classify_model(model_catalog.get("flux.schnell"), _hw(24), None)
    assert flux["status"] == "unavailable"  # no downloadable artifact in this build


def test_compat_tight_gpu_limited():
    from app.domain import compatibility, model_catalog
    # 4.5 GiB → usable 3.8 GiB ≥ est 3.6 but < est+headroom → "limited"
    c = compatibility.classify_model(model_catalog.get("sd15.fp16"), _hw(4.5), None)
    assert c["status"] == "limited"
    assert c["estimate"]["vram_bytes"] > 0
    c2 = compatibility.classify_model(model_catalog.get("sdxl.base"), _hw(4), None)
    assert c2["status"] == "unsupported"  # 4 GiB card < 8 GiB minimum
    assert any("VRAM" in r or "vram" in r.lower() for r in c2["reasons"])


def test_compat_big_gpu_compatible():
    from app.domain import compatibility, model_catalog
    c = compatibility.classify_model(model_catalog.get("sd15.fp16"), _hw(16), None)
    assert c["status"] == "compatible"
    assert c["warnings"] == []


def test_compat_restricted_license_wins():
    from app.domain import compatibility, model_catalog
    c = compatibility.classify_model(model_catalog.get("sdxl.turbo"), _hw(24), None)
    assert c["status"] == "restricted"


def test_estimate_vram_scales_with_batch():
    from app.domain import compatibility, model_catalog
    spec = model_catalog.get("sd15.fp16")
    one = compatibility.estimate_vram(spec, 512, 512, 1)
    four = compatibility.estimate_vram(spec, 512, 512, 4)
    big = compatibility.estimate_vram(spec, 1024, 1024, 1)
    assert one > 0 and four > one and big > one


def test_hardware_profiles():
    from app.domain import compatibility
    assert compatibility.classify_profile(_hw(None))["key"] == "cpu_only"
    assert compatibility.classify_profile(_hw(4))["key"] == "entry"
    assert compatibility.classify_profile(_hw(10))["key"] == "mid"
    assert compatibility.classify_profile(_hw(20))["key"] == "high"
    assert compatibility.classify_profile(_hw(24))["key"] == "pro"
    assert compatibility.classify_profile(_hw([24, 24]))["key"] == "workstation_multi"


# --------------------------------------------------------- 3. GPU scheduler

def _sched(hw, prefs=None):
    from app.domain import gpu_sched
    return gpu_sched.GpuScheduler(hw_probe=hw, prefs=prefs)


def test_scheduler_auto_fills_gpus():
    s = _sched(_hw([8, 8]))
    a = s.acquire("j1", {"device": "auto"})
    b = s.acquire("j2", {"device": "auto"})
    c = s.acquire("j3", {"device": "auto"})
    assert {a, b} == {0, 1}
    assert c is None  # every GPU busy → stays queued
    s.release("j1")
    assert s.acquire("j4", {"device": "auto"}) == a


def test_scheduler_manual_pref():
    s = _sched(_hw([8, 8]), prefs={"mode": "manual", "preferred_gpu": 1})
    assert s.acquire("j1", {}) == 1
    assert s.acquire("j2", {}) is None  # pinned GPU busy → queue
    # explicit request beats prefs
    s.release("j1")
    assert s.acquire("j3", {"device": 0}) == 0


def test_scheduler_cpu_lane():
    s = _sched(_hw([8]))
    assert s.acquire("j1", {"device": "cpu"}) == -1
    # cpu lane doesn't consume the GPU slot
    assert s.acquire("j2", {"device": "auto"}) == 0


def test_scheduler_vram_budget_skips_small_gpu():
    s = _sched(_hw([2, 8]))
    got = s.acquire("j1", {"device": "auto"}, est_vram=4 * GIB)
    assert got == 1  # 2 GiB card skipped for a 4 GiB job


def test_scheduler_state_reports_slots():
    s = _sched(_hw([8, 12]))
    s.acquire("j1", {"device": 0})
    st = s.state()
    assert st["gpu_count"] == 2 and st["free"] == 1
    assert st["slots"][0]["busy_job_id"] == "j1" and st["slots"][1]["busy_job_id"] is None


# ------------------------------------------------------------ 4. model files

def test_models_root_default_and_override(client, tmp_path):
    from app.domain import model_files
    app = client.app
    with app.state.session_factory() as s:
        default = model_files.models_root(s, app.state.settings)
        assert "models" in str(default)
        custom = tmp_path / "somewhere" / "weights"
        root = model_files.set_models_root(s, str(custom))
        s.commit()
        assert root == custom
        assert model_files.models_root(s, app.state.settings) == custom
        model_files.unset_models_root(s)
        s.commit()
        assert model_files.models_root(s, app.state.settings) == default


def test_verify_model_file_checks(tmp_path):
    """Integrity verification: sha256 first (when the source publishes one),
    then declared size, then format magic — a truncated/corrupt download can
    never look 'installed'."""
    from app.domain import model_catalog, model_files
    # sha256-declared spec: any wrong bytes must fail the hash check
    spec = model_catalog.get("sd15.fp16")  # gguf, verified sha256 in catalog
    assert spec.files[0].sha256  # the fixture relies on a published hash
    short = tmp_path / spec.files[0].name
    short.write_bytes(b"GGUF" + b"\x00" * 64)
    ok, why = model_files.verify_model_file(short, spec)
    assert not ok and "sha256" in why
    # no-hash spec: falls back to size + magic
    spec2 = model_catalog.get("sdxl.base")  # safetensors, no published sha256
    assert not spec2.files[0].sha256
    short2 = tmp_path / spec2.files[0].name
    short2.write_bytes(b"\x10\x00\x00\x00\x00\x00\x00\x00{}" + b"\x00" * 64)
    ok2, why2 = model_files.verify_model_file(short2, spec2)
    assert not ok2 and "size" in why2
    # and the magic-bytes check exists for when sizes can't be trusted
    assert not model_files._format_magic_ok(short2, "gguf")
    assert model_files._format_magic_ok(short2, "safetensors")


# ------------------------------------------------- 5. generate.image command

def test_generate_unknown_model_422(client):
    p = make_project(client)
    r = _commit(client, p["id"], "generate.image",
                {"model_key": "nope", "prompt": "a cat"}, "gen-unknown", expect=422)
    assert "unknown model" in r.json()["detail"]


def test_generate_restricted_model_422(client):
    p = make_project(client)
    r = _commit(client, p["id"], "generate.image",
                {"model_key": "sdxl.turbo", "prompt": "a cat"}, "gen-restricted", expect=422)
    assert "license" in r.json()["detail"]


def test_generate_not_installed_422(client):
    p = make_project(client)
    r = _commit(client, p["id"], "generate.image",
                {"model_key": "sd15.fp16", "prompt": "a cat"}, "gen-notinst", expect=422)
    assert "not installed" in r.json()["detail"]


def test_generate_requires_prompt_422(client):
    p = make_project(client)
    r = _commit(client, p["id"], "generate.image",
                {"model_key": "sd15.fp16", "prompt": "  "}, "gen-noprompt", expect=422)
    assert "prompt" in r.json()["detail"]


def test_generate_installed_enqueues_job(client, tmp_path, monkeypatch):
    """Committing generate.image on an installed model enqueues a GPU-lane
    job carrying model_key; the job then fails cleanly because no runtime
    binary exists — proving failure isolation, not data corruption."""
    _install_row(client, "sd15.fp16", tmp_path)
    p = make_project(client)
    out = _commit(client, p["id"], "generate.image",
                  {"model_key": "sd15.fp16", "prompt": "a red barn at dusk"},
                  "gen-ok")
    assert out["job_id"]
    job = wait_for_job(client, p["id"], out["job_id"])
    assert job["model_key"] == "sd15.fp16"
    # no runtime installed → clean failure with an actionable message
    assert job["status"] == "failed"
    assert "runtime" in (job["error"] or "").lower() or "not installed" in (job["error"] or "").lower()
    # the project itself is untouched
    assert client.get(f"/api/projects/{p['id']}").status_code == 200


# --------------------------------------------- 6. sd.cpp adapter end-to-end

_FAKE_WORKER = r'''
import os, sys, time
out = None
sleep = float(os.environ.get("FAKE_SD_SLEEP", "0"))
for i, a in enumerate(sys.argv):
    if a == "-o":
        out = sys.argv[i + 1]
if sleep:
    time.sleep(sleep)
# dump argv for the real-CLI contract assertions
argv_file = os.environ.get("FAKE_SD_ARGV_FILE")
if argv_file:
    open(argv_file, "w").write("\n".join(sys.argv[1:]))
# minimal valid PNG (1x1)
png = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082")
open(out, "wb").write(png)
print("fake sdcpp wrote", out)
'''


def _fake_cmd(monkeypatch, tmp_path, sleep="0"):
    import sys
    script = tmp_path / "fake_sdcpp.py"
    script.write_text(_FAKE_WORKER)
    monkeypatch.setenv("ATOMIC_SDCPP_CMD", f"{sys.executable} {script}")
    monkeypatch.setenv("FAKE_SD_SLEEP", sleep)


def test_sdcpp_e2e_fake_worker(client, tmp_path, monkeypatch):
    _fake_cmd(monkeypatch, tmp_path)
    _install_row(client, "sd15.fp16", tmp_path)
    p = make_project(client)
    scene = _commit(client, p["id"], "scene.create", {"title": "S1"}, "scene-1")
    board = client.get(f"/api/projects/{p['id']}/storyboard").json()
    sid = board[0]["scene"]["id"]
    _commit(client, p["id"], "shot.create", {"scene_id": sid, "title": "Shot 1"}, "shot-1")
    board = client.get(f"/api/projects/{p['id']}/storyboard").json()
    shot_id = board[0]["shots"][0]["shot"]["id"]

    argv_file = tmp_path / "worker_argv.txt"
    monkeypatch.setenv("FAKE_SD_ARGV_FILE", str(argv_file))
    out = _commit(client, p["id"], "generate.image", {
        "model_key": "sd15.fp16", "prompt": "a lighthouse", "seed": 42,
        "shot_id": shot_id, "set_thumbnail": True, "device": "cpu",
    }, "gen-e2e")
    job = wait_for_job(client, p["id"], out["job_id"])
    assert job["status"] == "succeeded", job
    # real-CLI contract: mode name + seed flag must match current sd-cli
    args = argv_file.read_text().splitlines()
    assert "-M" in args and args[args.index("-M") + 1] == "img_gen"
    assert "--seed" in args and "-s" not in args
    assert "-m" in args and "-p" in args and "-W" in args and "-H" in args
    assert job["output_asset_id"]
    assert job["model_key"] == "sd15.fp16"
    assets = client.get(f"/api/projects/{p['id']}/assets?origin=generation").json()["items"]
    assert any(a["id"] == job["output_asset_id"] for a in assets)
    # provenance persisted on the asset row (model, adapter, params)
    from app.db.models import Asset, AssetVersion
    with client.app.state.session_factory() as s:
        a = s.get(Asset, job["output_asset_id"])
        prov = a.provenance_json or {}
        assert prov.get("model_key") == "sd15.fp16"
        assert prov.get("request", {}).get("prompt") == "a lighthouse"
        ver = s.get(AssetVersion, a.current_version_id)
        media = ver.media_json or {}
        assert media.get("model_key") == "sd15.fp16" and media.get("seed") == 42
        assert media.get("is_mock") is False
    # apply_command ran: the shot thumbnail now points at the generated asset
    board = client.get(f"/api/projects/{p['id']}/storyboard").json()
    assert board[0]["shots"][0]["shot"]["thumbnail_asset_id"] == job["output_asset_id"]
    # and it went through the command/audit system
    revs = client.get(f"/api/projects/{p['id']}/revisions").json()
    assert any("thumbnail" in (rev["summary"] or "").lower() or rev["revision"] > 0 for rev in revs)


def test_sdcpp_cancel_kills_worker(client, tmp_path, monkeypatch):
    _fake_cmd(monkeypatch, tmp_path, sleep="30")
    _install_row(client, "sd15.fp16", tmp_path)
    p = make_project(client)
    out = _commit(client, p["id"], "generate.image",
                  {"model_key": "sd15.fp16", "prompt": "x", "device": "cpu"}, "gen-cancel")
    jid = out["job_id"]
    # wait until running, then cancel
    deadline = time.time() + 10
    while time.time() < deadline:
        jobs = client.get(f"/api/projects/{p['id']}/jobs").json()
        if next(j for j in jobs if j["id"] == jid)["status"] == "running":
            break
        time.sleep(0.1)
    r = client.post(f"/api/jobs/{jid}/cancel")
    assert r.status_code == 200, r.text
    job = wait_for_job(client, p["id"], jid)
    assert job["status"] == "cancelled"
    assert job["output_asset_id"] is None
    # no half-written asset appeared
    assets = client.get(f"/api/projects/{p['id']}/assets?origin=generation").json()["items"]
    assert assets == []


# --------------------------------------------------- 7. model install/remove

def test_models_endpoint_lists_catalog(client):
    r = client.get("/api/models")
    assert r.status_code == 200
    models = r.json()
    assert len(models) >= 8
    for m in models:
        assert m["compatibility"]["status"] in {
            "compatible", "limited", "unsupported", "unknown", "restricted", "unavailable", "planned"}
        assert m["license"]["name"]
        assert "download_bytes" in m
        assert m["modality"] in ("image", "video", "audio", "multimodal")
        assert m["support"] in ("production", "tested", "experimental", "planned")
        assert m["modes"]
    keys = {m["key"] for m in models}
    assert {"ltx.2.5", "wan.2.2.ti2v", "hunyuan.video", "qwen.image"} <= keys
    # only the validated Wan entry may be generatable — every other video
    # model stays below 'tested' until real-weight validation
    assert models and all(
        m["support"] in ("planned", "experimental")
        for m in models if m["modality"] == "video" and m["key"] != "wan21.t2v.1_3b"
    )


def test_install_restricted_or_unavailable_422(client):
    assert client.post("/api/models/sdxl.turbo/install", json={}).status_code == 422
    assert client.post("/api/models/flux.schnell/install", json={}).status_code == 422
    assert client.post("/api/models/nope/install", json={}).status_code == 404


def test_model_install_download_job(client, tmp_path, monkeypatch):
    """The whole install path with a faked download + faked runtime install:
    job runs app-level (project_id=null), install row lands 'installed'."""
    from app.db.models import RuntimeInstall, new_id, utcnow
    from app.domain import model_files
    monkeypatch.setattr(model_files, "verify_component",
                        lambda path, f, fmt: (True, "ok"))

    def fake_dl(dest, url, *, expected_size=None, progress=None,
                cancel_check=None, timeout=60):
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"FAKE" * 1024)
        if progress:
            progress(dest.stat().st_size, expected_size or dest.stat().st_size)
        return {"bytes": dest.stat().st_size, "sha256": "0" * 64}
    monkeypatch.setattr(model_files, "download_to", fake_dl)

    # Release resolution hits the live GitHub releases API — keep ordinary CI
    # deterministic by returning a fixed asset (real-network coverage lives in
    # the opt-in ATOMIC_TEST_NETWORK integration test).
    def fake_resolve(spec, flavor, *, fetch=None):
        return {"url": "https://example.com/runtime.zip",
                "name": "runtime-win.zip", "size": 1024,
                "tag": "test-tag", "pinned": False,
                "resolved_from": "mock", "resolved_at": "2026-01-01T00:00:00"}
    monkeypatch.setattr(model_files, "resolve_release_asset", fake_resolve)

    def fake_rt_install(session, settings, spec, flavor, asset, cancel_check=None):
        d = settings.data_dir / "runtimes" / f"{spec.key}-{flavor}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "sd-cli.exe").write_bytes(b"MZ")
        row = RuntimeInstall(
            id=new_id(), runtime_key=f"{spec.key}-{flavor}", version=spec.version,
            status="installed", install_dir=str(d), exe_path=str(d / "sd-cli.exe"),
            byte_size=2, verified=True, meta_json={}, created_at=utcnow(), updated_at=utcnow())
        session.add(row)
        return row
    monkeypatch.setattr(model_files, "install_runtime_zip", fake_rt_install)

    r = client.post("/api/models/sd15.q8/install", json={})
    assert r.status_code == 200, r.text
    job_id = r.json()["job_id"]
    deadline = time.time() + 15
    job = None
    while time.time() < deadline:
        job = next((j for j in client.get("/api/jobs").json() if j["id"] == job_id), None)
        if job and job["status"] in {"succeeded", "failed", "cancelled"}:
            break
        time.sleep(0.2)
    assert job is not None
    assert job["project_id"] is None  # app-level job
    assert job["model_key"] == "sd15.q8"
    assert job["status"] == "succeeded", \
        f"job failed: {job.get('error') or job.get('error_message') or job}"

    models = {m["key"]: m for m in client.get("/api/models").json()}
    assert models["sd15.q8"]["install"]["status"] == "installed"

    # double-install guard
    assert client.post("/api/models/sd15.q8/install", json={}).status_code == 409

    # remove
    r = client.post("/api/models/sd15.q8/remove")
    assert r.status_code == 200, r.text
    models = {m["key"]: m for m in client.get("/api/models").json()}
    assert models["sd15.q8"]["install"]["status"] in {"not_installed", "removed"}


def test_gpu_settings_roundtrip(client):
    r = client.get("/api/gpu/scheduler")
    assert r.status_code == 200
    r = client.put("/api/settings/gpu", json={"mode": "auto"})
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "auto"
    r = client.put("/api/settings/gpu", json={"mode": "bogus"})
    assert r.status_code == 422


# -------------------------------------------------------------- 8. migration

def test_alembic_0006_tables_exist(settings):
    import sqlalchemy as sa
    from app.main import run_migrations
    run_migrations(settings.db_url)
    eng = sa.create_engine(settings.db_url)
    insp = sa.inspect(eng)
    names = set(insp.get_table_names())
    assert {"model_installs", "runtime_installs"} <= names
    cols = {c["name"]: c for c in insp.get_columns("generation_jobs")}
    assert "gpu_index" in cols and "model_key" in cols
    assert cols["project_id"]["nullable"] is True
    # M4.0 recovery columns land via 0007
    assert "attempt_no" in cols and "execution_scope" in cols and "recovery_state" in cols
    with eng.connect() as c:
        v = c.execute(sa.text("select version_num from alembic_version")).scalar()
    assert v == "0011_c4b"


# ------------------------------------------------ 9. Phase-2 follow-ups

def test_pinned_release_resolution():
    """release_tag pins the release: the tag endpoint is queried and the
    result is marked pinned for reproducible installs."""
    from app.domain import model_catalog, model_files
    spec = model_catalog.get_runtime("sdcpp")
    calls = []

    def fake_fetch(url):
        calls.append(url)
        return {"tag_name": "master-1073-991f825", "assets": [
            {"name": "sd-master-991f825-bin-win-cuda12-x64.zip",
             "browser_download_url": "https://example.com/cuda.zip", "size": 367000000},
            {"name": "sd-master-991f825-bin-win-vulkan-x64.zip",
             "browser_download_url": "https://example.com/vk.zip", "size": 30000000}]}

    assert spec.release_tag == "master-1073-991f825"
    a = model_files.resolve_release_asset(spec, "cuda", fetch=fake_fetch)
    assert calls[0].endswith("/tags/master-1073-991f825")
    assert a["name"] == "sd-master-991f825-bin-win-cuda12-x64.zip"
    assert a["pinned"] is True and a["tag"] == "master-1073-991f825"


@pytest.mark.network
@pytest.mark.skipif(not os.environ.get("ATOMIC_TEST_NETWORK"),
                    reason="real-network integration — run with ATOMIC_TEST_NETWORK=1")
def test_release_resolution_real_github():
    """Opt-in: resolve a pinned runtime release against the live GitHub API.
    Exercises the unmocked fetch path ordinary CI keeps offline."""
    from app.domain import model_catalog, model_files
    spec = model_catalog.get_runtime("sdcpp")
    a = model_files.resolve_release_asset(spec, "vulkan")
    assert a["url"].startswith("https://") and a["size"] > 0
    assert a["pinned"] and a["tag"] == spec.release_tag


def test_pick_flavor_prefers_manual_then_measured_then_installed(client, tmp_path, monkeypatch):
    """Selection order: manual request > fastest measured installed >
    installed (catalog order) > probe default."""
    from app.adapters.inference import SdCppAdapter
    from app.db.models import RuntimeInstall, new_id, utcnow
    from app.domain import model_catalog, model_files
    import sqlalchemy as sa
    monkeypatch.setattr(model_files, "smoke_check_runtime", lambda *_a, **_k: (True, "ok"))

    app = client.app
    spec = model_catalog.get_runtime("sdcpp")
    with app.state.session_factory() as s:
        for fl, med in (("vulkan", 9000.0), ("cuda", 5000.0)):
            s.add(RuntimeInstall(
                id=new_id(), runtime_key=f"sdcpp-{fl}", version="t",
                status="installed", install_dir=str(tmp_path / fl),
                exe_path=str(tmp_path / fl / "sd-cli.exe"), verified=True,
                meta_json={"benchmark": {"median_total_ms": med, "runs": 5}},
                created_at=utcnow(), updated_at=utcnow()))
        s.commit()

    ad = SdCppAdapter(app.state.session_factory, app.state.settings)
    # manual wins even when slower
    assert ad._pick_flavor("vulkan", spec) == ("vulkan", "manually selected backend")
    # measured-fastest installed wins on auto
    fl, reason = ad._pick_flavor(None, spec)
    assert fl == "cuda" and "measured" in reason
    # no benchmark data -> catalog order
    with app.state.session_factory() as s:
        for row in s.execute(sa.select(RuntimeInstall)).scalars():
            row.meta_json = {}
        s.commit()
    fl, reason = ad._pick_flavor(None, spec)
    assert fl == spec.backends[0] and "benchmark" in reason.lower()


def test_runtime_only_install_endpoint(client, monkeypatch):
    from app.domain import model_files
    # Keep the enqueued install job off the live GitHub releases API.
    monkeypatch.setattr(
        model_files, "resolve_release_asset",
        lambda spec, flavor, *, fetch=None: {
            "url": "https://example.com/runtime.zip", "name": "runtime-win.zip",
            "size": 1024, "tag": "test-tag", "pinned": False,
            "resolved_from": "mock", "resolved_at": "2026-01-01T00:00:00"})
    r = client.post("/api/runtimes/sdcpp/install", json={"flavor": "cuda"})
    assert r.status_code in (200, 409), r.text
    if r.status_code == 200:
        assert r.json()["job_id"]
    r2 = client.post("/api/runtimes/sdcpp/install", json={"flavor": "bogus"})
    assert r2.status_code == 422
    r3 = client.post("/api/runtimes/nope/install", json={"flavor": "vulkan"})
    assert r3.status_code == 404


def test_queued_reason_strings():
    from app.domain.gpu_sched import GpuScheduler
    gpus = [{"index": 0, "name": "G", "vram_bytes": 8 * 2**30, "vram_free_bytes": 8 * 2**30}]
    sched = GpuScheduler({"gpus": gpus})
    assert sched.acquire("j1", {}, est_vram=None) == 0
    # all busy
    assert sched.acquire("j2", {}) is None and "busy" in sched.last_reason
    sched.release("j1")
    # doesn't fit
    assert sched.acquire("j3", {}, est_vram=64 * 2**30) is None
    assert "no device fits" in sched.last_reason
    # no CPU path allowed
    sched0 = GpuScheduler({"gpus": []})
    assert sched0.acquire("j4", {"allow_cpu": False}) is None
    assert "no GPU" in sched0.last_reason


def test_asset_list_exposes_provenance(client, tmp_path):
    """Current-version media_json lands in the asset list response."""
    pid = make_project(client)["id"]
    from app.db.models import Asset, AssetFile, AssetVersion, StorageLocation, new_id, utcnow
    app = client.app
    with app.state.session_factory() as s:
        loc = s.execute(__import__("sqlalchemy").select(StorageLocation)).scalars().first() \
            or StorageLocation(id=new_id(), label="t", root_path=str(tmp_path))
        if loc.id is None:
            s.add(loc); s.flush()
        s.add(AssetFile(sha256="0" * 64, storage_location_id=loc.id,
                        rel_path="jobs/x.png", byte_size=1, status="ready"))
        a = Asset(id=new_id(), project_id=pid, name="g.png", kind="image",
                  origin="generation", scope="project", status="ready",
                  uri="jobs/x.png", sha256="0" * 64, byte_size=1,
                  provenance_json={"adapter_id": "gen.image.sdcpp"})
        s.add(a); s.flush()
        v = AssetVersion(id=new_id(), asset_id=a.id, version_no=1,
                         sha256="0" * 64,
                         media_json={"seed": 42, "runtime": {"flavor": "vulkan"}})
        s.add(v); s.flush()
        a.current_version_id = v.id
        s.commit()
    items = client.get(f"/api/projects/{pid}/assets").json()["items"]
    row = next(i for i in items if i["id"] == a.id)
    assert row["media_json"]["seed"] == 42
    assert row["media_json"]["runtime"]["flavor"] == "vulkan"
    assert row["provenance_json"]["adapter_id"] == "gen.image.sdcpp"


def test_runtime_benchmark_endpoint(client, tmp_path):
    """Benchmark records land on the flavor-keyed install row."""
    from app.db.models import RuntimeInstall, new_id, utcnow
    app = client.app
    with app.state.session_factory() as s:
        s.add(RuntimeInstall(
            id=new_id(), runtime_key="sdcpp-vulkan", version="t",
            status="installed", install_dir=str(tmp_path),
            exe_path=str(tmp_path / "sd-cli.exe"), meta_json={},
            created_at=utcnow(), updated_at=utcnow()))
        s.commit()
    r = client.post("/api/runtimes/sdcpp/benchmark",
                    json={"flavor": "vulkan", "median_total_ms": 7750,
                          "runs": 5, "gpu": "G"})
    assert r.status_code == 200, r.text
    assert r.json()["runtime_key"] == "sdcpp-vulkan"
    assert r.json()["benchmark"]["median_total_ms"] == 7750
    r2 = client.post("/api/runtimes/sdcpp/benchmark",
                     json={"flavor": "cpu", "median_total_ms": 1, "runs": 1})
    assert r2.status_code == 404


def test_estimate_vram_realistic_scaling():
    """512² ≈ catalog baseline; extreme jobs get large-but-sane estimates."""
    from app.domain import compatibility, model_catalog
    spec = model_catalog.get("sd15.fp16")
    base = compatibility.estimate_vram(spec, 512, 512, 1)
    assert abs(base - spec.vram_baseline_bytes) < 2**28  # within 256 MiB
    # 8192² (ratio 256) was a deliberate stress test: sqrt scaling → ~16x act,
    # not the 256x a linear model produced (360 GiB was absurd messaging).
    huge = compatibility.estimate_vram(spec, 8192, 8192, 1)
    assert base < huge < 64 * 2**30
    assert huge > 6 * 2**30  # still honestly queued on a 6-GiB-usable card


def test_pick_flavor_excludes_broken_installs(client, tmp_path, monkeypatch):
    """An installed-but-broken flavor (missing DLLs) is not auto-picked and
    produces an actionable error when manually requested."""
    import pytest
    from app.adapters.inference import SdCppAdapter
    from app.db.models import RuntimeInstall, new_id, utcnow
    from app.domain import model_catalog, model_files

    def fake_smoke(exe, **_):
        return (False, "exit 1: no output") if "cuda" in str(exe) else (True, "ok")
    monkeypatch.setattr(model_files, "smoke_check_runtime", fake_smoke)

    app = client.app
    spec = model_catalog.get_runtime("sdcpp")
    with app.state.session_factory() as s:
        for fl in ("vulkan", "cuda"):
            s.add(RuntimeInstall(
                id=new_id(), runtime_key=f"sdcpp-{fl}", version="t",
                status="installed", install_dir=str(tmp_path / fl),
                exe_path=str(tmp_path / fl / "sd-cli.exe"), verified=True,
                meta_json={}, created_at=utcnow(), updated_at=utcnow()))
        s.commit()
    ad = SdCppAdapter(app.state.session_factory, app.state.settings)
    # auto: broken cuda excluded; vulkan is the only operational pick
    fl, reason = ad._pick_flavor(None, spec)
    assert fl == "vulkan" and "operational" in reason
    # the failed smoke demoted the row to error with an actionable message
    with app.state.session_factory() as s:
        row = s.execute(__import__("sqlalchemy").select(RuntimeInstall).where(
            RuntimeInstall.runtime_key == "sdcpp-cuda")).scalar_one()
        assert row.status == "error" and "launch check" in row.error
    # manual pick of the broken flavor -> actionable ModelError, not a failed job
    with pytest.raises(model_files.ModelError, match="not operational"):
        ad._pick_flavor("cuda", spec)


def test_benchmark_scoped_to_recording_gpu(client, tmp_path, monkeypatch):
    """A benchmark recorded on a different GPU must not drive selection."""
    from app.adapters.inference import SdCppAdapter
    from app.db.models import RuntimeInstall, new_id, utcnow
    from app.domain import hardware, model_catalog, model_files
    monkeypatch.setattr(model_files, "smoke_check_runtime", lambda *_a, **_k: (True, "ok"))
    monkeypatch.setattr(hardware, "probe",
                        lambda *a, **k: {"gpus": [{"name": "GPU-A"}], "backends": {}})
    app = client.app
    spec = model_catalog.get_runtime("sdcpp")
    with app.state.session_factory() as s:
        s.add(RuntimeInstall(id=new_id(), runtime_key="sdcpp-vulkan", version="t",
                             status="installed", install_dir=str(tmp_path / "v"),
                             exe_path=str(tmp_path / "v" / "sd-cli.exe"), verified=True,
                             meta_json={"benchmark": {"median_total_ms": 9000,
                                                      "runs": 5, "gpu": "OTHER-GPU"}},
                             created_at=utcnow(), updated_at=utcnow()))
        s.add(RuntimeInstall(id=new_id(), runtime_key="sdcpp-cuda", version="t",
                             status="installed", install_dir=str(tmp_path / "c"),
                             exe_path=str(tmp_path / "c" / "sd-cli.exe"), verified=True,
                             meta_json={"benchmark": {"median_total_ms": 5000,
                                                      "runs": 5, "gpu": "OTHER-GPU"}},
                             created_at=utcnow(), updated_at=utcnow()))
        s.commit()
    ad = SdCppAdapter(app.state.session_factory, app.state.settings)
    fl, reason = ad._pick_flavor(None, spec)
    # neither benchmark applies on GPU-A -> catalog-order installed pick
    assert fl == spec.backends[0] and "benchmark" in reason.lower()
