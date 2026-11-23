"""API routers — health, hardware, projects, prompt draft, commands, jobs,
assets (CAS), bibles, storage locations, backups, websocket.
All project data is scoped through get_project — no unscoped reads."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_project
from app.db.models import (
    Asset,
    AssetVersion,
    AuditEvent,
    BackupRun,
    Character,
    CreativeCommand,
    EntityRevision,
    GenerationJob,
    Location,
    Project,
    ProjectRevision,
    PromptDraft,
    RuntimeInstall,
    SharedAssetLink,
    StorageLocation,
    World,
    new_id,
    utcnow,
)
from app.domain import bibles, commands as cmd, hardware, model_catalog, model_files
from app.domain.backup import PackageError, read_manifest
from app.schemas.api import (
    AdapterProfileOut,
    AppBackupDirUpdate,
    ArtifactCompatOut,
    AssetOut,
    AssetPageOut,
    AssetVersionOut,
    BackupDestinationOut,
    BackupDestinationValidate,
    BackupRunOut,
    CommandCommitRequest,
    CommandOut,
    CommandPreviewRequest,
    CommandProposalOut,
    CompatEvalOut,
    CompatRulesetOut,
    EntityAssetLinkOut,
    ReferenceSetOut,
    EntityOut,
    EntityRevisionOut,
    GpuSettingsOut,
    GpuSettingsUpdate,
    ImportCapabilitiesOut,
    DiscoveryCandidateDetailOut,
    DiscoveryCandidateOut,
    DiscoveryRootIn,
    DiscoveryRootOut,
    DiscoveryScanOut,
    ResourceSettingsOut,
    ResourceSettingsUpdate,
    JobOut,
    LoraPreviewOut,
    LoraPreviewRequest,
    ManagedCopyReleaseOut,
    ModelArtifactDetailOut,
    ModelArtifactOut,
    ModelCatalogEntryOut,
    ModelImportOut,
    ModelImportRequest,
    ModelInstallRequest,
    ModelsDirOut,
    PackageImportRequest,
    PackagePreviewOut,
    ProjectCardOut,
    ProjectCreate,
    ProjectOut,
    ProjectRevisionOut,
    PromptDraftOut,
    RuntimeOut,
    SharedAssetOut,
    StorageLocationCreate,
    StorageLocationOut,
    StorageLocationUpdate,
    StoryboardSceneOut,
    TimelineDetailOut,
    TimelineOut,
    TimelineRevisionOut,
)

# /health stays token-free so the desktop shell can poll readiness.
open_router = APIRouter()
# Everything under /api/* is protected by the session token (applied at include time).
api_router = APIRouter()

ENTITY_MODELS = {"character": Character, "world": World, "location": Location}
DATA_COL = {"character": "profile_json", "world": "lore_json", "location": "details_json"}


def _entity_out(row) -> dict:
    etype = row.__tablename__.rstrip("s")
    return {
        "id": row.id, "project_id": row.project_id, "name": row.name,
        "summary": row.summary, "status": row.status, "revision": row.revision,
        "created_at": row.created_at, "updated_at": row.updated_at,
        "world_id": getattr(row, "world_id", None),
        "data": getattr(row, DATA_COL[etype]) or {},
    }


@open_router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "atomic-dream-engine-backend", "version": "0.1.0"}


@api_router.get("/api/hardware")
def get_hardware(refresh: bool = False) -> dict:
    return hardware.probe(force=refresh)


@api_router.get("/api/adapters", response_model=list[AdapterProfileOut])
def list_adapters(request: Request) -> list[dict]:
    return request.app.state.adapter_registry.list_profiles()


# ---------- models & runtimes (M3) ----------

def _model_view(request: Request, db: Session, spec) -> dict:
    from app.domain import compatibility, model_files
    installs = model_files.installs_by_key(db)
    root = model_files.models_root(db, request.app.state.settings)
    install = installs.get(spec.key)
    if install:
        install["disk_free_bytes"] = model_files.disk_free_bytes(Path(install["install_root"]))
    else:
        install = {"status": "not_installed", "disk_free_bytes": model_files.disk_free_bytes(root)}
    compat = compatibility.classify_model(spec, hardware.probe(), install)
    return {
        **model_catalog.spec_to_dict(spec),
        "install": {k: v for k, v in (install or {}).items() if k != "disk_free_bytes"},
        "compatibility": compat,
    }


@api_router.get("/api/models", response_model=list[ModelCatalogEntryOut])
def list_models(request: Request, db: Session = Depends(get_db)):
    """The full catalog: available/installed/compatible/limited/
    unsupported/restricted/unavailable — each with install state,
    license, requirements, and a compatibility verdict."""
    return [_model_view(request, db, spec) for spec in model_catalog.catalog()]


@api_router.get("/api/models/dir", response_model=ModelsDirOut)
def get_models_dir(request: Request, db: Session = Depends(get_db)):
    # NOTE: must precede /api/models/{model_key} or "dir" is parsed as a key.
    root = model_files.models_root(db, request.app.state.settings)
    builtin = request.app.state.settings.data_dir / "models"
    custom = model_files._get_setting(db, "models_dir")
    used = 0
    if root.is_dir():
        used = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    return {
        "dir": str(root),
        "source": "app setting" if custom else "built-in default",
        "builtin_dir": str(builtin),
        "exists": root.is_dir(),
        "writable": os.access(root if root.is_dir() else root.parent, os.W_OK),
        "free_bytes": model_files.disk_free_bytes(root),
        "used_bytes": used,
    }


@api_router.get("/api/models/artifacts",
                response_model=list[ModelArtifactOut])
def list_model_artifacts(request: Request, db: Session = Depends(get_db)):
    """U-1 — the universal artifact registry: catalog-seeded model and
    adapter rows with install state projected from model_installs.
    Read-only view; install rows remain authoritative.
    NOTE: declared before /api/models/{model_key} so "artifacts" is not
    parsed as a model key."""
    from app.domain import model_artifacts
    return model_artifacts.list_artifacts(db)


@api_router.get("/api/models/artifacts/{artifact_id}",
                response_model=ModelArtifactDetailOut)
def get_model_artifact(artifact_id: str, request: Request,
                       db: Session = Depends(get_db)):
    """U-2a — authenticated detail view: the only surface that returns
    source_uri and the full inspection report."""
    from app.domain import model_artifacts
    from app.db.models import ModelArtifact
    row = db.get(ModelArtifact, artifact_id)
    if row is None:
        raise HTTPException(404, "unknown artifact")
    inst = None
    if row.artifact_id.startswith(model_artifacts.PREFIX_CATALOG):
        from app.domain.model_files import installs_by_key
        inst = installs_by_key(db).get(
            row.artifact_id[len(model_artifacts.PREFIX_CATALOG):])
        if inst:
            inst = {k: v for k, v in inst.items() if k != "disk_free_bytes"}
    else:
        inst = {"status": "registered", "managed_by": row.managed_by}
    return model_artifacts.artifact_to_dict(row, inst, detail=True)


@api_router.post("/api/models/artifacts/{artifact_id}/release-managed-copy",
                 response_model=ManagedCopyReleaseOut)
def release_managed_copy(artifact_id: str, request: Request,
                         db: Annotated[Session, Depends(get_db)]):
    """U-2b-2a — synchronous metadata-only release of a managed import.

    Detaches the artifact's managed uri (external/reference-only state,
    source provenance retained). The managed bytes are NEVER touched —
    physical managed-file deletion does not exist in this milestone.
    Deliberately NOT feature-gated: disabling new managed copies must
    not strand existing managed artifacts.
    """
    from app.domain import model_artifacts
    try:
        out = model_artifacts.release_managed_copy(db, artifact_id)
    except LookupError:
        raise HTTPException(404, "unknown artifact") from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    db.commit()
    return out


# ---------- U-4b compatibility verdicts ----------

def _compat_in_flight(db: Session):
    """Live ops.model.compat_eval jobs — the only verdict writers."""
    return db.execute(
        select(GenerationJob).where(
            GenerationJob.adapter_id == "ops.model.compat_eval",
            GenerationJob.status.in_(("queued", "running")),
        )
    ).scalars().all()


def _compat_enqueue(request: Request, db: Session, *,
                    scope: str, scope_key: str,
                    request_payload: dict, fp: str):
    """One deterministic enqueue contract for both compat-eval scopes.

    Same scope key + same inputs-fingerprint → the SAME durable job
    (idempotency_key dedupes), whether queued, running, or succeeded.
    Same scope key + different fingerprint while in flight → 409 with
    the existing job reference. A failed/cancelled job for identical
    inputs gets a deterministic retry suffix so evaluation can never
    strand. No synchronous verdict write exists anywhere.
    """
    base = f"compat_eval:{scope}:{scope_key}:{fp[:16]}"
    for j in _compat_in_flight(db):
        r = j.request_json or {}
        same_scope = r.get("scope") == scope and (
            (r.get("artifact_id") if scope == "artifact" else scope)
            == scope_key)
        if not same_scope:
            continue
        if j.idempotency_key == base or \
                j.idempotency_key.startswith(base + ":retry"):
            return j                       # identical request → handle
        raise HTTPException(409, {
            "code": "compat_eval_in_flight",
            "job_id": j.id,
            "message": "a compatibility evaluation with different "
                       "inputs is already in progress"})
    prior_jobs = db.execute(
        select(GenerationJob).where(
            GenerationJob.idempotency_key.like(base + "%"))
    ).scalars().all()
    same = [j for j in prior_jobs
            if j.idempotency_key == base
            or j.idempotency_key.startswith(base + ":retry")]
    done = next((j for j in same if j.status == "succeeded"), None)
    if done is not None:
        return done                        # identical inputs → same job
    # All prior attempts (if any) terminated failed/cancelled — derive
    # the next deterministic retry key so identical inputs can never
    # strand behind a dead job handle.
    key = f"{base}:retry{len(same)}" if same else base
    job = request.app.state.job_engine.enqueue(
        db, project_id=None, adapter_id="ops.model.compat_eval",
        kind="model_compat_eval", request=request_payload,
        idempotency_key=key)
    db.commit()
    request.app.state.job_engine.wake()
    return job


@api_router.get("/api/models/artifacts/{artifact_id}/compat",
                response_model=ArtifactCompatOut)
def get_artifact_compat(artifact_id: str, request: Request,
                        db: Session = Depends(get_db)):
    """U-4b — stored verdicts for one artifact + LIVE stale flags.

    Read-only: recomputes each verdict's input fingerprint and reports
    staleness — never writes, never rewrites verdicts on read.
    """
    from app.db.models import CompatVerdict, ModelArtifact
    from app.domain import compat_verdicts, hardware
    row = db.get(ModelArtifact, artifact_id)
    if row is None:
        raise HTTPException(404, "unknown artifact")
    rows = db.execute(
        select(CompatVerdict).where(
            CompatVerdict.artifact_id == artifact_id)
        .order_by(CompatVerdict.runtime_key)
    ).scalars().all()
    hw = hardware.probe()
    prefs = compat_verdicts.gpu_prefs(db)
    verdicts = []
    for v in rows:
        live = compat_verdicts.fingerprint_for_runtime(
            db, row, v.runtime_key, hw, prefs)
        verdicts.append({
            "artifact_id": v.artifact_id,
            "runtime_key": v.runtime_key,
            "arch_axis": v.arch_axis,
            "arch_confidence": v.arch_confidence,
            "runtime_axis": v.runtime_axis,
            "hardware_axis": v.hardware_axis,
            "policy_axis": v.policy_axis,
            "level": v.level,
            "domain_levels": v.domain_levels_json or {},
            "overall": v.overall,
            "blocking_axes": v.blocking_axes_json or [],
            "confidence": v.confidence,
            "reasons": v.reasons_json or [],
            "warnings": v.warnings_json or [],
            "missing": v.missing_json or [],
            "hardware_limits": v.hardware_limits_json or [],
            "actions": v.actions_json or [],
            "stale": live != v.inputs_fingerprint,
            "ruleset_version": v.ruleset_version,
            "evaluated_at": v.evaluated_at,
        })
    return {"artifact_id": artifact_id, "verdicts": verdicts}


@api_router.post("/api/models/artifacts/{artifact_id}/compat/evaluate",
                 status_code=202, response_model=CompatEvalOut)
def evaluate_artifact_compat(artifact_id: str, request: Request,
                             db: Session = Depends(get_db)):
    """U-4b — enqueue scope=artifact evaluation. Always 202 + job; all
    verdict writes happen inside the serialized ops lane."""
    from app.db.models import ModelArtifact
    from app.domain import compat_verdicts, hardware
    row = db.get(ModelArtifact, artifact_id)
    if row is None:
        raise HTTPException(404, "unknown artifact")
    fp = compat_verdicts.artifact_fingerprint(
        db, row, hardware.probe(), compat_verdicts.gpu_prefs(db))
    job = _compat_enqueue(
        request, db, scope="artifact", scope_key=artifact_id,
        request_payload={"scope": "artifact", "artifact_id": artifact_id},
        fp=fp)
    return {"job_id": job.id, "status": job.status}


@api_router.post("/api/models/compat/evaluate-all",
                 status_code=202, response_model=CompatEvalOut)
def evaluate_all_compat(request: Request,
                        db: Session = Depends(get_db)):
    """U-4b — enqueue scope=all evaluation over every registered
    artifact. Explicitly user-triggered; no automatic evaluate-all."""
    from app.db.models import ModelArtifact
    from app.domain import compat_verdicts, hardware
    hw = hardware.probe()
    prefs = compat_verdicts.gpu_prefs(db)
    fps = sorted(
        compat_verdicts.artifact_fingerprint(db, r, hw, prefs) or ""
        for r in db.execute(select(ModelArtifact)).scalars())
    import hashlib
    fp_all = hashlib.sha256("|".join(fps).encode()).hexdigest()
    job = _compat_enqueue(
        request, db, scope="all", scope_key="all",
        request_payload={"scope": "all"}, fp=fp_all)
    return {"job_id": job.id, "status": job.status}


@api_router.get("/api/compat/ruleset", response_model=CompatRulesetOut)
def compat_ruleset(request: Request):
    """U-4b — read-only description of the active ruleset: family
    registry + runtime capability declarations at their versions."""
    from app.domain import compat_verdicts, model_families, runtime_caps
    return {
        "ruleset_version": runtime_caps.ruleset_version(),
        "families_version": model_families.FAMILIES_VERSION,
        "caps_schema_version": runtime_caps.CAPS_SCHEMA_VERSION,
        "max_verification_level": compat_verdicts.MAX_LEVEL,
        "axes": {
            "arch": list(compat_verdicts.ARCH_AXES),
            "runtime": list(compat_verdicts.RUNTIME_AXES),
            "hardware": list(compat_verdicts.HARDWARE_AXES),
            "policy": list(compat_verdicts.POLICY_AXES),
            "overall": list(compat_verdicts.OVERALLS),
        },
        "runtimes": [
            {
                "runtime_key": d.runtime_key,
                "display_name": d.display_name,
                "caps_version": d.caps_version,
                "planned": d.planned,
                "backends": list(d.backends),
                "validated_versions": list(d.validated_versions),
                "families": {
                    k: {
                        "grade": f.grade,
                        "fmts_tested": list(f.fmts_tested),
                        "fmts_documented": list(f.fmts_documented),
                        "modes": list(f.modes),
                        "dtypes_tested": list(f.dtypes_tested),
                        "components_required":
                            list(f.components_required),
                        "policy_gated": f.policy_gated,
                    }
                    for k, f in d.families.items()
                },
            }
            for d in runtime_caps.decl_catalog()
        ],
    }


@api_router.get("/api/models/import-capabilities",
                response_model=ImportCapabilitiesOut)
def import_capabilities(request: Request):
    """U-2b-2b — read-only import-mode capability probe. Reports whether
    the managed-copy gate is enabled so the desktop can offer (or hide)
    the option honestly — never exposes filesystem paths, env vars, or
    configuration details. Reference mode is always advertised."""
    enabled = bool(getattr(request.app.state.settings,
                         "enable_managed_copy", False))
    return {"managed_copy_enabled": enabled,
            "modes": ["reference", "managed_copy"] if enabled
                     else ["reference"]}


# ---------- U-3 model discovery ----------

def _root_out(request, db, r) -> DiscoveryRootOut:
    from app.domain import model_discovery
    return DiscoveryRootOut(
        root_id=r.root_id, display_path=r.display_path,
        enabled=r.enabled, max_depth=r.max_depth,
        added_at=r.added_at, archived_at=r.archived_at,
        last_scan_outcome=r.last_scan_outcome, last_code=r.last_code,
        last_scan_at=r.last_scan_at,
        last_entries_seen=r.last_entries_seen,
        last_candidates=r.last_candidates,
        candidate_count=model_discovery.candidate_count(db, r.root_id))


@api_router.get("/api/models/discovery/roots",
                response_model=list[DiscoveryRootOut])
def discovery_roots(request: Request,
                    db: Session = Depends(get_db)):  # noqa: B008
    """U-3 — list approved discovery roots (management surface; the
    display path is the verbatim user-approved location, same trust
    boundary as the models_dir setting)."""
    from app.domain import model_discovery
    return [_root_out(request, db, r)
            for r in model_discovery.list_roots(db)]


@api_router.post("/api/models/discovery/roots",
                 response_model=DiscoveryRootOut)
def add_discovery_root(body: DiscoveryRootIn, request: Request,
                       db: Session = Depends(get_db)):  # noqa: B008
    """U-3 — approve (or idempotently re-approve) a scan root. Validates
    to a canonical LOCAL directory: UNC/network, links, and
    indeterminate drive types refuse closed. Sync validation only —
    no enumeration runs here."""
    from app.domain import model_discovery
    try:
        row, _created = model_discovery.add_root(
            db, body.path, max_depth=body.max_depth,
            settings=request.app.state.settings)
    except model_discovery.DiscoveryRefused as e:
        raise HTTPException(422, e.code) from None
    db.commit()
    return _root_out(request, db, row)


@api_router.post("/api/models/discovery/roots/{root_id}/archive",
                 response_model=DiscoveryRootOut)
def archive_discovery_root(root_id: str, request: Request,
                           db: Session = Depends(get_db)):  # noqa: B008
    """U-3 — soft-archive: stops future scans, keeps all candidates and
    provenance. There is no hard-delete path for roots or files."""
    from app.domain import model_discovery
    row = model_discovery.archive_root(db, root_id)
    if row is None:
        raise HTTPException(404, "not_found")
    db.commit()
    return _root_out(request, db, row)


def _cand_out(c, r) -> DiscoveryCandidateOut:
    from app.domain import model_discovery
    return DiscoveryCandidateOut(
        candidate_id=c.candidate_id, name=c.name,
        rel_path=model_discovery.rel_path(c, r),
        root_id=c.root_id,
        root_label=Path(r.canonical_path).name or r.root_id,
        fmt_guess=c.fmt_guess, byte_size=c.byte_size,
        presence=c.presence, linked_artifact_id=c.linked_artifact_id,
        seen_count=c.seen_count, first_seen_at=c.first_seen_at,
        last_seen_at=c.last_seen_at)


@api_router.get("/api/models/discovery/candidates",
                response_model=list[DiscoveryCandidateOut])
def discovery_candidates(request: Request,
                         db: Session = Depends(get_db),  # noqa: B008
                         root_id: str | None = None,
                         presence: str | None = None,
                         limit: int = 200, offset: int = 0):
    """U-3 — paged candidate list. Contract: no absolute paths — only
    root-relative `rel_path` and the root's basename label."""
    from app.domain import model_discovery
    return [_cand_out(c, r) for c, r in model_discovery.list_candidates(
        db, root_id=root_id, presence=presence, limit=limit,
        offset=offset)]


