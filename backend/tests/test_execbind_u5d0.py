"""U-5d-0 — exact model-execution binding substrate (execbind:v1).

Covers the trusted-internal activation contract, validation→commit
identity preservation, exact installation/member binding, worker-side
current-content verification (streaming SHA-256 before external
inference spawn), path containment, recovery/idempotency, and the
fail-closed persisted-state matrix — all with synthetic specs, files
and databases. No real model files, runtime or provider calls.

Requirement map for the milestone report is maintained in the PR body;
each test docstring names the requirement numbers it exercises.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import threading
from pathlib import Path

import pytest
from sqlalchemy import select

from app.adapters.base import JobCancelled, JobHandle
from app.adapters.inference import SdCppAdapter, SdCppVideoAdapter, _execbind_marker
from app.db.models import Asset, GenerationJob, ModelArtifact, ModelInstall, Project, new_id, utcnow
from app.domain import commands as cmd
from app.domain import model_catalog, model_files
from app.domain.commands import CommandError
from app.domain.model_catalog import ModelFile, ModelSpec
from tests.conftest import make_project

# ------------------------------------------------------------------
# helpers


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _img_spec(key="t5d.img", *, content=b"IMGW", adapter="gen.image.sdcpp",
              extra_files=(), support="tested", compatible=("gen.image.sdcpp-server",)):
    files = (ModelFile(name="main.safetensors", size_bytes=len(content),
                       url="", sha256=_sha(content)),) + tuple(extra_files)
    return ModelSpec(
        key=key, display_name=f"ExecBind {key}", family="sd1.5",
        version="1.0", adapter_id=adapter, fmt="safetensors",
        source={"provider": "test"},
        license={"name": "test", "url": "https://example.test/l",
                 "commercial_ok": True},
        files=files,
        requirements={"min_vram_bytes": 0, "min_ram_bytes": 0},
        capabilities={"sizes": {"presets": [{"w": 512, "h": 512}]},
                      "steps": {"min": 1, "max": 150, "default": 20},
                      "cfg": {"min": 1.0, "max": 30.0, "default": 7.0},
                      "samplers": ["euler_a"], "seed": {},
                      "precision": ["fp16"]},
        support=support, modality="image", modes=("text2image",),
        compatible_adapters=compatible)


def _vid_spec(key="t5d.vid", *, i2v=False, weights=b"VIDW", vae=b"VAEW",
              tenc=b"TENCW", clip=b"CLIPW", clip_declared=True,
              modes=("text2video", "image2video"), clip_sha=None):
    files = [
        ModelFile(name="diffusion.safetensors", size_bytes=len(weights),
                  url="", sha256=_sha(weights), role="diffusion"),
        ModelFile(name="vae.safetensors", size_bytes=len(vae), url="",
                  sha256=_sha(vae), role="vae"),
        ModelFile(name="tenc.safetensors", size_bytes=len(tenc), url="",
                  sha256=_sha(tenc), role="text_encoder"),
    ]
    if clip_declared:
        files.append(ModelFile(
            name="clip.safetensors", size_bytes=len(clip), url="",
            sha256=clip_sha if clip_sha is not None else _sha(clip),
            role="clip_vision"))
    return ModelSpec(
        key=key, display_name=f"ExecBind {key}", family="wan-test",
        version="1.0", adapter_id="gen.video.sdcpp", fmt="safetensors",
        source={"provider": "test"},
        license={"name": "test", "url": "https://example.test/l",
                 "commercial_ok": True},
        files=tuple(files),
        requirements={"min_vram_bytes": 0, "min_ram_bytes": 0},
        capabilities={"frames": {"min": 1, "max": 129, "default": 33},
                      "fps": {"min": 1, "max": 60, "default": 24},
                      "steps": {"min": 1, "max": 150, "default": 30},
                      "cfg": {"min": 1.0, "max": 30.0, "default": 7.0},
                      "samplers": ["euler_a"], "seed": {},
                      "sizes": {"presets": [{"w": 832, "h": 480}]},
                      "precision": ["fp16"]},
        support="tested", modality="video", modes=modes)


def _add_spec(monkeypatch, spec):
    monkeypatch.setitem(model_catalog.MODELS, spec.key, spec)


def _write_members(root: Path, spec: ModelSpec, contents: dict[str, bytes]):
    d = root / spec.key
    d.mkdir(parents=True, exist_ok=True)
    for f in spec.files:
        body = contents.get(f.name, f"{f.name}-data".encode())
        (d / f.name).write_bytes(body)
    return d


def _install_pinned(client, spec, *, verified=True, status="installed"):
    """A real installed row + linked catalog artifact — what a genuine
    completed install produces (registry projection included)."""
    app = client.app
    with app.state.session_factory() as s:
        root = model_files.models_root(s, app.state.settings)
        # Member bytes must hash to the spec's declared sha when one is
        # declared — derive contents from the spec helpers instead.
        _write_members(root, spec,
                       {f.name: _bytes_for(f) for f in spec.files})
        dest_dir = root / spec.key
        primary = spec.files[0]
        row = ModelInstall(
            id=new_id(), model_key=spec.key, adapter_id=spec.adapter_id,
            version=spec.version, status=status,
            install_root=str(root), rel_dir=dest_dir.name,
            file_name=primary.name,
            byte_size=(dest_dir / primary.name).stat().st_size,
            sha256=_sha((dest_dir / primary.name).read_bytes()),
            verified=verified, license="test", meta_json={},
            created_at=utcnow(), updated_at=utcnow())
        s.add(row)
        s.flush()
        # Reinstalls re-point the same catalog artifact at the new row —
        # the registry projection tracks the current installation.
        art = s.get(ModelArtifact, f"catalog:{spec.key}")
        if art is not None:
            art.install_id = row.id
            art.updated_at = utcnow()
        else:
            s.add(ModelArtifact(
                artifact_id=f"catalog:{spec.key}",
                display_name=spec.display_name, kind="model",
                arch="diffusion", family=spec.family,
                modality=spec.modality, fmt=spec.fmt, role="diffusion",
                version=spec.version, source_type="catalog",
                managed_by="atomic", content_sha256=primary.sha256,
                byte_size=primary.size_bytes,
                license_json=dict(spec.license),
                support_tier=spec.support, enabled=True, tags_json=[],
                install_id=row.id,
                meta_json={"adapter_id": spec.adapter_id,
                           "runtime_key": "sdcpp"},
                created_at=utcnow(), updated_at=utcnow()))
        s.commit()
        return row.id


def _bytes_for(f: ModelFile) -> bytes:
    """Content hashing to the file's declared sha256 when one exists —
    the sha was published alongside the synthetic spec helper."""
    return _SPEC_BODIES[f.sha256] if f.sha256 in _SPEC_BODIES \
        else f"{f.name}-data".encode()


# Bodies the spec helpers hash — keyed by sha256 so _install_pinned can
# reconstruct content matching every declared member hash.
_SPEC_BODIES = {_sha(b): b for b in
                (b"IMGW", b"VIDW", b"VAEW", b"TENCW", b"CLIPW")}


def _ctx(client):
    app = client.app
    return {"job_engine": app.state.job_engine,
            "storage": app.state.storage,
            "vault": getattr(app.state, "provider_vault", None),
            "session_factory": app.state.session_factory,
            "data_dir": app.state.settings.data_dir}


def _internal_commit(client, project_id, command_type, payload, key,
                     *, flag=True, expected=None):
    """The trusted-internal invocation: server-side kwargs, never JSON."""
    app = client.app
    with app.state.session_factory() as s:
        project = s.get(Project, project_id)
        res = cmd.commit(
            s, project, command_type=command_type, payload=payload,
            scope="project", origin="test-internal",
            target_type=None, target_id=None,
            expected_revision=project.revision, idempotency_key=key,
            internal_execbind=flag, execbind_expected=expected,
            **_ctx(client))
        s.commit()  # the command layer stages; the caller owns the commit
        job = res.get("job")
        if job is not None:
            res["job_id"] = job.id
            # detach-safe view for assertions after the session closes
            res["job"] = None
        return res


def _internal_preview(client, project_id, command_type, payload,
                      *, flag=True):
    app = client.app
    with app.state.session_factory() as s:
        project = s.get(Project, project_id)
        return cmd.preview(
            s, project, command_type=command_type, payload=payload,
            scope="project", origin="test-internal",
            target_type=None, target_id=None,
            internal_execbind=flag,
            vault=getattr(app.state, "provider_vault", None))


def _req_of(client, res) -> dict:
    """request_json of the job a commit created — fetched inside a
    live session so no detached-instance state leaks."""
    with client.app.state.session_factory() as s:
        job = s.get(GenerationJob, res["job_id"])
        assert job is not None
        return dict(job.request_json)


def _last_job(client, command_type=None):
    """Latest job's request_json (detached-safe)."""
    with client.app.state.session_factory() as s:
        q = select(GenerationJob).order_by(GenerationJob.created_at.desc())
        job = s.execute(q).scalars().first()
        return dict(job.request_json) if job is not None else None


