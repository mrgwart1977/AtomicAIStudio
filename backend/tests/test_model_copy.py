"""U-2b-2a — managed-copy pipeline: offline synthetic-fixture tests.

Covers the approved security-contract matrix: gated managed copying,
canonical no-overwrite publication, content-verified adoption, staging
ownership, release (metadata-only), models-root locking, recovery, and
privacy — all with real safetensors/GGUF byte structures built in-memory.
No network, no real weights, no inference, no canonical-file deletion.

NOTE on platform coverage: Windows reparse/junction behavior is exercised
through monkeypatched _is_reparse/is_junction/is_symlink shims — the same
technique the U-2a suite uses. Real NTFS junction behavior is NOT claimed
proven by these POSIX-mock tests (Issue #178 remains open).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import pathlib
import struct
import time

import pytest
from sqlalchemy import select

from app.db.models import GenerationJob, ModelArtifact, ModelInstall
from app.domain import model_artifacts, model_copy, model_files, model_import
from app.domain.instance_lock import acquire_data_dir_lock
from app.domain.model_import import ImportRefused

# ---------------------------------------------------------------- fixtures

def _st_bytes(tensors: dict[str, dict] | None = None,
              metadata: dict | None = None,
              body: bytes = b"\x00" * 64) -> bytes:
    tensors = tensors if tensors is not None else {
        "model.diffusion_model.input_blocks.0.0.weight": {
            "dtype": "F16", "shape": [8, 8],
            "data_offsets": [0, len(body)],
        },
    }
    header = dict(tensors)
    if metadata:
        header["__metadata__"] = metadata
    hb = json.dumps(header).encode()
    return len(hb).to_bytes(8, "little") + hb + body


def _lora_st_bytes() -> bytes:
    body = b"\x01" * 32
    return _st_bytes({
        "lora_unet_down_blocks_0_attentions_0_to_q.lora_down.weight": {
            "dtype": "F16", "shape": [4, 4], "data_offsets": [0, 16]},
        "lora_unet_down_blocks_0_attentions_0_to_q.lora_up.weight": {
            "dtype": "F16", "shape": [4, 4], "data_offsets": [16, 32]},
    }, body=body)


def _gguf_bytes(arch: str = "llama") -> bytes:
    def gstr(s: str) -> bytes:
        b = s.encode()
        return struct.pack("<Q", len(b)) + b
    kv = (gstr("general.architecture") + struct.pack("<I", 8) + gstr(arch))
    return b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + kv + b"\x00" * 64


def _write(tmp_path, name: str, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _enable(client):
    """Turn the feature gate on for this app's settings object — the
    route and adapter both read it live from app.state.settings."""
    client.app.state.settings.enable_managed_copy = True
    return client


def _import(client, authed, path, **kw):
    body = {"path": path, "license": {"name": "MIT"}, **kw}
    return client.post("/api/models/import", json=body, headers=authed)


def _wait_job(client, job_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for j in client.get("/api/jobs").json():
            if j["id"] == job_id and j["status"] in {
                    "succeeded", "failed", "cancelled"}:
                return j
        time.sleep(0.15)
    raise TimeoutError(f"job {job_id} unfinished")


def _import_ok(client, authed, path, **kw):
    r = _import(client, authed, path, **kw)
    assert r.status_code == 202, r.text
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "succeeded", job.get("error")
    return job["result_json"]


def _managed_ok(client, authed, path, **kw):
    return _import_ok(client, authed, path, mode="managed_copy", **kw)


def _sha(p) -> str:
    return hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()


def _models_root(client) -> pathlib.Path:
    with client.app.state.session_factory() as s:
        return model_files.models_root(s, client.app.state.settings)


def _imports(client) -> pathlib.Path:
    return model_copy.imports_root(_models_root(client))


def _staging(client) -> pathlib.Path:
    return model_copy.staging_dir(_imports(client))


def _unit_call(client, src, rep=None):
    """Direct managed_copy invocation for deterministic injection tests."""
    rp = model_import.validate_source_path(str(src))
    rep = rep or model_import.inspect_file(rp)
    with client.app.state.session_factory() as s:
        return model_copy.managed_copy(
            s, client.app.state.settings,
            job_id="unittestjob", src_rp=rp, report=rep)


def _release(client, authed, artifact_id):
    return client.post(
        f"/api/models/artifacts/{artifact_id}/release-managed-copy",
        headers=authed)


@pytest.fixture(autouse=True)
def _clean_managed_locks():
    """Held models-root locks are process-lifetime — drop them between
    tests so tmp-path roots from prior tests never linger."""
    yield
    model_copy.release_managed_locks()


# =================================================================== gate

def test_gate_off_refuses_managed_copy(client, authed, tmp_path):
    """Default OFF: managed_copy fails closed at enqueue — 422, no job,
    no artifact, no filesystem writes at all."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    r = _import(client, authed, src, mode="managed_copy")
    assert r.status_code == 422
    assert "mode_not_supported" in r.text
    with client.app.state.session_factory() as s:
        assert s.execute(select(GenerationJob).where(
            GenerationJob.adapter_id == "ops.model.import")).scalars().all() == []
        assert s.execute(select(ModelArtifact).where(
            ModelArtifact.source_type == "import")).scalars().all() == []
    root = _models_root(client)
    assert not model_copy.imports_root(root).exists()


