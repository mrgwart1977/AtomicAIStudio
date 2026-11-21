"""U-2a — ops.model.import: secure reference-only registration.

Covers the full contract matrix with synthetic offline fixtures — real
safetensors/GGUF byte structures built in-memory. No network, no real
weights, no inference.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import struct
import time

import pytest
from sqlalchemy import select

from app.db.models import GenerationJob, ModelArtifact, ModelInstall
from app.domain import model_artifacts, model_catalog, model_import
from app.domain.model_import import ImportRefused

# ---------------------------------------------------------------- fixtures

def _st_bytes(tensors: dict[str, dict] | None = None,
              metadata: dict | None = None,
              body: bytes = b"\x00" * 64) -> bytes:
    """Build a real safetensors file: 8-byte LE len + JSON header + body."""
    tensors = tensors if tensors is not None else {
        "model.diffusion_model.input_blocks.0.0.weight": {
            "dtype": "F16", "shape": [8, 8],
            "data_offsets": [0, len(body)],
        },
        "first_stage_model.encoder.conv_in.weight": {
            "dtype": "F16", "shape": [4], "data_offsets": [0, 16]},
        "cond_stage_model.transformer.weight": {
            "dtype": "F16", "shape": [4], "data_offsets": [16, 32]},
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


def _gguf_bytes(arch: str = "llama", name: str = "test-model") -> bytes:
    """Minimal real GGUF v3: magic, version, tensor/kv counts, 2 kv."""
    def gstr(s: str) -> bytes:
        b = s.encode()
        return struct.pack("<Q", len(b)) + b
    kv = (
        gstr("general.architecture") + struct.pack("<I", 8) + gstr(arch) +
        gstr("general.name") + struct.pack("<I", 8) + gstr(name)
    )
    return b"GGUF" + struct.pack("<IQQ", 3, 0, 2) + kv + b"\x00" * 64


def _write(tmp_path, name: str, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _import(client, authed, path, license=None, **kw):
    body = {"path": path,
            "license": {"name": "MIT"} if license is None else license,
            **kw}
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


# ---------------------------------------------------------------- migration

def test_migration_adds_columns(tmp_path):
    from app.db.session import create_engine_for
    from app.main import run_migrations
    url = f"sqlite:///{tmp_path}/mig.db"
    run_migrations(url)
    engine = create_engine_for(url)
    with engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(model_artifacts)")}
    engine.dispose()
    assert {"source_uri", "imported_at", "import_report_json"} <= cols


def test_existing_0027_db_upgrades(tmp_path):
    """A 0027 database with artifact rows upgrades to 0028: new columns
    appear with NULL/empty defaults and existing rows survive intact.
    (ORM always maps HEAD — a raw row is seeded at 0027 level.)"""
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    from app.db.session import create_engine_for, session_factory
    from app.main import _alembic_script_location, run_migrations

    url = f"sqlite:///{tmp_path}/mig.db"
    run_migrations(url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_alembic_script_location()))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.downgrade(cfg, "0027_u1")
    engine = create_engine_for(url)
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO model_artifacts "
            "(artifact_id, display_name, kind, arch, family, modality, fmt,"
            " role, version, source_type, managed_by, byte_size,"
            " support_tier, enabled, license_json, tags_json, meta_json,"
            " created_at, updated_at) "
            "VALUES ('catalog:test.row', 'existing row', 'model', 'sd1.5',"
            " 'sd1.5', 'image', 'gguf', 'diffusion', '1.5', 'catalog',"
            " 'atomic', 0, 'production', 1, '{}', '[]', '{}',"
            " '2024-01-01', '2024-01-01')")
    engine.dispose()
    run_migrations(url)  # 0027 → 0028
    engine = create_engine_for(url)
    sf = session_factory(engine)
    with sf() as s:
        art = s.get(ModelArtifact, "catalog:test.row")
        assert art is not None and art.display_name == "existing row"
        assert art.source_uri is None and art.imported_at is None
        assert art.import_report_json in (None, {})
    engine.dispose()


def test_downgrade_drops_only_new_columns(tmp_path):
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig

    from app.db.session import create_engine_for
    from app.main import _alembic_script_location, run_migrations

    url = f"sqlite:///{tmp_path}/mig.db"
    run_migrations(url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_alembic_script_location()))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.downgrade(cfg, "0027_u1")
    engine = create_engine_for(url)
    with engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(model_artifacts)")}
    engine.dispose()
    assert {"source_uri", "imported_at", "import_report_json"}.isdisjoint(cols)
    assert "artifact_id" in cols


# ---------------------------------------------------------------- API + job

def test_import_happy_path(client, authed, tmp_path):
    """Valid safetensors: 202 → job succeeds → artifact registered with
    real sha, import provenance, and the unknown execution tier."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    res = _import_ok(client, authed, src)
    assert res["status"] == "registered"
    assert res["report"]["fmt"] == "safetensors"
    assert res["report"]["sha256"] == hashlib.sha256(
        pathlib.Path(src).read_bytes()).hexdigest()
    aid = res["artifact_id"]
    assert aid.startswith("import:")
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, aid)
        assert art.source_type == "import"
        assert art.managed_by == "external"
        assert art.support_tier == "unknown"
        assert art.arch == "sd1.5"  # signature suggestion
        assert art.content_sha256 == res["report"]["sha256"]
        assert art.source_uri == src
        assert art.uri is None  # path never in the list-view field
        assert art.imported_at is not None
        assert art.import_report_json["registration_stat"]["size"] > 0