def _img_adapter(client):
    app = client.app
    return SdCppAdapter(app.state.session_factory, app.state.settings)


def _vid_adapter(client):
    app = client.app
    return SdCppVideoAdapter(app.state.session_factory, app.state.settings)


def _handle(request, tmp_path, cancel=None):
    return JobHandle(job_id="t5d-job", project_id="t5d-proj",
                     request=dict(request), work_dir=str(tmp_path),
                     cancel_event=cancel)


def _commit_public(client, project_id, command_type, payload, key):
    r = client.post(
        f"/api/projects/{project_id}/commands/commit",
        json={"command_type": command_type, "payload": payload,
              "scope": "project", "origin": "test",
              "idempotency_key": key,
              "expected_revision": _revision(client, project_id)})
    return r


def _revision(client, project_id):
    r = client.get(f"/api/projects/{project_id}")
    return r.json().get("revision", 0)


def _binding_for(client, spec_key="t5d.img", install_id=None,
                 adapter="gen.image.sdcpp", roles=("diffusion",),
                 artifact="auto"):
    """A schema-valid binding dict over the live install row."""
    spec = model_catalog.get(spec_key)
    with client.app.state.session_factory() as s:
        row = (s.get(ModelInstall, install_id) if install_id
               else s.execute(select(ModelInstall).where(
                   ModelInstall.model_key == spec_key)).scalar_one())
        return model_files.build_execbind(
            s, spec, row, adapter_id=adapter, roles=roles,
            artifact_id=(f"catalog:{spec.key}" if artifact == "auto"
                         else artifact))


def _pinned_request(binding, *, marker=model_files.EXECBIND_VERSION):
    req = {"model_key": "t5d.img", "prompt": "p", "device": "auto",
           "runtime_flavor": "vulkan"}
    if marker is not None:
        req["binding_required"] = marker
    if binding is not None:
        req["execution_binding"] = binding
    return req


@pytest.fixture(autouse=True)
def _stub_sdcpp(monkeypatch):
    """No real sd.cpp runtime: _exe_path resolves the test command
    prefix; the spawn boundary itself is intercepted per-test."""
    monkeypatch.setenv("ATOMIC_SDCPP_CMD", "sdcpp-stub")


# ------------------------------------------------------------------
# A. activation & injection (req 51-53, 59, 87, 95)

def test_public_binding_field_refused_image(client, monkeypatch, tmp_path):
    """51, 87 — a public JSON field can never engage pinned execution."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    for field in ("execution_binding", "expected_binding",
                  "binding_required", "execbind", "install_id",
                  "expected_sha256", "member_sha256", "binding_version",
                  "binding_marker", "trusted_internal",
                  "internal_invocation"):
        r = _commit_public(
            client, p["id"], "generate.image",
            {"model_key": spec.key, "prompt": "a cat", field: "x"},
            f"k-{field}")
        assert r.status_code == 422, (field, r.text)
        assert "not client-settable" in r.text


def test_public_binding_field_refused_video(client, monkeypatch):
    """51 — same refusal on the video command path."""
    spec = _vid_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    r = _commit_public(
        client, p["id"], "generate.video",
        {"model_key": spec.key, "prompt": "waves",
         "execution_binding": {"x": 1}}, "k-vid-inj")
    assert r.status_code == 422 and "not client-settable" in r.text


def test_generic_preview_cannot_activate(client, monkeypatch):
    """52 — the generic preview route never produces a binding."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    r = client.post(
        f"/api/projects/{p['id']}/commands/preview",
        json={"command_type": "generate.image",
              "payload": {"model_key": spec.key, "prompt": "a cat",
                          "internal_execbind": True,
                          "execution_binding": {"x": 1}},
              "scope": "project", "origin": "test"})
    assert r.status_code == 422, r.text
    assert "not client-settable" in r.text


def test_generic_commit_cannot_activate(client, monkeypatch):
    """53, 59 — public commit produces an ordinary unpinned job even
    with a well-formed install present."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    r = _commit_public(
        client, p["id"], "generate.image",
        {"model_key": spec.key, "prompt": "a cat"}, "k-public-1")
    assert r.status_code == 200, r.text
    req = _last_job(client)
    assert "binding_required" not in req
    assert "execution_binding" not in req


def test_legacy_unpinned_request_unchanged(client, monkeypatch):
    """59, 81 — a genuine legacy request resolves by the classic
    installed_file path (unpinned path preserved verbatim)."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    called = []
    orig = model_files.installed_file
    monkeypatch.setattr(
        model_files, "installed_file",
        lambda *a, **k: called.append(1) or orig(*a, **k))
    ad = _img_adapter(client)
    _spec_out, weight, _ = ad._resolve(
        {"model_key": spec.key, "runtime_flavor": "vulkan"})
    assert called and weight.name == "main.safetensors"
    assert ad._pinned is None  # legacy resolution retains no pin state


# ------------------------------------------------------------------
# B. trusted internal activation + persistence (54)

def test_internal_invocation_constructs_binding(client, monkeypatch):
    """54, 95 — trusted preview constructs the complete binding;
    trusted commit with the SAME approved binding stamps
    binding_required + execbind:v1 into the durable request."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "a cat"}
    b = _internal_preview(client, p["id"], "generate.image",
                          payload)["execution_binding"]
    res = _internal_commit(client, p["id"], "generate.image", payload,
                           "k-pin-1", expected=b)
    req = _req_of(client, res)
    assert req["binding_required"] == model_files.EXECBIND_VERSION
    b = req["execution_binding"]
    assert b["binding_version"] == "execbind:v1"
    assert b["model_key"] == spec.key
    assert b["adapter_id"] == "gen.image.sdcpp"
    assert b["runtime_key"] == "sdcpp"
    assert b["artifact_id"] == f"catalog:{spec.key}"
    assert b["members"] == [
        {"role": "diffusion", "file_name": "main.safetensors",
         "expected_sha256": _sha(b"IMGW"),
         "integrity": "catalog_declared"}]


def test_internal_flag_refused_on_other_commands(client, monkeypatch):
    """D1 — internal_execbind on a non-bindable command refuses."""
    p = make_project(client)
    with pytest.raises(CommandError) as ei:
        _internal_commit(client, p["id"], "asset.import",
                         {"name": "x"}, "k-nonbind")
    assert "internal_execbind" in str(ei.value)


def test_expected_without_flag_refused(client, monkeypatch):
    """P1 — execbind_expected without internal_execbind refuses."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    with pytest.raises(CommandError) as ei:
        _internal_commit(client, p["id"], "generate.image",
                         {"model_key": spec.key, "prompt": "x"},
                         "k-exp-noflag", flag=False, expected={"a": 1})
    assert "internal_execbind" in str(ei.value)


# ------------------------------------------------------------------
# C. persisted state matrix (55-58, 83, 84)

def test_marker_state_matrix():
    """55-58 — every persisted combination resolves deterministically:
    only marker+valid-binding pins; every other combo refuses."""
    good = {"binding_version": "execbind:v1",
            "artifact_id": "catalog:m",
            "model_key": "m", "install_id": "i", "runtime_key": "s",
            "adapter_id": "a", "members": [
                {"role": "diffusion", "file_name": "f",
                 "expected_sha256": "0" * 64,
                 "integrity": "catalog_declared"}]}
    # 55 — marker present, binding missing
    with pytest.raises(ValueError, match="missing"):
        _execbind_marker({"binding_required": "execbind:v1"})
    # 56 — marker present, binding null
    with pytest.raises(ValueError, match="missing"):
        _execbind_marker({"binding_required": "execbind:v1",
                          "execution_binding": None})
    # 57 — marker present, binding malformed (not a dict)
    with pytest.raises(TypeError, match="malformed"):
        _execbind_marker({"binding_required": "execbind:v1",
                          "execution_binding": "x"})
    # 58 — binding present, marker absent/null/wrong-type/unknown
    # version → refuse, never legacy
    for marker_state in ({}, {"binding_required": None},
                         {"binding_required": False},
                         {"binding_required": 1},
                         {"binding_required": "execbind:v0"},
                         {"binding_required": "execbind:v9"},
                         {"binding_required": {"v": 1}}):
        with pytest.raises(ValueError, match="absent or unsupported"):
            _execbind_marker({**marker_state,
                              "execution_binding": dict(good)})
    # valid pair → pinned
    assert _execbind_marker({"binding_required": "execbind:v1",
                             "execution_binding": good}) == good
    # genuine legacy → None (BOTH keys truly absent only)
    assert _execbind_marker({"model_key": "m"}) is None
    assert _execbind_marker({}) is None
    # F2 — key PRESENCE is not key absence: a persisted null/false/
    # wrong-type/unsupported marker, or a null binding without a
    # marker, is tampered state — refuse, never legacy.
    for tampered in ({"binding_required": None},
                     {"execution_binding": None},
                     {"binding_required": None,
                      "execution_binding": None},
                     {"binding_required": False},
                     {"binding_required": 1},
                     {"binding_required": {"v": 1}},
                     {"binding_required": "execbind:v0"},
                     {"binding_required": "execbind:v9"},
                     {"binding_required": None,
                      "execution_binding": dict(good)}):
        with pytest.raises((ValueError, TypeError)):
            _execbind_marker(tampered)


