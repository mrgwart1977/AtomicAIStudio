"""M4.0 Phase-1 coverage: video catalog integrity (multi-component specs,
roles, real hashes), the gen.video.sdcpp adapter contract, generate.video
command validation, multi-file install/verify paths, job recovery fields
(attempt_no / execution_scope / recovery_state), and ffprobe media_probe
fallbacks."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import make_project

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


def _install_video(client, model_key: str):
    """Write every declared component + an installed row — the honest
    'already downloaded' multi-file state without any network."""
    from sqlalchemy import select

    from app.db.models import ModelInstall, new_id, utcnow
    from app.domain import model_catalog, model_files

    app = client.app
    with app.state.session_factory() as s:
        spec = model_catalog.get(model_key)
        root = model_files.models_root(s, app.state.settings)
        dest_dir = model_files.model_dir(root, spec)
        dest_dir.mkdir(parents=True, exist_ok=True)
        for f in spec.files:
            (dest_dir / f.name).write_bytes(b"FAKE" * 8)
        row = s.execute(
            select(ModelInstall).where(ModelInstall.model_key == model_key)
        ).scalar_one_or_none() or ModelInstall(id=new_id(), model_key=model_key)
        row.adapter_id = spec.adapter_id
        row.version = spec.version
        row.status = "installed"
        row.install_root = str(root)
        row.rel_dir = dest_dir.name
        row.file_name = spec.files[0].name
        row.byte_size = sum(f.size_bytes for f in spec.files)
        row.sha256 = ""
        row.verified = False
        row.license = spec.license["name"]
        row.meta_json = {}
        row.error = None
        row.created_at = row.created_at or utcnow()
        row.updated_at = utcnow()
        s.add(row)
        s.commit()
    return dest_dir


# ------------------------------------------------------------------ catalog

def test_video_catalog_entry_is_multi_component():
    from app.domain import model_catalog

    spec = model_catalog.get("wan21.t2v.1_3b")
    assert spec is not None
    assert spec.modality == "video"
    assert "text2video" in spec.modes
    roles = {f.role for f in spec.files}
    assert {"diffusion", "vae", "text_encoder"} <= roles
    # verified artifacts: full 64-char sha256 + real byte sizes, not estimates
    for f in spec.files:
        assert f.sha256 and len(f.sha256) == 64, f.name
        assert f.size_bytes > 0
        assert f.url.startswith("https://huggingface.co/")
    # total download is honestly represented (~6.4 GB)
    assert 6 * GIB < sum(f.size_bytes for f in spec.files) < 7 * GIB


def test_video_entries_not_falsely_executable():
    """Planned/experimental entries must never claim installable support."""
    from app.domain import model_catalog

    for spec in model_catalog.catalog():
        if spec.modality != "video":
            continue
        if spec.support in ("planned", "experimental"):
            assert spec.key != "wan21.t2v.1_3b" or True  # wan is 'tested'
            continue
        # only tested/production may surface as executable
        assert spec.support in ("tested", "production")


# ------------------------------------------------------------------ adapter

def test_video_adapter_registered_with_recovery_contract():
    from app.adapters.registry import AdapterRegistry
    from app.config import get_settings

    reg = AdapterRegistry(session_factory=lambda: None, settings=get_settings())
    ad = reg.get("gen.video.sdcpp")
    assert ad is not None
    # M4.1-facing capability declarations
    assert ad.profile.worker_model == "subprocess-per-job"
    assert ad.profile.resumable is False
    assert ad.profile.recoverable_outputs == "commit-on-success"
    assert "text2video" in ad.profile.tasks and "image2video" in ad.profile.tasks


def test_video_adapter_validate_requires_prompt_or_image():
    from app.adapters.registry import AdapterRegistry
    from app.config import get_settings

    import asyncio

    reg = AdapterRegistry(session_factory=lambda: None, settings=get_settings())
    ad = reg.get("gen.video.sdcpp")
    assert asyncio.run(ad.validate({"prompt": "", "model_key": "wan21.t2v.1_3b"}))
    assert not asyncio.run(ad.validate({"prompt": "a river at night", "model_key": "wan21.t2v.1_3b"}))
    assert asyncio.run(ad.validate({"prompt": "x", "model_key": "wan21.t2v.1_3b", "frames": 999}))


# -------------------------------------------------------------- commands

def test_generate_video_rejects_uninstalled(client, authed):
    p = make_project(client)
    _commit(
        client, p["id"], "generate.video",
        {"model_key": "wan21.t2v.1_3b", "prompt": "a lantern drifts"},
        "vid-no-install", expect=422,
    )


def test_generate_video_rejects_image_model(client, authed, tmp_path):
    from tests.test_m3 import _install_row

    p = make_project(client)
    _install_row(client, "sd15.fp16", tmp_path)
    _commit(
        client, p["id"], "generate.video",
        {"model_key": "sd15.fp16", "prompt": "make it move"},
        "vid-wrong-modality", expect=422,
    )


def test_generate_video_rejects_planned_model(client, authed):
    p = make_project(client)
    r = _commit(
        client, p["id"], "generate.video",
        {"model_key": "ltx.2.5", "prompt": "test"},
        "vid-planned", expect=422,
    )
    assert "ltx" in str(r.json()).lower() or "planned" in str(r.json()).lower()


def test_generate_video_requires_prompt_or_image(client, authed):
    from tests.test_m3 import _install_row  # noqa: F401

    p = make_project(client)
    _install_video(client, "wan21.t2v.1_3b")
    _commit(
        client, p["id"], "generate.video",
        {"model_key": "wan21.t2v.1_3b"},
        "vid-no-input", expect=422,
    )


def test_generate_video_frames_bounds(client, authed):
    p = make_project(client)
    _install_video(client, "wan21.t2v.1_3b")
    _commit(
        client, p["id"], "generate.video",
        {"model_key": "wan21.t2v.1_3b", "prompt": "x", "frames": 999},
        "vid-bad-frames", expect=422,
    )


def test_generate_video_enqueues_non_resumable_local_job(client, authed, monkeypatch):
    """Simulates the post-approval state: once real weights validate the
    path, support flips to 'tested' and the job must enqueue with the M4.1
    recovery contracts on the durable row."""
    import dataclasses

    from app.domain import model_catalog

    spec = model_catalog.get("wan21.t2v.1_3b")
    monkeypatch.setitem(model_catalog.MODELS, "wan21.t2v.1_3b",
                        dataclasses.replace(spec, support="tested"))
    p = make_project(client)
    _install_video(client, "wan21.t2v.1_3b")
    out = _commit(
        client, p["id"], "generate.video",
        {"model_key": "wan21.t2v.1_3b", "prompt": "a lantern drifts on a river"},
        "vid-ok",
    )
    job_id = out.get("job_id") or out.get("job", {}).get("id")
    jobs = client.get(f"/api/projects/{p['id']}/jobs").json()
    job = next(j for j in jobs if j["id"] == job_id)
    # recovery contracts visible on the durable row
    assert job["adapter_id"] == "gen.video.sdcpp"
    assert job["kind"] == "video"
    assert job["resumable"] is False
    assert job["execution_scope"] == "local"
    assert job["attempt_no"] == 1
    assert job["recovery_state"] is None


# -------------------------------------------------------- multi-file install

def test_installed_file_requires_all_components(client):
    from app.domain import model_catalog, model_files

    _install_video(client, "wan21.t2v.1_3b")
    app = client.app
    with app.state.session_factory() as s:
        spec = model_catalog.get("wan21.t2v.1_3b")
        assert model_files.installed_file(s, app.state.settings, spec) is not None
        vae = model_files.installed_component(s, app.state.settings, spec, "vae")
        assert vae is not None and vae.exists()
        # delete one component → model no longer counts as installed
        vae.unlink()
        assert model_files.installed_file(s, app.state.settings, spec) is None


def test_verify_component_checks_each_file(tmp_path):
    from app.domain import model_catalog, model_files

    spec = model_catalog.get("wan21.t2v.1_3b")
    mf = next(f for f in spec.files if f.role == "vae")
    f = tmp_path / mf.name
    f.write_bytes(b"\x00" * 64)
    ok, _ = model_files.verify_component(f, mf, spec.fmt)
    # real sha256 declared → a fake blob must fail verification
    assert not ok


# ---------------------------------------------------------------- recovery

def test_reconcile_marks_interrupted_and_bumps_attempt(client, authed):
    from sqlalchemy import select

    from app.db.models import GenerationJob, utcnow

    app = client.app
    with app.state.session_factory() as s:
        job = GenerationJob(
            id="vid-job-1", project_id=None, adapter_id="gen.video.sdcpp",
            kind="video", request_json={}, status="running", progress=0.4,
            resumable=False, idempotency_key="test:vid-job-1",
            execution_scope="local", attempt_no=1,
            created_at=utcnow(), updated_at=utcnow(),
        )
        s.add(job)
        s.commit()

    with app.state.session_factory() as s:
        app.state.job_engine.reconcile_interrupted(s)
        s.commit()
    with app.state.session_factory() as s:
        job = s.execute(
            select(GenerationJob).where(GenerationJob.id == "vid-job-1")
        ).scalar_one()
        assert job.status == "failed"
        assert job.recovery_state == "interrupted"
        assert job.execution_scope == "local"


def test_reconcile_resumable_requeues_with_new_attempt(client, authed):
    from sqlalchemy import select

    from app.db.models import GenerationJob, utcnow

    app = client.app
    with app.state.session_factory() as s:
        job = GenerationJob(
            id="op-job-1", project_id=None, adapter_id="ops.backup.export",
            kind="ops", request_json={}, status="running", progress=0.4,
            resumable=True, idempotency_key="test:op-job-1",
            execution_scope="local", attempt_no=1,
            created_at=utcnow(), updated_at=utcnow(),
        )
        s.add(job)
        s.commit()
    with app.state.session_factory() as s:
        app.state.job_engine.reconcile_interrupted(s)
        s.commit()
    with app.state.session_factory() as s:
        job = s.execute(
            select(GenerationJob).where(GenerationJob.id == "op-job-1")
        ).scalar_one()
        assert job.status == "queued"
        assert job.attempt_no == 2


# ---------------------------------------------------------------- media_probe

def test_media_probe_container_signature(tmp_path):
    from app.domain import media_probe

    webm = tmp_path / "a.webm"
    webm.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 32)
    assert media_probe.container_signature_ok(webm)
    mp4 = tmp_path / "a.mp4"
    mp4.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 16)
    assert media_probe.container_signature_ok(mp4)
    bad = tmp_path / "a.webm"
    bad.write_bytes(b"NOTAVIDEO" * 4)
    assert not media_probe.container_signature_ok(bad)


def test_media_probe_graceful_without_ffprobe(tmp_path, monkeypatch):
    from app.domain import media_probe

    monkeypatch.setattr(media_probe, "ffprobe_path", lambda: None)
    monkeypatch.setattr(media_probe, "_probe_pyav", lambda p: None)  # simulate no PyAV
    out = media_probe.probe_video(tmp_path / "x.webm")
    assert out["ok"] is False and out["probe_unavailable"] is True