def test_import_gguf(client, authed, tmp_path):
    src = _write(tmp_path, "model.gguf", _gguf_bytes())
    res = _import_ok(client, authed, src)
    assert res["report"]["fmt"] == "gguf"
    assert res["report"]["suggested_arch"] == "llama"


def test_import_lora_signature(client, authed, tmp_path):
    src = _write(tmp_path, "adapter.safetensors", _lora_st_bytes())
    res = _import_ok(client, authed, src, role_hint="lora",
                     family="sd1.5")
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, res["artifact_id"])
        assert art.kind == "adapter" and art.role == "lora"
        assert art.family == "sd1.5"  # user declaration preserved
        assert art.meta_json["suggestions"]["role"] == "lora"


def test_import_lora_signature_no_hint(client, authed, tmp_path):
    """Signature-only lora detection (no role_hint) → kind=adapter,
    role=lora — not a doubled 'lora:lora'."""
    src = _write(tmp_path, "adapter.safetensors", _lora_st_bytes())
    res = _import_ok(client, authed, src)
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, res["artifact_id"])
        assert art.kind == "adapter" and art.role == "lora"


def test_idempotent_reimport_same_content(client, authed, tmp_path):
    """Repeat import of unchanged file → job runs full hash → existing
    artifact returned with reused=true."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    first = _import_ok(client, authed, src)
    second = _import_ok(client, authed, src)
    assert second["status"] == "reused"
    assert second["artifact_id"] == first["artifact_id"]
    with client.app.state.session_factory() as s:
        assert s.execute(select(ModelArtifact).where(
            ModelArtifact.source_uri == src)).scalars().all().__len__() == 1


def test_changed_source_creates_new_artifact(client, authed, tmp_path):
    """Source content changed → new artifact; the old row is untouched
    (no silent checksum update)."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    first = _import_ok(client, authed, src)
    old_sha = first["report"]["sha256"]
    time.sleep(0.01)
    (tmp_path / "weights.safetensors").write_bytes(
        _st_bytes(body=b"\x7f" * 128))
    second = _import_ok(client, authed, src)
    assert second["status"] == "registered"
    assert second["artifact_id"] != first["artifact_id"]
    with client.app.state.session_factory() as s:
        old = s.get(ModelArtifact, first["artifact_id"])
        assert old.content_sha256 == old_sha  # original sha preserved


def test_distinct_sources_same_content(client, authed, tmp_path):
    """Same bytes at two paths → two artifacts, suffix allocation."""
    data = _st_bytes()
    a = _import_ok(client, authed, _write(tmp_path, "a.safetensors", data))
    b = _import_ok(client, authed, _write(tmp_path, "b.safetensors", data))
    assert a["artifact_id"] != b["artifact_id"]
    assert {a["artifact_id"], b["artifact_id"]} == {
        f"import:{a['report']['sha256'][:12]}",
        f"import:{a['report']['sha256'][:12]}:2"}