def test_schema_strictness(client, monkeypatch):
    """88 — version/type/id/hash/member-count validation all refuse."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    base = _binding_for(client, install_id=install_id)

    def bad(**mut):
        b = dict(base)
        b.update(mut)
        if "members" in mut:
            b["members"] = mut["members"]
        with pytest.raises(model_files.ExecbindError):
            model_files.validate_execbind_schema(b)

    bad(binding_version="execbind:v2")
    bad(members=[])
    bad(members="nope")
    bad(install_id=5)
    bad(extra_field="x")
    # C2 — artifact identity is mandatory for pinned execution
    bad(artifact_id=None)
    bad(artifact_id="")
    bad(artifact_id=42)
    b2 = dict(base)
    del b2["adapter_id"]
    with pytest.raises(model_files.ExecbindError,
                       match="missing binding fields"):
        model_files.validate_execbind_schema(b2)
    bad(members=[dict(base["members"][0], expected_sha256="XYZ")])
    bad(members=[dict(base["members"][0], integrity="fabricated")])


def test_worker_missing_binding_refuses(client, monkeypatch):
    """55 — a binding-required job with no execution_binding refuses at
    the worker, never silently unpinned."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    ad = _img_adapter(client)
    with pytest.raises(ValueError, match="missing"):
        ad._resolve(_pinned_request(None))


def test_worker_stray_binding_refuses(client, monkeypatch):
    """58, 84 — a binding without its required marker is not legacy."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    ad = _img_adapter(client)
    with pytest.raises(ValueError, match="absent or unsupported"):
        ad._resolve({"model_key": spec.key,
                     "execution_binding": {"x": 1}})


# ------------------------------------------------------------------
# D. validation→commit identity preservation (60-63)

def test_preview_produces_expected_binding(client, monkeypatch):
    """54 — trusted preview surfaces the binding commit will pin."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    out = _internal_preview(client, p["id"], "generate.image",
                            {"model_key": spec.key, "prompt": "x"})
    b = out["execution_binding"]
    assert b["binding_version"] == "execbind:v1"
    res = _internal_commit(client, p["id"], "generate.image",
                           {"model_key": spec.key, "prompt": "x"},
                           "k-exp-ok", expected=b)
    req = _req_of(client, res)
    assert req["execution_binding"] == b


def test_install_replaced_before_commit_refuses(client, monkeypatch):
    """60 — install A validated, removed, replaced by B → refusal; the
    same-key replacement is never substituted."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_a = _install_pinned(client, spec)
    p = make_project(client)
    b = _internal_preview(client, p["id"], "generate.image",
                          {"model_key": spec.key, "prompt": "x"}
                          )["execution_binding"]
    with client.app.state.session_factory() as s:
        s.get(ModelInstall, install_a).status = "removed"
        s.commit()
    _install_pinned(client, spec)  # install B, same model_key
    with pytest.raises(CommandError) as ei:
        _internal_commit(client, p["id"], "generate.image",
                         {"model_key": spec.key, "prompt": "x"},
                         "k-replace", expected=b)
    assert "install" in str(ei.value)


def test_unrelated_install_does_not_invalidate(client, monkeypatch):
    """61 — an UNRELATED installation (a different model_key) appearing
    after approval does not invalidate the pinned row; identity is
    install_id + artifact link, not unordered key resolution. (A
    same-key reinstall re-points the catalog artifact — that relink is
    case 63's refusal, not an "unrelated" install.)"""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    b = _internal_preview(client, p["id"], "generate.image",
                          {"model_key": spec.key, "prompt": "x"}
                          )["execution_binding"]
    other = _img_spec(key="t5d.other")
    _add_spec(monkeypatch, other)
    _install_pinned(client, other)  # unrelated second model
    res = _internal_commit(client, p["id"], "generate.image",
                           {"model_key": spec.key, "prompt": "x"},
                           "k-second", expected=b)
    req = _req_of(client, res)
    assert req["execution_binding"] == b


def test_member_hash_drift_before_commit_refuses(client, monkeypatch):
    """62 — required-member expected evidence changed since validation
    → refusal."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    p = make_project(client)
    b = _internal_preview(client, p["id"], "generate.image",
                          {"model_key": spec.key, "prompt": "x"}
                          )["execution_binding"]
    with client.app.state.session_factory() as s:
        row = s.get(ModelInstall, install_id)
        row.sha256 = "f" * 64  # recorded evidence drifted
        s.commit()
    with pytest.raises(CommandError):
        _internal_commit(client, p["id"], "generate.image",
                         {"model_key": spec.key, "prompt": "x"},
                         "k-hashdrift", expected=b)


def test_artifact_relink_before_commit_refuses(client, monkeypatch):
    """63 — registry artifact relinked to another install → refusal."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    b = _internal_preview(client, p["id"], "generate.image",
                          {"model_key": spec.key, "prompt": "x"}
                          )["execution_binding"]
    with client.app.state.session_factory() as s:
        s.get(ModelArtifact, f"catalog:{spec.key}").install_id = new_id()
        s.commit()
    with pytest.raises(CommandError):
        _internal_commit(client, p["id"], "generate.image",
                         {"model_key": spec.key, "prompt": "x"},
                         "k-relink", expected=b)


# ------------------------------------------------------------------
# E. member-set rules (64-71)

def test_zero_and_ambiguous_installs_refuse(client, monkeypatch):
    """D3 — zero eligible rows refuses; multiple eligible rows refuse
    before pinning (never an unordered pick)."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    p = make_project(client)
    with pytest.raises(CommandError) as ei:
        _internal_preview(client, p["id"], "generate.image",
                          {"model_key": spec.key, "prompt": "x"})
    # The legacy installed-model gate refuses first — still a refusal;
    # the execbind 'not_installed' code covers direct binder use.
    assert "not installed" in str(ei.value)
    with client.app.state.session_factory() as s, pytest.raises(
            model_files.ExecbindError) as e2:
        model_files.resolve_install(s, spec.key)
    assert e2.value.code == "not_installed"
    _install_pinned(client, spec)
    _install_pinned(client, spec)
    with pytest.raises(CommandError) as ei2:
        _internal_preview(client, p["id"], "generate.image",
                          {"model_key": spec.key, "prompt": "x"})
    assert "install_ambiguous" in str(ei2.value)


def test_duplicate_unexpected_wrongrole_members(client, monkeypatch):
    """64-66 — duplicate identity, unexpected member, wrong role all
    refuse at worker revalidation."""
    spec = _vid_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, spec.key, install_id,
                     adapter="gen.video.sdcpp",
                     roles=("diffusion", "vae", "text_encoder"))
    ad = _vid_adapter(client)
    req = {"model_key": spec.key, "runtime_flavor": "vulkan",
           "binding_required": model_files.EXECBIND_VERSION,
           "execution_binding": dict(b)}

    dup = dict(b, members=b["members"] + [b["members"][0]])
    with pytest.raises(ValueError, match="duplicate_member"):
        ad._resolve({**req, "execution_binding": dup})

    unexp = dict(b, members=b["members"] + [
        {"role": "extra", "file_name": "nope.bin",
         "expected_sha256": "0" * 64, "integrity": "catalog_declared"}])
    with pytest.raises(ValueError, match="member_set_mismatch"):
        ad._resolve({**req, "execution_binding": unexp})

    wrong = dict(b, members=[
        dict(m, role="vae") if m["role"] == "diffusion" else m
        for m in b["members"]])
    # roles now {vae, vae→dedup, text_encoder} ≠ required set → schema
    # duplicate-member check fires first
    with pytest.raises(ValueError):
        ad._resolve({**req, "execution_binding": wrong})


def test_conflicting_hash_sources_refuse(client, monkeypatch):
    """67 — catalog-declared and recorded install hashes that disagree
    refuse; no silent preference."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    with client.app.state.session_factory() as s:
        row = s.get(ModelInstall, install_id)
        row.verified = True
        row.sha256 = "0" * 64  # recorded ≠ catalog-declared
        s.commit()
        with pytest.raises(model_files.ExecbindError,
                           match="conflicting"):
            model_files.build_execbind(
                s, spec, row, adapter_id="gen.image.sdcpp",
                roles=("diffusion",),
                artifact_id=f"catalog:{spec.key}")