@api_router.get("/api/models/discovery/candidates/{candidate_id}",
                response_model=DiscoveryCandidateDetailOut)
def discovery_candidate_detail(candidate_id: str, request: Request,
                               db: Session = Depends(get_db)):  # noqa: B008
    """U-3 — authenticated detail view: the ONLY surface exposing the
    absolute candidate path (needed to prefill the import dialog)."""
    from app.db.models import DiscoveryCandidate, DiscoveryRoot
    row = db.get(DiscoveryCandidate, candidate_id)
    if row is None:
        raise HTTPException(404, "not_found")
    root = db.get(DiscoveryRoot, row.root_id)
    out = _cand_out(row, root)
    return DiscoveryCandidateDetailOut(
        **out.model_dump(), resolved_path=row.resolved_path,
        mtime_ns=row.mtime_ns,
        root_display_path=root.display_path if root else "",
        linked_at=row.linked_at)


@api_router.post("/api/models/discovery/scan", status_code=202,
                 response_model=DiscoveryScanOut)
def discovery_scan(request: Request,
                   db: Session = Depends(get_db)):  # noqa: B008
    """U-3 — enqueue a durable ops.model.discover job. One in flight at
    a time on the ops lane; a queued/running scan returns 409."""
    from app.db.models import GenerationJob
    in_flight = db.execute(
        select(GenerationJob).where(
            GenerationJob.adapter_id == "ops.model.discover",
            GenerationJob.status.in_(("queued", "running")),
        )
    ).scalars().first()
    if in_flight is not None:
        raise HTTPException(409, "a discovery scan is already in progress")
    job = request.app.state.job_engine.enqueue(
        db, project_id=None, adapter_id="ops.model.discover",
        kind="model_discovery", request={},
        idempotency_key=f"modeldisc:{new_id()[:8]}")
    db.commit()
    request.app.state.job_engine.wake()
    return {"job_id": job.id, "status": job.status}


@api_router.post("/api/models/import", status_code=202,
                 response_model=ModelImportOut)
