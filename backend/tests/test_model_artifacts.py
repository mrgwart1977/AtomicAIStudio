"""U-1 — model_artifacts: universal registry foundations.

Covers: fresh + existing-DB migration, idempotent seeding, catalog
backfill, install association, three-layer identity (artifact / content
/ install), multi-component manifests, external-ownership invariants,
and proof that existing behavior (ModelInstall authority, LoRA gates,
backup scope, filesystem) is unchanged. Entirely offline.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db.models import ModelArtifact, ModelInstall, new_id
from app.domain import model_artifacts, model_catalog


def _artifacts(session):
    return session.execute(select(ModelArtifact)).scalars().all()


def _install_row(session, key="sd15.fp16", status="installed"):
    row = ModelInstall(
        id=new_id(), model_key=key, adapter_id="gen.image.sdcpp",
        version="1.0", status=status,
        install_root="/tmp/models", rel_dir=key,
        file_name="weights.gguf", byte_size=123, sha256="ab" * 32,
        verified=(status == "installed"), license="test",
        meta_json={"source": {}},
    )
    session.add(row)
    session.flush()
    return row


# ---------- migration ----------

def test_fresh_migration_creates_and_seeds(client, settings):
    """App boot on an empty data dir: table exists and every catalog
    entry has an artifact row."""
    with client.app.state.session_factory() as s:
        arts = _artifacts(s)
        assert len(arts) == len(model_catalog.catalog())
        keys = {a.artifact_id for a in arts}
        assert "catalog:sd15.fp16" in keys
        assert "catalog:lora.id.babylittlelo" in keys


def test_existing_db_migration(tmp_path, settings):
    """A populated 0026_a7 database upgrades to 0027_u1: the table is
    created and existing install rows are left completely untouched."""
    from alembic import command as alembic_command
    from alembic.config import Config as AlembicConfig
    from app.db.session import create_engine_for, session_factory
    from app.main import _alembic_script_location, run_migrations

    url = f"sqlite:///{tmp_path}/mig.db"
    run_migrations(url)
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(_alembic_script_location()))
    cfg.set_main_option("sqlalchemy.url", url)
    alembic_command.downgrade(cfg, "0026_a7")

    engine = create_engine_for(url)
    sf = session_factory(engine)
    with sf() as s:
        inst = _install_row(s, "sd15.fp16")
        s.commit()
        inst_id = inst.id
    engine.dispose()

    run_migrations(url)  # 0026_a7 → 0027_u1

    engine = create_engine_for(url)
    sf = session_factory(engine)
    with sf() as s:
        assert s.get(ModelInstall, inst_id) is not None
        inst = s.get(ModelInstall, inst_id)
        assert inst.status == "installed" and inst.verified is True
        model_artifacts.sync_catalog(s)
        model_artifacts.link_installs(s)
        s.commit()
        art = s.get(ModelArtifact, "catalog:sd15.fp16")
        assert art is not None and art.install_id == inst_id
    engine.dispose()


def test_migration_idempotent(tmp_path):
    """upgrade head on a DB already at head is a no-op."""
    from app.db.session import create_engine_for
    from app.main import run_migrations

    url = f"sqlite:///{tmp_path}/mig.db"
    run_migrations(url)
    run_migrations(url)
    engine = create_engine_for(url)
    with engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql(
            "PRAGMA table_info(model_artifacts)")}
    engine.dispose()
    assert "artifact_id" in cols and "install_id" in cols


def test_sync_idempotent(client):
    """Repeated sync+link changes nothing and duplicates nothing."""
    with client.app.state.session_factory() as s:
        r1 = model_artifacts.sync_catalog(s)
        model_artifacts.link_installs(s)
        s.commit()
    with client.app.state.session_factory() as s:
        r2 = model_artifacts.sync_catalog(s)
        assert r2 == {"created": 0, "updated_fields": 0}
        assert model_artifacts.link_installs(s) == 0
        assert len(_artifacts(s)) == len(model_catalog.catalog())


# ---------- catalog backfill & identity ----------

def test_catalog_backfill_fields(client):
    with client.app.state.session_factory() as s:
        arts = {a.artifact_id: a for a in _artifacts(s)}
    sd = arts["catalog:sd15.fp16"]
    assert sd.kind == "model" and sd.arch == "sd1.5"
    assert sd.modality == "image" and sd.fmt == "gguf"
    assert sd.source_type == "catalog" and sd.managed_by == "atomic"
    assert sd.support_tier == "production" and sd.enabled is True
    assert sd.content_sha256 is not None and len(sd.content_sha256) == 64
    lora = arts["catalog:lora.id.babylittlelo"]
    assert lora.kind == "adapter" and lora.role == "lora:identity"
    assert lora.family == "sd1.5" and lora.support_tier == "experimental"
    assert arts["catalog:lora.style.pixelart"].role == "lora:style"


def test_content_identity_full_sha(client):
    """content_sha256 is the FULL declared checksum — never truncated."""
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, "catalog:lora.id.babylittlelo")
    spec = model_catalog.get("lora.id.babylittlelo")
    assert art.content_sha256 == spec.files[0].sha256
    assert len(art.content_sha256) == 64


def test_missing_checksum_is_null(client):
    """Entries whose files declare no sha256 get NULL content identity —
    a manifest gap is honest, not fabricated."""
    with client.app.state.session_factory() as s:
        arts = {a.artifact_id: a for a in _artifacts(s)}
    specs = {sp.key: sp for sp in model_catalog.catalog()}
    for aid, art in arts.items():
        key = aid.split(":", 1)[1]
        spec = specs[key]
        declared = spec.files[0].sha256 if spec.files else None
        assert art.content_sha256 == (declared or None)


def test_multicomponent_manifest(client):
    """wan21 declares 3 component files — the manifest preserves all."""
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, "catalog:wan21.t2v.1_3b")
    files = art.meta_json["files"]
    assert len(files) == 3
    roles = {f["role"] for f in files}
    assert "diffusion" in roles
    assert all({"name", "size_bytes", "sha256", "role"} <= set(f)
               for f in files)


def test_distinct_source_identities_same_content(client):
    """Two registry rows may share content_sha256 — identity is the
    artifact_id, not the bytes."""
    with client.app.state.session_factory() as s:
        shared = "cd" * 32
        model_artifacts.register_external(
            s, artifact_id="import:aaa", uri="D:/models/a.safetensors",
            content_sha256=shared, display_name="copy A")
        model_artifacts.register_external(
            s, artifact_id="import:bbb", uri="D:/models/b.safetensors",
            content_sha256=shared, display_name="copy B")
        s.commit()
        rows = s.execute(select(ModelArtifact).where(
            ModelArtifact.content_sha256 == shared)).scalars().all()
        assert len(rows) == 2
        assert {r.artifact_id for r in rows} == {"import:aaa", "import:bbb"}


# ---------- ownership ----------

def test_external_row_untouched_by_sync(client):
    """managed_by=external rows are out of scope for catalog sync —
    fields, enabled flag, and uri all survive a sync+link cycle."""
    with client.app.state.session_factory() as s:
        model_artifacts.register_external(
            s, artifact_id="import:ext1", uri="E:/lib/custom.gguf",
            display_name="User model", arch="sdxl",
            support_tier="unknown")
        s.commit()
    with client.app.state.session_factory() as s:
        row = s.get(ModelArtifact, "import:ext1")
        row.enabled = False  # user toggle
        s.commit()
        model_artifacts.sync_catalog(s)
        model_artifacts.link_installs(s)
        s.commit()
        row = s.get(ModelArtifact, "import:ext1")
        assert row.display_name == "User model"
        assert row.arch == "sdxl" and row.enabled is False
        assert row.uri == "E:/lib/custom.gguf"
        assert row.managed_by == "external"
        assert row.install_id is None  # never linked to an install row


def test_catalog_prefix_reserved(client):
    with client.app.state.session_factory() as s:
        with pytest.raises(ValueError, match="reserved catalog"):
            model_artifacts.register_external(
                s, artifact_id="catalog:forgery", uri="E:/x.gguf")


def test_sync_only_touches_owned_fields(client):
    """User metadata on catalog rows (enabled/tags) survives re-sync even
    though spec-derived fields refresh."""
    with client.app.state.session_factory() as s:
        art = s.get(ModelArtifact, "catalog:sd15.fp16")
        art.enabled = False
        art.tags_json = ["mine"]
        s.commit()
        model_artifacts.sync_catalog(s)
        s.commit()
        art = s.get(ModelArtifact, "catalog:sd15.fp16")
        assert art.enabled is False and art.tags_json == ["mine"]


# ---------- install association (non-destructive) ----------

def test_install_association_and_projection(client, tmp_path):
    """link_installs associates but never modifies the ModelInstall row,
    and the API view projects install status from it."""
    with client.app.state.session_factory() as s:
        inst = _install_row(s, "sd15.q8")
        s.commit()
        before = (inst.status, inst.verified, inst.sha256, inst.install_root)
        model_artifacts.link_installs(s)
        s.commit()
        art = s.get(ModelArtifact, "catalog:sd15.q8")
        assert art.install_id == inst.id
        assert art.uri == "/tmp/models/sd15.q8/weights.gguf"
        inst2 = s.get(ModelInstall, inst.id)
        assert (inst2.status, inst2.verified, inst2.sha256,
                inst2.install_root) == before
    r = client.get("/api/models/artifacts")
    assert r.status_code == 200
    entry = next(a for a in r.json() if a["artifact_id"] == "catalog:sd15.q8")
    assert entry["install"]["status"] == "installed"
    assert entry["install"]["verified"] is True


def test_no_install_projects_not_installed(client):
    r = client.get("/api/models/artifacts")
    entry = next(a for a in r.json()
                 if a["artifact_id"] == "catalog:flux.dev")
    assert entry["install"]["status"] == "not_installed"


def test_api_route_not_parsed_as_model_key(client):
    """/api/models/artifacts resolves the registry route, not a
    {model_key} lookup (route-order guard)."""
    r = client.get("/api/models/artifacts")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
    assert {a["artifact_id"] for a in r.json()} >= {
        "catalog:sd15.fp16", "catalog:wan21.t2v.1_3b"}


# ---------- existing behavior preserved ----------

def test_lora_gates_unchanged(client):
    """check_loras still refuses experimental adapters — registry exists
    but generation gates are untouched."""
    from app.domain.commands import CommandError
    from app.domain.lora_resolve import check_loras

    spec = model_catalog.get("lora.id.babylittlelo")
    base = model_catalog.get("sd15.fp16")
    with client.app.state.session_factory() as s:
        with pytest.raises(CommandError) as ei:
            check_loras(s, None, [{"key": spec.key, "strength": 1.0}],
                        base, shot=None)
        assert ei.value.status_code == 422
        assert "experimental" in ei.value.detail


def test_artifacts_not_in_backup_scope():
    """Registry rows are app-level, not project data — backups unchanged."""
    from app.domain.backup import ENTITY_TABLES
    assert "model_artifacts" not in ENTITY_TABLES
    assert all("artifact" not in t for t in ENTITY_TABLES)


def test_no_filesystem_changes(client, settings):
    """Seeding/linking touches the DB only — no directories or files are
    created under the data dir by the artifact registry."""
    def snapshot():
        return {str(p.relative_to(settings.data_dir))
                for p in settings.data_dir.rglob("*")}
    before = snapshot()
    with client.app.state.session_factory() as s:
        model_artifacts.sync_catalog(s)
        model_artifacts.link_installs(s)
        s.commit()
    assert snapshot() == before
