"""Unified Creative Command System — M0 slice.

One canonical mutation pathway for every interface. Each change is
validated into a dry-run proposal (human-readable diff, affected IDs,
capability warnings), committed transactionally with optimistic
concurrency and an idempotency key, revisioned, audited, and — where
the change is safely reversible — undoable. Generation-triggering
commands enqueue durable jobs only at commit, never at preview.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import (
    AuditEvent,
    CommandTransaction,
    CreativeCommand,
    GenerationJob,
    Project,
    ProjectRevision,
    PromptDraft,
    new_id,
    utcnow,
)

SUPPORTED_COMMANDS = {
    "prompt.set_text",
    "project.rename",
    "project.archive",
    "project.restore",
    "generate.image.mock",
}

# Command types that enqueue paid/GPU-visible work; preview must stay free.
GENERATION_COMMANDS = {"generate.image.mock"}

# Commands that can be safely undone in M0 (compute can never be unspent).
UNDOABLE = {"prompt.set_text", "project.rename"}


class CommandError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _summary(command_type: str, payload: dict) -> str:
    if command_type == "prompt.set_text":
        text = (payload.get("text") or "").strip()
        return f"Update prompt draft ({len(text)} chars)"
    if command_type == "project.rename":
        return f"Rename project to '{payload.get('name', '')}'"
    if command_type == "project.archive":
        return "Archive project"
    if command_type == "project.restore":
        return "Restore project"
    if command_type == "generate.image.mock":
        return f"Generate image via mock adapter — '{(payload.get('prompt') or '')[:60]}'"
    return command_type


def _validate(command_type: str, payload: dict) -> list[str]:
    warnings: list[str] = []
    if command_type == "generate.image.mock":
        if not (payload.get("prompt") or "").strip():
            raise CommandError(422, "prompt is required for image generation")
        warnings.append("Uses the mock adapter — output is a placeholder, not AI inference")
    if command_type in {"project.archive", "project.restore"}:
        warnings.append("Applies to the whole project")
    return warnings


def preview(session: Session, project: Project, *, command_type: str, payload: dict,
            scope: str, origin: str, target_type: str | None, target_id: str | None) -> dict:
    if command_type not in SUPPORTED_COMMANDS:
        raise CommandError(422, f"unsupported command_type '{command_type}'")
    warnings = _validate(command_type, payload)
    return {
        "command": {
            "project_id": project.id,
            "command_type": command_type,
            "scope": scope,
            "origin": origin,
            "target_type": target_type,
            "target_id": target_id,
            "payload": payload,
        },
        "summary": _summary(command_type, payload),
        "affected": [{"type": "project", "id": project.id, "change": _summary(command_type, payload)}],
        "warnings": warnings,
        "estimated_cost": {"compute": "local/mock", "currency_usd": 0.0, "requires_approval": False},
        "current_revision": project.revision,
        "launches_generation": command_type in GENERATION_COMMANDS,
    }


def commit(session: Session, project: Project, *, command_type: str, payload: dict,
           scope: str, origin: str, target_type: str | None, target_id: str | None,
           expected_revision: int, idempotency_key: str,
           job_engine=None) -> dict:
    from sqlalchemy import select

    existing = session.execute(
        select(CreativeCommand).where(CreativeCommand.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return {"command": existing, "idempotent_replay": True}

    if project.status == "archived" and command_type != "project.restore":
        raise CommandError(409, "project is archived; restore it before making changes")
    if expected_revision != project.revision:
        raise CommandError(409, f"revision conflict: expected {expected_revision}, current {project.revision}")

    warnings = _validate(command_type, payload)
    before_revision = project.revision
    audit: dict[str, Any] = {"before": {}, "after": {}, "warnings": warnings}
    job: GenerationJob | None = None

    if command_type == "prompt.set_text":
        draft = session.get(PromptDraft, project.id) or session.execute(
            select(PromptDraft).where(PromptDraft.project_id == project.id)
        ).scalar_one_or_none()
        if draft is None:
            draft = PromptDraft(id=new_id(), project_id=project.id, text="", scope=scope)
            session.add(draft)
            session.flush()
        audit["before"]["text"] = draft.text
        audit["after"]["text"] = payload.get("text", "")
        draft.text = payload.get("text", "")

    elif command_type == "project.rename":
        name = (payload.get("name") or "").strip()
        if not name:
            raise CommandError(422, "name is required")
        audit["before"]["name"] = project.name
        audit["after"]["name"] = name
        project.name = name

    elif command_type in {"project.archive", "project.restore"}:
        new_status = "archived" if command_type == "project.archive" else "active"
        audit["before"]["status"] = project.status
        audit["after"]["status"] = new_status
        project.status = new_status

    elif command_type == "generate.image.mock":
        if job_engine is None:
            raise CommandError(500, "job engine unavailable")

    command = CreativeCommand(
        id=new_id(),
        project_id=project.id,
        origin=origin,
        command_type=command_type,
        scope=scope,
        target_type=target_type,
        target_id=target_id,
        payload_json=payload,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        status="committed",
    )
    session.add(command)
    session.flush()

    if command_type == "generate.image.mock":
        job = job_engine.enqueue(
            session,
            project_id=project.id,
            adapter_id="mock.image",
            kind="image",
            request={"prompt": payload["prompt"], "seed": payload.get("seed", 0)},
            idempotency_key=f"job:{idempotency_key}",
            command_id=command.id,
        )
        audit["after"]["job_id"] = job.id

    project.revision += 1
    session.add(
        ProjectRevision(
            id=new_id(), project_id=project.id, revision=project.revision,
            command_id=command.id, summary=_summary(command_type, payload),
        )
    )
    session.add(
        CommandTransaction(
            id=new_id(), command_id=command.id, project_id=project.id,
            before_revision=before_revision, after_revision=project.revision,
            status="applied", audit_json=audit,
        )
    )
    session.add(
        AuditEvent(
            id=new_id(), project_id=project.id, event_type="command.committed",
            payload_json={"command_id": command.id, "command_type": command_type, "job_id": job.id if job else None},
        )
    )
    return {"command": command, "job": job, "idempotent_replay": False, "warnings": warnings}


def undo(session: Session, project: Project, *, command_id: str) -> CreativeCommand:
    command = session.get(CreativeCommand, command_id)
    if command is None or command.project_id != project.id:
        raise CommandError(404, "command not found in this project")
    if command.status != "committed":
        raise CommandError(409, f"command already {command.status}")
    if command.command_type not in UNDOABLE:
        raise CommandError(
            409,
            "this command cannot be undone (completed generation/jobs are not reversible; "
            "external compute cannot be refunded)",
        )

    from sqlalchemy import select

    txn = session.execute(
        select(CommandTransaction).where(CommandTransaction.command_id == command.id)
    ).scalar_one_or_none()
    before = (txn.audit_json or {}).get("before", {}) if txn else {}

    if command.command_type == "prompt.set_text":
        draft = session.execute(
            select(PromptDraft).where(PromptDraft.project_id == project.id)
        ).scalar_one_or_none()
        if draft is not None:
            draft.text = before.get("text", draft.text)
    elif command.command_type == "project.rename":
        project.name = before.get("name", project.name)

    command.status = "undone"
    session.add(
        AuditEvent(
            id=new_id(), project_id=project.id, event_type="command.undone",
            payload_json={"command_id": command.id},
        )
    )
    return command


def new_idempotency_key() -> str:
    return str(uuid.uuid4())