def import_model(body: ModelImportRequest, request: Request,
                 db: Session = Depends(get_db)):
    """U-2a — enqueue a durable reference-only import of ONE user-owned
    file. Always 202: inspection + full SHA-256 run on the job; reuse of
    an existing artifact happens inside the job after fresh hashing, so
    no pre-commit '201 registered' exists. Sync checks are limited to
    request shape + cheap path classification (no content reads)."""
    from app.domain.model_import import ImportRefused, validate_source_path

    if body.mode == "managed_copy":
        # U-2b-2a gate — fail closed unless the feature flag is enabled;
        # the adapter re-checks at claim and execution time.
        if not getattr(request.app.state.settings,
                       "enable_managed_copy", False):
            raise HTTPException(422, "mode_not_supported")
    elif body.mode != "reference":
        raise HTTPException(422, "mode_not_supported")
    if not (body.license or {}).get("name", "").strip():
        raise HTTPException(422, "license_required")
    display_name = (body.display_name or "").strip()
    if len(display_name) > 200:
        raise HTTPException(422, "display_name_too_long")
    if any(ord(ch) < 0x20 for ch in display_name):
        raise HTTPException(422, "display_name_invalid")
    try:
        rp = validate_source_path(body.path)
    except ImportRefused as e:
        raise HTTPException(422, e.code) from None

    in_flight = db.execute(
        select(GenerationJob).where(
            GenerationJob.adapter_id == "ops.model.import",
            GenerationJob.status.in_(("queued", "running")),
        )
    ).scalars().all()
    resolved = str(rp)
    for j in in_flight:
        if (j.request_json or {}).get("source_path") == resolved:
            raise HTTPException(
                409, "an import for this file is already in progress")

    job = request.app.state.job_engine.enqueue(
        db, project_id=None, adapter_id="ops.model.import",
        kind="model_import",
        request={"source_path": resolved,
                 "license": body.license,
                 "family": body.family,
                 "role_hint": body.role_hint,
                 "display_name": display_name,
                 # mode persisted ONLY for managed copies — the job needs
                 # it for execution and crash requeue; reference stays
                 # unset (default) exactly as before.
                 **({"mode": "managed_copy"}
                    if body.mode == "managed_copy" else {})},
        idempotency_key=f"modelimp:{new_id()[:8]}",
    )
    db.commit()
    request.app.state.job_engine.wake()
    return {"job_id": job.id, "status": job.status}


@api_router.get("/api/models/{model_key}", response_model=ModelCatalogEntryOut)
def get_model(model_key: str, request: Request, db: Session = Depends(get_db)):
    spec = model_catalog.get(model_key)
    if spec is None:
        raise HTTPException(404, "unknown model")
    return _model_view(request, db, spec)


@api_router.post("/api/models/{model_key}/install")
def install_model(model_key: str, request: Request,
                  body: ModelInstallRequest | None = None,
                  db: Session = Depends(get_db)):
    """Queue a user-approved model download as an app-level job.
    Refuses restricted-license models and models already installed or
    mid-download."""
    spec = model_catalog.get(model_key)
    if spec is None:
        raise HTTPException(404, "unknown model")
    if spec.support == "planned":
        raise HTTPException(
            422, f"{spec.display_name} is a planned {spec.modality} model — no local support in this build")
    if not spec.license.get("commercial_ok", True):
        raise HTTPException(422, f"{spec.display_name} is license-restricted and cannot be installed")
    if not spec.files:
        raise HTTPException(422, f"{spec.display_name} has no downloadable artifact on a supported runtime")
    installs = model_files.installs_by_key(db)
    cur = installs.get(model_key) or {}
    if cur.get("status") == "installed":
        raise HTTPException(409, "model is already installed")
    in_flight = db.execute(
        select(GenerationJob).where(
            GenerationJob.adapter_id == "ops.model.download",
            GenerationJob.model_key == model_key,
            GenerationJob.status.in_(("queued", "running")),
        )
    ).scalars().first()
    if in_flight is not None:
        raise HTTPException(409, "a download for this model is already in progress")
    job = request.app.state.job_engine.enqueue(
        db, project_id=None, adapter_id="ops.model.download", kind="model_download",
        request={"model_key": model_key,
                 "runtime_flavor": (body or ModelInstallRequest()).runtime_flavor},
        idempotency_key=f"modeldl:{model_key}:{new_id()[:8]}",
        model_key=model_key,
    )
    db.commit()
    request.app.state.job_engine.wake()
    return {"job_id": job.id, "status": job.status}


@api_router.post("/api/models/{model_key}/remove")
def remove_model(model_key: str, request: Request, db: Session = Depends(get_db)):
    spec = model_catalog.get(model_key)
    if spec is None:
        raise HTTPException(404, "unknown model")
    busy = db.execute(
        select(GenerationJob).where(
            GenerationJob.model_key == model_key,
            GenerationJob.status == "running",
        )
    ).scalars().first()
    if busy is not None:
        raise HTTPException(409, "a job is using this model right now — wait for it or cancel it")
    try:
        result = model_files.remove_model(db, spec)
    except model_files.ModelError as exc:
        raise HTTPException(422, str(exc)) from exc
    db.commit()
    return result


@api_router.put("/api/settings/models-dir", response_model=ModelsDirOut)
def set_models_dir(body: AppBackupDirUpdate, request: Request, db: Session = Depends(get_db)):
    if body.dir:
        try:
            model_files.set_models_root(db, body.dir)
        except model_files.ModelError as exc:
            raise HTTPException(422, str(exc)) from exc
    else:
        model_files.unset_models_root(db)
    db.commit()
    return get_models_dir(request, db)


@api_router.get("/api/runtimes", response_model=list[RuntimeOut])
def list_runtimes(request: Request, db: Session = Depends(get_db)):
    rows = model_files.runtime_rows(db)
    out = []
    for spec in model_catalog.RUNTIMES.values():
        d = model_catalog.runtime_to_dict(spec)
        flavors = {k: v for k, v in rows.items() if k.startswith(f"{spec.key}-")}
        d["installs"] = flavors
        d["install"] = next(iter(flavors.values()), None)
        out.append(d)
    return out


class RuntimeInstallRequest(BaseModel):
    flavor: str = "vulkan"


class RuntimeBenchmarkRequest(BaseModel):
    flavor: str
    median_total_ms: float
    runs: int
    gpu: str | None = None
    params: dict | None = None


@api_router.post("/api/runtimes/{runtime_key}/install")
def install_runtime(runtime_key: str, request: Request,
                    body: RuntimeInstallRequest | None = None,
                    db: Session = Depends(get_db)):
    """User-approved runtime-flavor download (e.g. the CUDA build after the
    Vulkan build). Queues an app-level ops.model.download job that installs
    ONLY the runtime — no model weights are touched."""
    rt_spec = model_catalog.get_runtime(runtime_key)
    if rt_spec is None:
        raise HTTPException(404, "unknown runtime")
    flavor = (body or RuntimeInstallRequest()).flavor
    if flavor not in rt_spec.backends:
        raise HTTPException(422, f"runtime '{runtime_key}' has no '{flavor}' backend")
    rows = model_files.runtime_rows(db)
    cur = rows.get(f"{runtime_key}-{flavor}") or {}
    if cur.get("status") == "installed":
        raise HTTPException(409, f"runtime {runtime_key}-{flavor} is already installed")
    in_flight = db.execute(
        select(GenerationJob).where(
            GenerationJob.adapter_id == "ops.model.download",
            GenerationJob.status.in_(("queued", "running")),
        )
    ).scalars().first()
    if in_flight is not None:
        raise HTTPException(409, "a download is already in progress — ops lane is serialized")
    job = request.app.state.job_engine.enqueue(
        db, project_id=None, adapter_id="ops.model.download", kind="model_download",
        request={"runtime_only": True, "runtime_key": runtime_key,
                 "runtime_flavor": flavor},
        idempotency_key=f"rtdl:{runtime_key}:{flavor}:{new_id()[:8]}",
        model_key=None,
    )
    db.commit()
    request.app.state.job_engine.wake()
    return {"job_id": job.id, "status": job.status}


@api_router.post("/api/runtimes/{runtime_key}/benchmark")
def record_benchmark(runtime_key: str, body: RuntimeBenchmarkRequest,
                     db: Session = Depends(get_db)):
    """Record a measured benchmark result for an installed runtime flavor.
    Automatic backend selection prefers the fastest measured installed
    backend (B.3 — measured over assumed)."""
    row_key = f"{runtime_key}-{body.flavor}"
    row = db.execute(
        select(RuntimeInstall).where(RuntimeInstall.runtime_key == row_key)
    ).scalar_one_or_none()
    if row is None or row.status != "installed":
        raise HTTPException(404, f"runtime {row_key} is not installed")
    meta = dict(row.meta_json or {})
    meta["benchmark"] = {
        "median_total_ms": body.median_total_ms,
        "runs": body.runs,
        "gpu": body.gpu,
        "runtime_version": row.version,
        "params": body.params or {},
        "measured_at": utcnow().isoformat(),
    }
    row.meta_json = meta
    db.commit()
    return {"runtime_key": row_key, "benchmark": meta["benchmark"]}


# ---------- C5-E: consolidated compatibility ----------

@api_router.get("/api/compatibility")
def get_compatibility(request: Request, db: Session = Depends(get_db)):
    """Consolidated model/runtime/hardware compatibility matrix — one
    availability class per model (installed_operational |
    installed_limited | installable | planned | blocked), runtime flavor
    state, estimates, limitations. Read-only."""
    from app.domain import compat_report
    return compat_report.build_report(db, request.app.state.settings,
                                      hardware.probe())


@api_router.get("/api/models/{model_key}/preflight")
def get_model_preflight(model_key: str, request: Request,
                        db: Session = Depends(get_db)):
    """Beginner install preflight: download size, disk space, VRAM/RAM
    estimates, runtime state, license, limitations. Approval is still
    required — this only informs it."""
    from app.domain import compat_report
    spec = model_catalog.get(model_key)
    if spec is None:
        raise HTTPException(404, "unknown model")
    return compat_report.preflight(db, request.app.state.settings, spec,
                                   hardware.probe())


@api_router.get("/api/models/{model_key}/validation")
def get_model_validation(model_key: str, request: Request,
                         verify_hashes: bool = False,
                         db: Session = Depends(get_db)):
    """Read-only validation report: catalog completeness, runtime/binary
    availability, model files, optional sha256 verification, capability
    contract consistency, parameter sanity, driver/runtime match.
    Never runs inference."""
    from app.domain import compat_report
    spec = model_catalog.get(model_key)
    if spec is None:
        raise HTTPException(404, "unknown model")
    return compat_report.validate_model(
        db, request.app.state.settings, spec,
        request.app.state.adapter_registry, hardware.probe(),
        verify_hashes=verify_hashes)


class ModelBenchmarkRequest(BaseModel):
    runs: int = 3
    runtime_flavor: str | None = None


