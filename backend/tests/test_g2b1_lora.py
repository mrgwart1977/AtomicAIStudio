"""G2-B/B-1 — LoRA subprocess foundation: validated structured
selections, fail-closed prompt-tag control, per-job staging with
sha-pinned re-verification, and honest provenance.

No real inference: the end-to-end tests drive the fake sd.cpp worker
(ATOMIC_SDCPP_CMD); staging/validation tests use injected catalog specs
and real files on disk only.
"""
import hashlib
import json
import struct
from pathlib import Path

import pytest

from tests.conftest import make_project, wait_for_job
from tests.test_c4b_shot_generate import (
    _commit, _preview, _job_req, _setup, _shot, GEN)
from tests.test_a3_keyframes import _mk_kf
from tests.test_a2_storyboard import _scene2, _shot2
from tests.test_m3 import _install_row, _fake_cmd
from app.domain import model_catalog
from app.domain.model_catalog import ModelFile, ModelSpec


def _lora_bytes(keys=("block.lora_down.weight", "block.lora_up.weight")):
    hdr = {k: {"dtype": "F16", "shape": [1, 1], "data_offsets": [0, 2]}
           for k in keys}
    h = json.dumps(hdr).encode()
    return struct.pack("<Q", len(h)) + h + b"\x00\x00"


def _lora_spec(key="test.id", *, family="sd1.5", role="identity",
               fmt="safetensors", support="tested", sha=None):
    ext = ".safetensors" if fmt == "safetensors" else ".gguf"
    return ModelSpec(
        key=key, display_name=f"LoRA {key}", family=family, version="1.0",
        adapter_id="gen.image.sdcpp", fmt=fmt,
        source={"provider": "test"},
        license={"name": "test", "commercial_ok": True},
        files=(ModelFile(name=f"{key}{ext}", size_bytes=0, url="",
                         sha256=sha),),
        requirements={}, capabilities={"lora_role": role},
        support=support, modality="lora", modes=())


def _add_lora(monkeypatch, spec):
    monkeypatch.setitem(model_catalog.MODELS, spec.key, spec)


def _install_lora(client, spec, content=None):
    """test_m3._install_row for loras — a structurally valid safetensors
    file and the real sha256 recorded on the install row."""
    from sqlalchemy import select
    from app.db.models import ModelInstall, new_id, utcnow
    from app.domain import model_files
    app = client.app
    with app.state.session_factory() as s:
        root = model_files.models_root(s, app.state.settings)
        dest_dir = model_files.model_dir(root, spec)
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / spec.files[0].name
        dest.write_bytes(content if content is not None else _lora_bytes())
        row = s.execute(select(ModelInstall).where(
            ModelInstall.model_key == spec.key)).scalar_one_or_none() \
            or ModelInstall(id=new_id(), model_key=spec.key)
        row.adapter_id = spec.adapter_id
        row.version = spec.version
        row.status = "installed"
        row.install_root = str(root)
        row.rel_dir = dest_dir.name
        row.file_name = spec.files[0].name
        row.byte_size = dest.stat().st_size
        row.sha256 = hashlib.sha256(dest.read_bytes()).hexdigest()
        row.verified = True
        row.license = "test"
        row.meta_json = {}
        row.error = None
        row.created_at = row.created_at or utcnow()
        row.updated_at = utcnow()
        s.add(row)
        s.commit()
    return dest


def _bind_loras(client, pid, shot_id, char_id, *entries):
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": shot_id, "character_id": char_id,
             "state": {"loras": list(entries)}})


def _kf_local(client, pid, sh, expect=200, **payload):
    return _commit(client, pid, "keyframe.generate",
                   {"keyframe_id": _mk_kf(
                       client, pid, sh["shot"]["id"], "start",
                       prompt="a red door")["id"],
                    "execution_target": "local", "local_adapter": "sdcpp",
                    "model_key": "sd15.fp16", **payload}, expect=expect)


def _kf_setup(client, tmp_path, monkeypatch, *, role="identity",
              key="lora.id"):
    """project + char + shot + kf + installed sd15 + installed lora."""
    spec = _lora_spec(key, role=role)
    _add_lora(monkeypatch, spec)
    _install_row(client, "sd15.fp16", tmp_path)
    _install_lora(client, spec)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="a red door")
    return pid, c, sh, spec


# ------------------------------------------------ target/dispatch refusals