def test_gate_off_reference_still_works(client, authed, tmp_path):
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    res = _import_ok(client, authed, src)
    assert res["status"] in ("registered", "reused")
    root = _models_root(client)
    # Reference mode writes nothing — no imports dir, no managed lock.
    assert not model_copy.imports_root(root).exists()


def test_gate_disabled_midflight(client, authed, tmp_path):
    """Queued while enabled → flag flipped off before claim → the job
    fails mode_not_supported (claim-time AND execution-time gates)."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    engine = client.app.state.job_engine
    with client.app.state.session_factory() as s:
        job = engine.enqueue(
            s, project_id=None, adapter_id="ops.model.import",
            kind="model_import",
            request={"source_path": src, "license": {"name": "MIT"},
                     "mode": "managed_copy"},
            idempotency_key="gateoff:test")
        job_id = job.id
        s.commit()
    client.app.state.settings.enable_managed_copy = False
    engine.wake()
    job = _wait_job(client, job_id)
    assert job["status"] == "failed"
    assert "mode_not_supported" in (job.get("error") or "")
    root = _models_root(client)
    assert not model_copy.imports_root(root).exists()


def test_request_json_carries_mode_only_for_managed(client, authed, tmp_path):
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    _managed_ok(client, authed, src)
    src2 = _write(tmp_path, "ref.safetensors", _st_bytes(body=b"\x02" * 64))
    _import_ok(client, authed, src2)
    with client.app.state.session_factory() as s:
        jobs = {j.request_json.get("source_path"): j
                for j in s.execute(select(GenerationJob)).scalars()}
        assert jobs[src].request_json["mode"] == "managed_copy"
        assert "mode" not in jobs[src2].request_json


# ============================================================== happy path

def test_managed_copy_happy_path(client, authed, tmp_path):
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    res = _managed_ok(client, authed, src)
    assert res["status"] == "registered"
    assert res["managed"]["provenance"] == "published"
    sha = res["report"]["sha256"]
    dest = _imports(client) / f"{sha}.safetensors"
    assert dest.is_file()
    assert dest.read_bytes() == pathlib.Path(src).read_bytes()
    assert res["managed"]["dest_rel"] == dest.name
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, res["artifact_id"])
        assert art.source_type == "import"
        assert art.managed_by == "atomic"
        assert art.uri == str(dest)
        assert art.support_tier == "unknown"
        assert art.source_uri == src
        assert art.license_json["declared_by"] == "user"
        cp = art.import_report_json["copy"]
        assert cp["dest_rel"] == dest.name
        assert cp["provenance"] == "published"
        assert cp["expected_sha256"] == sha
        assert cp["verified_sha256"] == sha
        # Basename only — no absolute managed path in the report block.
        assert str(dest) not in json.dumps(cp)
    # Staging dir is empty after success — only .staging/ itself may exist.
    leftover = list(_staging(client).iterdir()) if _staging(client).is_dir() else []
    assert leftover == []


def test_source_immutable_through_copy(client, authed, tmp_path):
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    before = pathlib.Path(src).read_bytes()
    stat_before = pathlib.Path(src).stat()
    _managed_ok(client, authed, src)
    assert pathlib.Path(src).read_bytes() == before
    stat_after = pathlib.Path(src).stat()
    assert stat_after.st_mtime_ns == stat_before.st_mtime_ns
    assert stat_after.st_ino == stat_before.st_ino


def test_writes_confined_to_models_root(client, authed, tmp_path, settings):
    """Every new file lands under the models root — imports/, the managed
    lock, and nothing else anywhere in the data dir."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    root = _models_root(client)
    def snap():
        return {str(p.relative_to(settings.data_dir))
                for p in settings.data_dir.rglob("*") if p.is_file()}
    before = snap()
    _managed_ok(client, authed, src)
    new_files = snap() - before
    assert new_files  # the managed copy + lock landed somewhere
    for rel in new_files:
        abs_p = (settings.data_dir / rel).resolve()
        assert root.resolve() in abs_p.parents or abs_p == root.resolve()