@api_router.post("/api/models/{model_key}/benchmark")
def start_model_benchmark(model_key: str, request: Request,
                          body: ModelBenchmarkRequest | None = None,
                          db: Session = Depends(get_db)):
    """Explicit, user-initiated benchmark of an INSTALLED local model —
    durable ops-lane job, cancellable via /api/jobs/{id}/cancel.
    No downloads, no cloud calls; uninstalled/planned/non-local models
    are refused."""
    spec = model_catalog.get(model_key)
    if spec is None:
        raise HTTPException(404, "unknown model")
    if spec.support == "planned":
        raise HTTPException(422, "planned models cannot be benchmarked")
    if spec.adapter_id != "gen.image.sdcpp":
        raise HTTPException(
            422, f"benchmark harness not available for '{spec.adapter_id}' "
                 "models yet — only local sd.cpp image models are supported")
    installs = model_files.installs_by_key(db)
    if (installs.get(model_key) or {}).get("status") != "installed":
        raise HTTPException(
            422, f"model '{model_key}' is not installed — benchmarks never "
                 "download; install it first")
    body = body or ModelBenchmarkRequest()
    if not 1 <= body.runs <= 5:
        raise HTTPException(422, "runs must be between 1 and 5")
    job = request.app.state.job_engine.enqueue(
        db, project_id=None, adapter_id="ops.model.benchmark",
        kind="model_benchmark",
        request={"model_key": model_key, "runs": body.runs,
                 "runtime_flavor": body.runtime_flavor},
        idempotency_key=f"bench:{model_key}:{new_id()[:8]}",
        model_key=model_key,
    )
    db.commit()
    request.app.state.job_engine.wake()
    return {"job_id": job.id, "status": job.status}


@api_router.get("/api/gpu/scheduler")
def gpu_scheduler_state(request: Request):
    sched = getattr(request.app.state, "gpu_scheduler", None)
    if sched is None:
        return {"gpu_count": 0, "free": 0, "slots": [], "prefs": {}}
    return sched.state()


@api_router.put("/api/settings/gpu", response_model=GpuSettingsOut)
def set_gpu_settings(body: GpuSettingsUpdate, request: Request, db: Session = Depends(get_db)):
    if body.mode is not None and body.mode not in ("auto", "manual"):
        raise HTTPException(422, "mode must be 'auto' or 'manual'")
    if body.preferred_gpu is not None:
        idx = body.preferred_gpu
        gpus = hardware.probe().get("gpus") or []
        if idx < 0 or not any(g.get("index") == idx for g in gpus):
            raise HTTPException(422, f"no detected GPU with index {idx}")
    if body.mode is not None:
        model_files._set_setting(db, "gpu_mode", body.mode)
    if body.preferred_gpu is not None:
        model_files._set_setting(db, "preferred_gpu", body.preferred_gpu)
    db.commit()
    sched = getattr(request.app.state, "gpu_scheduler", None)
    st = sched.state() if sched else {"slots": [], "free": 0, "gpu_count": 0}
    return {
        "mode": model_files._get_setting(db, "gpu_mode") or "auto",
        "preferred_gpu": model_files._get_setting(db, "preferred_gpu"),
        **st,
    }


# ---------- C4-E — unified resource status ----------


_RESOURCE_SETTING_KEYS = (
    ("os_headroom_gib", "res_os_headroom_gib"),
    ("critical_commit_gib", "res_critical_commit_gib"),
    ("critical_phys_gib", "res_critical_phys_gib"),
    ("min_disk_free_gib", "res_min_disk_gib"),
    ("vram_headroom_gib", "res_vram_headroom_gib"),
)


def _resource_settings(db: Session) -> dict:
    return {field: model_files._get_setting(db, key)
            for field, key in _RESOURCE_SETTING_KEYS}


@api_router.get("/api/system/resources")
def system_resources(request: Request, db: Session = Depends(get_db)):
    """C4-E — one honest snapshot for the resource view: measured RAM,
    pressure, GPU slots, disk free on the volumes that matter, queued/
    running jobs with their wait reasons, and the thresholds in force
    (user override vs default is explicit)."""
    from app.domain import resources
    from app.db.models import GenerationJob
    sched = getattr(request.app.state, "gpu_scheduler", None)
    overrides = resources.settings_overrides(db)
    margins = resources.safety_margins(overrides)
    mem = resources.system_memory()
    jobs = db.execute(
        select(GenerationJob).where(
            GenerationJob.status.in_(("queued", "running")))
        .order_by(GenerationJob.created_at)
    ).scalars().all()
    data_dir = request.app.state.settings.data_dir
    from app.db.models import StorageLocation
    loc = db.execute(select(StorageLocation).where(
        StorageLocation.is_default.is_(True))).scalar_one_or_none()
    return {
        "memory": {**mem, "pressure": resources.pressure_state()},
        "gpus": sched.state() if sched else
                {"gpu_count": 0, "free": 0, "slots": [], "prefs": {}},
        "disk": {
            "work_dir": str(data_dir),
            "work_dir_free_bytes": resources.disk_free_bytes(data_dir),
            "storage_root": (loc.root_path if loc else None),
            "storage_root_free_bytes": (
                resources.disk_free_bytes(loc.root_path) if loc else None),
        },
        "jobs": {
            "queued": [{"id": j.id, "kind": j.kind,
                        "adapter_id": j.adapter_id,
                        "project_id": j.project_id,
                        "message": j.message} for j in jobs
                       if j.status == "queued"],
            "running": [{"id": j.id, "kind": j.kind,
                         "adapter_id": j.adapter_id,
                         "project_id": j.project_id,
                         "gpu_index": j.gpu_index,
                         "progress": j.progress,
                         "message": j.message} for j in jobs
                        if j.status == "running"],
        },
        "margins": {
            "effective": margins,
            "user_overrides": overrides,
            # measured vs estimated is explicit — nothing silently mixes
            "basis": "measured" if not overrides else
                     "measured + user overrides",
        },
        "settings": _resource_settings(db),
    }


@api_router.put("/api/settings/resources",
                response_model=ResourceSettingsOut)
def set_resource_settings(body: ResourceSettingsUpdate,
                          request: Request,
                          db: Session = Depends(get_db)):
    """C4-E — write user admission thresholds. Values are GiB; None
    leaves the field at its default. An override can never weaken the
    built-in critical floors, and non-finite or absurd values refuse."""
    import math
    from app.domain import resources
    # Hard floors — user overrides may only make admission STRICTER.
    floors = resources.safety_margins()
    floor_gib = {
        "critical_commit_gib":
            floors["critical_commit_headroom_bytes"] / (1024 ** 3),
        "critical_phys_gib":
            floors["critical_avail_phys_bytes"] / (1024 ** 3),
    }
    vals = body.model_dump(exclude_unset=True)
    for field, v in vals.items():
        if v is None:
            continue
        if not math.isfinite(v):
            raise HTTPException(422, f"{field} must be a finite number")
        if v < 0:
            raise HTTPException(422, f"{field} cannot be negative")
        if v > 4096:
            raise HTTPException(422, f"{field} cannot exceed 4096 GiB")
        if field in floor_gib and v < floor_gib[field]:
            raise HTTPException(
                422, f"{field} cannot be below the built-in safety "
                     f"floor ({floor_gib[field]:g} GiB)")
        key = dict(_RESOURCE_SETTING_KEYS)[field]
        model_files._set_setting(db, key, float(v))
    db.commit()
    return _resource_settings(db)


# ---------- projects ----------

@api_router.get("/api/projects", response_model=list[ProjectCardOut])
def list_projects(
    db: Session = Depends(get_db),
    search: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    sort: str = "updated_at",
    order: str = "desc",
    include_archived: bool = True,
) -> list[dict]:
    q = select(Project)
    if search:
        q = q.where(Project.name.like(f"%{search}%"))
    if status:
        q = q.where(Project.status == status)
    elif not include_archived:
        q = q.where(Project.status == "active")
    if kind:
        q = q.where(Project.kind == kind)
    sort_col = {"updated_at": Project.updated_at, "created_at": Project.created_at,
                "name": Project.name}.get(sort, Project.updated_at)
    q = q.order_by(asc(sort_col) if order == "asc" else desc(sort_col))
    projects = db.execute(q).scalars().all()

    # Rollup counts + thumbnails in a handful of grouped queries (O(#projects)
    # rows scanned, not O(assets * projects)).
    out: list[dict] = []
    if not projects:
        return out
    pids = [p.id for p in projects]

    def counts_for(model):
        return dict(
            db.execute(
                select(model.project_id, func.count())
                .where(model.project_id.in_(pids))
                .group_by(model.project_id)
            ).all()
        )

    char_c = counts_for(Character)
    world_c = counts_for(World)
    loc_c = counts_for(Location)
    asset_c = counts_for(Asset)
    job_c = dict(
        db.execute(
            select(GenerationJob.project_id, func.count())
            .where(GenerationJob.project_id.in_(pids), GenerationJob.status.in_(["queued", "running"]))
            .group_by(GenerationJob.project_id)
        ).all()
    )
    thumbs = dict(
        db.execute(
            select(Asset.project_id, func.min(Asset.id))
            .where(Asset.project_id.in_(pids), Asset.kind == "image", Asset.status == "ready")
            .group_by(Asset.project_id)
        ).all()
    )
    for p in projects:
        d = {c.name: getattr(p, c.name) for c in Project.__table__.columns}
        d["counts"] = {
            "characters": char_c.get(p.id, 0),
            "worlds": world_c.get(p.id, 0),
            "locations": loc_c.get(p.id, 0),
            "assets": asset_c.get(p.id, 0),
        }
        d["running_jobs"] = job_c.get(p.id, 0)
        d["thumbnail_asset_id"] = thumbs.get(p.id)
        out.append(d)
    return out