def test_lora_refused_on_mock_target(client, monkeypatch):
    spec = _lora_spec("lora.id")
    _add_lora(monkeypatch, spec)
    pid = make_project(client)["id"]
    sc = _scene2(client, pid)
    sh = _shot2(client, pid, sc["scene"]["id"])
    r = _commit(client, pid, "keyframe.generate", {
        "keyframe_id": _mk_kf(client, pid, sh["shot"]["id"], "start",
                              prompt="x")["id"],
        "loras": [{"key": "lora.id"}]}, expect=422)
    assert "loras" in r.json()["detail"]


def test_lora_refused_on_cloud_shot(client, tmp_path, monkeypatch):
    spec = _lora_spec("lora.id")
    _add_lora(monkeypatch, spec)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    r = _commit(client, pid, "shot.generate",
                {**GEN, "shot_id": sh["shot"]["id"],
                 "loras": [{"key": "lora.id"}]}, expect=422)
    assert "loras" in r.json()["detail"]


def test_lora_refused_on_persistent_adapter(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    r = _commit(client, pid, "keyframe.generate", {
        "keyframe_id": _mk_kf(client, pid, sh["shot"]["id"], "start",
                              prompt="x")["id"],
        "execution_target": "local", "local_adapter": "sdcpp-server",
        "model_key": "sd15.fp16",
        "loras": [{"key": spec.key}]}, expect=422)
    assert "subprocess" in r.json()["detail"] \
        or "persistent" in r.json()["detail"]


def test_generate_image_persistent_lora_refused(client, tmp_path,
                                                monkeypatch):
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    r = _commit(client, pid, "generate.image.persistent", {
        "model_key": "sd15.fp16", "prompt": "x",
        "loras": [{"key": spec.key}]}, expect=422)
    assert "subprocess" in r.json()["detail"] \
        or "persistent" in r.json()["detail"]


# ------------------------------------------------ selection validation

def test_lora_unknown_key_refused(client, tmp_path):
    _install_row(client, "sd15.fp16", tmp_path)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": "nope.lora"}])
    assert "unknown LoRA" in r.json()["detail"]


def test_lora_non_lora_spec_refused(client, tmp_path):
    """A base model is not an adapter — modality gate."""
    _install_row(client, "sd15.fp16", tmp_path)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": "sd15.fp16"}])
    assert "unknown LoRA" in r.json()["detail"]


def test_lora_planned_tier_refused(client, tmp_path, monkeypatch):
    spec = _lora_spec("lora.planned", support="planned")
    _add_lora(monkeypatch, spec)
    _install_row(client, "sd15.fp16", tmp_path)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key}])
    assert "planned" in r.json()["detail"]


def test_lora_family_mismatch_refused(client, tmp_path, monkeypatch):
    spec = _lora_spec("lora.xl", family="sdxl")
    _add_lora(monkeypatch, spec)
    _install_row(client, "sd15.fp16", tmp_path)
    _install_lora(client, spec)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key}])
    assert "sdxl" in r.json()["detail"]


def test_lora_gguf_refused(client, tmp_path, monkeypatch):
    spec = _lora_spec("lora.gg", fmt="gguf")
    _add_lora(monkeypatch, spec)
    _install_row(client, "sd15.fp16", tmp_path)
    _install_lora(client, spec, content=b"GGUF" + b"\x00" * 64)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key}])
    assert "safetensors" in r.json()["detail"].lower() \
        or "gguf" in r.json()["detail"].lower()


def test_lora_not_installed_refused(client, tmp_path, monkeypatch):
    spec = _lora_spec("lora.id")
    _add_lora(monkeypatch, spec)
    _install_row(client, "sd15.fp16", tmp_path)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key}])
    assert "not installed" in r.json()["detail"]


@pytest.mark.parametrize("bad", [0, -0.5, 2.1, "abc", True])
def test_lora_strength_invalid(client, tmp_path, monkeypatch, bad):
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key, "strength": bad}])
    assert "strength" in r.json()["detail"]


def test_lora_strength_nonfinite(client, tmp_path, monkeypatch):
    """NaN/inf can't arrive via JSON — exercise the function directly."""
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    from app.db.models import Project, Shot
    from app.domain.commands import CommandError, _check_loras
    with client.app.state.session_factory() as s:
        project = s.get(Project, pid)
        shot = s.get(Shot, sh["shot"]["id"])
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(CommandError):
                _check_loras(s, project, [{"key": spec.key,
                                           "strength": bad}],
                             model_catalog.get("sd15.fp16"), shot=shot)