def test_no_canonical_unlink_anywhere(client, authed, tmp_path, monkeypatch):
    """U-2b-2a physically deletes NOTHING outside .staging — spy every
    unlink primitive for the whole job."""
    _enable(client)
    unlinked: list[str] = []
    orig_pu = pathlib.Path.unlink
    orig_ou = os.unlink
    orig_or = os.remove

    def spy_pu(self, *a, **kw):
        unlinked.append(str(self))
        return orig_pu(self, *a, **kw)

    def spy_ou(p, *a, **kw):
        unlinked.append(str(p))
        return orig_ou(p, *a, **kw)

    def spy_or(p, *a, **kw):
        unlinked.append(str(p))
        return orig_or(p, *a, **kw)
    monkeypatch.setattr(pathlib.Path, "unlink", spy_pu)
    monkeypatch.setattr(os, "unlink", spy_ou)
    monkeypatch.setattr(os, "remove", spy_or)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    _managed_ok(client, authed, src)
    staging = _staging(client).resolve()
    imports = _imports(client).resolve()
    for p in unlinked:
        rp = pathlib.Path(p).resolve()
        # Only .staging children — never a canonical managed file.
        assert staging in rp.parents or staging == rp, f"unlinked {p}"
        assert imports != rp


def test_fsync_before_link(client, authed, tmp_path, monkeypatch):
    """Byte durability precedes publication: fsync(staging) → os.link."""
    _enable(client)
    calls: list[str] = []
    orig_fsync = os.fsync
    orig_link = os.link

    def fsync_spy(fd):
        calls.append("fsync")
        return orig_fsync(fd)

    def link_spy(src_, dst, *a, **kw):
        calls.append("link")
        return orig_link(src_, dst, *a, **kw)
    monkeypatch.setattr(model_copy.os, "fsync", fsync_spy)
    monkeypatch.setattr(model_copy.os, "link", link_spy)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    _managed_ok(client, authed, src)
    assert "fsync" in calls and "link" in calls
    assert calls.index("fsync") < calls.index("link")


# ============================================================ dedup/adopt

def test_dedup_shared_dest(client, authed, tmp_path):
    """Two distinct sources, identical content → ONE canonical file,
    two artifacts sharing uri (refcount derives from rows)."""
    _enable(client)
    data = _st_bytes()
    a = _managed_ok(client, authed, _write(tmp_path, "a.safetensors", data))
    b = _managed_ok(client, authed, _write(tmp_path, "b.safetensors", data))
    assert a["artifact_id"] != b["artifact_id"]
    dest = _imports(client) / f"{a['report']['sha256']}.safetensors"
    assert dest.is_file()
    with client.app.state.session_factory() as s:
        ra = s.get(ModelArtifact, a["artifact_id"])
        rb = s.get(ModelArtifact, b["artifact_id"])
        assert ra.uri == rb.uri == str(dest)