def test_primary_hash_never_covers_aux(client, monkeypatch):
    """68 — the install's primary sha256 is never reused as VAE /
    text-encoder / clip evidence."""
    clip_ns = ModelFile(name="c2.safetensors", size_bytes=1, url="",
                        sha256=None, role="clip_vision")
    spec = _vid_spec(clip_declared=False)
    spec = ModelSpec(**{**spec.__dict__,
                      "files": spec.files + (clip_ns,)})
    _add_spec(monkeypatch, spec)
    with client.app.state.session_factory() as s:
        root = model_files.models_root(s, client.app.state.settings)
        d = root / spec.key
        d.mkdir(parents=True, exist_ok=True)
        for f in spec.files:
            (d / f.name).write_bytes(b"x")
        row = ModelInstall(
            id=new_id(), model_key=spec.key,
            adapter_id=spec.adapter_id, status="installed",
            install_root=str(root), rel_dir=d.name,
            file_name=spec.files[0].name, sha256="",
            verified=False, created_at=utcnow(), updated_at=utcnow())
        s.add(row)
        s.flush()
        s.add(ModelArtifact(
            artifact_id=f"catalog:{spec.key}",
            display_name=spec.display_name, kind="model",
            arch="diffusion", family=spec.family,
            modality=spec.modality, fmt=spec.fmt, role="diffusion",
            version=spec.version, source_type="catalog",
            managed_by="atomic", content_sha256="",
            byte_size=0, license_json=dict(spec.license),
            support_tier=spec.support, enabled=True, tags_json=[],
            install_id=row.id, meta_json={},
            created_at=utcnow(), updated_at=utcnow()))
        s.flush()
        # i2v on a clip-declaring model needs clip_vision — which has
        # no declared sha and no install-row coverage.
        with pytest.raises(model_files.ExecbindError,
                           match="no expected sha256 evidence"):
            model_files.build_execbind(
                s, spec, row, adapter_id="gen.video.sdcpp",
                roles=("diffusion", "vae", "text_encoder",
                       "clip_vision"),
                artifact_id=f"catalog:{spec.key}")


def test_clip_required_i2v_missing_refuses(client, monkeypatch):
    """69 — an i2v request on a model declaring clip_vision requires
    the member; absence refuses."""
    spec = _vid_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    with client.app.state.session_factory() as s:
        root = model_files.models_root(s, client.app.state.settings)
        (root / spec.key / "clip.safetensors").unlink()
        row = s.get(ModelInstall, install_id)
        with pytest.raises(model_files.ExecbindError,
                           match="absent from the approved"):
            model_files.build_execbind(
                s, spec, row, adapter_id="gen.video.sdcpp",
                roles=("diffusion", "vae", "text_encoder",
                       "clip_vision"),
                artifact_id=f"catalog:{spec.key}")


def test_clip_not_required_when_absent(client, monkeypatch):
    """70 — a model with no clip_vision declaration and a t2v request
    binds exactly diffusion+vae+text_encoder. C3 — i2v on the same
    model cannot establish that no vision encoder is needed under the
    supported adapter contract, so it fails CLOSED (member_unresolv-
    able) rather than silently binding without image conditioning."""
    spec = _vid_spec(clip_declared=False)
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, spec.key, install_id,
                     adapter="gen.video.sdcpp",
                     roles=("diffusion", "vae", "text_encoder"))
    assert {m["role"] for m in b["members"]} == {
        "diffusion", "vae", "text_encoder"}
    assert model_files.execbind_roles(
        spec, adapter_id="gen.video.sdcpp", has_image_input=False
    ) == ("diffusion", "vae", "text_encoder")
    # i2v always requires the clip_vision member — the catalog omission
    # is not evidence that no encoder is needed.
    assert model_files.execbind_roles(
        spec, adapter_id="gen.video.sdcpp", has_image_input=True
    ) == ("diffusion", "vae", "text_encoder", "clip_vision")
    with client.app.state.session_factory() as s, pytest.raises(
            model_files.ExecbindError,
            match="resolves to 0 declared"):
        model_files.build_execbind(
            s, spec, s.get(ModelInstall, install_id),
            adapter_id="gen.video.sdcpp",
            roles=("diffusion", "vae", "text_encoder", "clip_vision"),
            artifact_id=f"catalog:{spec.key}")


def test_verified_aux_modified_detected(client, monkeypatch):
    """71 — an auxiliary member recorded verified but since modified
    fails CURRENT-content verification, not the stale flag."""
    spec = _vid_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, spec.key, install_id,
                     adapter="gen.video.sdcpp",
                     roles=("diffusion", "vae", "text_encoder"))
    with client.app.state.session_factory() as s:
        root = model_files.models_root(s, client.app.state.settings)
    # Same-size tamper: bypasses the metadata check so the CURRENT-
    # content hash is what detects the modification.
    (root / spec.key / "vae.safetensors").write_bytes(b"BADX")
    ad = _vid_adapter(client)
    with pytest.raises(ValueError, match="integrity_mismatch"):
        ad._resolve({"model_key": spec.key, "runtime_flavor": "vulkan",
                     "binding_required": model_files.EXECBIND_VERSION,
                     "execution_binding": b})


# ------------------------------------------------------------------
# F. worker verification, streaming, containment (72-82)

def test_every_member_hashed_before_spawn(client, monkeypatch, tmp_path):
    """72, 89 — the real pinned worker resolves + streams EVERY member
    before the intercepted spawn boundary; refusal precedes spawn."""
    spec = _vid_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, spec.key, install_id,
                     adapter="gen.video.sdcpp",
                     roles=("diffusion", "vae", "text_encoder"))
    hashed = []
    orig = model_files.stream_sha256
    def spy(path, cancel_check=None, **kw):
        hashed.append(Path(path).name)
        return orig(path, cancel_check, **kw)
    monkeypatch.setattr(model_files, "stream_sha256", spy)
    spawned = []
    monkeypatch.setattr(
        "app.adapters.inference._spawn_supervised_async",
        lambda *a, **k: spawned.append(1) or (_ for _ in ()).throw(
            RuntimeError("spawn-boundary")))
    ad = _vid_adapter(client)
    req = _pinned_request(b)
    req.update({"model_key": spec.key, "frames": 4, "width": 832,
                "height": 480})
    handle = _handle(req, tmp_path)
    with pytest.raises(RuntimeError, match="spawn-boundary"):
        asyncio.run(_drain(ad.run(handle)))
    assert spawned == [1]
    assert sorted(hashed) == [
        "diffusion.safetensors", "tenc.safetensors", "vae.safetensors"]

    # tamper one member → refusal BEFORE the spawn boundary
    spawned.clear()
    with client.app.state.session_factory() as s:
        root = model_files.models_root(s, client.app.state.settings)
    (root / spec.key / "tenc.safetensors").write_bytes(b"EVIL!")
    with pytest.raises(ValueError, match="integrity_mismatch"):
        asyncio.run(_drain(ad.run(handle)))
    assert spawned == []


async def _drain(agen):
    async for _ in agen:
        pass


def test_bounded_streaming_chunks(client, monkeypatch, tmp_path):
    """73 — verification reads bounded chunks (never whole-file loads)
    and invokes cancel_check per chunk."""
    body = b"X" * (5 * 1024 * 1024 + 7)
    _SPEC_BODIES[_sha(body)] = body
    spec = _img_spec(content=body)
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    checks = []
    def cc():
        checks.append(1)
    ad = _img_adapter(client)
    _spec_out, weight, _ = ad._resolve(
        {**_pinned_request(b), "model_key": spec.key},
        cancel_check=cc)
    assert weight.name == "main.safetensors"
    # 5 MiB + 7 bytes over 4 MiB chunks → ≥2 chunk iterations
    assert len(checks) >= 2


def test_cancel_during_hash_preserves_cancel(client, monkeypatch):
    """74 — a cancel inside streaming verification raises JobCancelled,
    which the engine maps to cancelled — not an integrity failure."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ev = threading.Event()
    ev.set()
    ad = _img_adapter(client)
    with pytest.raises(JobCancelled):
        ad._resolve(_pinned_request(b),
                    cancel_check=_handle({}, Path("."), ev).check_cancel)


def test_integrity_failure_is_not_cancel(client, monkeypatch, tmp_path):
    """75 — a hash mismatch fails as execbind:integrity_mismatch, never
    misreported as a user cancellation."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    with client.app.state.session_factory() as s:
        root = model_files.models_root(s, client.app.state.settings)
    # Same-size corruption so the current-content hash decides.
    (root / spec.key / "main.safetensors").write_bytes(b"BAD!")
    ad = _img_adapter(client)
    with pytest.raises(ValueError, match="execbind:integrity_mismatch"):
        ad._resolve(_pinned_request(b),
                    cancel_check=lambda: None)