@api_router.post("/api/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    if body.storage_location_id and db.get(StorageLocation, body.storage_location_id) is None:
        raise HTTPException(404, "storage location not found")
    project = Project(
        name=body.name, template=body.template, kind=body.kind,
        aspect_ratio=body.aspect_ratio, width=body.width, height=body.height,
        fps_num=body.fps_num, fps_den=body.fps_den, style=body.style,
        storage_location_id=body.storage_location_id,
        settings_json=body.settings,
    )
    db.add(project)
    db.commit()
    return project


@api_router.get("/api/projects/{project_id}", response_model=ProjectOut)
def read_project(project: Project = Depends(get_project)) -> Project:
    return project


@api_router.get("/api/projects/{project_id}/revisions", response_model=list[ProjectRevisionOut])
def list_revisions(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    return db.execute(
        select(ProjectRevision)
        .where(ProjectRevision.project_id == project.id)
        .order_by(ProjectRevision.revision.desc())
        .limit(200)
    ).scalars().all()


@api_router.get("/api/projects/{project_id}/prompt-draft", response_model=PromptDraftOut)
def read_prompt_draft(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    draft = db.execute(select(PromptDraft).where(PromptDraft.project_id == project.id)).scalar_one_or_none()
    if draft is None:
        return {"project_id": project.id, "text": "", "scope": "this_take", "updated_at": project.updated_at}
    return draft


# ---------- commands ----------

@api_router.post("/api/projects/{project_id}/commands/preview", response_model=CommandProposalOut)
def preview_command(
    body: CommandPreviewRequest,
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    try:
        st = request.app.state
        return cmd.preview(
            db, project,
            command_type=body.command_type, payload=body.payload, scope=body.scope,
            origin=body.origin, target_type=body.target_type, target_id=body.target_id,
            vault=getattr(st, "provider_vault", None),
            gpu_scheduler=getattr(st, "gpu_scheduler", None),
            adapter_registry=getattr(st, "adapter_registry", None),
        )
    except cmd.CommandError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def _spend_isolation(body: CommandCommitRequest, db: Session = Depends(get_db)):
    """Scoped locking for spend-guarded commands (C2.0b): these commits
    check-then-act on financial state, so the whole transaction must
    BEGIN IMMEDIATE — the write lock is held before the first SELECT,
    and concurrent admissions serialize on SQLite's single-writer lock
    plus busy_timeout. Resolved BEFORE get_project so the project's
    own SELECT lands inside the immediate transaction. All other
    commands keep the default deferred behavior. If a transaction is
    somehow already open, sqlite raises rather than silently degrading
    to a racy deferred read."""
    if body.command_type in cmd.SPEND_GUARDED_COMMANDS:
        from sqlalchemy import text as _text
        db.execute(_text("BEGIN IMMEDIATE"))


@api_router.post("/api/projects/{project_id}/commands/commit")
async def commit_command(
    request: Request,
    body: CommandCommitRequest,
    _spend_lock: None = Depends(_spend_isolation),
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    try:
        result = cmd.commit(
            db, project,
            command_type=body.command_type, payload=body.payload, scope=body.scope,
            origin=body.origin, target_type=body.target_type, target_id=body.target_id,
            expected_revision=body.expected_revision, idempotency_key=body.idempotency_key,
            job_engine=request.app.state.job_engine,
            storage=request.app.state.storage,
            vault=request.app.state.provider_vault,
            session_factory=getattr(
                request.app.state, "session_factory", None),
            data_dir=getattr(
                getattr(request.app.state, "settings", None),
                "data_dir", None),
        )
    except cmd.CommandError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    command: CreativeCommand = result["command"]
    db.commit()
    post_commit = result.get("post_commit")
    if post_commit is not None:
        # B2a validate_run — the live dispatch runs only AFTER the
        # command transaction has released the write lock. Refusals
        # here are real dispatch outcomes; the command stays committed
        # as the durable record of the attempt.
        try:
            post_commit()
        except Exception as exc:
            from app.domain import live_permit as _lp
            from app.domain.workers.live_transport import (
                LiveDispatchRefused)
            if isinstance(exc, (_lp.PermitRefused, LiveDispatchRefused)):
                raise HTTPException(
                    422, {"error": exc.code,
                          "detail": str(exc)[:300]}) from exc
            raise
    await request.app.state.event_bus.publish(
        "command.committed", project.id, command.id,
        {"command_type": command.command_type, "revision": project.revision},
    )
    if result.get("job") is not None:
        request.app.state.job_engine.wake()
    return {
        "command_id": command.id,
        "status": command.status,
        "revision": project.revision,
        "idempotent_replay": result["idempotent_replay"],
        "job_id": result["job"].id if result.get("job") else None,
        "duplicate_project_id": result.get("duplicate_project_id"),
        "deleted": result.get("deleted", False),
        "warnings": result.get("warnings", []),
        "bridge": result.get("bridge"),
    }


@api_router.get("/api/projects/{project_id}/spend/summary")
def spend_summary(
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
    environment: str = Query("live"),
):
    """Per-scope consumption vs budget limits. environment is a
    reporting filter — 'simulated' rows never mix into 'live' sums."""
    from app.domain import spend as _spend
    if environment not in ("live", "simulated"):
        raise HTTPException(422, "environment must be live or simulated")
    return _spend.summary(db, project_id=project.id,
                          environment=environment,
                          app_state=request.app.state)


@api_router.get("/api/projects/{project_id}/spend/reservations")
def spend_reservations(
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    """Reservations for this project's remote jobs — open holds first.
    Read-only; all mutations stay on the command path."""
    from app.domain import spend as _spend
    return _spend.reservation_list(db, project_id=project.id)


@api_router.get("/api/projects/{project_id}/cloud/live-status")
def cloud_live_status(
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    """C2 exit gate Stage A — read-only enablement/admission view.
    Mutations stay on the command path (cloud.live.*)."""
    from app.domain import live_gate as _lg
    return _lg.status_report(db, request.app.state.provider_vault)


@api_router.get("/api/projects/{project_id}/commands", response_model=list[CommandOut])
def list_commands(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    return db.execute(
        select(CreativeCommand)
        .where(CreativeCommand.project_id == project.id)
        .order_by(CreativeCommand.created_at.desc())
        .limit(200)
    ).scalars().all()


@api_router.post("/api/projects/{project_id}/commands/{command_id}/undo", response_model=CommandOut)
def undo_command(command_id: str, project: Project = Depends(get_project), db: Session = Depends(get_db)):
    try:
        command = cmd.undo(db, project, command_id=command_id)
    except cmd.CommandError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    db.commit()
    return command


# ---------- B-3c — LoRA resolution preview (read-only) ----------

@api_router.post("/api/projects/{project_id}/loras/preview",
                 response_model=LoraPreviewOut)
def lora_resolution_preview(
    body: LoraPreviewRequest,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    """Backend-authoritative preview of which LoRA selections the B-1
    gate would accept for a local shot/keyframe generation request.

    Read-only and side-effect-free: resolver SELECTs + file stat only —
    no command recorded, no job queued, no subprocess, no download.
    Refusals return 200 with accepted=false + explicit reasons so they
    render next to accepted output; a result is advisory only — any
    later generation re-validates against current state."""
    from app.domain.lora_resolve import resolve_lora_preview
    return resolve_lora_preview(
        db, project,
        model_key=body.model_key,
        loras_in=body.loras,
        shot_id=body.shot_id,
        keyframe_id=body.keyframe_id,
        prompt=body.prompt,
        negative_prompt=body.negative_prompt,
    )


# ---------- jobs ----------

@api_router.get("/api/projects/{project_id}/jobs", response_model=list[JobOut])
def list_jobs(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    return db.execute(
        select(GenerationJob)
        .where(GenerationJob.project_id == project.id)
        .order_by(GenerationJob.created_at.desc())
        .limit(200)
    ).scalars().all()


@api_router.get("/api/jobs", response_model=list[JobOut])
def list_all_jobs(db: Session = Depends(get_db)):
    return db.execute(
        select(GenerationJob).order_by(GenerationJob.created_at.desc()).limit(100)
    ).scalars().all()


@api_router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, db: Session = Depends(get_db)):
    job = db.get(GenerationJob, job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    result = request.app.state.job_engine.request_cancel(job_id)
    if result == "not_found":
        raise HTTPException(404, "job not found")
    if result == "terminal":
        raise HTTPException(409, f"job already {job.status}")
    return {"status": result}


# ---------- assets ----------

def _image_dims_backfill(storage, db: Session, asset) -> dict:
    """S3 — header-only dimension probe for legacy image assets whose
    AssetVersion.media_json predates dimension capture. Read-only and
    non-destructive: fills response metadata without touching CAS rows
    or hashes; any failure yields {} rather than breaking the list."""
    try:
        path = storage.asset_path(db, asset)
        if path is None or not path.exists():
            return {}
        from PIL import Image
        with Image.open(path) as im:
            return {"width": im.width, "height": im.height,
                    "format": (im.format or "").upper()}
    except Exception:
        return {}


@api_router.get("/api/projects/{project_id}/assets", response_model=AssetPageOut)
def list_assets(
    request: Request,
    project: Project = Depends(get_project), db: Session = Depends(get_db),
    offset: int = 0, limit: int = Query(default=100, le=500),
    kind: str | None = None, q: str | None = None, status: str | None = None,
    origin: str | None = None,
):
    base = select(Asset).where(Asset.project_id == project.id, Asset.status != "deleted")
    if kind:
        base = base.where(Asset.kind == kind)
    if q:
        base = base.where(Asset.name.like(f"%{q}%"))
    if status:
        base = base.where(Asset.status == status)
    if origin:
        base = base.where(Asset.origin == origin)
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    items = db.execute(
        base.order_by(Asset.created_at.desc()).offset(offset).limit(limit)
    ).scalars().all()
    # Provenance surface: generation metadata lives on the current
    # AssetVersion — join it so the list API exposes it (issue #20).
    ver_ids = [a.current_version_id for a in items if a.current_version_id]
    media_by_ver = {}
    if ver_ids:
        for v in db.execute(
            select(AssetVersion).where(AssetVersion.id.in_(ver_ids))
        ).scalars():
            media_by_ver[v.id] = v.media_json
    out_items = []
    storage = request.app.state.storage
    for a in items:
        media = dict(media_by_ver.get(a.current_version_id) or {})
        if a.kind == "image" and "width" not in media:
            media = {**_image_dims_backfill(storage, db, a), **media}
        out_items.append({
            "id": a.id, "project_id": a.project_id, "name": a.name,
            "kind": a.kind, "origin": a.origin, "scope": a.scope,
            "status": a.status, "uri": a.uri, "sha256": a.sha256,
            "byte_size": a.byte_size, "current_version_id": a.current_version_id,
            "created_at": a.created_at,
            "provenance_json": a.provenance_json or {},
            "media_json": media,
        })
    return {"items": out_items, "total": total, "offset": offset, "limit": limit}


@api_router.get("/api/projects/{project_id}/assets/{asset_id}/versions", response_model=list[AssetVersionOut])
def list_asset_versions(asset_id: str, project: Project = Depends(get_project), db: Session = Depends(get_db)):
    asset = db.get(Asset, asset_id)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(404, "asset not found in this project")
    return db.execute(
        select(AssetVersion).where(AssetVersion.asset_id == asset_id).order_by(AssetVersion.version_no)
    ).scalars().all()


@api_router.get("/api/projects/{project_id}/assets/{asset_id}/file")
def asset_file(
    request: Request, asset_id: str,
    project: Project = Depends(get_project), db: Session = Depends(get_db),
):
    asset = db.get(Asset, asset_id)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(404, "asset not found in this project")
    path = request.app.state.storage.asset_path(db, asset)
    if path is None or not path.exists():
        raise HTTPException(404, "asset file missing on disk")
    return FileResponse(path)


def _waveform_manifest(db: Session, asset: Asset) -> dict:
    """The version's waveform manifest (N2-B), or 404/409."""
    ver = db.get(AssetVersion, asset.current_version_id)
    media = (ver.media_json or {}) if ver else {}
    wf = media.get("waveform") or {}
    if not wf.get("levels"):
        raise HTTPException(
            409, "no waveform built for this asset — run "
                 "asset.waveform first")
    if wf.get("built_sha256") != (ver.sha256 if ver else None):
        raise HTTPException(
            409, "waveform predates the current source version — "
                 "rebuild via asset.waveform")
    return wf


@api_router.get("/api/projects/{project_id}/assets/{asset_id}/waveform")
def waveform_manifest(
    asset_id: str,
    project: Project = Depends(get_project), db: Session = Depends(get_db),
):
    """N2-B — the envelope manifest: available LOD levels (samples-per-
    bucket, bucket counts, CAS shas) + source rate/channels/samples."""
    asset = db.get(Asset, asset_id)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(404, "asset not found in this project")
    return _waveform_manifest(db, asset)


@api_router.get("/api/projects/{project_id}/assets/{asset_id}/waveform/level")
def waveform_level_slice(
    request: Request, asset_id: str,
    level: int, start_bucket: int = 0, end_bucket: int | None = None,
    project: Project = Depends(get_project), db: Session = Depends(get_db),
):
    """N2-B — a windowed slice of one LOD level's peaks. The client
    asks for exactly the bucket range its viewport needs — long audio
    never lands in frontend memory wholesale."""
    asset = db.get(Asset, asset_id)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(404, "asset not found in this project")
    wf = _waveform_manifest(db, asset)
    entry = next((l for l in wf["levels"] if l["spb"] == level), None)
    if entry is None:
        raise HTTPException(
            404, f"no level at {level} samples/bucket — "
                 f"available: {[l['spb'] for l in wf['levels']]}")
    from app.db.models import AssetFile
    af = db.get(AssetFile, entry["sha256"])
    if af is None:
        raise HTTPException(404, "waveform blob missing from CAS")
    path = request.app.state.storage.file_path(db, af)
    if not path.exists():
        raise HTTPException(404, "waveform blob missing on disk")
    # CAS integrity, verified at serve time — a corrupted or swapped
    # blob never masquerades as this source's waveform.
    import hashlib as _hashlib
    import json as _json
    raw = path.read_bytes()
    if _hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise HTTPException(
            409, "waveform blob failed CAS integrity — "
                 "rebuild via asset.waveform")
    try:
        doc = _json.loads(raw)
    except _json.JSONDecodeError:
        raise HTTPException(
            409, "waveform blob corrupt — rebuild via asset.waveform")
    ver = db.get(AssetVersion, asset.current_version_id)
    if doc.get("source_sha256") != (ver.sha256 if ver else None):
        raise HTTPException(
            409, "waveform blob names a different source — "
                 "rebuild via asset.waveform")
    total = doc["buckets"]
    s = max(0, min(start_bucket, total))
    e = total if end_bucket is None else max(s, min(end_bucket, total))
    return {
        "format": doc["format"],
        "spb": doc["spb"],
        "buckets": total,
        "bucket_start": s,
        "bucket_end": e,
        "channels": doc["channels"],
        "sample_rate": doc["sample_rate"],
        "peaks": [ch[s:e] for ch in doc["peaks"]],
    }


@api_router.post("/api/projects/{project_id}/assets/upload-source")
async def upload_source_image(
    request: Request,
    project: Project = Depends(get_project),
):
    """C3-D4 — stage painted/editor-produced image bytes to a server
    path so the standard `asset.import` command can ingest them through
    the normal CAS job path. The payload passes the same image-safety
    gate as every image input; the file lands under data_dir/uploads
    with a content-derived name — never caller-controlled."""
    from app.domain import image_safety, upload_staging
    data = await request.body()
    try:
        info = image_safety.inspect_image(data)
    except Exception as exc:
        raise HTTPException(422, f"not a valid image payload: {exc}")
    import hashlib
    sha = hashlib.sha256(data).hexdigest()
    up_dir = upload_staging.uploads_dir(
        request.app.state.settings.data_dir, project.id)
    up_dir.mkdir(parents=True, exist_ok=True)
    path = upload_staging.stage_path(
        request.app.state.settings.data_dir, project.id, sha, info.format)
    if not path.exists():
        path.write_bytes(data)
    # S2 — bounded lazy sweep of this project's expired staging files.
    # Recoverable-job references are preserved; counts only, no paths.
    with request.app.state.session_factory() as db:
        sweep = upload_staging.cleanup_project_staged(
            db, request.app.state.settings.data_dir, project.id)
    out = {"path": str(path), "sha256": sha,
           "width": info.width, "height": info.height,
           "format": info.format}
    if sweep["deleted"]:
        out["staging_sweep"] = {"deleted": sweep["deleted"],
                                "reclaimed_bytes": sweep["reclaimed_bytes"]}
    return out


@api_router.get("/api/projects/{project_id}/shared-assets", response_model=list[SharedAssetOut])
def list_shared(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    """Shared-library assets visible to this project: globally shared rows
    plus anything already linked in. This is the only cross-project read —
    it goes through the sharing model, never a raw join on another
    project's private assets."""
    shared = db.execute(select(Asset).where(Asset.scope == "shared")).scalars().all()
    linked_ids = {
        r[0]
        for r in db.execute(
            select(SharedAssetLink.asset_id).where(SharedAssetLink.project_id == project.id)
        ).all()
    }
    names = dict(db.execute(select(Project.id, Project.name)).all())
    return [
        {
            "asset": a,
            "source_project_name": names.get(a.project_id, "?"),
            "linked_here": a.id in linked_ids or a.project_id == project.id,
        }
        for a in shared
    ]


# ---------- storage locations (app-level) ----------

@api_router.get("/api/storage/locations", response_model=list[StorageLocationOut])
def list_locations(db: Session = Depends(get_db)):
    return db.execute(select(StorageLocation).order_by(StorageLocation.created_at)).scalars().all()


@api_router.post("/api/storage/locations", response_model=StorageLocationOut, status_code=201)
def create_location(body: StorageLocationCreate, request: Request, db: Session = Depends(get_db)):
    root = Path(body.root_path)
    if not root.is_absolute():
        raise HTTPException(422, "root_path must be absolute")
    loc = StorageLocation(
        id=new_id(), label=body.label, root_path=str(root), kind=body.kind,
        status="online" if root.is_dir() else "missing",
    )
    db.add(loc)
    db.commit()
    return loc


@api_router.patch("/api/storage/locations/{location_id}", response_model=StorageLocationOut)
def update_location(
    location_id: str, body: StorageLocationUpdate, request: Request, db: Session = Depends(get_db),
):
    loc = db.get(StorageLocation, location_id)
    if loc is None:
        raise HTTPException(404, "storage location not found")
    if body.label is not None:
        loc.label = body.label
    if body.root_path is not None:
        try:
            request.app.state.storage.relink_location(db, loc.id, Path(body.root_path))
        except FileNotFoundError as exc:
            raise HTTPException(422, str(exc)) from exc
    db.commit()
    return loc


# ---------- backups ----------

@api_router.get("/api/settings/backup-dir", response_model=BackupDestinationOut)
def get_backup_destination(request: Request, db: Session = Depends(get_db)):
    """App-level effective backup directory + validation snapshot."""
    from app.domain import backup_dest
    return backup_dest.destination_snapshot(db, request.app.state.settings, None)


@api_router.put("/api/settings/backup-dir", response_model=BackupDestinationOut)
def set_backup_destination(body: AppBackupDirUpdate, request: Request, db: Session = Depends(get_db)):
    """Set (or reset, dir=null) the app-wide default backup directory.
    The dir is validated — errors are returned as warnings in the
    snapshot so the UI can surface them, but an unusable dir is NOT
    saved."""
    from app.domain import backup_dest
    if body.dir:
        p = Path(body.dir)
        if not p.is_absolute():
            raise HTTPException(422, "backup dir must be an absolute path")
        result = backup_dest.validate_destination(p)
        if result["errors"]:
            raise HTTPException(422, result["errors"][0])
        backup_dest.set_app_default(db, str(p))
    else:
        backup_dest.set_app_default(db, None)  # reset → built-in
    db.commit()
    return backup_dest.destination_snapshot(db, request.app.state.settings, None)


@api_router.get(
    "/api/projects/{project_id}/backup-destination",
    response_model=BackupDestinationOut,
)
def get_project_backup_destination(
    request: Request,
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    """Effective destination for this project — resolves the override
    chain and validates live (free space, writability, size estimate)."""
    from app.domain import backup_dest
    return backup_dest.destination_snapshot(db, request.app.state.settings, project)


@api_router.post("/api/backups/validate-destination", response_model=BackupDestinationOut)
def validate_backup_destination(body: BackupDestinationValidate, request: Request, db: Session = Depends(get_db)):
    """Probe an arbitrary candidate directory without changing anything."""
    from app.domain import backup_dest
    project = db.get(Project, body.project_id) if body.project_id else None
    needed = backup_dest.estimate_package_bytes(db, project.id) if project else 0
    result = backup_dest.validate_destination(body.path, needed_bytes=needed)
    return {
        "dir": result["path"],
        "source": "candidate",
        "app_default_dir": "",
        "builtin_dir": str(request.app.state.settings.backups_dir),
        "project_override": "",
        "estimated_bytes": needed,
        **result,
    }


@api_router.get("/api/projects/{project_id}/backups", response_model=list[BackupRunOut])
def list_backups(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    return db.execute(
        select(BackupRun).where(BackupRun.project_id == project.id)
        .order_by(BackupRun.created_at.desc()).limit(100)
    ).scalars().all()


@api_router.get("/api/backups", response_model=list[BackupRunOut])
def list_all_backups(db: Session = Depends(get_db)):
    return db.execute(select(BackupRun).order_by(BackupRun.created_at.desc()).limit(100)).scalars().all()


@api_router.post("/api/packages/preview", response_model=PackagePreviewOut)
def preview_package(body: PackageImportRequest, request: Request):
    pkg = Path(body.package_path)
    if not pkg.is_file():
        raise HTTPException(404, f"package not found: {pkg}")
    try:
        manifest = read_manifest(pkg)
    except PackageError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "project_name": manifest["project"].get("name", "?"),
        "format_version": manifest["format_version"],
        "created_at": manifest.get("created_at"),
        "counts": {
            "characters": len(manifest["entities"].get("characters", [])),
            "worlds": len(manifest["entities"].get("worlds", [])),
            "locations": len(manifest["entities"].get("locations", [])),
            "assets": len(manifest.get("assets", [])),
        },
        "files": len(manifest.get("files", [])),
        "byte_size": pkg.stat().st_size,
    }


@api_router.post("/api/packages/import")
async def import_package(body: PackageImportRequest, request: Request, db: Session = Depends(get_db)):
    """Create an 'importing' stub project and commit a backup.import command
    on it — the job fills the stub transactionally; a failure leaves no
    half-project visible."""
    pkg = Path(body.package_path)
    if not pkg.is_file():
        raise HTTPException(404, f"package not found: {pkg}")
    try:
        manifest = read_manifest(pkg)
    except PackageError as exc:
        raise HTTPException(422, str(exc)) from exc
    stub = Project(
        id=new_id(), name=f"Importing {manifest['project'].get('name', 'package')}…",
        status="importing", settings_json={"import_source": str(pkg)},
    )
    db.add(stub)
    db.flush()
    try:
        result = cmd.commit(
            db, stub,
            command_type="backup.import", payload={"package_path": str(pkg)},
            scope="project", origin="packages_import", target_type=None, target_id=None,
            expected_revision=0, idempotency_key=f"import:{stub.id}",
            job_engine=request.app.state.job_engine,
        )
    except cmd.CommandError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    db.commit()
    request.app.state.job_engine.wake()
    return {
        "project_id": stub.id,
        "job_id": result["job"].id if result.get("job") else None,
        "command_id": result["command"].id,
    }


# ---------- storyboard (M2) ----------
# Literal routes — must precede the dynamic {etype} catch-all below.

@api_router.get("/api/projects/{project_id}/storyboard", response_model=list[StoryboardSceneOut])
def get_storyboard(
    project: Project = Depends(get_project), db: Session = Depends(get_db),
    include_archived: bool = True,
):
    """Full board in one payload: scenes → shots → cast with names."""
    from app.domain import storyboard as sb
    return sb.storyboard(db, project.id, include_archived=include_archived)


@api_router.get("/api/projects/{project_id}/scenes/{scene_id}/versions",
                response_model=list[EntityRevisionOut])
def scene_versions(scene_id: str, project: Project = Depends(get_project),
                   db: Session = Depends(get_db)):
    from app.domain import storyboard as sb
    try:
        sb.get_scene(db, scene_id, project.id)
    except KeyError:
        raise HTTPException(404, "scene not found in this project") from None
    return sb.revision_history(db, "scene", scene_id)


@api_router.get("/api/projects/{project_id}/shots/{shot_id}/versions",
                response_model=list[EntityRevisionOut])
def shot_versions(shot_id: str, project: Project = Depends(get_project),
                  db: Session = Depends(get_db)):
    from app.domain import storyboard as sb
    try:
        sb.get_shot(db, shot_id, project.id)
    except KeyError:
        raise HTTPException(404, "shot not found in this project") from None
    return sb.revision_history(db, "shot", shot_id)


# ---------- wizard (A8) ----------

@api_router.get("/api/projects/{project_id}/wizard")
def get_wizard_state(project: Project = Depends(get_project),
                     db: Session = Depends(get_db)):
    """A8 — derived workflow state for the guided film wizard.

    Read-only: reports what already exists (screenplay → export), the
    persisted progress blob from project settings, generation-state
    honesty labels, and missing-input warnings. Mutations stay in the
    command system; progress writes ride `project.update_settings`.
    """
    from app.domain import wizard as wiz
    return wiz.wizard_state(db, project)


# ---------- keyframes (A3) ----------

@api_router.get("/api/projects/{project_id}/shots/{shot_id}/keyframes")
def list_keyframes(shot_id: str, project: Project = Depends(get_project),
                   db: Session = Depends(get_db),
                   include_archived: bool = True):
    """A shot's keyframes in position order — mutations are commands."""
    from app.domain import keyframes as kd
    try:
        rows = kd.list_keyframes(db, project.id, shot_id)
    except ValueError:
        raise HTTPException(404, "shot not found in this project") from None
    if not include_archived:
        rows = [r for r in rows if r.status == "active"]
    return [kd.serialize_keyframe(db, r) for r in rows]


@api_router.get("/api/projects/{project_id}/keyframes/{keyframe_id}")
def get_keyframe(keyframe_id: str, project: Project = Depends(get_project),
                 db: Session = Depends(get_db)):
    from app.domain import keyframes as kd
    try:
        return kd.serialize_keyframe(
            db, kd._get(db, project.id, keyframe_id))
    except ValueError:
        raise HTTPException(404, "keyframe not found in this project") from None


@api_router.get("/api/projects/{project_id}/keyframes/{keyframe_id}/versions",
                response_model=list[EntityRevisionOut])
def keyframe_versions(keyframe_id: str,
                      project: Project = Depends(get_project),
                      db: Session = Depends(get_db)):
    from app.domain import keyframes as kd
    from app.domain import storyboard as sb
    try:
        kd._get(db, project.id, keyframe_id)
    except ValueError:
        raise HTTPException(404, "keyframe not found in this project") from None
    return sb.revision_history(db, "keyframe", keyframe_id)


@api_router.get("/api/projects/{project_id}/keyframes/{keyframe_id}/draft")
def keyframe_draft(keyframe_id: str,
                   project: Project = Depends(get_project),
                   db: Session = Depends(get_db)):
    """Read-only compose draft — same output the keyframe.compose
    preview emits, for UI inspection without entering the command flow."""
    from app.domain import keyframes as kd
    try:
        return kd.compose_draft(db, project, keyframe_id)
    except ValueError as exc:
        msg = str(exc)
        raise HTTPException(404 if "not found" in msg else 422, msg) from None


# ---------- motion clips (A4) ----------

@api_router.get("/api/projects/{project_id}/shots/{shot_id}/clips")
def list_motion_clips(shot_id: str, project: Project = Depends(get_project),
                      db: Session = Depends(get_db),
                      include_archived: bool = True):
    """A shot's motion clips in position order — mutations are commands."""
    from app.domain import motion_clips as mc
    try:
        rows = mc.list_clips(db, project.id, shot_id)
    except ValueError:
        raise HTTPException(404, "shot not found in this project") from None
    if not include_archived:
        rows = [r for r in rows if r.status == "active"]
    return [mc.serialize_clip(db, r) for r in rows]


@api_router.get("/api/projects/{project_id}/clips/{clip_id}")
def get_motion_clip(clip_id: str, project: Project = Depends(get_project),
                    db: Session = Depends(get_db)):
    from app.domain import motion_clips as mc
    try:
        return mc.serialize_clip(
            db, mc._get(db, project.id, clip_id))
    except ValueError:
        raise HTTPException(404, "motion clip not found in this project") \
            from None


@api_router.get("/api/projects/{project_id}/clips/{clip_id}/versions",
                response_model=list[EntityRevisionOut])
def motion_clip_versions(clip_id: str,
                         project: Project = Depends(get_project),
                         db: Session = Depends(get_db)):
    from app.domain import motion_clips as mc
    from app.domain import storyboard as sb
    try:
        mc._get(db, project.id, clip_id)
    except ValueError:
        raise HTTPException(404, "motion clip not found in this project") \
            from None
    return sb.revision_history(db, "motion_clip", clip_id)


@api_router.get("/api/projects/{project_id}/clips/{clip_id}/draft")
def motion_clip_draft(clip_id: str,
                      project: Project = Depends(get_project),
                      db: Session = Depends(get_db)):
    """Read-only motion-prompt draft — same output the clip.compose
    preview emits, for UI inspection without entering the command flow."""
    from app.domain import motion_clips as mc
    try:
        return mc.compose_draft(db, project, clip_id)
    except ValueError as exc:
        msg = str(exc)
        raise HTTPException(404 if "not found" in msg else 422, msg) from None


# ---------- dialogue segments (A5) ----------

@api_router.get("/api/projects/{project_id}/dialogue")
def list_dialogue_segments(project: Project = Depends(get_project),
                           db: Session = Depends(get_db),
                           sp_scene_id: str | None = None,
                           scene_id: str | None = None,
                           shot_id: str | None = None,
                           include_archived: bool = True):
    """The project's dialogue segments in position order, filterable
    by screenplay scene, storyboard scene, or shot — mutations are
    commands."""
    from app.domain import dialogue as dlg
    rows = dlg.list_segments(db, project.id, sp_scene_id=sp_scene_id,
                             scene_id=scene_id, shot_id=shot_id)
    if not include_archived:
        rows = [r for r in rows if r.status == "active"]
    return [dlg.serialize_segment(db, r) for r in rows]


@api_router.get("/api/projects/{project_id}/dialogue/{segment_id}")
def get_dialogue_segment(segment_id: str,
                         project: Project = Depends(get_project),
                         db: Session = Depends(get_db)):
    from app.domain import dialogue as dlg
    try:
        return dlg.serialize_segment(
            db, dlg._get(db, project.id, segment_id))
    except ValueError:
        raise HTTPException(
            404, "dialogue segment not found in this project") from None


@api_router.get("/api/projects/{project_id}/dialogue/{segment_id}/versions",
                response_model=list[EntityRevisionOut])
def dialogue_segment_versions(segment_id: str,
                              project: Project = Depends(get_project),
                              db: Session = Depends(get_db)):
    from app.domain import dialogue as dlg
    from app.domain import storyboard as sb
    try:
        dlg._get(db, project.id, segment_id)
    except ValueError:
        raise HTTPException(
            404, "dialogue segment not found in this project") from None
    return sb.revision_history(db, "dialogue_segment", segment_id)


# ---------- first-cut assembly (A6) ----------

@api_router.get("/api/projects/{project_id}/assemblies")
def list_assembly_plans(project: Project = Depends(get_project),
                        db: Session = Depends(get_db),
                        timeline_id: str | None = None):
    """Assembly plans for the project (optionally per timeline) —
    mutations are commands: assembly.plan/apply/review/revert."""
    from app.domain import assembly as asm
    return asm.list_plans(db, project.id, timeline_id=timeline_id)


@api_router.get("/api/projects/{project_id}/assemblies/{plan_id}")
def get_assembly_plan(plan_id: str,
                      project: Project = Depends(get_project),
                      db: Session = Depends(get_db)):
    from app.domain import assembly as asm
    try:
        return asm.serialize_plan(
            db, asm.get_plan(db, project.id, plan_id))
    except ValueError:
        raise HTTPException(
            404, "assembly plan not found in this project") from None


@api_router.get("/api/projects/{project_id}/assemblies/{plan_id}/versions",
                response_model=list[EntityRevisionOut])
def assembly_plan_versions(plan_id: str,
                           project: Project = Depends(get_project),
                           db: Session = Depends(get_db)):
    from app.domain import assembly as asm
    from app.domain import storyboard as sb
    try:
        asm.get_plan(db, project.id, plan_id)
    except ValueError:
        raise HTTPException(
            404, "assembly plan not found in this project") from None
    return sb.revision_history(db, "assembly_plan", plan_id)


# ---------- screenplay + shot-list (A1) ----------

@api_router.get("/api/projects/{project_id}/screenplays")
def list_screenplays(
    project: Project = Depends(get_project), db: Session = Depends(get_db),
    include_archived: bool = False,
):
    """Project's screenplays, ordered — scenes NOT embedded (use the
    detail route); the list stays light for navigation."""
    from app.domain import screenplay as spd
    return spd.list_screenplays(db, project.id, include_archived=include_archived)


@api_router.get("/api/projects/{project_id}/screenplays/{screenplay_id}")
def get_screenplay(
    screenplay_id: str, project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    """One screenplay with its ordered scenes and elements."""
    from app.domain import screenplay as spd
    try:
        sp = spd.get_screenplay(db, screenplay_id, project.id)
    except KeyError:
        raise HTTPException(404, "screenplay not found in this project") from None
    return spd.screenplay_payload(db, sp)


@api_router.get("/api/projects/{project_id}/screenplays/{screenplay_id}/versions",
                response_model=list[EntityRevisionOut])
def screenplay_versions(screenplay_id: str,
                        project: Project = Depends(get_project),
                        db: Session = Depends(get_db)):
    from app.domain import screenplay as spd
    from app.domain import storyboard as sb
    try:
        spd.get_screenplay(db, screenplay_id, project.id)
    except KeyError:
        raise HTTPException(404, "screenplay not found in this project") from None
    return sb.revision_history(db, "screenplay", screenplay_id)


@api_router.get("/api/projects/{project_id}/screenplays/{screenplay_id}/shotlist-plan")
def screenplay_shotlist_plan(screenplay_id: str,
                             project: Project = Depends(get_project),
                             db: Session = Depends(get_db)):
    """The deterministic scaffold/sync diff without committing — same
    plan a shotlist.* preview emits, for read-only inspection."""
    from app.domain import screenplay as spd
    try:
        return spd.shotlist_plan(db, project.id, screenplay_id)
    except KeyError:
        raise HTTPException(404, "screenplay not found in this project") from None


@api_router.get("/api/projects/{project_id}/entity-assets",
                response_model=list[EntityAssetLinkOut])
def list_entity_asset_links(
    project: Project = Depends(get_project), db: Session = Depends(get_db),
    entity_type: str | None = None, entity_id: str | None = None,
):
    from app.domain import storyboard as sb
    return sb.list_entity_assets(db, project.id, entity_type=entity_type, entity_id=entity_id)


@api_router.get("/api/projects/{project_id}/reference-sets",
                response_model=list[ReferenceSetOut])
def list_reference_sets(
    project: Project = Depends(get_project), db: Session = Depends(get_db),
    character_id: str | None = None,
):
    """C4-A — a character's named reference sets + member counts."""
    from app.domain import storyboard as sb
    out = []
    for rs in sb.list_refsets(db, project.id, character_id=character_id):
        d = {c.name: getattr(rs, c.name) for c in rs.__table__.columns}
        d["member_count"] = len(sb.set_members(db, rs.id, project.id))
        out.append(d)
    return out


# ---------- timelines (C5-A) ----------

@api_router.get("/api/projects/{project_id}/timelines",
                response_model=list[TimelineOut])
def list_timelines(project: Project = Depends(get_project),
                   db: Session = Depends(get_db)):
    from app.domain import timeline as tld
    return tld.list_timelines(db, project.id)


@api_router.get("/api/projects/{project_id}/timelines/{timeline_id}",
                response_model=TimelineDetailOut)
def get_timeline(timeline_id: str,
                 project: Project = Depends(get_project),
                 db: Session = Depends(get_db)):
    from app.domain import timeline as tld
    try:
        tl = tld.get_timeline(db, project.id, timeline_id)
    except KeyError:
        raise HTTPException(404, "timeline not found in this project") from None
    d = {c.name: getattr(tl, c.name) for c in tl.__table__.columns}
    clips = tld.list_clips(db, tl.id)
    d["clips"] = clips
    # N1-A — tracks describe the lanes clips are placed on
    d["tracks"] = tld.list_tracks(db, tl.id)
    # N2-D — crossfade records with read-time feasibility
    d["crossfades"] = [
        {**{c.name: getattr(x, c.name)
            for c in x.__table__.columns},
         "satisfied": tld.crossfade_satisfied(db, tl, x)}
        for x in tld.list_crossfades(db, tl.id)]
    # program end = max(clip end) across tracks, in timeline frames —
    # exact rational; seconds fall out at the boundary
    prog = tld.program_end_frac(db, tl)
    d["total_duration_s"] = round(
        float(prog * tld.Fraction(tl.fps_den, tl.fps_num)), 6)
    # N1-E — undo/redo availability + human-readable tips for the UI
    d["undo_label"] = (tld.undo_tip(tl) or {}).get("label")
    d["redo_label"] = (tld.redo_tip(tl) or {}).get("label")
    return d


@api_router.get("/api/projects/{project_id}/timelines/{timeline_id}/edl")
def timeline_edl(timeline_id: str,
                 project: Project = Depends(get_project),
                 db: Session = Depends(get_db),
                 version: int = 1):
    """Edit-decision serialization — `ade-edl/1` for legacy sequential
    timelines, `ade-edl/2` (?version=2) for track-aware layouts.
    Deterministic JSON; v1 refuses non-representable layouts."""
    from app.domain import timeline as tld
    try:
        tl = tld.get_timeline(db, project.id, timeline_id)
    except KeyError:
        raise HTTPException(404, "timeline not found in this project") from None
    try:
        if version == 2:
            return tld.edl_json_v2(db, tl)
        if version == 1:
            return tld.edl_json(db, tl)
        raise tld.TimelineError("edl version must be 1 or 2")
    except tld.TimelineError as exc:
        raise HTTPException(409, str(exc)) from exc


@api_router.get("/api/projects/{project_id}/timelines/{timeline_id}/revisions",
                response_model=list[TimelineRevisionOut])
def timeline_revisions(timeline_id: str,
                       project: Project = Depends(get_project),
                       db: Session = Depends(get_db)):
    from app.domain import timeline as tld
    try:
        tl = tld.get_timeline(db, project.id, timeline_id)
    except KeyError:
        raise HTTPException(404, "timeline not found in this project") from None
    return [{"revision": r.revision, "summary": r.summary,
             "created_at": r.created_at}
            for r in tld.revision_history(db, tl.id)]


# ---------- bibles ----------
# NOTE: registered after all literal /api/projects/{id}/<name> routes —
# FastAPI matches in order and {etype} would otherwise shadow them.

# C1: per-project provider overlays (read-only; writes go through commands).
@api_router.get("/api/projects/{project_id}/providers")
def project_providers(
    request: Request,
    project: Project = Depends(get_project), db: Session = Depends(get_db),
):
    from app.domain.providers import registry as preg
    overrides = (project.settings_json or {}).get("providers", {}) or {}
    merged = preg.list_providers(
        db, request.app.state.provider_vault, project_overrides=overrides)
    return {"providers": merged}


def _list_entities(etype: str, project: Project, db: Session, include_archived: bool):
    rows = bibles.list_entities(db, project.id, etype, include_archived=include_archived)
    return [_entity_out(r) for r in rows]


@api_router.get("/api/projects/{project_id}/{etype}", response_model=list[EntityOut])
def list_entities(
    etype: str, project: Project = Depends(get_project), db: Session = Depends(get_db),
    include_archived: bool = False,
):
    if etype not in ENTITY_MODELS:
        raise HTTPException(404, "unknown entity type")
    return _list_entities(etype, project, db, include_archived)


@api_router.get("/api/projects/{project_id}/{etype}/{entity_id}", response_model=EntityOut)
def get_entity(
    etype: str, entity_id: str,
    project: Project = Depends(get_project), db: Session = Depends(get_db),
):
    if etype not in ENTITY_MODELS:
        raise HTTPException(404, "unknown entity type")
    try:
        row = bibles.get_entity(db, etype, entity_id, project.id)
    except KeyError:
        raise HTTPException(404, f"{etype} not found in this project") from None
    return _entity_out(row)


@api_router.get(
    "/api/projects/{project_id}/{etype}/{entity_id}/versions",
    response_model=list[EntityRevisionOut],
)
def entity_versions(
    etype: str, entity_id: str,
    project: Project = Depends(get_project), db: Session = Depends(get_db),
):
    if etype not in ENTITY_MODELS:
        raise HTTPException(404, "unknown entity type")
    try:
        bibles.get_entity(db, etype, entity_id, project.id)
    except KeyError:
        raise HTTPException(404, f"{etype} not found in this project") from None
    return db.execute(
        select(EntityRevision)
        .where(EntityRevision.entity_type == etype, EntityRevision.entity_id == entity_id)
        .order_by(EntityRevision.revision.desc())
    ).scalars().all()


# ---------- websocket ----------

async def websocket_endpoint(websocket: WebSocket) -> None:
    from app.security import (
        WS_MAX_UNAUTHENTICATED, is_auth_frame, ws_origin_allowed)

    state = websocket.app.state

    # A2.1 — origin policy before ANY accept (defense in depth; the
    # token still gates).
    if not ws_origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    # First-frame authentication under a bounded unauthenticated slot:
    # UNAUTHENTICATED → AUTHENTICATED → CLOSED. No subscription or
    # event delivery exists before this point. A3 — ?token= carries no
    # meaning here: every client authenticates in-band.
    async with state.ws_unauth_lock:
        if state.ws_unauth_count >= WS_MAX_UNAUTHENTICATED:
            slot = False
        else:
            state.ws_unauth_count += 1
            slot = True
    if not slot:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return
    try:
        # Accept first — a WS client cannot send its auth frame until
        # the handshake completes. The socket is still UNAUTHENTICATED:
        # no subscription, no events.
        await websocket.accept()
        authed = await _ws_first_frame_auth(websocket, state.settings)
    finally:
        async with state.ws_unauth_lock:
            state.ws_unauth_count -= 1
    if not authed:
        return
    # auth_ok is the first post-handshake server message — the client
    # treats the connection as authenticated only then.
    await websocket.send_json({"type": "auth_ok"})

    if websocket.client is None:
        return
    bus = state.event_bus
    await bus.subscribe(websocket)
    try:
        while True:
            msg = await websocket.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg["type"] != "websocket.receive":
                continue
            text = msg.get("text")
            if text is not None and is_auth_frame(text):
                # Duplicate/late authentication on an established
                # socket — protocol violation, close it.
                await websocket.close(code=status.WS_1002_PROTOCOL_ERROR)
                break
    except WebSocketDisconnect:
        pass
    finally:
        await bus.unsubscribe(websocket)


async def _ws_first_frame_auth(websocket: WebSocket, settings) -> bool:
    """UNAUTHENTICATED state: the socket is already accepted (a client
    cannot send frames pre-handshake) but has no subscription and no
    event delivery. Wait for the auth frame, validate, and return True
    on success — the caller then sends auth_ok and subscribes.

    Any failure closes the socket and returns False:
      - timeout            → 1008 (policy)
      - oversized frame    → 1009 (too big)
      - malformed/non-auth → 1002 (protocol error)
      - wrong token        → 1008 (policy violation)
    The token is never logged or echoed.
    """
    import asyncio
    from app.security import (
        WS_AUTH_MAX_BYTES, WS_AUTH_TIMEOUT_S, parse_auth_frame)
    try:
        msg = await asyncio.wait_for(
            websocket.receive(), WS_AUTH_TIMEOUT_S)
    except (asyncio.TimeoutError, TimeoutError):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False
    except WebSocketDisconnect:
        return False
    if msg["type"] == "websocket.disconnect":
        return False                    # client bailed mid-handshake
    if msg["type"] != "websocket.receive":
        await websocket.close(code=status.WS_1002_PROTOCOL_ERROR)
        return False
    raw = msg.get("text")
    if raw is None:
        raw = (msg.get("bytes") or b"").decode("utf-8", "replace")
    if len(raw.encode("utf-8")) > WS_AUTH_MAX_BYTES:
        await websocket.close(code=status.WS_1009_MESSAGE_TOO_BIG)
        return False
    token = parse_auth_frame(raw)
    if token is None:
        await websocket.close(code=status.WS_1002_PROTOCOL_ERROR)
        return False
    if token != settings.session_token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False
    return True