def test_adopt_preexisting_dest(client, authed, tmp_path):
    """A canonical dest already holding the right bytes is ADOPTED —
    verified by full re-hash, zero bytes copied."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    sha = _sha(src)
    dest = _imports(client) / f"{sha}.safetensors"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(pathlib.Path(src).read_bytes())
    res = _managed_ok(client, authed, src)
    assert res["managed"]["provenance"] == "adopted"
    assert res["report"]["copy"]["bytes_written"] == 0


def test_corrupt_dest_refused_never_overwritten(client, authed, tmp_path):
    """Canonical name, wrong content → managed_dest_corrupt and the
    existing file is byte-identical afterwards (no repair, no delete)."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    sha = _sha(src)
    dest = _imports(client) / f"{sha}.safetensors"
    dest.parent.mkdir(parents=True, exist_ok=True)
    foreign = b"\xde\xad" * 64
    dest.write_bytes(foreign)
    r = _import(client, authed, src, mode="managed_copy")
    assert r.status_code == 202
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "managed_dest_corrupt" in (job.get("error") or "")
    assert dest.read_bytes() == foreign
    with client.app.state.session_factory() as s:
        assert s.execute(select(ModelArtifact).where(
            ModelArtifact.source_type == "import")).scalars().all() == []


def test_link_fileexists_falls_to_adoption(client, authed, tmp_path,
                                         monkeypatch):
    """os.link FileExistsError (lost race) → existing dest verified and
    adopted — the only collision outcome."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    sha = _sha(src)
    dest = _imports(client) / f"{sha}.safetensors"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(pathlib.Path(src).read_bytes())

    def boom(a, b, *args, **kw):
        raise FileExistsError(errno.EEXIST, "exists")
    monkeypatch.setattr(model_copy.os, "link", boom)
    res = _managed_ok(client, authed, src)
    assert res["managed"]["provenance"] == "adopted"


# ================================================================ refusals

def test_link_unsupported_hard_refusal(client, authed, tmp_path, monkeypatch):
    """No hard-link capability → managed_copy_unsupported; no canonical
    write attempted; this attempt's staging is cleaned up."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())

    def nope(a, b, *args, **kw):
        raise OSError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(model_copy.os, "link", nope)
    r = _import(client, authed, src, mode="managed_copy")
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "managed_copy_unsupported" in (job.get("error") or "")
    sha = _sha(src)
    assert not (_imports(client) / f"{sha}.safetensors").exists()
    leftover = list(_staging(client).iterdir()) if _staging(client).is_dir() else []
    assert leftover == []


def test_disk_full_preflight(client, authed, tmp_path, monkeypatch):
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    monkeypatch.setattr(model_files, "disk_free_bytes", lambda p: 1)
    r = _import(client, authed, src, mode="managed_copy")
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "disk_full" in (job.get("error") or "")
    sha = _sha(src)
    assert not (_imports(client) / f"{sha}.safetensors").exists()
    leftover = list(_staging(client).iterdir()) if _staging(client).is_dir() else []
    assert leftover == []


def test_dest_ancestor_reparse_refused(client, authed, tmp_path, monkeypatch):
    """A reparse point on the managed-storage chain refuses the copy —
    lexical walk mirrors the source-path defense."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    root = _models_root(client)
    real = model_import._is_reparse

    def spy(p, *, is_dir):
        if p == root / "imports":
            return True
        return real(p, is_dir=is_dir)
    monkeypatch.setattr(model_import, "_is_reparse", spy)
    r = _import(client, authed, src, mode="managed_copy")
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "managed_target_escape" in (job.get("error") or "")


def test_dest_symlink_refused(client, authed, tmp_path, monkeypatch):
    """A symlink AT the canonical name refuses — never followed, never
    overwritten. (Monkeypatched — Windows symlink creation needs rights.)"""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    sha = _sha(src)
    dest = _imports(client) / f"{sha}.safetensors"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"x")
    real_islink = pathlib.Path.is_symlink
    monkeypatch.setattr(
        pathlib.Path, "is_symlink",
        lambda self: str(self) == str(dest) or real_islink(self))
    r = _import(client, authed, src, mode="managed_copy")
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "managed_target_escape" in (job.get("error") or "")
    assert dest.read_bytes() == b"x"  # untouched


def test_path_changed_before_copy(client, authed, tmp_path, monkeypatch):
    """The copy handle's fstat must re-bind to the inspected identity —
    a swapped source between inspect and copy refuses path_changed."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    rp = model_import.validate_source_path(src)
    rep = model_import.inspect_file(rp)
    rep.registration_stat = dict(rep.registration_stat)
    rep.registration_stat["size"] += 1   # fabricated drift
    with pytest.raises(ImportRefused) as e, \
            client.app.state.session_factory() as s:
        model_copy.managed_copy(
            s, client.app.state.settings,
            job_id="u1", src_rp=rp, report=rep)
    assert e.value.code == "path_changed"


