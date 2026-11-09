"""Character / World / Location foundations — persistent, versioned.

Every mutation goes through `apply_*` helpers called from the command
system. Each committed change bumps the row's `revision` and writes an
immutable `EntityRevision` snapshot, so history is append-only and any
version can be reopened or reverted (revert = new revision copying an
old snapshot, never a rewrite).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Character,
    EntityRevision,
    Location,
    World,
    new_id,
    utcnow,
)

ENTITY_MODELS = {
    "character": Character,
    "world": World,
    "location": Location,
}

# Which JSON column carries the structured creative data per type.
DATA_COLUMNS = {"character": "profile_json", "world": "lore_json", "location": "details_json"}


def _entity_type(command_type: str) -> str:
    return command_type.split(".", 1)[0]


def get_entity(session: Session, entity_type: str, entity_id: str, project_id: str):
    model = ENTITY_MODELS[entity_type]
    entity = session.get(model, entity_id)
    if entity is None or entity.project_id != project_id:
        raise KeyError(f"{entity_type} not found in this project")
    return entity


def snapshot(entity) -> dict[str, Any]:
    """Full serializable state of an entity row (id..revision)."""
    out: dict[str, Any] = {}
    for col in entity.__table__.columns:
        val = getattr(entity, col.name)
        if hasattr(val, "isoformat"):
            val = val.isoformat()
        out[col.name] = val
    return out


def write_revision(
    session: Session, entity, *, command_id: str | None, summary: str,
) -> EntityRevision:
    row = EntityRevision(
        id=new_id(),
        project_id=entity.project_id,
        entity_type=entity.__tablename__.rstrip("s"),
        entity_id=entity.id,
        revision=entity.revision,
        snapshot_json=snapshot(entity),
        summary=summary,
        command_id=command_id,
    )
    session.add(row)
    return row


def create_entity(
    session: Session, project_id: str, entity_type: str,
    *, name: str, summary: str = "", data: dict | None = None,
    world_id: str | None = None, command_id: str | None = None,
):
    model = ENTITY_MODELS[entity_type]
    kwargs: dict[str, Any] = {
        "id": new_id(),
        "project_id": project_id,
        "name": name.strip(),
        "summary": summary,
        DATA_COLUMNS[entity_type]: data or {},
        "revision": 1,
    }
    if entity_type == "location":
        kwargs["world_id"] = world_id
    entity = model(**kwargs)
    session.add(entity)
    session.flush()
    write_revision(session, entity, command_id=command_id, summary=f"Created {entity_type}")
    return entity


def update_entity(
    session: Session, entity, *, name=None, summary=None, data=None,
    world_id: object = ..., command_id: str | None = None, change_summary: str,
) -> None:
    before = snapshot(entity)
    if name is not None:
        entity.name = name.strip()
    if summary is not None:
        entity.summary = summary
    if data is not None:
        col = DATA_COLUMNS[entity.__tablename__.rstrip("s")]
        setattr(entity, col, {**(getattr(entity, col) or {}), **data})
    if world_id is not ... and hasattr(entity, "world_id"):
        entity.world_id = world_id
    entity.revision += 1
    entity.updated_at = utcnow()
    session.flush()
    write_revision(session, entity, command_id=command_id, summary=change_summary)
    return before


def archive_entity(session: Session, entity, *, archived: bool, command_id: str | None):
    before_status = entity.status
    entity.status = "archived" if archived else "active"
    entity.revision += 1
    entity.updated_at = utcnow()
    session.flush()
    write_revision(
        session, entity, command_id=command_id,
        summary="Archived" if archived else "Restored",
    )
    return before_status


def revert_entity(session: Session, entity, *, to_revision: int, command_id: str | None):
    """Create a NEW revision whose content equals an old snapshot."""
    row = session.execute(
        select(EntityRevision).where(
            EntityRevision.entity_type == entity.__tablename__.rstrip("s"),
            EntityRevision.entity_id == entity.id,
            EntityRevision.revision == to_revision,
        )
    ).scalar_one_or_none()
    if row is None:
        raise KeyError(f"revision {to_revision} not found")
    snap = row.snapshot_json
    entity.name = snap["name"]
    entity.summary = snap["summary"]
    col = DATA_COLUMNS[entity.__tablename__.rstrip("s")]
    setattr(entity, col, snap.get(col) or {})
    if hasattr(entity, "world_id"):
        entity.world_id = snap.get("world_id")
    entity.status = snap.get("status", "active")
    entity.revision += 1
    entity.updated_at = utcnow()
    session.flush()
    write_revision(session, entity, command_id=command_id, summary=f"Reverted to revision {to_revision}")


def list_entities(session: Session, project_id: str, entity_type: str, *, include_archived=False):
    model = ENTITY_MODELS[entity_type]
    q = select(model).where(model.project_id == project_id)
    if not include_archived:
        q = q.where(model.status == "active")
    return session.execute(q.order_by(model.updated_at.desc())).scalars().all()


def revision_history(session: Session, entity_type: str, entity_id: str) -> list[EntityRevision]:
    return session.execute(
        select(EntityRevision)
        .where(EntityRevision.entity_type == entity_type, EntityRevision.entity_id == entity_id)
        .order_by(EntityRevision.revision.desc())
    ).scalars().all()


def count_entities(session: Session, project_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for etype, model in ENTITY_MODELS.items():
        counts[etype + "s"] = session.execute(
            select(func.count()).select_from(model).where(
                model.project_id == project_id, model.status == "active"
            )
        ).scalar_one()
    return counts