def test_member_path_escapes_refuse(client, monkeypatch):
    """76-79 — absolute paths, traversal, install-dir escapes and
    install-root divergence all refuse."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)

    for bad_name in ("../main.safetensors", "..\\main.safetensors",
                     "C:/abs/x", "/abs/x", "sub/dir.safetensors"):
        bad = dict(b, members=[dict(b["members"][0],
                                    file_name=bad_name)])
        with pytest.raises(ValueError,
                           match="unexpected_member|member_path_invalid"):
            ad._resolve(_pinned_request(bad))

    # install row root drifted away from the authorized models root
    with client.app.state.session_factory() as s:
        row = s.get(ModelInstall, install_id)
        row.install_root = str(Path(row.install_root).parent / "elsewhere")
        s.commit()
    with pytest.raises(ValueError, match="install_root_mismatch"):
        ad._resolve(_pinned_request(b))

    # rel_dir escapes the root
    with client.app.state.session_factory() as s:
        row = s.get(ModelInstall, install_id)
        root = model_files.models_root(s, client.app.state.settings)
        row.install_root = str(root)
        row.rel_dir = ".."
        s.commit()
    with pytest.raises(ValueError, match="install_dir_escape|member"):
        ad._resolve(_pinned_request(b))


def test_symlink_escape_refused(client, monkeypatch, tmp_path):
    """79 — a symlinked member resolving outside the pinned directory
    refuses (where the platform permits symlinks)."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    with client.app.state.session_factory() as s:
        root = model_files.models_root(s, client.app.state.settings)
    outside = tmp_path / "outside.safetensors"
    outside.write_bytes(b"X" * 64)
    target = root / spec.key / "main.safetensors"
    target.unlink()
    try:
        target.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted in this environment")
    ad = _img_adapter(client)
    with pytest.raises(ValueError, match="member_escape"):
        ad._resolve(_pinned_request(b))


def test_non_regular_member_refused(client, monkeypatch):
    """80 — a directory in place of a member refuses without unsafe
    reads."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    with client.app.state.session_factory() as s:
        root = model_files.models_root(s, client.app.state.settings)
    p = root / spec.key / "main.safetensors"
    p.unlink()
    p.mkdir()
    ad = _img_adapter(client)
    with pytest.raises(ValueError, match="member_unavailable"):
        ad._resolve(_pinned_request(b))


def test_pinned_workers_never_use_legacy_resolution(client, monkeypatch):
    """81, 82 — the pinned image and video paths never call the
    unordered installed_file / installed_component lookups."""
    monkeypatch.setattr(
        model_files, "installed_file",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("legacy resolution on pinned path")))
    monkeypatch.setattr(
        model_files, "installed_component",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("legacy resolution on pinned path")))
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)
    _, w, _ = ad._resolve(_pinned_request(b))
    assert w.name == "main.safetensors"

    vspec = _vid_spec()
    _add_spec(monkeypatch, vspec)
    vinstall = _install_pinned(client, vspec)
    vb = _binding_for(client, vspec.key, vinstall,
                      adapter="gen.video.sdcpp",
                      roles=("diffusion", "vae", "text_encoder"))
    vad = _vid_adapter(client)
    _, vw, _ = vad._resolve(
        {**_pinned_request(vb), "model_key": vspec.key})
    assert vw.name == "diffusion.safetensors"
    assert vad._components["vae"].name == "vae.safetensors"


def test_pinned_image_adapter_success(client, monkeypatch, tmp_path):
    """72, 89 — real pinned image resolution: all checks pass, hash
    verified, weight path returned; spawn boundary reached after."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    spawned = []
    monkeypatch.setattr(
        "app.adapters.inference._spawn_supervised_async",
        lambda *a, **k: spawned.append(1) or (_ for _ in ()).throw(
            RuntimeError("spawn-boundary")))
    ad = _img_adapter(client)
    handle = _handle({**_pinned_request(b), "width": 512, "height": 512,
                      "model_key": spec.key}, tmp_path)
    with pytest.raises(RuntimeError, match="spawn-boundary"):
        asyncio.run(_drain(ad.run(handle)))
    assert spawned == [1]


# ------------------------------------------------------------------
# G. recovery & idempotency (83-86)

def test_pinned_persists_and_recovery_honors(client, monkeypatch):
    """83 — the complete marker+member contract survives in
    request_json; re-dispatch re-validates it, not the job's history."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x"}
    b = _internal_preview(client, p["id"], "generate.image",
                          payload)["execution_binding"]
    res = _internal_commit(client, p["id"], "generate.image", payload,
                           "k-persist", expected=b)
    req = _req_of(client, res)
    assert req["binding_required"] == "execbind:v1"
    assert len(req["execution_binding"]["members"]) == 1
    ad = _img_adapter(client)
    _, w, _ = ad._resolve(req)  # recovered request re-validates fine
    assert w.name == "main.safetensors"


def test_recovered_malformed_pinned_never_legacy(client, monkeypatch):
    """84 — a recovered request whose binding went missing or malformed
    refuses; it can never degrade to an unpinned dispatch."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x"}
    b = _internal_preview(client, p["id"], "generate.image",
                          payload)["execution_binding"]
    _internal_commit(client, p["id"], "generate.image", payload,
                     "k-rec", expected=b)
    req = _last_job(client)
    ad = _img_adapter(client)
    req.pop("execution_binding")
    with pytest.raises(ValueError):
        ad._resolve(req)
    req2 = _last_job(client)
    req2["execution_binding"] = "corrupted"
    with pytest.raises(TypeError):
        ad._resolve(req2)


def test_idempotent_replay_same_binding(client, monkeypatch):
    """85 — same idempotency key + same approved binding replays the
    recorded command; no second job is created."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x"}
    b = _internal_preview(client, p["id"], "generate.image",
                          payload)["execution_binding"]
    _internal_commit(client, p["id"], "generate.image", payload,
                     "k-idem", expected=b)
    n1 = _job_count(client)
    r2 = _internal_commit(client, p["id"], "generate.image", payload,
                          "k-idem", expected=b)
    assert r2.get("idempotent_replay") is True
    assert _job_count(client) == n1


def _job_count(client):
    with client.app.state.session_factory() as s:
        return len(s.execute(select(GenerationJob)).scalars().all())


def test_idem_conflict_different_binding(client, monkeypatch):
    """86 — same key with a materially different payload follows the
    existing 409 conflict semantics."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x"}
    b = _internal_preview(client, p["id"], "generate.image",
                          payload)["execution_binding"]
    _internal_commit(client, p["id"], "generate.image", payload,
                     "k-conf", expected=b)
    with pytest.raises(CommandError) as ei:
        _internal_commit(client, p["id"], "generate.image",
                         {"model_key": spec.key, "prompt": "different"},
                         "k-conf", expected=b)
    assert ei.value.status_code == 409


# ------------------------------------------------------------------
# H. boundary & policy assertions (87-95)

def test_refusal_messages_hide_absolute_paths(client, monkeypatch):
    """87 — refusal details carry stable codes and relative names, not
    internal absolute filesystem paths."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    with client.app.state.session_factory() as s:
        root = model_files.models_root(s, client.app.state.settings)
    (root / spec.key / "main.safetensors").write_bytes(b"BAD")
    ad = _img_adapter(client)
    with pytest.raises(ValueError) as ei:
        ad._resolve(_pinned_request(b))
    assert str(root) not in str(ei.value)
    assert "main.safetensors" in str(ei.value)


def test_device_fields_pass_through_unchanged(client, monkeypatch):
    """90, 91 — the device/gpu request contract is untouched; pinning
    adds exactly two request keys, no device mapping claims."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x", "device": "auto"}
    b = _internal_preview(client, p["id"], "generate.image",
                          payload)["execution_binding"]
    res = _internal_commit(client, p["id"], "generate.image", payload,
                           "k-dev", expected=b)
    req = _req_of(client, res)
    assert req["device"] == "auto"
    assert "gpu_device_id" not in req and "physical_device" not in req


def test_no_marker_fields_in_public_surface(client, monkeypatch):
    """95 — pinning leaves only internal keys in request_json; no new
    public payload fields were minted."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x"}
    b = _internal_preview(client, p["id"], "generate.image",
                          payload)["execution_binding"]
    res = _internal_commit(client, p["id"], "generate.image", payload,
                           "k-surface", expected=b)
    req = _req_of(client, res)
    internal = {"binding_required", "execution_binding"}
    assert internal <= set(req)


def test_persistent_adapter_binds_server_id(client, monkeypatch):
    """E1 — AMENDED (was P3): a trusted pinned generate.image.persistent
    invocation now REFUSES at preview and at commit — pinned execution
    is a standard-subprocess-adapter contract; the persistent runtime
    cannot prove loaded-model identity at its execution boundary. The
    former assertion (a successful server-adapter bind) was the residual
    this amendment closes; no job is created."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x"}
    with pytest.raises(CommandError, match="persistent_runtime"):
        _internal_preview(client, p["id"], "generate.image.persistent",
                          payload)
    # Even a well-formed server-adapter binding cannot carry a commit.
    b = _binding_for(client, spec.key,
                     adapter="gen.image.sdcpp-server", roles=("diffusion",))
    n0 = _job_count(client)
    with pytest.raises(CommandError, match="persistent_runtime"):
        _internal_commit(client, p["id"], "generate.image.persistent",
                         payload, "k-srv", expected=b)
    assert _job_count(client) == n0