def test_source_mutated_mid_copy(client, authed, tmp_path, monkeypatch):
    """Post-copy fstat re-check catches identity drift during the copy."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    rp = model_import.validate_source_path(src)
    rep = model_import.inspect_file(rp)
    real = model_import._stat_identity
    calls = {"n": 0}

    def drift(st):
        ident = real(st)
        calls["n"] += 1
        if calls["n"] >= 2:      # 1st = pre-copy check passes; 2nd drifts
            ident = dict(ident)
            ident["size"] += 1
        return ident
    monkeypatch.setattr(model_import, "_stat_identity", drift)
    with pytest.raises(ImportRefused) as e, \
            client.app.state.session_factory() as s:
        model_copy.managed_copy(
            s, client.app.state.settings,
            job_id="u2", src_rp=rp, report=rep)
    assert e.value.code == "path_changed"


def test_copy_mismatch_refused(client, authed, tmp_path, monkeypatch):
    """A copy whose bytes don't hash to the inspected sha refuses and the
    staged bytes are cleaned — copy_mismatch never publishes."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())

    class BadHasher:
        def update(self, b):
            pass

        def hexdigest(self):
            return "0" * 64
    monkeypatch.setattr(model_copy, "_new_hasher", lambda: BadHasher())
    r = _import(client, authed, src, mode="managed_copy")
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "copy_mismatch" in (job.get("error") or "")
    sha = _sha(src)
    assert not (_imports(client) / f"{sha}.safetensors").exists()
    leftover = list(_staging(client).iterdir()) if _staging(client).is_dir() else []
    assert leftover == []


# =============================================================== staging

def test_staging_foreign_objects_untouched(client, authed, tmp_path):
    """Foreign files under .staging/ and imports/ are never deleted —
    name or sidecar alone confers zero deletion authority."""
    _enable(client)
    sd = _staging(client)
    sd.mkdir(parents=True, exist_ok=True)
    foreign = {
        "deadjob-aaaa1111.part": b"partial-bytes",
        "deadjob-aaaa1111.part.json": b'{"job_id":"deadjob","nonce":"aaaa1111"}',
        "random.bin": b"not ours at all",
    }
    for name, data in foreign.items():
        (sd / name).write_bytes(data)
    imp_file = _imports(client) / "dropped-here.safetensors"
    imp_file.write_bytes(_st_bytes())   # non-canonical name — foreign
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    _managed_ok(client, authed, src)
    for name, data in foreign.items():
        assert (sd / name).read_bytes() == data
    assert imp_file.read_bytes() == _st_bytes()


def test_staging_replaced_object_left_untouched(client, authed, tmp_path,
                                                monkeypatch):
    """A staging object replaced mid-run is ambiguous → cleanup must
    leave it (fail-closed), not delete a foreign file."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    rp = model_import.validate_source_path(src)
    rep = model_import.inspect_file(rp)
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"REPLACED-FOREIGN-CONTENT")

    def swapped_link(a, b, *args, **kw):
        # Swap the object at the staging name for a different inode in
        # the post-write window (the held fd is closed by publish time —
        # the realistic replacement surface), then refuse the link.
        os.replace(replacement, a)
        raise OSError(errno.EPERM, "no links")
    monkeypatch.setattr(model_copy.os, "link", swapped_link)
    with pytest.raises(ImportRefused) as e, \
            client.app.state.session_factory() as s:
        model_copy.managed_copy(
            s, client.app.state.settings,
            job_id="u3", src_rp=rp, report=rep)
    assert e.value.code == "managed_copy_unsupported"
    # The foreign object at the staging name survived — identity mismatch
    # refused the deletion.
    survivors = [p for p in _staging(client).iterdir()
                 if p.name.endswith(".part")]
    assert len(survivors) == 1
    assert survivors[0].read_bytes() == b"REPLACED-FOREIGN-CONTENT"


def test_cancel_during_copy(client, authed, tmp_path):
    """JobCancelled mid-stream → propagates; this attempt's staging is
    cleaned (verified-identity unlink), nothing left behind."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    rp = model_import.validate_source_path(src)
    rep = model_import.inspect_file(rp)
    from app.adapters.base import JobCancelled
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise JobCancelled()
    with pytest.raises(JobCancelled), \
            client.app.state.session_factory() as s:
        model_copy.managed_copy(
            s, client.app.state.settings,
            job_id="u4", src_rp=rp, report=rep, cancel_check=boom)
    leftover = list(_staging(client).iterdir()) if _staging(client).is_dir() else []
    assert leftover == []