def test_detail_endpoint_returns_source_and_report(client, authed, tmp_path):
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    aid = _import_ok(client, authed, src)["artifact_id"]
    r = client.get(f"/api/models/artifacts/{aid}", headers=authed)
    assert r.status_code == 200
    body = r.json()
    assert body["source_uri"] == src
    assert body["import_report"]["fmt"] == "safetensors"
    assert body["install"]["status"] == "registered"
    r2 = client.get("/api/models/artifacts/nope", headers=authed)
    assert r2.status_code == 404


def test_source_path_not_in_list_view(client, authed, tmp_path):
    src = _write(tmp_path, "secret_name.safetensors", _st_bytes())
    _import_ok(client, authed, src)
    r = client.get("/api/models/artifacts", headers=authed)
    body = json.dumps(r.json())
    # Full path and its directory components must not appear; the
    # basename is the user-visible display name (the complete
    # source_uri only surfaces on the authenticated detail view).
    assert src not in body and "source_uri" not in body
    assert "pytest-of-gwart" not in body


def test_source_path_not_in_job_listing(client, authed, tmp_path):
    src = _write(tmp_path, "secret_path.safetensors", _st_bytes())
    _import_ok(client, authed, src)
    for j in client.get("/api/jobs").json():
        if j["kind"] == "model_import":
            dumped = json.dumps(j)
            assert src not in dumped and "secret_path" not in dumped


def test_source_path_not_in_audit(client, authed, tmp_path):
    src = _write(tmp_path, "audit_secret.safetensors", _st_bytes())
    _import_ok(client, authed, src)
    with client.app.state.session_factory() as s:
        from app.db.models import AuditEvent
        evts = s.execute(select(AuditEvent).where(
            AuditEvent.event_type == "model_import.registered")).scalars().all()
        assert len(evts) == 1
        assert src not in json.dumps(evts[0].payload_json)
        assert "secret" not in json.dumps(evts[0].payload_json)


def test_409_in_flight_duplicate(client, authed, tmp_path):
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    r1 = _import(client, authed, src)
    assert r1.status_code == 202
    r2 = _import(client, authed, src)
    if r2.status_code == 202:  # job may have finished already
        _wait_job(client, r2.json()["job_id"])
    else:
        assert r2.status_code == 409
    _wait_job(client, r1.json()["job_id"])


# ---------------------------------------------------------------- path safety

def test_refusals(client, authed, tmp_path):
    assert _import(client, authed, "relative/path.safetensors").status_code == 422
    assert "path_not_absolute" in _import(
        client, authed, "rel.safetensors").json()["detail"]
    assert "path_not_found" in _import(
        client, authed, str(tmp_path / "missing.safetensors")).json()["detail"]
    assert "path_not_file" in _import(
        client, authed, str(tmp_path)).json()["detail"]
    assert "license_required" in _import(
        client, authed, _write(tmp_path, "x.safetensors", _st_bytes()),
        license={}).json()["detail"]


def test_symlink_refused(client, authed, tmp_path, monkeypatch):
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    monkeypatch.setattr(pathlib.Path, "is_symlink",
                        lambda self: str(self) == src)
    with pytest.raises(ImportRefused) as e:
        model_import.validate_source_path(src)
    assert e.value.code == "path_is_link"


def test_junction_ancestor_refused(client, authed, tmp_path, monkeypatch):
    """A junction anywhere up the ancestor chain refuses the import."""
    real = tmp_path / "real"
    real.mkdir()
    src = real / "weights.safetensors"
    src.write_bytes(_st_bytes())
    orig = pathlib.Path.is_junction
    monkeypatch.setattr(
        pathlib.Path, "is_junction",
        lambda self: str(self) == str(tmp_path) or orig(self))
    with pytest.raises(ImportRefused) as e:
        model_import.validate_source_path(str(src))
    assert e.value.code == "path_is_link"