def test_lora_duplicate_refused(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key}, {"key": spec.key}])
    assert "duplicate" in r.json()["detail"]


def test_lora_same_sha_refused(client, tmp_path, monkeypatch):
    """Same weights under two catalog keys = duplicate."""
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    spec2 = _lora_spec("lora.id2", role="style")
    _add_lora(monkeypatch, spec2)
    _install_lora(client, spec2)  # identical content → identical sha
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key,
                spec2.key)
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key}, {"key": spec2.key}])
    assert "same weights" in r.json()["detail"]


def test_lora_two_identity_refused(client, tmp_path, monkeypatch):
    spec2 = _lora_spec("lora.id2", role="identity")
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _add_lora(monkeypatch, spec2)
    _install_lora(client, spec2,
                  content=_lora_bytes(("other.lora_down.weight",)))
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key,
                spec2.key)
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key}, {"key": spec2.key}])
    assert "one identity" in r.json()["detail"]


def test_lora_identity_unbound_refused(client, tmp_path, monkeypatch):
    """Installed + compatible is not enough — no cast binding, no run.
    Refused at preview AND commit identically."""
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"],
                "unrelated.other")  # bound a DIFFERENT key
    kf_id = _mk_kf(client, pid, sh["shot"]["id"], "start", prompt="x")["id"]
    p = _preview(client, pid, "keyframe.generate", {
        "keyframe_id": kf_id, "execution_target": "local",
        "local_adapter": "sdcpp", "model_key": "sd15.fp16",
        "loras": [{"key": spec.key}]}, expect=422)
    assert "not bound" in p.json()["detail"]
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key}])
    assert "not bound" in r.json()["detail"]


def test_lora_style_unbound_refused(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch,
                               role="style", key="lora.style")
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key}])
    assert "not linked" in r.json()["detail"]


# ------------------------------------------------ prompt-tag injection

@pytest.mark.parametrize("frag", [
    "<lora:evil:9>", "< lora :evil>", "<<lora:x:1>>", "<LYCO:x:1>",
    "</lora>", "<lora"])
def test_lora_syntax_in_prompt_refused(client, tmp_path, monkeypatch,
                                       frag):
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    r = _commit(client, pid, "keyframe.generate", {
        "keyframe_id": _mk_kf(client, pid, sh["shot"]["id"], "start",
                              prompt="x")["id"],
        "execution_target": "local", "local_adapter": "sdcpp",
        "model_key": "sd15.fp16", "loras": [{"key": spec.key}],
        "prompt": f"a door {frag}"}, expect=422)
    assert "LoRA" in r.json()["detail"]


def test_lora_syntax_in_negative_refused(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    r = _commit(client, pid, "keyframe.generate", {
        "keyframe_id": _mk_kf(client, pid, sh["shot"]["id"], "start",
                              prompt="x")["id"],
        "execution_target": "local", "local_adapter": "sdcpp",
        "model_key": "sd15.fp16", "loras": [{"key": spec.key}],
        "negative_prompt": "bad <lora:neg:1>"}, expect=422)
    assert "negative_prompt" in r.json()["detail"]


def test_lora_syntax_in_cast_context_refused(client, tmp_path, monkeypatch):
    """Context fields (cast state) feed the composed prompt — an injected
    tag there is refused exactly like a typed one."""
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _commit(client, pid, "shot.cast.assign",
            {"shot_id": sh["shot"]["id"], "character_id": c["id"],
             "state": {"loras": [spec.key],
                      "wardrobe_variant": "cape <lora:hidden:2>"}})
    r = _kf_local(client, pid, sh, expect=422,
                  loras=[{"key": spec.key}])
    assert "LoRA" in r.json()["detail"]


def test_lora_syntax_refused_even_without_loras(client, tmp_path,
                                                monkeypatch):
    """The scan runs on every local generation, not just lora jobs."""
    _install_row(client, "sd15.fp16", tmp_path)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="x")
    r = _commit(client, pid, "keyframe.generate", {
        "keyframe_id": _mk_kf(client, pid, sh["shot"]["id"], "start",
                              prompt="x")["id"],
        "execution_target": "local", "local_adapter": "sdcpp",
        "model_key": "sd15.fp16",
        "prompt": "looks like <lora:free:1>"}, expect=422)
    assert "LoRA" in r.json()["detail"]


# ------------------------------------------------ staging internals

def _adapter(client):
    from app.adapters.inference import SdCppAdapter
    return SdCppAdapter(client.app.state.session_factory,
                        client.app.state.settings)