# ================================================================ recovery

def test_requeue_idempotent_managed(client, authed, tmp_path):
    """Crash mid-run → reconcile requeues → the managed job completes
    and the published dest is exactly right (convergent pipeline)."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    engine = client.app.state.job_engine
    with client.app.state.session_factory() as s:
        job = engine.enqueue(
            s, project_id=None, adapter_id="ops.model.import",
            kind="model_import",
            request={"source_path": src, "license": {"name": "MIT"},
                     "mode": "managed_copy"},
            idempotency_key="crash:managed")
        job.status = "running"
        job_id = job.id
        s.commit()
        assert engine.reconcile_interrupted(s) == 1
        s.commit()
        assert s.get(GenerationJob, job_id).request_json["mode"] == "managed_copy"
    engine.wake()
    done = _wait_job(client, job_id)
    assert done["status"] == "succeeded"
    sha = _sha(src)
    assert (_imports(client) / f"{sha}.safetensors").is_file()
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact,
                    done["result_json"]["artifact_id"])
        assert art.managed_by == "atomic"


def test_db_failure_after_publish_orphan_adoptable(client, authed,
                                                   tmp_path, monkeypatch):
    """Commit failure after publish → the verified dest remains and is
    adopted by the retry (the file is truth before commit; the DB after).
    """
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    real = model_artifacts.register_import

    def boom(*a, **kw):
        from sqlalchemy.exc import OperationalError
        raise OperationalError("db gone", None, Exception("x"))
    monkeypatch.setattr(model_artifacts, "register_import", boom)
    r = _import(client, authed, src, mode="managed_copy")
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "db_error" in (job.get("error") or "")
    sha = _sha(src)
    dest = _imports(client) / f"{sha}.safetensors"
    assert dest.is_file()   # published orphan — adoptable, not a loss
    monkeypatch.setattr(model_artifacts, "register_import", real)
    res = _managed_ok(client, authed, src)
    assert res["managed"]["provenance"] == "adopted"
    assert res["report"]["copy"]["bytes_written"] == 0


# ==================================================================== lock

def test_models_root_lock_contention(client, authed, tmp_path):
    """A second backend owning the models root blocks managed copies —
    fail closed, no writes."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    root = _models_root(client)
    root.mkdir(parents=True, exist_ok=True)
    foreign = acquire_data_dir_lock(root)   # stands in for another backend
    try:
        r = _import(client, authed, src, mode="managed_copy")
        job = _wait_job(client, r.json()["job_id"])
        assert job["status"] == "failed"
        assert "managed_copy_unsupported" in (job.get("error") or "")
        assert not model_copy.imports_root(root).exists()
    finally:
        foreign.release()