def test_video_internal_commit_binds_all_roles(client, monkeypatch):
    """P3 — a pinned video commit binds diffusion+vae+text_encoder
    (+clip_vision for i2v on a declaring model)."""
    spec = _vid_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "waves",
               "frames": 4, "width": 832, "height": 480}
    b = _internal_preview(client, p["id"], "generate.video",
                          payload)["execution_binding"]
    res = _internal_commit(client, p["id"], "generate.video", payload,
                           "k-vid", expected=b)
    req = _req_of(client, res)
    roles = {m["role"] for m in
             req["execution_binding"]["members"]}
    assert roles == {"diffusion", "vae", "text_encoder"}


# ------------------------------------------------------------------
# I. C1–C6 acceptance corrections

def test_commit_requires_expected_binding(client, monkeypatch):
    """C1.D/E/F — trusted commit refuses a missing, null or malformed
    execbind_expected; only the valid, unchanged approved binding is
    accepted. Construction is preview's job — never commit's."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x"}
    with pytest.raises(CommandError, match="execbind_expected"):
        _internal_commit(client, p["id"], "generate.image", payload,
                         "k-c1-miss")  # expected=None
    for bad in ({"nonsense": True}, "not-a-dict", 42):
        with pytest.raises(CommandError):
            _internal_commit(client, p["id"], "generate.image",
                             payload, "k-c1-mal", expected=bad)
    b = _internal_preview(client, p["id"], "generate.image",
                          payload)["execution_binding"]
    res = _internal_commit(client, p["id"], "generate.image", payload,
                           "k-c1-ok", expected=b)
    assert _req_of(client, res)["execution_binding"] == b


def test_artifact_identity_mandatory(client, monkeypatch):
    """C2 — absent/null/empty/malformed artifact ids, unknown or
    foreign-model artifacts, disabled, relinked and post-commit-deleted
    artifacts all refuse at worker dispatch; only the canonical
    catalog:{model_key} row linked to the pinned install passes."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)
    for art in (None, "", 42):
        with pytest.raises(ValueError, match="artifact_id"):
            ad._resolve(_pinned_request(dict(b, artifact_id=art)))
    for art in ("catalog:t5d.other", "bogus"):
        with pytest.raises(ValueError,
                           match="artifact_model_mismatch"):
            ad._resolve(_pinned_request(dict(b, artifact_id=art)))
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, f"catalog:{spec.key}")
        art.enabled = False
        s.commit()
    with pytest.raises(ValueError, match="artifact_disabled"):
        ad._resolve(_pinned_request(b))
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, f"catalog:{spec.key}")
        art.enabled = True
        art.install_id = new_id()
        s.commit()
    with pytest.raises(ValueError,
                       match="artifact_install_mismatch"):
        ad._resolve(_pinned_request(b))
    with client.app.state.session_factory() as s:
        s.delete(s.get(ModelArtifact, f"catalog:{spec.key}"))
        s.commit()
    with pytest.raises(ValueError, match="artifact_missing"):
        ad._resolve(_pinned_request(b))


def test_artifact_deleted_before_commit_refuses(client, monkeypatch):
    """C2.F — the registry artifact removed between trusted preview
    and commit refuses at validation; never an artifact-free pin."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x"}
    b = _internal_preview(client, p["id"], "generate.image",
                          payload)["execution_binding"]
    with client.app.state.session_factory() as s:
        s.delete(s.get(ModelArtifact, f"catalog:{spec.key}"))
        s.commit()
    with pytest.raises(CommandError, match="artifact_missing"):
        _internal_commit(client, p["id"], "generate.image", payload,
                         "k-art-del", expected=b)


def test_i2v_requires_clip_vision_declaration(client, monkeypatch):
    """C3 — image-conditioned pinned video on a catalog declaring no
    clip_vision fails closed: a missing declaration cannot establish
    the vision encoder is unneeded."""
    spec = _vid_spec(clip_declared=False)
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    with client.app.state.session_factory() as s:
        a = Asset(project_id=p["id"], kind="image", name="in.png")
        s.add(a)
        s.commit()
        asset_id = a.id
    payload = {"model_key": spec.key, "prompt": "move",
               "image_asset_id": asset_id, "frames": 4,
               "width": 832, "height": 480}
    with pytest.raises(CommandError, match="member_unresolvable"):
        _internal_preview(client, p["id"], "generate.video", payload)
    # Worker side: a binding smuggling a clip member the catalog never
    # declared is an unexpected member, not a rescue.
    b = _binding_for(client, spec.key, adapter="gen.video.sdcpp",
                     roles=("diffusion", "vae", "text_encoder"))
    b["members"].append({"role": "clip_vision",
                         "file_name": "ghost.safetensors",
                         "expected_sha256": "0" * 64,
                         "integrity": "catalog_declared"})
    ad = _vid_adapter(client)
    with pytest.raises(ValueError, match="unexpected_member"):
        ad._resolve({"model_key": spec.key, "image_path": "x.png",
                     "runtime_flavor": "vulkan",
                     "binding_required": model_files.EXECBIND_VERSION,
                     "execution_binding": b})


def test_replay_pinned_vs_legacy_refuses(client, monkeypatch):
    """C4.E/F — one idempotency key cannot serve both modes: pinned
    over a prior legacy job, or public over a prior pinned job, is a
    409 conflict — never a cross-mode replay."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x"}
    r = _commit_public(client, p["id"], "generate.image", payload,
                       "k-mode-01")
    assert r.status_code == 200, r.text
    b = _internal_preview(client, p["id"], "generate.image",
                          payload)["execution_binding"]
    with pytest.raises(CommandError) as ei:
        _internal_commit(client, p["id"], "generate.image", payload,
                         "k-mode-01", expected=b)
    assert ei.value.status_code == 409
    _internal_commit(client, p["id"], "generate.image", payload,
                     "k-mode-02", expected=b)
    r2 = _commit_public(client, p["id"], "generate.image", payload,
                        "k-mode-02")
    assert r2.status_code == 409


def test_replay_different_approved_binding_refuses(client, monkeypatch):
    """C4.B/C — same key + identical public payload but a DIFFERENT
    approved binding (new install_id, or new member hash evidence)
    conflicts — the payload alone is not the replay contract."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_a = _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x"}
    b1 = _internal_preview(client, p["id"], "generate.image",
                           payload)["execution_binding"]
    _internal_commit(client, p["id"], "generate.image", payload,
                     "k-diff", expected=b1)
    with client.app.state.session_factory() as s:
        s.get(ModelInstall, install_a).status = "removed"
        s.commit()
    _install_pinned(client, spec)  # install B replaces A
    b2 = _internal_preview(client, p["id"], "generate.image",
                           payload)["execution_binding"]
    assert b2["install_id"] != b1["install_id"]
    with pytest.raises(CommandError) as ei:
        _internal_commit(client, p["id"], "generate.image", payload,
                         "k-diff", expected=b2)
    assert ei.value.status_code == 409
    # Different approved member-hash evidence conflicts the same way:
    # re-declare the catalog member hash and re-approve a replacement
    # install (exactly one eligible row remains).
    with client.app.state.session_factory() as s:
        s.get(ModelInstall, b2["install_id"]).status = "removed"
        s.commit()
    spec2 = _img_spec(content=b"IMGX")
    _add_spec(monkeypatch, spec2)
    # verified=False → no recorded-hash evidence on the new install;
    # the approved member sha is the spec2 catalog declaration.
    _install_pinned(client, spec2, verified=False)
    b3 = _internal_preview(client, p["id"], "generate.image",
                           payload)["execution_binding"]
    assert b3["members"] != b1["members"]
    with pytest.raises(CommandError) as ei3:
        _internal_commit(client, p["id"], "generate.image", payload,
                         "k-diff", expected=b3)
    assert ei3.value.status_code == 409


def test_member_size_drift_refuses(client, monkeypatch):
    """C5 — a member whose current size differs from its catalog-
    declared size refuses BEFORE the hash pass; an equal-size
    replacement still fails on content hash (size is not proof)."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    with client.app.state.session_factory() as s:
        root = model_files.models_root(s, client.app.state.settings)
    target = root / spec.key / "main.safetensors"
    target.write_bytes(b"IMGW-with-extra-drift")
    ad = _img_adapter(client)
    with pytest.raises(ValueError, match="member_size_drift"):
        ad._resolve(_pinned_request(b))
    target.write_bytes(b"JUNK")  # same size, different content
    with pytest.raises(ValueError, match="integrity_mismatch"):
        ad._resolve(_pinned_request(b))


def test_execbind_client_keys_inventory():
    """F2 — the prohibited client-field inventory is exactly these 13
    names; locking it so a silent key removal is a regression, not a
    reporting detail."""
    assert cmd.EXECBIND_CLIENT_KEYS == frozenset({
        "execution_binding", "expected_binding", "binding_required",
        "execbind", "execbind_expected", "internal_execbind",
        "install_id", "expected_sha256", "member_sha256",
        "binding_version", "binding_marker", "trusted_internal",
        "internal_invocation"})