def test_reparse_attribute_refused(client, authed, tmp_path, monkeypatch):
    """st_file_attributes REPARSE bit refuses even when is_symlink/
    is_junction say no — covers non-junction reparse types."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    st = pathlib.Path(src).lstat()

    class FakeStat:
        st_file_attributes = 0x400
        def __getattr__(self, name):
            return getattr(st, name)
    monkeypatch.setattr(pathlib.Path, "lstat",
                        lambda self, **kw: FakeStat())
    with pytest.raises(ImportRefused) as e:
        model_import.validate_source_path(src)
    assert e.value.code == "path_is_link"


def test_path_changed_during_inspect(tmp_path, monkeypatch):
    """fstat identity drift during hashing → path_changed refusal."""
    src = tmp_path / "weights.safetensors"
    src.write_bytes(_st_bytes())
    orig_fstat = model_import.os.fstat
    calls = {"n": 0}

    def drifting(fd):
        st = orig_fstat(fd)
        calls["n"] += 1
        if calls["n"] >= 2:
            return type(st)(tuple(list(st[:6]) + [st.st_size + 1]
                                  + list(st[7:])))
        return st
    monkeypatch.setattr(model_import.os, "fstat", drifting)
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code == "path_changed"


# ---------------------------------------------------------------- parsers

def test_malformed_safetensors(tmp_path):
    src = tmp_path / "bad.safetensors"
    src.write_bytes(b"\x10\x00\x00\x00\x00\x00\x00\x00{not json")
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code == "malformed_safetensors"


def test_safetensors_header_too_large(tmp_path):
    src = tmp_path / "huge.safetensors"
    n = model_import.MAX_ST_HEADER + 1
    src.write_bytes(n.to_bytes(8, "little") + b"{" + b" " * 100)
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code == "malformed_safetensors"


def test_tensor_offsets_out_of_range(tmp_path):
    body = b"\x00" * 16
    src = tmp_path / "offs.safetensors"
    src.write_bytes(_st_bytes({
        "x.weight": {"dtype": "F16", "shape": [4],
                     "data_offsets": [0, 99999]}}, body=body))
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code == "tensor_offsets_invalid"


def test_malformed_gguf(tmp_path):
    src = tmp_path / "bad.gguf"
    src.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 0, 1) + b"\x02\x00")
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code in ("malformed_gguf", "metadata_too_large")


def test_unknown_format_refused(client, authed, tmp_path):
    """Random/unknown content gets NO registration — a structured job
    failure, never a successful row."""
    src = _write(tmp_path, "blob.bin", os.urandom(64) + b"PK" + os.urandom(32))
    r = _import(client, authed, src)
    assert r.status_code == 202
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "unsupported_format" in (
        job.get("error") or "") + json.dumps(job.get("result_json") or {})
    with client.app.state.session_factory() as s:
        assert s.execute(select(ModelArtifact).where(
            ModelArtifact.source_type == "import")).scalars().all() == []


def test_no_modelinstall_created(client, authed, tmp_path):
    """Imports never create model_installs rows — that table stays
    catalog-download-specific."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    _import_ok(client, authed, src)
    with client.app.state.session_factory() as s:
        assert s.execute(select(ModelInstall)).scalars().all() == []


def test_generation_gates_unchanged(client, authed, tmp_path):
    """Imported LoRA → role=lora row, but check_loras still refuses:
    unknown tier + not a catalog key."""
    from app.domain.commands import CommandError
    from app.domain.lora_resolve import check_loras
    src = _write(tmp_path, "adapter.safetensors", _lora_st_bytes())
    aid = _import_ok(client, authed, src, role_hint="lora")["artifact_id"]
    base = model_catalog.get("sd15.fp16")
    with client.app.state.session_factory() as s, pytest.raises(CommandError) as e:
        check_loras(s, None, [{"key": aid, "strength": 1.0}],
                    base, shot=None)
    assert e.value.status_code == 422