def test_managed_lock_held_after_copy(client, authed, tmp_path):
    """Custom-root lock persists for the process lifetime after the copy."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    root = _models_root(client)
    _managed_ok(client, authed, src)
    assert root.resolve() in model_copy._MANAGED_LOCKS
    model_copy.release_managed_locks()
    assert model_copy._MANAGED_LOCKS == {}
    # Released → re-acquirable (same-process proof of a real release).
    rel = acquire_data_dir_lock(root)
    rel.release()


def test_custom_models_dir(client, authed, tmp_path):
    """models_dir app setting → managed copy lands under the custom root
    and that root is independently locked."""
    _enable(client)
    custom = tmp_path / "custom_models"
    with client.app.state.session_factory() as s:
        model_files.set_models_root(s, str(custom))
        s.commit()
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    res = _managed_ok(client, authed, src)
    sha = res["report"]["sha256"]
    dest = custom / "imports" / f"{sha}.safetensors"
    assert dest.is_file()
    assert custom.resolve() in model_copy._MANAGED_LOCKS
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, res["artifact_id"])
        assert art.uri == str(dest)


# ================================================================= release

def test_release_happy(client, authed, tmp_path):
    """Release detaches managed state — row goes external, uri NULL,
    provenance stamped; the managed bytes are NEVER touched."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    res = _managed_ok(client, authed, src)
    aid = res["artifact_id"]
    dest = _imports(client) / res["managed"]["dest_rel"]
    r = _release(client, authed, aid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"artifact_id": aid, "released": True, "already": False}
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, aid)
        assert art.managed_by == "external"
        assert art.uri is None
        assert art.source_uri == src          # provenance retained
        rel = art.import_report_json["release"]
        assert rel["managed_copy_released_at"]
        assert rel["last_uri_rel"] == res["managed"]["dest_rel"]
    assert dest.is_file()                    # bytes left exactly as-is


def test_release_idempotent_retry(client, authed, tmp_path):
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    aid = _managed_ok(client, authed, src)["artifact_id"]
    assert _release(client, authed, aid).json()["already"] is False
    again = _release(client, authed, aid)
    assert again.status_code == 200
    assert again.json()["already"] is True


def test_release_not_gated(client, authed, tmp_path):
    """Disabling the feature can never strand a managed artifact —
    release stays available with the gate off."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    aid = _managed_ok(client, authed, src)["artifact_id"]
    client.app.state.settings.enable_managed_copy = False
    r = _release(client, authed, aid)
    assert r.status_code == 200
    assert r.json()["released"] is True


def test_release_refusals(client, authed, tmp_path):
    assert _release(client, authed, "import:nope").status_code == 404
    with client.app.state.session_factory() as s:
        cat = s.execute(select(ModelArtifact).where(
            ModelArtifact.source_type == "catalog")).scalars().first()
        assert cat is not None
        r = _release(client, authed, cat.artifact_id)
        assert r.status_code == 422


def test_release_missing_managed_file(client, authed, tmp_path):
    """Externally-deleted managed file → release still detaches cleanly
    (existence is probed live; ownership is the row)."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    res = _managed_ok(client, authed, src)
    aid = res["artifact_id"]
    dest = _imports(client) / res["managed"]["dest_rel"]
    dest.unlink()
    r = client.get(f"/api/models/artifacts/{aid}", headers=authed)
    assert r.json()["managed_copy"]["present"] is False
    assert _release(client, authed, aid).status_code == 200
    with client.app.state.session_factory() as s:
        assert s.get(ModelArtifact, aid).uri is None


def test_managed_detail_fields(client, authed, tmp_path):
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    res = _managed_ok(client, authed, src)
    r = client.get(f"/api/models/artifacts/{res['artifact_id']}",
                   headers=authed)
    assert r.status_code == 200
    mc = r.json()["managed_copy"]
    assert mc["present"] is True
    assert mc["dest_rel"] == res["managed"]["dest_rel"]
    assert mc["provenance"] == "published"
    assert mc["published_at"]
    # Reference artifacts carry no managed block.
    src2 = _write(tmp_path, "ref.safetensors", _st_bytes(body=b"\x03" * 32))
    aid2 = _import_ok(client, authed, src2)["artifact_id"]
    r2 = client.get(f"/api/models/artifacts/{aid2}", headers=authed)
    assert r2.json()["managed_copy"] is None


# ================================================================= privacy

def test_managed_copy_privacy(client, authed, tmp_path):
    """No surface carries the source path; the only absolute path
    anywhere is the app-owned managed uri."""
    _enable(client)
    secret_dir = tmp_path / "USERSECRETDIR"
    secret_dir.mkdir()
    src = _write(secret_dir, "priv.safetensors", _st_bytes())
    res = _managed_ok(client, authed, src)
    # result_json: report + managed block — basename only.
    assert src not in json.dumps(res)
    assert "USERSECRETDIR" not in json.dumps(res)
    for j in client.get("/api/jobs").json():
        assert src not in json.dumps(j)
        assert "USERSECRETDIR" not in json.dumps(j)
    with client.app.state.session_factory() as s:
        from app.db.models import AuditEvent
        evts = s.execute(select(AuditEvent).where(
            AuditEvent.event_type == "model_import.registered")).scalars().all()
        assert len(evts) == 1
        payload = json.dumps(evts[0].payload_json)
        assert src not in payload and "USERSECRETDIR" not in payload
    # List view: uri is the managed path (app-owned — safe); source_uri absent.
    lst = client.get("/api/models/artifacts", headers=authed).json()
    row = next(a for a in lst if a["artifact_id"] == res["artifact_id"])
    assert row["uri"] == str(_imports(client) / res["managed"]["dest_rel"])
    assert "source_uri" not in row
    assert "USERSECRETDIR" not in json.dumps(row)