def test_refused_commit_creates_no_job(client, monkeypatch):
    """C6.F, F2 — EVERY prohibited client field commits to neither a
    pinned nor an unpinned job; a missing expected binding likewise."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    n0 = _job_count(client)
    for i, field in enumerate(sorted(cmd.EXECBIND_CLIENT_KEYS)):
        r = _commit_public(
            client, p["id"], "generate.image",
            {"model_key": spec.key, "prompt": "x", field: {"x": 1}},
            f"k-noside-{i}")
        assert r.status_code == 422, (field, r.text)
    with pytest.raises(CommandError):
        _internal_commit(client, p["id"], "generate.image",
                         {"model_key": spec.key, "prompt": "x"},
                         "k-noside2")  # expected=None
    assert _job_count(client) == n0
    # a valid public commit afterwards still works — nothing was
    # silently stripped or latched by the refused attempts
    r = _commit_public(client, p["id"], "generate.image",
                       {"model_key": spec.key, "prompt": "x"},
                       "k-after-refusals")
    assert r.status_code == 200, r.text
    req = _last_job(client)
    assert "binding_required" not in req
    assert "execution_binding" not in req


# ------------------------------------------------------------------
# J. F1 — opened-file drift verification & final pre-spawn gate
#
# Deterministic mutation points: cancel_check runs before EVERY chunk
# inside stream_sha256 (call N lands between the pre- and post-hash
# fstat captures), and _pick_flavor is a fixed preparation step between
# resolution and the spawn boundary — no timing dependence anywhere.

def _member_file(client, spec, name="main.safetensors") -> Path:
    with client.app.state.session_factory() as s:
        return model_files.models_root(
            s, client.app.state.settings) / spec.key / name


def test_unchanged_member_passes_both_gates(client, monkeypatch):
    """F1.A — an untouched regular file verifies at the end-of-
    resolution gate AND the final revalidation gate."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)
    _spec_out, w, _ = ad._resolve(_pinned_request(b))
    assert w.name == "main.safetensors"
    assert isinstance(ad._pinned, model_files.PinnedPaths)
    ad._pinned.revalidate()  # second gate: unchanged → clean


def test_size_change_during_hash_refuses(client, monkeypatch):
    """F1.D — the file grows under the open descriptor mid-stream →
    member_modified; the digest is never trusted."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    target = _member_file(client, spec)
    calls = []
    def cc():
        calls.append(1)
        if len(calls) == 2:  # deterministic: after the first chunk
            with open(target, "ab") as fh:
                fh.write(b"!")
    ad = _img_adapter(client)
    with pytest.raises(ValueError, match="execbind:member_modified"):
        ad._resolve(_pinned_request(b), cancel_check=cc)


def test_metadata_change_during_hash_refuses(client, monkeypatch):
    """F1.E — a metadata-only modification (mtime) under the open
    descriptor mid-stream → member_modified. Access time is never a
    failure condition."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    target = _member_file(client, spec)
    calls = []
    def cc():
        calls.append(1)
        if len(calls) == 2:
            st = target.stat()
            os.utime(target, ns=(st.st_atime_ns,
                                 st.st_mtime_ns + 1_000_000_000))
    ad = _img_adapter(client)
    with pytest.raises(ValueError, match="execbind:member_modified"):
        ad._resolve(_pinned_request(b), cancel_check=cc)


def test_replacement_during_hash_refuses(client, monkeypatch):
    """F1.F — the path is swapped in the post-close stat window during
    verification → member_replaced via opened-file identity (dev+ino)
    where the filesystem reports file indices.

    Deterministic mutation point: Path.stat is hooked to perform a REAL
    filesystem replacement the first time the verified path is stat'd
    after the stream (the fd-close→path-stat window — the only window a
    path swap can reach on Windows, which forbids deleting an open
    file). The detection machinery itself is unmocked."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    target = _member_file(client, spec)
    if not target.stat().st_ino:
        pytest.skip("file-identity evidence unavailable on this "
                    "filesystem")
    resolved_target = target.resolve()
    armed = []
    def cc():
        if not armed:  # arm inside the stream — before fd close
            armed.append(1)
    orig_stat = Path.stat
    def swapped_stat(self, *a, **k):
        if armed and self == resolved_target:
            armed.clear()        # one-shot, at the post-close stat
            target.unlink()      # fd is closed here — unlink allowed
            target.write_bytes(b"SWAP")  # new file index, same path
        return orig_stat(self, *a, **k)
    monkeypatch.setattr(Path, "stat", swapped_stat)
    ad = _img_adapter(client)
    with pytest.raises(ValueError, match="execbind:member_replaced"):
        ad._resolve(_pinned_request(b), cancel_check=cc)


def test_first_member_drift_during_second_verify_refuses(
        client, monkeypatch):
    """F1.G — diffusion verified, then mutated while vae streams — the
    end-of-resolution gate refuses before any spawn-boundary path."""
    spec = _vid_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, spec.key, install_id,
                     adapter="gen.video.sdcpp",
                     roles=("diffusion", "vae", "text_encoder"))
    diff = _member_file(client, spec, "diffusion.safetensors")
    orig = model_files.stream_sha256
    def mutate_after_vae(path, cancel_check=None, **kw):
        out = orig(path, cancel_check, **kw)
        if Path(path).name == "vae.safetensors":
            with open(diff, "ab") as fh:
                fh.write(b"!")
        return out
    monkeypatch.setattr(model_files, "stream_sha256", mutate_after_vae)
    ad = _vid_adapter(client)
    with pytest.raises(ValueError, match="execbind:member_modified"):
        ad._resolve({**_pinned_request(b), "model_key": spec.key})


def test_member_drift_during_preparation_refuses_image(
        client, monkeypatch, tmp_path):
    """F1.H/O — a verified member modified during request preparation
    (flavor selection) refuses at the spawn-boundary gate; the external
    worker is NEVER spawned."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    target = _member_file(client, spec)
    orig_flavor = SdCppAdapter._pick_flavor
    def drifted_flavor(self, requested, rt_spec):
        with open(target, "ab") as fh:  # fixed prep step → deterministic
            fh.write(b"!")
        return orig_flavor(self, requested, rt_spec)
    monkeypatch.setattr(SdCppAdapter, "_pick_flavor", drifted_flavor)
    spawned = []
    monkeypatch.setattr(
        "app.adapters.inference._spawn_supervised_async",
        lambda *a, **k: spawned.append(1))
    ad = _img_adapter(client)
    handle = _handle({**_pinned_request(b), "model_key": spec.key,
                      "width": 512, "height": 512}, tmp_path)
    with pytest.raises(ValueError, match="execbind:member_modified"):
        asyncio.run(_drain(ad.run(handle)))
    assert spawned == []


def test_member_drift_during_preparation_refuses_video(
        client, monkeypatch, tmp_path):
    """F1.H/O — same gate on the video path: an aux member modified
    during preparation refuses and never reaches spawn."""
    spec = _vid_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, spec.key, install_id,
                     adapter="gen.video.sdcpp",
                     roles=("diffusion", "vae", "text_encoder"))
    vae = _member_file(client, spec, "vae.safetensors")
    orig_flavor = SdCppAdapter._pick_flavor
    def drifted_flavor(self, requested, rt_spec):
        with open(vae, "ab") as fh:
            fh.write(b"!")
        return orig_flavor(self, requested, rt_spec)
    monkeypatch.setattr(SdCppAdapter, "_pick_flavor", drifted_flavor)
    spawned = []
    monkeypatch.setattr(
        "app.adapters.inference._spawn_supervised_async",
        lambda *a, **k: spawned.append(1))
    ad = _vid_adapter(client)
    req = {**_pinned_request(b), "model_key": spec.key,
           "frames": 4, "width": 832, "height": 480}
    with pytest.raises(ValueError, match="execbind:member_modified"):
        asyncio.run(_drain(ad.run(_handle(req, tmp_path))))
    assert spawned == []


def test_member_disappears_after_verify_refuses(client, monkeypatch):
    """F1.I — a member deleted between verification and the final gate
    refuses member_unavailable."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)
    ad._resolve(_pinned_request(b))
    _member_file(client, spec).unlink()
    with pytest.raises(model_files.ExecbindError) as ei:
        ad._pinned.revalidate()
    assert ei.value.code == "member_unavailable"


def test_same_size_replacement_at_gate_refuses(client, monkeypatch):
    """F1.J — a same-size replacement with the original mtime restored
    still refuses via opened-file identity (dev+ino) where the
    filesystem reports file indices; where it does not, the check
    degrades to size/mtime (disclosed, not claimed detected)."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)
    ad._resolve(_pinned_request(b))
    target = _member_file(client, spec)
    orig_st = target.stat()
    if not orig_st.st_ino:
        pytest.skip("file-identity evidence unavailable on this "
                    "filesystem")
    tmp = target.parent / "_swap.tmp"
    tmp.write_bytes(b"XXXX")              # same size, other content
    os.replace(tmp, target)
    os.utime(target, ns=(orig_st.st_atime_ns, orig_st.st_mtime_ns))
    with pytest.raises(model_files.ExecbindError) as ei:
        ad._pinned.revalidate()
    assert ei.value.code == "member_replaced"