def test_catalog_sync_untouched_by_imports(client, authed, tmp_path):
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    aid = _import_ok(client, authed, src)["artifact_id"]
    with client.app.state.session_factory() as s:
        before = s.get(ModelArtifact, aid).display_name
        model_artifacts.sync_catalog(s)
        s.commit()
        art = s.get(ModelArtifact, aid)
        assert art.display_name == before
        assert art.source_type == "import"


def test_no_filesystem_writes(client, authed, tmp_path, settings):
    """Reference mode: zero writes anywhere under the data dir; source
    file byte-identical."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    before_bytes = pathlib.Path(src).read_bytes()
    def snap():
        return {str(p.relative_to(settings.data_dir))
                for p in settings.data_dir.rglob("*") if p.is_file()}
    before = snap()
    _import_ok(client, authed, src)
    assert snap() == before
    assert pathlib.Path(src).read_bytes() == before_bytes


def test_backup_scope_unchanged():
    from app.domain.backup import ENTITY_TABLES
    assert "model_artifacts" not in ENTITY_TABLES


def test_identity_exhaustion(client, authed, tmp_path):
    """Occupied deterministic ids by foreign sources → bounded refusal,
    never overwrite."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    sha = hashlib.sha256(pathlib.Path(src).read_bytes()).hexdigest()
    with client.app.state.session_factory() as s:
        for i in range(10):
            aid = f"import:{sha[:12]}" + ("" if i == 0 else f":{i + 1}")
            s.add(ModelArtifact(
                artifact_id=aid, display_name="x", kind="model",
                arch="unknown", family="", modality="image", fmt="safetensors",
                role="", version="", source_type="import",
                managed_by="external", uri=None, content_sha256=sha,
                byte_size=1, license_json={}, support_tier="unknown",
                enabled=True, tags_json=[], meta_json={},
                source_uri=f"/other/{i}", install_id=None))
        s.commit()
    r = _import(client, authed, src)
    assert r.status_code == 202
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    assert "identity_exhausted" in (job.get("error") or "")
    # no row overwritten — the 10 planted rows are all still there
    with client.app.state.session_factory() as s:
        assert s.execute(select(ModelArtifact).where(
            ModelArtifact.artifact_id.like(f"import:{sha[:12]}%"))
        ).scalars().all().__len__() == 10


