"""API routers — health, hardware, projects, prompt draft, commands, jobs,
assets (CAS), bibles, storage locations, backups, websocket.
All project data is scoped through get_project — no unscoped reads."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
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
    SharedAssetLink,
    StorageLocation,
    World,
    new_id,
    utcnow,
)
from app.domain import bibles, commands as cmd, hardware
from app.domain.backup import PackageError, read_manifest
from app.schemas.api import (
    AdapterProfileOut,
    AppBackupDirUpdate,
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
    EntityOut,
    EntityRevisionOut,
    JobOut,
    PackageImportRequest,
    PackagePreviewOut,
    ProjectCardOut,
    ProjectCreate,
    ProjectOut,
    ProjectRevisionOut,
    PromptDraftOut,
    SharedAssetOut,
    StorageLocationCreate,
    StorageLocationOut,
    StorageLocationUpdate,
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
    return {"status": "ok", "service": "atomic-ai-studio-backend", "version": "0.1.0"}


@api_router.get("/api/hardware")
def get_hardware() -> dict:
    return hardware.probe()


@api_router.get("/api/adapters", response_model=list[AdapterProfileOut])
def list_adapters(request: Request) -> list[dict]:
    return request.app.state.adapter_registry.list_profiles()


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
    project: Project = Depends(get_project),
    db: Session = Depends(get_db),
):
    try:
        return cmd.preview(
            db, project,
            command_type=body.command_type, payload=body.payload, scope=body.scope,
            origin=body.origin, target_type=body.target_type, target_id=body.target_id,
        )
    except cmd.CommandError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


@api_router.post("/api/projects/{project_id}/commands/commit")
async def commit_command(
    request: Request,
    body: CommandCommitRequest,
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
        )
    except cmd.CommandError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    command: CreativeCommand = result["command"]
    db.commit()
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
    }


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

@api_router.get("/api/projects/{project_id}/assets", response_model=AssetPageOut)
def list_assets(
    project: Project = Depends(get_project), db: Session = Depends(get_db),
    offset: int = 0, limit: int = Query(default=100, le=500),
    kind: str | None = None, q: str | None = None, status: str | None = None,
):
    base = select(Asset).where(Asset.project_id == project.id, Asset.status != "deleted")
    if kind:
        base = base.where(Asset.kind == kind)
    if q:
        base = base.where(Asset.name.like(f"%{q}%"))
    if status:
        base = base.where(Asset.status == status)
    total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
    items = db.execute(
        base.order_by(Asset.created_at.desc()).offset(offset).limit(limit)
    ).scalars().all()
    return {"items": items, "total": total, "offset": offset, "limit": limit}


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


# ---------- bibles ----------
# NOTE: registered after all literal /api/projects/{id}/<name> routes —
# FastAPI matches in order and {etype} would otherwise shadow them.

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
    from app.security import require_token_ws

    await require_token_ws(websocket)
    if websocket.client is None:
        return
    bus = websocket.app.state.event_bus
    await bus.subscribe(websocket)
    try:
        while True:
            await websocket.receive_text()  # keepalive / future client messages
    except WebSocketDisconnect:
        pass
    finally:
        await bus.unsubscribe(websocket)
