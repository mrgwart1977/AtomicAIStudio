"""API routers — health, hardware, projects, prompt draft, commands, jobs,
assets, adapters, websocket. All project data is scoped through get_project."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db, get_project
from app.db.models import Asset, CreativeCommand, GenerationJob, Project, PromptDraft
from app.domain import commands as cmd
from app.domain import hardware
from app.schemas.api import (
    AdapterProfileOut,
    AssetOut,
    CommandCommitRequest,
    CommandOut,
    CommandPreviewRequest,
    CommandProposalOut,
    JobOut,
    ProjectCreate,
    ProjectOut,
    PromptDraftOut,
)

# /health stays token-free so the desktop shell can poll readiness.
open_router = APIRouter()
# Everything under /api/* is protected by the session token (applied at include time).
api_router = APIRouter()


@open_router.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "atomic-ai-studio-backend", "version": "0.1.0"}


@api_router.get("/api/hardware")
def get_hardware() -> dict:
    return hardware.probe()


@api_router.get("/api/adapters", response_model=list[AdapterProfileOut])
def list_adapters(request: Request) -> list[dict]:
    return request.app.state.adapter_registry.list_profiles()


@api_router.get("/api/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db)) -> list[Project]:
    return db.execute(select(Project).order_by(Project.created_at.desc())).scalars().all()


@api_router.post("/api/projects", response_model=ProjectOut, status_code=201)
def create_project(body: ProjectCreate, db: Session = Depends(get_db)) -> Project:
    project = Project(name=body.name, template=body.template, settings_json=body.settings)
    db.add(project)
    db.commit()
    return project


@api_router.get("/api/projects/{project_id}", response_model=ProjectOut)
def read_project(project: Project = Depends(get_project)) -> Project:
    return project


@api_router.get("/api/projects/{project_id}/prompt-draft", response_model=PromptDraftOut)
def read_prompt_draft(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    draft = db.execute(select(PromptDraft).where(PromptDraft.project_id == project.id)).scalar_one_or_none()
    if draft is None:
        return {"project_id": project.id, "text": "", "scope": "this_take", "updated_at": project.updated_at}
    return draft


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
        "warnings": result.get("warnings", []),
    }


@api_router.get("/api/projects/{project_id}/commands", response_model=list[CommandOut])
def list_commands(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    return db.execute(
        select(CreativeCommand)
        .where(CreativeCommand.project_id == project.id)
        .order_by(CreativeCommand.created_at.desc())
        .limit(100)
    ).scalars().all()


@api_router.post("/api/projects/{project_id}/commands/{command_id}/undo", response_model=CommandOut)
def undo_command(command_id: str, project: Project = Depends(get_project), db: Session = Depends(get_db)):
    try:
        command = cmd.undo(db, project, command_id=command_id)
    except cmd.CommandError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc
    db.commit()
    return command


@api_router.get("/api/projects/{project_id}/jobs", response_model=list[JobOut])
def list_jobs(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    return db.execute(
        select(GenerationJob)
        .where(GenerationJob.project_id == project.id)
        .order_by(GenerationJob.created_at.desc())
        .limit(100)
    ).scalars().all()


@api_router.get("/api/projects/{project_id}/assets", response_model=list[AssetOut])
def list_assets(project: Project = Depends(get_project), db: Session = Depends(get_db)):
    return db.execute(
        select(Asset).where(Asset.project_id == project.id).order_by(Asset.created_at.desc()).limit(200)
    ).scalars().all()


@api_router.get("/api/projects/{project_id}/assets/{asset_id}/file")
def asset_file(
    request: Request, asset_id: str,
    project: Project = Depends(get_project), db: Session = Depends(get_db),
):
    asset = db.get(Asset, asset_id)
    if asset is None or asset.project_id != project.id:
        raise HTTPException(404, "asset not found in this project")
    path = Path(asset.uri)
    if not path.is_absolute():
        path = request.app.state.settings.data_dir / path
    if not path.exists():
        raise HTTPException(404, "asset file missing on disk")
    return FileResponse(path)


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