def test_null_checksum_candidate_gets_suffix(client, authed, tmp_path):
    """An existing row at the deterministic id with NO recorded checksum
    can never verify as reuse → suffix allocated, original untouched."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    sha = hashlib.sha256(pathlib.Path(src).read_bytes()).hexdigest()
    with client.app.state.session_factory() as s:
        s.add(ModelArtifact(
            artifact_id=f"import:{sha[:12]}", display_name="legacy",
            kind="model", arch="unknown", family="", modality="image",
            fmt="safetensors", role="", version="", source_type="import",
            managed_by="external", uri=None, content_sha256=None,
            byte_size=1, license_json={}, support_tier="unknown",
            enabled=True, tags_json=[], meta_json={}, source_uri=src,
            install_id=None))
        s.commit()
    res = _import_ok(client, authed, src)
    assert res["artifact_id"] == f"import:{sha[:12]}:2"
    with client.app.state.session_factory() as s:
        legacy = s.get(ModelArtifact, f"import:{sha[:12]}")
        assert legacy.content_sha256 is None  # untouched


# ---------------------------------------------------------------- GGUF bounds

def test_gguf_bad_magic_refused(tmp_path):
    """Wrong magic → sniff 'unknown' → unsupported_format (distinct from
    malformed_gguf, where magic IS present but metadata is broken)."""
    src = tmp_path / "bad.gguf"
    src.write_bytes(b"GUFX" + struct.pack("<IQQ", 3, 0, 0) + b"\x00" * 32)
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code == "unsupported_format"


def test_gguf_unsupported_version(tmp_path):
    src = tmp_path / "v9.gguf"
    src.write_bytes(b"GGUF" + struct.pack("<IQQ", 9, 0, 0) + b"\x00" * 16)
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code == "malformed_gguf"


def test_gguf_huge_counts_bounded(tmp_path):
    """Crafted astronomic tensor/kv counts must not allocate — bounded
    refusal via checked arithmetic, never len*caps allocation."""
    src = tmp_path / "huge.gguf"
    src.write_bytes(b"GGUF" + struct.pack("<IQQ", 3, 2**60, 2**60)
                    + b"\x00" * 64)
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code in ("malformed_gguf", "metadata_too_large")


def test_gguf_kv_count_over_cap(tmp_path):
    """kv_count beyond the documented cap → bounded refusal."""
    src = tmp_path / "kv.gguf"
    src.write_bytes(b"GGUF" + struct.pack(
        "<IQQ", 3, 0, model_import.GGUF_MAX_KV + 1) + b"\x00" * 64)
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code == "metadata_too_large"


def test_safetensors_json_truncated(tmp_path):
    """Header length fits the file but the JSON itself is malformed —
    a distinct path from the file-size truncation check."""
    hb = b'{"a": {"dtype": "F16", "shape": [1], "data_offsets": [0,'
    src = tmp_path / "trunc.safetensors"
    src.write_bytes(len(hb).to_bytes(8, "little") + hb)
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code == "malformed_safetensors"


# ------------------------------------------------- cancellation + recovery

def test_cancel_during_hash(tmp_path):
    """cancel_check fires mid-stream → propagates; no partial report."""
    from app.adapters.base import JobCancelled
    src = tmp_path / "weights.safetensors"
    src.write_bytes(_st_bytes())
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        if calls["n"] >= 2:
            raise JobCancelled()
    with pytest.raises(JobCancelled):
        model_import.inspect_file(src, cancel_check=boom)


def test_cancel_before_transaction_leaves_no_row(client, authed, tmp_path):
    """Cancel landing between inspection and commit → JobCancelled
    propagates out of the adapter; zero artifact rows."""
    import asyncio
    import threading

    from app.adapters.base import JobCancelled, JobHandle
    from app.adapters.import_adapter import ModelImportAdapter
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    ev = threading.Event()
    handle = JobHandle(
        job_id="j1", project_id=None,
        request={"source_path": src, "license": {"name": "MIT"}},
        work_dir=str(tmp_path), cancel_event=ev)
    adapter = ModelImportAdapter(
        client.app.state.session_factory, client.app.state.settings)

    async def drain():
        async for _ in adapter.run(handle):
            ev.set()  # cancel lands at the next checkpoint — pre-commit
    with pytest.raises(JobCancelled):
        asyncio.run(drain())
    with client.app.state.session_factory() as s:
        assert s.execute(select(ModelArtifact).where(
            ModelArtifact.source_type == "import")).scalars().all() == []


def test_cancel_after_success_row_stays(client, authed, tmp_path):
    """Committed-then-cancelled: the artifact is a valid registration —
    cancel endpoint reports terminal, row intact."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    r = _import(client, authed, src)
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    job = _wait_job(client, job_id)
    assert job["status"] == "succeeded"
    r2 = client.post(f"/api/jobs/{job_id}/cancel", headers=authed)
    assert r2.status_code == 409
    aid = job["result_json"]["artifact_id"]
    with client.app.state.session_factory() as s:
        assert s.get(ModelArtifact, aid) is not None


# ------------------------------------------------------ corrective F1-F4

def test_db_failure_never_leaks_path(client, authed, tmp_path, monkeypatch):
    """F1 — a commit-time DB failure whose message embeds row params
    (incl. the source path) must surface only a sanitized db_error code."""
    from sqlalchemy.exc import IntegrityError
    src = _write(tmp_path, "leak_probe.safetensors", _st_bytes())
    marker = "DISTINCTIVE_PATH_MARKER_9f3e"
    # A real StatementError whose str() echoes parameters — the exact
    # payload that would leak if it reached job.error raw.
    boom = IntegrityError("UNIQUE constraint failed",
                          "INSERT INTO model_artifacts (...) VALUES (?)",
                          (f"{src}::{marker}",), Exception("sqlite3"))
    # The repr embeds the params payload — the marker proves a leak path
    # exists if the raw exception ever reaches a serialized surface.
    assert marker in str(boom)

    def _explode(*a, **kw):
        raise boom
    monkeypatch.setattr(model_artifacts, "register_import", _explode)
    r = _import(client, authed, src)
    assert r.status_code == 202
    job = _wait_job(client, r.json()["job_id"])
    assert job["status"] == "failed"
    dumped = json.dumps(job)
    assert "db_error" in dumped
    assert marker not in dumped and src not in dumped
    with client.app.state.session_factory() as s:
        assert s.execute(select(ModelArtifact).where(
            ModelArtifact.source_type == "import")).scalars().all() == []
        from app.db.models import AuditEvent
        assert s.execute(select(AuditEvent).where(
            AuditEvent.event_type == "model_import.registered")
        ).scalars().all() == []


