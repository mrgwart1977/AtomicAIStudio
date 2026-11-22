"""U-1 — Universal Model and LoRA Manager: artifact registry foundations.

`model_artifacts` is a READ-ONLY PROJECTION over the curated catalog plus
(existing) `model_installs` rows. During U-1 it is seeded from
`model_catalog` on startup and linked to install rows for read views.
Nothing in this module modifies `ModelInstall` rows, catalog entries, or
the filesystem — `model_installs` remains the authoritative install
record until a separately approved cutover.

Identity model (three distinct layers, per the U-0 contract):
  artifact_id   — stable registry identity. Catalog-seeded rows use
                  "catalog:<model_key>". Future sources use their own
                  reserved prefixes (import:/disc:/train:) so a user
                  import can never collide with or overwrite a catalog
                  artifact.
  content_sha256— CONTENT identity: the full SHA-256 of the primary
                  weight file when the catalog declares it (else NULL).
                  Never truncated, never used as the PK — two artifacts
                  may share content bytes while remaining distinct
                  registry entries (different source/version).
  install_id    — INSTALLATION identity: an association to the current
                  `model_installs` row for this catalog key, if any.
                  Install state itself (status/verified) is always read
                  from `model_installs` at view time, never copied.

Ownership model:
  managed_by="atomic"   — files live under the configured models root;
                          the app may install/remove them (not in U-1).
  managed_by="external" — files referenced in place; catalog sync and
                          every future maintenance path MUST NOT modify
                          or delete them. U-1 seeds no external rows, but
                          the invariant is enforced here so it cannot be
                          weakened accidentally later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import ModelArtifact, ModelInstall, utcnow
from app.domain import model_catalog
from app.domain.model_catalog import ModelSpec

# Reserved artifact_id prefixes. Only "catalog:" is produced in U-1.
PREFIX_CATALOG = "catalog:"
PREFIX_IMPORT = "import:"
PREFIX_DISCOVERED = "disc:"
PREFIX_TRAINED = "train:"

# A catalog sync may only ever update fields it owns. Everything else —
# identity, source_type, managed_by, user metadata (enabled/tags) — is
# written once at insert and never re-touched.
_SYNCED_FIELDS = (
    "display_name", "kind", "arch", "family", "modality", "fmt", "role",
    "version", "byte_size", "content_sha256", "license_json",
    "support_tier", "meta_json",
)


def artifact_id_for(spec: ModelSpec) -> str:
    return f"{PREFIX_CATALOG}{spec.key}"


def kind_for(spec: ModelSpec) -> str:
    if spec.modality == "lora":
        return "adapter"
    primary = spec.files[0].role if spec.files else ""
    if primary in {"vae", "text_encoder", "clip", "t5", "upscaler"}:
        return "auxiliary"
    return "model"


def arch_for(spec: ModelSpec) -> str:
    # The catalog family is the declared architecture for every current
    # entry (base models and adapters alike). "unknown" is reserved for
    # imported/discovered artifacts that have not been inspected.
    return spec.family or "unknown"


def role_for(spec: ModelSpec) -> str:
    if spec.modality == "lora":
        lora_role = (spec.capabilities or {}).get("lora_role", "")
        return f"lora:{lora_role}" if lora_role else "lora"
    if spec.files:
        return spec.files[0].role or "diffusion"
    return "diffusion"


def _files_manifest(spec: ModelSpec) -> list[dict[str, Any]]:
    return [
        {"name": f.name, "size_bytes": f.size_bytes, "sha256": f.sha256,
         "role": f.role}
        for f in spec.files
    ]


def _sync_values(spec: ModelSpec) -> dict[str, Any]:
    primary = spec.files[0] if spec.files else None
    return {
        "display_name": spec.display_name,
        "kind": kind_for(spec),
        "arch": arch_for(spec),
        "family": spec.family,
        "modality": spec.modality,
        "fmt": spec.fmt,
        "role": role_for(spec),
        "version": spec.version,
        "byte_size": primary.size_bytes if primary else 0,
        "content_sha256": (primary.sha256 or None) if primary else None,
        "license_json": dict(spec.license or {}),
        "support_tier": spec.support,
        "meta_json": {
            "source": spec.source,
            "runtime_key": spec.runtime_key,
            "adapter_id": spec.adapter_id,
            "compatible_adapters": list(spec.compatible_adapters),
            "modes": list(spec.modes),
            "requirements": spec.requirements,
            "files": _files_manifest(spec),
        },
    }


def sync_catalog(session: Session) -> dict[str, int]:
    """Seed/update catalog-owned artifact rows. Idempotent.

    Only rows whose artifact_id carries the reserved "catalog:" prefix
    and whose source_type is "catalog" are created or updated — import:,
    disc:, train:, and every managed_by="external" row are out of scope
    for catalog synchronization by construction.
    """
    created = updated = 0
    existing = {
        r.artifact_id: r
        for r in session.execute(select(ModelArtifact)).scalars()
        if r.artifact_id.startswith(PREFIX_CATALOG)
        and r.source_type == "catalog"
    }
    for spec in model_catalog.catalog():
        aid = artifact_id_for(spec)
        values = _sync_values(spec)
        row = existing.get(aid)
        if row is None and session.get(ModelArtifact, aid) is not None:
            # Pathological: a catalog:-prefixed row whose source_type is
            # not "catalog" — unreachable via register_external (prefix
            # refused) but possible via manual DB writes. Never overwrite
            # a foreign row; skip rather than collide.
            continue
        if row is None:
            session.add(ModelArtifact(
                artifact_id=aid,
                source_type="catalog",
                managed_by="atomic",
                enabled=True,
                tags_json=[],
                **values,
            ))
            created += 1
            continue
        for field in _SYNCED_FIELDS:
            new_val = values[field]
            if getattr(row, field) != new_val:
                setattr(row, field, new_val)
                updated += 1
    session.flush()
    return {"created": created, "updated_fields": updated}


def link_installs(session: Session) -> int:
    """Associate each catalog artifact with its current install row.

    Association only — `model_installs` rows are never modified, and the
    artifact row stores no derived status. When a model is removed the
    association follows the surviving (status='removed') row so the view
    reports removal honestly.
    """
    latest: dict[str, ModelInstall] = {}
    rows = session.execute(
        select(ModelInstall).order_by(ModelInstall.created_at.desc())
    ).scalars().all()
    for r in rows:
        latest.setdefault(r.model_key, r)
    linked = 0
    for art in session.execute(select(ModelArtifact)).scalars():
        if not art.artifact_id.startswith(PREFIX_CATALOG):
            continue
        inst = latest.get(art.artifact_id[len(PREFIX_CATALOG):])
        new_id_val = inst.id if inst else None
        new_uri = (
            f"{inst.install_root}/{inst.rel_dir}/{inst.file_name}"
            if inst and inst.install_root and inst.file_name else None
        )
        if art.install_id != new_id_val or art.uri != new_uri:
            art.install_id = new_id_val
            art.uri = new_uri
            linked += 1
    session.flush()
    return linked


def artifact_to_dict(row: ModelArtifact, install: dict | None, *,
                     detail: bool = False) -> dict:
    out = {
        "artifact_id": row.artifact_id,
        "display_name": row.display_name,
        "kind": row.kind,
        "arch": row.arch,
        "family": row.family,
        "modality": row.modality,
        "fmt": row.fmt,
        "role": row.role,
        "version": row.version,
        "source_type": row.source_type,
        "managed_by": row.managed_by,
        "uri": row.uri,
        "content_sha256": row.content_sha256,
        "byte_size": row.byte_size,
        "license": row.license_json or {},
        "support_tier": row.support_tier,
        "enabled": row.enabled,
        "tags": row.tags_json or [],
        "meta": row.meta_json or {},
        "install_id": row.install_id,
        # Install status is projected from the authoritative
        # model_installs row — never stored on the artifact.
        "install": install or {"status": "not_installed"},
    }
    if detail:
        # source_uri only on the authenticated detail view — never in
        # list responses, job events, or audit payloads (path privacy).
        out["source_uri"] = row.source_uri
        out["imported_at"] = (
            row.imported_at.isoformat() if row.imported_at else None)
        out["import_report"] = row.import_report_json or {}
    return out


def list_artifacts(session: Session) -> list[dict]:
    from app.domain.model_files import installs_by_key

    installs = installs_by_key(session)
    out = []
    for row in session.execute(
        select(ModelArtifact).order_by(ModelArtifact.artifact_id)
    ).scalars():
        inst = None
        if row.artifact_id.startswith(PREFIX_CATALOG):
            inst = installs.get(row.artifact_id[len(PREFIX_CATALOG):])
            if inst:
                inst = {k: v for k, v in inst.items()
                        if k != "disk_free_bytes"}
        else:
            # Non-catalog rows have no model_installs row — registration
            # is the honest state, never "installed"/"verified".
            inst = {"status": "registered", "managed_by": row.managed_by}
        out.append(artifact_to_dict(row, inst))
    return out


_MAX_ID_ATTEMPTS = 9  # base id + suffixes :2..:9


def register_import(session: Session, *, source_uri: str, report: dict,
                    license_json: dict, family: str = "",
                    role_hint: str = "") -> tuple[ModelArtifact, bool]:
    """U-2a — transactional registration of one imported file.

    `report` is the sanitized inspection report (sha256, fmt, suggestions,
    registration_stat) — the job has ALREADY run the full-content hash
    before this is called, so a matching (artifact_id candidate,
    source_uri, content_sha256) triple is verified reuse, not a guess.

    Returns (row, reused). Raises ImportRefused("identity_exhausted")
    when every deterministic candidate is occupied by different sources —
    never overwrites an existing artifact.
    """
    from sqlalchemy.exc import IntegrityError

    from app.domain.model_import import ImportRefused

    sha = report["sha256"]
    base_id = f"{PREFIX_IMPORT}{sha[:12]}"
    fmt = report.get("fmt", "unknown")
    kind = "adapter" if (role_hint == "lora"
                         or report.get("suggested_role") == "lora") else "model"
    role = role_hint or report.get("suggested_role") or ""
    arch = report.get("suggested_arch", "unknown")

    candidate = base_id
    for attempt in range(_MAX_ID_ATTEMPTS):
        if attempt:
            candidate = f"{base_id}:{attempt + 1}"
        existing = session.get(ModelArtifact, candidate)
        if existing is not None:
            # Verified reuse: same resolved source identity AND same
            # full content hash (freshly computed by the job).
            if (existing.source_type == "import"
                    and existing.source_uri == source_uri
                    and existing.content_sha256 == sha):
                return existing, True
            continue  # different source at this candidate — try next
        row = ModelArtifact(
            artifact_id=candidate,
            display_name=f"Imported {Path(source_uri).name}",
            kind=kind, arch=arch, family=family,
            modality="image", fmt=fmt, role=role,
            version="", source_type="import",
            # uri stays NULL on imports — the list view must not expose
            # the source path; it lives only in source_uri (detail view).
            managed_by="external", uri=None,
            content_sha256=sha,
            byte_size=report.get("byte_size", 0),
            license_json={**license_json, "declared_by": "user"},
            support_tier="unknown", enabled=True, tags_json=[],
            meta_json={"suggestions": {
                "arch": report.get("suggested_arch"),
                "role": report.get("suggested_role"),
                "fmt": fmt,
            }},
            source_uri=source_uri,
            imported_at=utcnow(),
            import_report_json=report,
        )
        try:
            with session.begin_nested():
                session.add(row)
            return row, False
        except IntegrityError:
            # PK raced us — savepoint rolled back cleanly; next suffix.
            continue
    raise ImportRefused(
        "identity_exhausted",
        "all deterministic artifact ids for this content are occupied")


def register_external(session: Session, *, artifact_id: str, uri: str,
                      display_name: str = "", kind: str = "model",
                      arch: str = "unknown", family: str = "",
                      modality: str = "image", fmt: str = "unknown",
                      role: str = "", version: str = "",
                      content_sha256: str | None = None,
                      byte_size: int = 0, license_json: dict | None = None,
                      support_tier: str = "unknown",
                      meta_json: dict | None = None) -> ModelArtifact:
    """Register an externally managed artifact (used by future import and
    discovery milestones; U-1 calls it only from tests).

    Refuses catalog-prefixed ids and re-registration so an external row
    can never shadow or overwrite a catalog artifact.
    """
    if artifact_id.startswith(PREFIX_CATALOG):
        raise ValueError("artifact_id uses the reserved catalog: prefix")
    if session.get(ModelArtifact, artifact_id) is not None:
        raise ValueError(f"artifact {artifact_id!r} already registered")
    row = ModelArtifact(
        artifact_id=artifact_id,
        display_name=display_name or artifact_id,
        kind=kind, arch=arch, family=family, modality=modality, fmt=fmt,
        role=role, version=version, source_type="import",
        managed_by="external", uri=uri, content_sha256=content_sha256,
        byte_size=byte_size, license_json=license_json or {},
        support_tier=support_tier, enabled=True, tags_json=[],
        meta_json=meta_json or {},
    )
    session.add(row)
    session.flush()
    return row