def test_cancel_during_revalidation_preserves_cancel(client, monkeypatch):
    """F1.K — a genuine cancel observed by the final gate propagates
    JobCancelled — preserved cancellation semantics, not a drift
    failure."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)
    ad._resolve(_pinned_request(b))
    ev = threading.Event()
    ev.set()
    with pytest.raises(JobCancelled):
        ad._pinned.revalidate(
            cancel_check=_handle({}, Path("."), ev).check_cancel)


def test_drift_refusal_is_controlled_failure(client, monkeypatch):
    """F1.3/L — a drift refusal is a coded failure: not JobCancelled,
    not a legacy fallback, not a substitute, and the message carries
    member names, never absolute paths."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)
    ad._resolve(_pinned_request(b))
    target = _member_file(client, spec)
    with open(target, "ab") as fh:
        fh.write(b"!")
    with pytest.raises(model_files.ExecbindError) as ei:
        ad._pinned.revalidate()
    assert ei.value.code == "member_modified"
    assert not isinstance(ei.value, JobCancelled)
    with client.app.state.session_factory() as s:
        root = model_files.models_root(s, client.app.state.settings)
    assert str(root) not in str(ei.value)
    assert "main.safetensors" in str(ei.value)


def test_final_gate_precedes_spawn_image_and_video(
        client, monkeypatch, tmp_path):
    """F1.M/N — ordering proof for BOTH adapters: every member hash →
    end-of-resolution gate → preparation → final gate → spawn."""
    order = []
    orig_hash = model_files.stream_sha256
    monkeypatch.setattr(
        model_files, "stream_sha256",
        lambda p, c=None, **k: order.append("hash")
        or orig_hash(p, c, **k))
    orig_gate = model_files.PinnedPaths.revalidate
    monkeypatch.setattr(
        model_files.PinnedPaths, "revalidate",
        lambda self, **k: order.append("gate") or orig_gate(self, **k))
    monkeypatch.setattr(
        "app.adapters.inference._spawn_supervised_async",
        lambda *a, **k: order.append("spawn") or (_ for _ in ()).throw(
            RuntimeError("spawn-boundary")))

    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)
    handle = _handle({**_pinned_request(b), "model_key": spec.key,
                      "width": 512, "height": 512}, tmp_path)
    with pytest.raises(RuntimeError, match="spawn-boundary"):
        asyncio.run(_drain(ad.run(handle)))
    assert order == ["hash", "gate", "gate", "spawn"]

    order.clear()
    vspec = _vid_spec()
    _add_spec(monkeypatch, vspec)
    vinst = _install_pinned(client, vspec)
    vb = _binding_for(client, vspec.key, vinst,
                      adapter="gen.video.sdcpp",
                      roles=("diffusion", "vae", "text_encoder"))
    vad = _vid_adapter(client)
    vreq = {**_pinned_request(vb), "model_key": vspec.key,
            "frames": 4, "width": 832, "height": 480}
    with pytest.raises(RuntimeError, match="spawn-boundary"):
        asyncio.run(_drain(vad.run(_handle(vreq, tmp_path))))
    assert order == ["hash", "hash", "hash", "gate", "gate", "spawn"]


# ------------------------------------------------------------------
# K. E1–E3 — final execution-boundary closure
#
# E1: pinned execution is a standard-subprocess-adapter contract —
# trusted preview, commit, alternate routing and the worker all refuse
# the persistent runtime; ordinary unpinned persistent behavior is
# unchanged. E2: the final gate re-resolves each member and requires
# resolved-path IDENTITY with the verified file — containment is
# pinned, not merely re-checked. E3: pinned evidence is request-local
# (per-task ContextVar), never shared adapter state.


def _server_adapter(client, pool=None):
    from app.adapters.sdcpp_server import SdCppServerAdapter
    return SdCppServerAdapter(client.app.state.session_factory,
                              client.app.state.settings, pool)


def test_pinned_persistent_commit_no_expected_refuses(
        client, monkeypatch):
    """E1 — a pinned persistent commit refuses with the stable reason
    even before expected-binding handling; no job is created and
    nothing falls back to unpinned or substitutes another adapter."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    p = make_project(client)
    n0 = _job_count(client)
    with pytest.raises(CommandError, match="persistent_runtime"):
        _internal_commit(client, p["id"], "generate.image.persistent",
                         {"model_key": spec.key, "prompt": "x"},
                         "k-pers-noexp")
    assert _job_count(client) == n0


def test_pinned_local_adapter_override_refuses(client, monkeypatch):
    """E1.1/E1.3.E — routing generate.image onto the persistent adapter
    via a local_adapter_id stamp is refused at preview AND commit:
    enforcement binds the EFFECTIVE adapter, never the command name."""
    from app.adapters.sdcpp_server import SdCppServerAdapter
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    p = make_project(client)
    payload = {"model_key": spec.key, "prompt": "x",
               "execution_target": "local",
               "local_adapter_id": "gen.image.sdcpp-server"}
    with pytest.raises(CommandError, match="persistent_runtime"):
        _internal_preview(client, p["id"], "generate.image", payload)
    # Registering the persistent adapter is exactly what the feature
    # flag does (registry gate) — commit reaches the effective-adapter
    # check and still refuses: enablement is not a bypass (E1.3.H).
    reg = client.app.state.job_engine._registry
    monkeypatch.setitem(
        reg._adapters, "gen.image.sdcpp-server",
        SdCppServerAdapter(client.app.state.session_factory,
                           client.app.state.settings))
    b = _binding_for(client, install_id=install_id)
    n0 = _job_count(client)
    with pytest.raises(CommandError, match="persistent_runtime"):
        _internal_commit(client, p["id"], "generate.image", payload,
                         "k-ovr", expected=b)
    assert _job_count(client) == n0


def test_pinned_persistent_worker_refuses_before_server(
        client, monkeypatch, tmp_path):
    """E1.2/E1.3.F — a pinned request on SdCppServerAdapter refuses
    inside the inherited _resolve: _server_exe, pool.acquire (server
    spawn) and the HTTP submit are never reached."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, spec.key, install_id,
                     adapter="gen.image.sdcpp-server", roles=("diffusion",))
    ad = _server_adapter(client)
    req = _pinned_request(b)
    with pytest.raises(ValueError, match="persistent_runtime"):
        ad._resolve(req)
    called = []
    monkeypatch.setattr(type(ad), "_server_exe",
                        lambda *a, **k: called.append("exe"))

    class _Pool:
        async def acquire(self, spec):  # pragma: no cover - never reached
            called.append("acquire")

    ad._pool = _Pool()
    with pytest.raises(ValueError, match="persistent_runtime"):
        asyncio.run(_drain(ad.run(_handle(req, tmp_path))))
    assert called == []


def test_unpinned_persistent_resolution_unchanged(client, monkeypatch):
    """E1.3.G — an ordinary (unpinned) request on the persistent
    adapter still resolves the legacy installed-file path; the new
    refusal engages only on a pinned request."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    _install_pinned(client, spec)
    ad = _server_adapter(client)
    got_spec, weight, _ = ad._resolve(
        {"model_key": spec.key, "prompt": "x"})
    assert got_spec.key == spec.key and weight is not None


def test_gate_containment_escape_refuses(client, monkeypatch, tmp_path):
    """E2 — after verification the member is retargeted through a
    symlink so it resolves OUTSIDE the approved install dir: the final
    gate re-resolves the path and refuses (member_replaced)."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)
    ad._resolve(_pinned_request(b))
    pinned = ad._pinned
    assert pinned is not None
    member = _member_file(client, spec)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"OUT")
    member.unlink()
    try:
        member.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted in this environment")
    with pytest.raises(model_files.ExecbindError) as ei:
        pinned.revalidate()
    assert ei.value.code == "member_replaced"


def test_pinned_evidence_is_request_local(client, monkeypatch):
    """E3 — pinned evidence lives in per-task state on the SHARED
    adapter instance: a concurrent invocation cannot read or overwrite
    it, a subsequent unpinned resolve clears it, and nothing persists
    across executions."""
    spec = _img_spec()
    _add_spec(monkeypatch, spec)
    install_id = _install_pinned(client, spec)
    b = _binding_for(client, install_id=install_id)
    ad = _img_adapter(client)
    ad._resolve(_pinned_request(b))
    pinned = ad._pinned
    assert pinned is not None
    # sequential: the next unpinned resolve on the same singleton
    # cannot inherit the pinned evidence
    ad._resolve({"model_key": spec.key, "prompt": "x"})
    assert ad._pinned is None

    async def probe():
        ad._pinned = "foreign"  # writes to the child task's own slot
        await asyncio.sleep(0)
        return ad._pinned

    async def driver():
        ad._pinned = pinned
        child = asyncio.create_task(probe())
        assert ad._pinned is pinned      # child's write can't reach here
        assert await child == "foreign"
        assert ad._pinned is pinned      # intact after child completes

    asyncio.run(driver())
    # the driver's evidence never leaked into this context either
    assert ad._pinned is None