def test_resolved_ancestor_reparse_refused(tmp_path, monkeypatch):
    """F2 — an ancestor that only exists in the RESOLVED path is walked
    and refused, not just the lexical request path's ancestors."""
    src = tmp_path / "real.safetensors"
    src.write_bytes(_st_bytes())
    jdir = tmp_path / "junction_dir"
    jdir.mkdir()
    (jdir / "x.safetensors").write_bytes(_st_bytes())
    real = model_import._is_reparse

    def spy(p, *, is_dir):
        if p == jdir:
            return True  # resolved-only junction ancestor
        return real(p, is_dir=is_dir)
    monkeypatch.setattr(model_import, "_is_reparse", spy)
    monkeypatch.setattr(
        pathlib.Path, "resolve",
        lambda self, strict=False: jdir / "x.safetensors")
    with pytest.raises(ImportRefused) as e:
        model_import.validate_source_path(str(src))
    assert e.value.code == "path_is_link"


def test_non_regular_handle_refused(tmp_path, monkeypatch):
    """F3 — the opened handle's fstat mode must be S_ISREG; a FIFO-class
    fd refuses before any parsing/hashing."""
    import stat as _stat
    src = tmp_path / "weights.safetensors"
    src.write_bytes(_st_bytes())
    orig = model_import.os.fstat

    def fake_fstat(fd):
        st = orig(fd)
        # 0o170000 = the file-type bits (stat.S_IFMT is a function here,
        # not a mask constant).
        fifo_mode = (st.st_mode & ~0o170000) | _stat.S_IFIFO
        return os.stat_result((fifo_mode,) + tuple(st[1:]))
    monkeypatch.setattr(model_import.os, "fstat", fake_fstat)
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code == "path_not_file"


def test_safetensors_deeply_nested_json(tmp_path):
    """F4 — a deeply-nested JSON header raises RecursionError, mapped to
    malformed_safetensors (never a raw crash string in job.error)."""
    hb = b'{"a":' * 200_000 + b"0" + b"}" * 200_000
    src = tmp_path / "deep.safetensors"
    src.write_bytes(len(hb).to_bytes(8, "little") + hb)
    with pytest.raises(ImportRefused) as e:
        model_import.inspect_file(src)
    assert e.value.code == "malformed_safetensors"


def test_interrupted_job_requeues_and_completes(client, authed, tmp_path):
    """Crash mid-run → reconcile_interrupted requeues (resumable) and the
    persisted private request_json lets the job complete on restart —
    registration is idempotent, so re-execution is safe."""
    src = _write(tmp_path, "weights.safetensors", _st_bytes())
    engine = client.app.state.job_engine
    with client.app.state.session_factory() as s:
        job = engine.enqueue(
            s, project_id=None, adapter_id="ops.model.import",
            kind="model_import",
            request={"source_path": src, "license": {"name": "MIT"}},
            idempotency_key="crash:test")
        job.status = "running"  # crashed mid-execution
        job_id = job.id
        s.commit()
        requeued = engine.reconcile_interrupted(s)
        s.commit()
        assert requeued == 1
        again = s.get(GenerationJob, job_id)
        assert again.status == "queued"
        # Private source path retained for re-execution; JobOut never
        # serializes request_json (job-listing privacy test above).
        assert again.request_json["source_path"] == src
    engine.wake()
    done = _wait_job(client, job_id)
    assert done["status"] == "succeeded"
    assert done["result_json"]["artifact_id"].startswith("import:")