def test_stage_loras_hash_and_copy(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    src = _install_lora(client, spec)
    with client.app.state.session_factory() as s:
        from app.db.models import ModelInstall
        from sqlalchemy import select
        row = s.execute(select(ModelInstall).where(
            ModelInstall.model_key == spec.key)).scalar_one()
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    req = {"prompt": "a door", "loras": [{
        "key": spec.key, "install_row_id": row.id, "sha256": sha,
        "staged_name": f"{spec.key}-{sha[:12]}.safetensors",
        "strength": 0.8}]}
    adapter = _adapter(client)
    staged = tmp_path / "jobA-loras"
    prompt = adapter._stage_loras(req, staged)
    staged_file = staged / req["loras"][0]["staged_name"]
    assert staged_file.is_file()
    assert staged_file.read_bytes() == src.read_bytes()
    assert prompt.endswith(f"<lora:{spec.key}-{sha[:12]}:0.8>")
    # In-place source mutation cannot reach the staged snapshot.
    src.write_bytes(b"corrupted")
    assert staged_file.read_bytes() != src.read_bytes()
    # And a second stage of the same pinned req sees the tampering.
    staged2 = tmp_path / "jobB-loras"
    with pytest.raises(ValueError, match="changed since"):
        adapter._stage_loras(req, staged2)


def test_stage_concurrent_dirs_isolated(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    src = _install_lora(client, spec)
    with client.app.state.session_factory() as s:
        from app.db.models import ModelInstall
        from sqlalchemy import select
        row = s.execute(select(ModelInstall).where(
            ModelInstall.model_key == spec.key)).scalar_one()
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    req = {"prompt": "x", "loras": [{
        "key": spec.key, "install_row_id": row.id, "sha256": sha,
        "staged_name": f"{spec.key}-{sha[:12]}.safetensors",
        "strength": 1.0}]}
    adapter = _adapter(client)
    d1, d2 = tmp_path / "j-aaa-loras", tmp_path / "j-bbb-loras"
    adapter._stage_loras(req, d1)
    adapter._stage_loras(req, d2)
    assert d1.is_dir() and d2.is_dir() and d1 != d2
    assert (d1 / req["loras"][0]["staged_name"]).is_file()


def test_stage_removed_install_refused(client, tmp_path, monkeypatch):
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    src = _install_lora(client, spec)
    with client.app.state.session_factory() as s:
        from app.db.models import ModelInstall
        from sqlalchemy import select
        row = s.execute(select(ModelInstall).where(
            ModelInstall.model_key == spec.key)).scalar_one()
        row_id, sha = row.id, hashlib.sha256(
            src.read_bytes()).hexdigest()
        row.status = "removed"
        s.commit()
    req = {"prompt": "x", "loras": [{
        "key": spec.key, "install_row_id": row_id, "sha256": sha,
        "staged_name": f"{spec.key}-{sha[:12]}.safetensors",
        "strength": 1.0}]}
    with pytest.raises(ValueError, match="removed or changed"):
        _adapter(client)._stage_loras(req, tmp_path / "j-loras")


def test_stage_postcopy_tamper_refused(client, tmp_path, monkeypatch):
    """Verify→copy TOCTOU: if the bytes that land in staging differ from
    the queue-pinned sha256 (source swapped mid-copy, hostile redirect),
    staging refuses and leaves nothing behind."""
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    src = _install_lora(client, spec)
    with client.app.state.session_factory() as s:
        from app.db.models import ModelInstall
        from sqlalchemy import select
        row = s.execute(select(ModelInstall).where(
            ModelInstall.model_key == spec.key)).scalar_one()
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    req = {"prompt": "x", "loras": [{
        "key": spec.key, "install_row_id": row.id, "sha256": sha,
        "staged_name": f"{spec.key}-{sha[:12]}.safetensors",
        "strength": 1.0}]}
    import shutil as _shutil
    real_copy = _shutil.copyfile

    def evil_copy(src_p, dst_p):
        real_copy(src_p, dst_p)
        Path(dst_p).write_bytes(b"tampered weights")

    monkeypatch.setattr(_shutil, "copyfile", evil_copy)
    staged = tmp_path / "j-evil-loras"
    with pytest.raises(ValueError, match="pinned sha256"):
        _adapter(client)._stage_loras(req, staged)
    assert not staged.exists()  # cleaned up — nothing handed to sd.cpp


@pytest.mark.parametrize("bad", [
    "../evil-a1b2c3d4e5f6.safetensors",
    "dir/evil-a1b2c3d4e5f6.safetensors",
    "dir\\evil-a1b2c3d4e5f6.safetensors",
    "/abs/evil-a1b2c3d4e5f6.safetensors",
    "C:\\evil-a1b2c3d4e5f6.safetensors",
    "..\\up-a1b2c3d4e5f6.safetensors",
    "CON.safetensors",               # reserved device stem
    "NUL.safetensors",               # reserved device stem
    "evil-a1b2c3d4e5f6.gguf",        # wrong extension
    "plain.safetensors",             # no -<sha12> contract
    "evil-a1b2c3d4e5f6.safetensorsx",  # trailing junk
    "evil -a1b2c3d4e5f6.safetensors",  # space in name
    "",
])
def test_staged_name_adversarial(bad):
    """The staging boundary re-verifies the naming contract — a queued
    request can never redirect the runtime to another path."""
    from app.domain import lora_tags
    with pytest.raises(ValueError):
        lora_tags.validate_staged_name(bad)


def test_staged_name_contract_ok():
    from app.domain import lora_tags
    ok = "char.alice-v3-9f2c41ab77d2.safetensors"
    assert lora_tags.validate_staged_name(ok) == ok


def test_stage_bad_name_refused(client, tmp_path, monkeypatch):
    """End-to-end at the adapter: a poisoned staged_name dies before
    any file is copied."""
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    src = _install_lora(client, spec)
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    req = {"prompt": "x", "loras": [{
        "key": spec.key, "install_row_id": "row", "sha256": sha,
        "staged_name": "../escape-a1b2c3d4e5f6.safetensors",
        "strength": 1.0}]}
    staged = tmp_path / "j-bad-loras"
    with pytest.raises(ValueError, match="unsafe staged"):
        _adapter(client)._stage_loras(req, staged)
    assert not (tmp_path / "escape-a1b2c3d4e5f6.safetensors").exists()


def test_orphan_staging_purged(client, tmp_path):
    """_purge_work_dir removes {job_id}-loras dirs (finalize + reconcile)."""
    engine = client.app.state.job_engine
    work = tmp_path / "jobs" / "p1"
    stale = work / "job7-loras"
    stale.mkdir(parents=True)
    (stale / "x.safetensors").write_bytes(b"data")
    (work / "job7.png").write_bytes(b"img")
    engine._purge_work_dir(work, "job7")
    assert not stale.exists() and not (work / "job7.png").exists()


def test_purge_never_touches_other_jobs(client, tmp_path):
    engine = client.app.state.job_engine
    work = tmp_path / "jobs" / "p1"
    live = work / "job8-loras"
    live.mkdir(parents=True)
    engine._purge_work_dir(work, "job9")
    assert live.exists()


# ------------------------------------------------ verify_lora_file

def test_verify_lora_file_structure(tmp_path, monkeypatch):
    from app.domain import model_files
    spec = _lora_spec("lora.v")
    good = tmp_path / "good.safetensors"
    good.write_bytes(_lora_bytes())
    ok, _ = model_files.verify_lora_file(good, spec)
    assert ok
    bad_ext = tmp_path / "bad.bin"
    bad_ext.write_bytes(_lora_bytes())
    ok, why = model_files.verify_lora_file(bad_ext, spec)
    assert not ok and "extension" in why
    junk = tmp_path / "junk.safetensors"
    junk.write_bytes(b"not a safetensors file")
    ok, _ = model_files.verify_lora_file(junk, spec)
    assert not ok
    nolora = tmp_path / "nolora.safetensors"
    nolora.write_bytes(_lora_bytes(("model.weight", "other.bias")))
    ok, why = model_files.verify_lora_file(nolora, spec)
    assert not ok and "LoRA tensor" in why
    gg = _lora_spec("lora.gg", fmt="gguf")
    ok, why = model_files.verify_lora_file(good, gg)
    assert not ok and "gguf" in why


# ------------------------------------------------ cast-state schema

def test_cast_state_loras_structured_and_legacy():
    from app.domain import cast_state
    st = cast_state.parse_shot_cast_state(
        {"loras": ["bare.key", {"key": "styled", "strength": 0.7}]})
    # Entries keep their submitted shape — strings stay strings.
    assert st["loras"] == ["bare.key",
                           {"key": "styled", "strength": 0.7}]
    assert cast_state.lora_keys(st["loras"]) == {"bare.key", "styled"}
    with pytest.raises(cast_state.CastStateError):
        cast_state.parse_shot_cast_state({"loras": [{"nokey": 1}]})
    with pytest.raises(cast_state.CastStateError):
        cast_state.parse_shot_cast_state({"loras": [42]})


# ------------------------------------------------ end-to-end (fake worker)

def test_keyframe_lora_e2e(client, tmp_path, monkeypatch):
    _fake_cmd(monkeypatch, tmp_path)
    argv_file = tmp_path / "worker_argv.txt"
    monkeypatch.setenv("FAKE_SD_ARGV_FILE", str(argv_file))
    pid, c, sh, spec = _kf_setup(client, tmp_path, monkeypatch)
    src = _install_lora(client, spec)
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    out = _kf_local(client, pid, sh, loras=[{"key": spec.key,
                                           "strength": 0.9}])
    job = wait_for_job(client, pid, out["job_id"])
    assert job["status"] == "succeeded", job
    assert job["adapter_id"] == "gen.image.sdcpp"
    args = argv_file.read_text().splitlines()
    assert "--lora-model-dir" in args
    assert args[args.index("--lora-apply-mode") + 1] == "at_runtime"
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    prompt_i = args.index("-p") + 1
    assert f"<lora:{spec.key}-{sha[:12]}:0.9>" in args[prompt_i]
    # Staging dir is gone post-job on every exit path.
    lora_dir = args[args.index("--lora-model-dir") + 1]
    assert not __import__("pathlib").Path(lora_dir).exists()
    req = _job_req(client, job["id"])
    entry = req["loras"][0]
    assert entry["key"] == spec.key and entry["sha256"] == sha
    assert entry["install_row_id"] and entry["role"] == "identity"
    assert entry["bound_character_id"] == c["id"]
    # Base-model provenance: the pinned lora record carries the exact
    # base hash it was validated against (catalog-pinned sha256).
    base_spec = model_catalog.get("sd15.fp16")
    assert entry["base_model_key"] == "sd15.fp16"
    assert entry["base_model_sha256"] == base_spec.files[0].sha256
    assert req["runtime_prompt_sha256"] == hashlib.sha256(
        args[prompt_i].encode()).hexdigest()
    assert req["keyframe_generate"]["prompt_context"]["prompt_sha256"] \
        != req["runtime_prompt_sha256"]


def test_shot_lora_e2e_style(client, tmp_path, monkeypatch):
    _fake_cmd(monkeypatch, tmp_path)
    argv_file = tmp_path / "worker_argv.txt"
    monkeypatch.setenv("FAKE_SD_ARGV_FILE", str(argv_file))
    spec = _lora_spec("lora.style", role="style")
    _add_lora(monkeypatch, spec)
    _install_row(client, "sd15.fp16", tmp_path)
    _install_lora(client, spec)
    pid, c, sid, src, scene = _setup(client, tmp_path)
    sh = _shot(client, pid, scene["scene"]["id"], prompt="a wide alley")
    _bind_loras(client, pid, sh["shot"]["id"], c["id"], spec.key)
    out = _commit(client, pid, "shot.generate", {
        "shot_id": sh["shot"]["id"], "execution_target": "local",
        "local_adapter": "sdcpp", "model_key": "sd15.fp16",
        "device": "cpu", "loras": [{"key": spec.key}]})
    job = wait_for_job(client, pid, out["job_id"])
    assert job["status"] == "succeeded", job
    assert job["adapter_id"] == "gen.image.sdcpp"
    args = argv_file.read_text().splitlines()
    assert "--lora-model-dir" in args
    req = _job_req(client, job["id"])
    assert req["loras"][0]["role"] == "style"
    assert req["loras"][0]["bound_character_id"] is None
    assert req["loras"][0]["base_model_key"] == "sd15.fp16"
    assert req["origin_command_type"] == "shot.generate"
    assert req["runtime_prompt_sha256"]


def test_generate_video_loras_refused(client, tmp_path, monkeypatch):
    """loras on generate.video fails closed — never silently stripped
    into a video request, never launched."""
    spec = _lora_spec("lora.id")
    _add_lora(monkeypatch, spec)
    pid = make_project(client)["id"]
    r = _commit(client, pid, "generate.video", {
        "model_key": "any.video", "prompt": "a pan",
        "loras": [{"key": spec.key}]}, expect=422)
    assert "loras" in r.json()["detail"]
    assert "job_id" not in r.json()