def test_managed_error_never_leaks_path(client, authed, tmp_path, monkeypatch):
    """Failure paths surface sanitized codes only — the source path never
    reaches job.error even through wrapped exceptions."""
    _enable(client)
    src = _write(tmp_path, "LEAKPROBEDIR.safetensors", _st_bytes())
    monkeypatch.setattr(model_files, "disk_free_bytes",
                        lambda p: (_ for _ in ()).throw(
                            OSError(f"boom {src}")))
    r = _import(client, authed, src, mode="managed_copy")
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert src not in json.dumps(job)


# ============================================================ execution

def test_execution_gates_unchanged_managed(client, authed, tmp_path):
    """Managed LoRA: role=lora row in managed storage, but check_loras
    still refuses — unknown tier + not a catalog key. Managed bytes
    confer zero execution rights."""
    from app.domain import model_catalog
    from app.domain.commands import CommandError
    from app.domain.lora_resolve import check_loras
    _enable(client)
    src = _write(tmp_path, "adapter.safetensors", _lora_st_bytes())
    aid = _managed_ok(client, authed, src, role_hint="lora")["artifact_id"]
    base = model_catalog.get("sd15.fp16")
    with client.app.state.session_factory() as s, \
            pytest.raises(CommandError) as e:
        check_loras(s, None, [{"key": aid, "strength": 1.0}],
                    base, shot=None)
    assert e.value.status_code == 422
    with client.app.state.session_factory() as s:
        assert s.execute(select(ModelInstall)).scalars().all() == []


def test_serialized_same_content_imports(client, authed, tmp_path):
    """Two managed imports of identical content from different sources —
    the ops lane serializes them; one dest file, two artifacts."""
    _enable(client)
    data = _st_bytes()
    p1 = _write(tmp_path, "s1.safetensors", data)
    p2 = _write(tmp_path, "s2.safetensors", data)
    r1 = _import(client, authed, p1, mode="managed_copy")
    r2 = _import(client, authed, p2, mode="managed_copy")
    j1 = _wait_job(client, r1.json()["job_id"])
    j2 = _wait_job(client, r2.json()["job_id"])
    assert j1["status"] == j2["status"] == "succeeded"
    results = [j1["result_json"], j2["result_json"]]
    assert {r["artifact_id"] for r in results} != set() and \
        len({r["artifact_id"] for r in results}) == 2
    # One canonical file; the second job adopted or published-then-
    # deduped — either way exactly one dest exists.
    sha = _sha(p1)
    dest = _imports(client) / f"{sha}.safetensors"
    assert dest.is_file()
    provs = {r["managed"]["provenance"] for r in results}
    assert provs <= {"published", "adopted"}


def test_gguf_managed_copy(client, authed, tmp_path):
    _enable(client)
    src = _write(tmp_path, "model.gguf", _gguf_bytes())
    res = _managed_ok(client, authed, src)
    dest = _imports(client) / f"{res['report']['sha256']}.gguf"
    assert dest.is_file()
    assert res["managed"]["dest_rel"].endswith(".gguf")


def test_reference_mode_unaffected_when_flag_on(client, authed, tmp_path):
    """Gate ON does not change reference semantics — external row,
    uri NULL, and no managed-storage writes at all."""
    _enable(client)
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    res = _import_ok(client, authed, src)
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, res["artifact_id"])
        assert art.managed_by == "external" and art.uri is None
    root = _models_root(client)
    assert not model_copy.imports_root(root).exists()
    assert not (root / ".atomic.lock").exists()
