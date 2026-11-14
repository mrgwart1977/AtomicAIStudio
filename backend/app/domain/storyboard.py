"""Scenes, shots, casting and entity-asset links — the M2 storyboard core.

Same versioning contract as bibles: every committed mutation bumps the
row's revision and writes an immutable EntityRevision snapshot. Scene
and shot snapshots additionally embed their cast rows, so reverting a
shot restores both its fields AND who was in it.

Ordering: scenes carry a dense `position` within the project; shots
carry one within their scene; cast rows order within their parent.
Reorder/move renumbers — positions stay gap-free so the UI can render
by simple sort.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.cast_state import (
    CastStateError,
    parse_scene_cast_fields,
    parse_shot_cast_state,
)

from app.db.models import (
    Character,
    EntityAssetLink,
    EntityRevision,
    Location,
    Scene,
    SceneCharacter,
    Shot,
    ShotCharacter,
    World,
    new_id,
    utcnow,
)

SHOT_TYPES = (
    "establishing", "wide", "medium", "medium_close", "close", "extreme_close",
    "over_shoulder", "pov", "aerial", "insert", "two_shot", "group", "other",
)

ENTITY_ASSET_TYPES = ("character", "world", "location")
ENTITY_ASSET_ROLES = ("reference", "identity", "wardrobe", "voice", "style")
# C3-D1 — roles whose asset must be a safely decodable image.
ENTITY_ASSET_IMAGE_ROLES = ("reference", "identity", "wardrobe", "style")
# Optional per-link guidance strength bounds (C3-D1); NULL = provider
# default, honored only where the model declares reference_weights.
REFERENCE_WEIGHT_MIN = 0.0
REFERENCE_WEIGHT_MAX = 2.0
_UNSET = object()  # distinguishes "key absent" from "explicit null"

ENTITY_MODELS = {"character": Character, "world": World, "location": Location}


# ---------- fetch (project-scoped; KeyError on cross-project access) ----------

def get_scene(session: Session, scene_id: str, project_id: str) -> Scene:
    row = session.get(Scene, scene_id)
    if row is None or row.project_id != project_id:
        raise KeyError("scene not found in this project")
    return row


def get_shot(session: Session, shot_id: str, project_id: str) -> Shot:
    row = session.get(Shot, shot_id)
    if row is None or row.project_id != project_id:
        raise KeyError("shot not found in this project")
    return row


def get_character(session: Session, character_id: str, project_id: str) -> Character:
    row = session.get(Character, character_id)
    if row is None or row.project_id != project_id:
        raise KeyError("character not found in this project")
    return row


def _check_link(session: Session, model, link_id: str | None, project_id: str, label: str) -> None:
    if link_id is None:
        return
    row = session.get(model, link_id)
    if row is None or row.project_id != project_id:
        raise KeyError(f"{label} not found in this project")


# ---------- snapshots / revisions ----------

def _cols(row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if hasattr(val, "isoformat"):
            val = val.isoformat()
        out[col.name] = val
    return out


def scene_snapshot(session: Session, scene: Scene) -> dict[str, Any]:
    snap = _cols(scene)
    snap["cast"] = [
        _cols(c) for c in session.execute(
            select(SceneCharacter).where(SceneCharacter.scene_id == scene.id)
            .order_by(SceneCharacter.position)
        ).scalars()
    ]
    return snap


def shot_snapshot(session: Session, shot: Shot) -> dict[str, Any]:
    snap = _cols(shot)
    snap["cast"] = [
        _cols(c) for c in session.execute(
            select(ShotCharacter).where(ShotCharacter.shot_id == shot.id)
            .order_by(ShotCharacter.position)
        ).scalars()
    ]
    return snap


def write_scene_revision(session: Session, scene: Scene, *, command_id: str | None, summary: str) -> EntityRevision:
    row = EntityRevision(
        id=new_id(), project_id=scene.project_id, entity_type="scene", entity_id=scene.id,
        revision=scene.revision, snapshot_json=scene_snapshot(session, scene),
        summary=summary, command_id=command_id,
    )
    session.add(row)
    return row


def write_shot_revision(session: Session, shot: Shot, *, command_id: str | None, summary: str) -> EntityRevision:
    row = EntityRevision(
        id=new_id(), project_id=shot.project_id, entity_type="shot", entity_id=shot.id,
        revision=shot.revision, snapshot_json=shot_snapshot(session, shot),
        summary=summary, command_id=command_id,
    )
    session.add(row)
    return row


def revision_history(session: Session, entity_type: str, entity_id: str) -> list[EntityRevision]:
    return session.execute(
        select(EntityRevision)
        .where(EntityRevision.entity_type == entity_type, EntityRevision.entity_id == entity_id)
        .order_by(EntityRevision.revision.desc())
    ).scalars().all()


# ---------- scenes ----------

SCENE_FIELDS = ("title", "description", "intent")


def _next_position(session: Session, model, where) -> int:
    mx = session.execute(select(func.max(model.position)).where(where)).scalar_one()
    return (mx if mx is not None else -1) + 1


def create_scene(session: Session, project_id: str, *, title: str, description: str = "",
                 intent: str = "", world_id: str | None = None, location_id: str | None = None,
                 continuity: dict | None = None, command_id: str | None = None) -> Scene:
    _check_link(session, World, world_id, project_id, "world")
    _check_link(session, Location, location_id, project_id, "location")
    scene = Scene(
        id=new_id(), project_id=project_id, title=title.strip(), description=description,
        intent=intent, position=_next_position(session, Scene, Scene.project_id == project_id),
        world_id=world_id, location_id=location_id, continuity_json=continuity or {},
        revision=1,
    )
    session.add(scene)
    session.flush()
    write_scene_revision(session, scene, command_id=command_id, summary="Created scene")
    return scene


def update_scene(session: Session, scene: Scene, *, fields: dict, world_id=...,
                 location_id=..., continuity=None, command_id: str | None,
                 change_summary: str = "Updated scene") -> dict:
    before = scene_snapshot(session, scene)
    for f in SCENE_FIELDS:
        if f in fields and fields[f] is not None:
            setattr(scene, f, fields[f])
    if world_id is not ...:
        _check_link(session, World, world_id, scene.project_id, "world")
        scene.world_id = world_id
    if location_id is not ...:
        _check_link(session, Location, location_id, scene.project_id, "location")
        scene.location_id = location_id
    if continuity is not None:
        scene.continuity_json = {**(scene.continuity_json or {}), **continuity}
    scene.revision += 1
    scene.updated_at = utcnow()
    session.flush()
    write_scene_revision(session, scene, command_id=command_id, summary=change_summary)
    return before


def set_scene_archived(session: Session, scene: Scene, *, archived: bool,
                       command_id: str | None) -> str:
    before_status = scene.status
    scene.status = "archived" if archived else "active"
    scene.revision += 1
    scene.updated_at = utcnow()
    session.flush()
    write_scene_revision(session, scene, command_id=command_id,
                         summary="Archived scene" if archived else "Restored scene")
    return before_status


def _renumber(rows: list) -> dict[str, int]:
    """Assign dense positions 0..n-1; return {id: old_position} for undo."""
    before = {r.id: r.position for r in rows}
    for i, r in enumerate(rows):
        r.position = i
    return before


def move_scene(session: Session, scene: Scene, to_index: int, *, command_id: str | None) -> dict:
    rows = session.execute(
        select(Scene).where(Scene.project_id == scene.project_id).order_by(Scene.position)
    ).scalars().all()
    rows = [r for r in rows if r.id != scene.id]
    to_index = max(0, min(to_index, len(rows)))
    rows.insert(to_index, scene)
    before = _renumber(rows)
    scene.revision += 1
    scene.updated_at = utcnow()
    session.flush()
    write_scene_revision(session, scene, command_id=command_id,
                         summary=f"Moved scene to position {to_index + 1}")
    return {"positions": before}


def _copy_shot(session: Session, src: Shot, scene_id: str, project_id: str, position: int) -> Shot:
    shot = Shot(
        id=new_id(), project_id=project_id, scene_id=scene_id, title=src.title,
        description=src.description, intent=src.intent, prompt=src.prompt,
        shot_type=src.shot_type, duration_sec=src.duration_sec, position=position,
        status=src.status, location_id=src.location_id,
        thumbnail_asset_id=src.thumbnail_asset_id,
        reference_set_id=src.reference_set_id,
        source_asset_id=src.source_asset_id,
        continuity_json=dict(src.continuity_json or {}), revision=1,
    )
    session.add(shot)
    session.flush()
    cast = session.execute(
        select(ShotCharacter).where(ShotCharacter.shot_id == src.id).order_by(ShotCharacter.position)
    ).scalars().all()
    for c in cast:
        session.add(ShotCharacter(
            id=new_id(), project_id=project_id, shot_id=shot.id,
            character_id=c.character_id, position=c.position,
            wardrobe_variant=c.wardrobe_variant, blocking=c.blocking,
            expression=c.expression, emotion=c.emotion,
            action_instructions=c.action_instructions, dialogue=c.dialogue,
            voice_override=c.voice_override, model_ref=c.model_ref,
            lora_json=list(c.lora_json or []), continuity_json=dict(c.continuity_json or {}),
            reference_ids_json=list(c.reference_ids_json or []),
            ext_json=dict(c.ext_json or {}),
        ))
    return shot


def duplicate_scene(session: Session, scene: Scene, *, title: str | None,
                    command_id: str | None) -> Scene:
    # Insert the copy right after the original; shift later scenes down.
    later = session.execute(
        select(Scene).where(Scene.project_id == scene.project_id, Scene.position > scene.position)
    ).scalars().all()
    for s in later:
        s.position += 1
    dup = Scene(
        id=new_id(), project_id=scene.project_id,
        title=(title or f"{scene.title} (copy)").strip(),
        description=scene.description, intent=scene.intent,
        position=scene.position + 1, status="active",
        world_id=scene.world_id, location_id=scene.location_id,
        continuity_json=dict(scene.continuity_json or {}), revision=1,
    )
    session.add(dup)
    session.flush()
    cast = session.execute(
        select(SceneCharacter).where(SceneCharacter.scene_id == scene.id).order_by(SceneCharacter.position)
    ).scalars().all()
    for c in cast:
        session.add(SceneCharacter(
            id=new_id(), project_id=scene.project_id, scene_id=dup.id,
            character_id=c.character_id, position=c.position, role=c.role,
            notes_json=dict(c.notes_json or {}),
        ))
    shots = session.execute(
        select(Shot).where(Shot.scene_id == scene.id).order_by(Shot.position)
    ).scalars().all()
    for s in shots:
        _copy_shot(session, s, dup.id, scene.project_id, s.position)
    write_scene_revision(session, dup, command_id=command_id,
                         summary=f"Duplicated from '{scene.title}'")
    return dup


def revert_scene(session: Session, scene: Scene, to_revision: int, *, command_id: str | None) -> None:
    row = session.execute(
        select(EntityRevision).where(
            EntityRevision.entity_type == "scene",
            EntityRevision.entity_id == scene.id,
            EntityRevision.revision == to_revision,
        )
    ).scalar_one_or_none()
    if row is None:
        raise KeyError(f"revision {to_revision} not found")
    snap = row.snapshot_json
    for f in SCENE_FIELDS + ("status", "world_id", "location_id", "continuity_json", "position"):
        if f in snap:
            setattr(scene, f, snap[f])
    _restore_scene_cast(session, scene, snap.get("cast") or [])
    scene.revision += 1
    scene.updated_at = utcnow()
    session.flush()
    write_scene_revision(session, scene, command_id=command_id,
                         summary=f"Reverted to revision {to_revision}")


def _restore_scene_cast(session: Session, scene: Scene, cast_rows: list[dict]) -> None:
    """Rewrite scene cast to match a snapshot (keeps IDs where possible)."""
    want = {r["character_id"]: r for r in cast_rows}
    have = session.execute(
        select(SceneCharacter).where(SceneCharacter.scene_id == scene.id)
    ).scalars().all()
    for c in have:
        if c.character_id not in want:
            session.delete(c)
    existing = {c.character_id: c for c in have if c.character_id in want}
    for snap_row in cast_rows:
        row = existing.get(snap_row["character_id"])
        if row is None:
            row = SceneCharacter(
                id=new_id(), project_id=scene.project_id, scene_id=scene.id,
                character_id=snap_row["character_id"],
            )
            session.add(row)
        row.position = snap_row.get("position", 0)
        row.role = snap_row.get("role", "")
        row.notes_json = dict(snap_row.get("notes_json") or {})


# ---------- scene cast ----------

def scene_cast_assign(session: Session, scene: Scene, character: Character, *,
                      role: str = "", notes: dict | None = None) -> SceneCharacter:
    parse_scene_cast_fields(role, notes)
    row = session.execute(
        select(SceneCharacter).where(
            SceneCharacter.scene_id == scene.id, SceneCharacter.character_id == character.id
        )
    ).scalar_one_or_none()
    if row is not None:
        raise ValueError("character is already in this scene's cast")
    row = SceneCharacter(
        id=new_id(), project_id=scene.project_id, scene_id=scene.id,
        character_id=character.id,
        position=_next_position(session, SceneCharacter, SceneCharacter.scene_id == scene.id),
        role=role, notes_json=notes or {},
    )
    session.add(row)
    session.flush()
    return row


def scene_cast_update(session: Session, scene: Scene, character_id: str, *,
                      role: str | None = None, notes: dict | None = None,
                      position: int | None = None) -> SceneCharacter:
    row = session.execute(
        select(SceneCharacter).where(
            SceneCharacter.scene_id == scene.id, SceneCharacter.character_id == character_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise KeyError("character is not in this scene's cast")
    parse_scene_cast_fields(role, notes)
    if role is not None:
        row.role = role
    if notes is not None:
        row.notes_json = {**(row.notes_json or {}), **notes}
    if position is not None:
        row.position = position
    session.flush()
    return row


def scene_cast_remove(session: Session, scene: Scene, character_id: str) -> SceneCharacter:
    row = session.execute(
        select(SceneCharacter).where(
            SceneCharacter.scene_id == scene.id, SceneCharacter.character_id == character_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise KeyError("character is not in this scene's cast")
    session.delete(row)
    session.flush()
    return row


# ---------- shots ----------

SHOT_FIELDS = ("title", "description", "intent", "prompt", "shot_type", "duration_sec")


def create_shot(session: Session, scene: Scene, *, title: str = "", shot_type: str = "medium",
                description: str = "", intent: str = "", prompt: str = "",
                duration_sec: float = 0.0, location_id: str | None = None,
                continuity: dict | None = None, command_id: str | None = None) -> Shot:
    _check_link(session, Location, location_id, scene.project_id, "location")
    shot = Shot(
        id=new_id(), project_id=scene.project_id, scene_id=scene.id,
        title=title.strip(), description=description, intent=intent, prompt=prompt,
        shot_type=shot_type, duration_sec=max(float(duration_sec or 0), 0.0),
        position=_next_position(session, Shot, Shot.scene_id == scene.id),
        location_id=location_id, continuity_json=continuity or {}, revision=1,
    )
    session.add(shot)
    session.flush()
    write_shot_revision(session, shot, command_id=command_id, summary="Created shot")
    return shot


def update_shot(session: Session, shot: Shot, *, fields: dict, location_id=...,
                thumbnail_asset_id=..., continuity=None, command_id: str | None,
                reference_set_id=..., source_asset_id=...,
                change_summary: str = "Updated shot") -> dict:
    from app.db.models import Asset
    before = shot_snapshot(session, shot)
    for f in SHOT_FIELDS:
        if f in fields and fields[f] is not None:
            val = fields[f]
            if f == "shot_type" and val not in SHOT_TYPES:
                raise ValueError(f"unknown shot_type '{val}'")
            if f == "duration_sec":
                val = max(float(val), 0.0)
            setattr(shot, f, val)
    if location_id is not ...:
        _check_link(session, Location, location_id, shot.project_id, "location")
        shot.location_id = location_id
    if thumbnail_asset_id is not ...:
        if thumbnail_asset_id is not None:
            a = session.get(Asset, thumbnail_asset_id)
            if a is None or a.project_id != shot.project_id:
                raise KeyError("thumbnail asset not found in this project")
        shot.thumbnail_asset_id = thumbnail_asset_id
    # C4-B — generation bindings. None clears (explicit unbind); the
    # project-scope checks here mirror thumbnail_asset_id handling.
    if reference_set_id is not ...:
        if reference_set_id is not None:
            get_refset(session, reference_set_id, shot.project_id)
        shot.reference_set_id = reference_set_id
    if source_asset_id is not ...:
        if source_asset_id is not None:
            a = session.get(Asset, source_asset_id)
            if a is None or a.project_id != shot.project_id:
                raise KeyError("source asset not found in this project")
            if a.kind != "image":
                raise ValueError("shot source asset must be an image")
        shot.source_asset_id = source_asset_id
    if continuity is not None:
        shot.continuity_json = {**(shot.continuity_json or {}), **continuity}
    shot.revision += 1
    shot.updated_at = utcnow()
    session.flush()
    write_shot_revision(session, shot, command_id=command_id, summary=change_summary)
    return before


def set_shot_archived(session: Session, shot: Shot, *, archived: bool,
                      command_id: str | None) -> str:
    before_status = shot.status
    shot.status = "archived" if archived else "active"
    shot.revision += 1
    shot.updated_at = utcnow()
    session.flush()
    write_shot_revision(session, shot, command_id=command_id,
                        summary="Archived shot" if archived else "Restored shot")
    return before_status


def move_shot(session: Session, shot: Shot, to_index: int, *,
              to_scene_id: str | None = None, command_id: str | None) -> dict:
    """Reorder within the scene, or move to another scene at to_index."""
    before: dict[str, Any] = {"scene_id": shot.scene_id}
    target_scene_id = to_scene_id or shot.scene_id
    if target_scene_id != shot.scene_id:
        target = get_scene(session, target_scene_id, shot.project_id)
        old_rows = session.execute(
            select(Shot).where(Shot.scene_id == shot.scene_id).order_by(Shot.position)
        ).scalars().all()
        before["old_positions"] = _renumber([r for r in old_rows if r.id != shot.id])
        shot.scene_id = target.id
        new_rows = session.execute(
            select(Shot).where(Shot.scene_id == target.id, Shot.id != shot.id)
            .order_by(Shot.position)
        ).scalars().all()
        to_index = max(0, min(to_index, len(new_rows)))
        new_rows.insert(to_index, shot)
        before["new_positions"] = _renumber(new_rows)
    else:
        rows = session.execute(
            select(Shot).where(Shot.scene_id == shot.scene_id).order_by(Shot.position)
        ).scalars().all()
        rows = [r for r in rows if r.id != shot.id]
        to_index = max(0, min(to_index, len(rows)))
        rows.insert(to_index, shot)
        before["positions"] = _renumber(rows)
    shot.revision += 1
    shot.updated_at = utcnow()
    session.flush()
    write_shot_revision(session, shot, command_id=command_id,
                        summary=f"Moved shot to position {to_index + 1}")
    return before


def duplicate_shot(session: Session, shot: Shot, *, title: str | None,
                   command_id: str | None) -> Shot:
    later = session.execute(
        select(Shot).where(Shot.scene_id == shot.scene_id, Shot.position > shot.position)
    ).scalars().all()
    for s in later:
        s.position += 1
    dup = _copy_shot(session, shot, shot.scene_id, shot.project_id, shot.position + 1)
    if title:
        dup.title = title.strip()
    elif dup.title:
        dup.title = f"{dup.title} (copy)"
    write_shot_revision(session, dup, command_id=command_id,
                        summary=f"Duplicated from '{shot.title or 'shot'}'")
    return dup


def revert_shot(session: Session, shot: Shot, to_revision: int, *, command_id: str | None) -> None:
    row = session.execute(
        select(EntityRevision).where(
            EntityRevision.entity_type == "shot",
            EntityRevision.entity_id == shot.id,
            EntityRevision.revision == to_revision,
        )
    ).scalar_one_or_none()
    if row is None:
        raise KeyError(f"revision {to_revision} not found")
    snap = row.snapshot_json
    for f in SHOT_FIELDS + ("status", "scene_id", "location_id", "thumbnail_asset_id",
                            "reference_set_id", "source_asset_id",
                            "continuity_json", "position"):
        if f in snap:
            setattr(shot, f, snap[f])
    _restore_shot_cast(session, shot, snap.get("cast") or [])
    shot.revision += 1
    shot.updated_at = utcnow()
    session.flush()
    write_shot_revision(session, shot, command_id=command_id,
                        summary=f"Reverted to revision {to_revision}")


SHOT_CAST_FIELDS = (
    "wardrobe_variant", "blocking", "expression", "emotion",
    "action_instructions", "dialogue", "voice_override", "model_ref",
)


def _restore_shot_cast(session: Session, shot: Shot, cast_rows: list[dict]) -> None:
    want = {r["character_id"]: r for r in cast_rows}
    have = session.execute(
        select(ShotCharacter).where(ShotCharacter.shot_id == shot.id)
    ).scalars().all()
    for c in have:
        if c.character_id not in want:
            session.delete(c)
    existing = {c.character_id: c for c in have if c.character_id in want}
    for snap_row in cast_rows:
        row = existing.get(snap_row["character_id"])
        if row is None:
            row = ShotCharacter(
                id=new_id(), project_id=shot.project_id, shot_id=shot.id,
                character_id=snap_row["character_id"],
            )
            session.add(row)
        for f in SHOT_CAST_FIELDS + ("position",):
            if f in snap_row:
                setattr(row, f, snap_row[f])
        row.lora_json = list(snap_row.get("lora_json") or [])
        row.continuity_json = dict(snap_row.get("continuity_json") or {})
        row.reference_ids_json = list(snap_row.get("reference_ids_json") or [])
        row.ext_json = dict(snap_row.get("ext_json") or {})


# ---------- shot cast ----------

def shot_cast_assign(session: Session, shot: Shot, character: Character, *,
                     state: dict | None = None) -> ShotCharacter:
    row = session.execute(
        select(ShotCharacter).where(
            ShotCharacter.shot_id == shot.id, ShotCharacter.character_id == character.id
        )
    ).scalar_one_or_none()
    if row is not None:
        raise ValueError("character is already in this shot's cast")
    st = parse_shot_cast_state(state)
    row = ShotCharacter(
        id=new_id(), project_id=shot.project_id, shot_id=shot.id,
        character_id=character.id,
        position=_next_position(session, ShotCharacter, ShotCharacter.shot_id == shot.id),
        lora_json=st["loras"],
        continuity_json=st["continuity"],
        reference_ids_json=st["reference_asset_ids"],
        ext_json=st["ext"],
    )
    for f, v in st["fields"].items():
        setattr(row, f, v)
    if st["position"] is not None:
        row.position = st["position"]
    session.add(row)
    session.flush()
    return row


def shot_cast_update(session: Session, shot: Shot, character_id: str, *,
                     state: dict) -> ShotCharacter:
    row = session.execute(
        select(ShotCharacter).where(
            ShotCharacter.shot_id == shot.id, ShotCharacter.character_id == character_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise KeyError("character is not in this shot's cast")
    st = parse_shot_cast_state(state, partial=True)
    for f, v in st["fields"].items():
        setattr(row, f, v)
    if st["loras"] is not None:
        row.lora_json = st["loras"]
    if st["continuity"]:
        row.continuity_json = {**(row.continuity_json or {}), **st["continuity"]}
    if st["reference_asset_ids"] is not None:
        row.reference_ids_json = st["reference_asset_ids"]
    if st["ext"]:
        row.ext_json = {**(row.ext_json or {}), **st["ext"]}
    if st["position"] is not None:
        row.position = st["position"]
    row.updated_at = utcnow()
    session.flush()
    return row


def shot_cast_remove(session: Session, shot: Shot, character_id: str) -> ShotCharacter:
    row = session.execute(
        select(ShotCharacter).where(
            ShotCharacter.shot_id == shot.id, ShotCharacter.character_id == character_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise KeyError("character is not in this shot's cast")
    session.delete(row)
    session.flush()
    return row


# ---------- entity ↔ asset links ----------

def _check_refset_membership(session: Session, project_id: str, *,
                            entity_type: str, entity_id: str,
                            set_id: str | None) -> str | None:
    """A link may only join a set owned by the same project AND the same
    entity — cross-entity membership would silently leak references."""
    if not set_id:
        return None
    rs = get_refset(session, set_id, project_id)
    if entity_type != "character" or rs.character_id != entity_id:
        raise KeyError("reference set does not belong to this entity")
    return rs.id


def link_entity_asset(session: Session, project_id: str, *, entity_type: str,
                      entity_id: str, asset_id: str, role: str, label: str = "",
                      weight=_UNSET, set_id=_UNSET) -> EntityAssetLink:
    from app.db.models import Asset
    if entity_type not in ENTITY_ASSET_TYPES:
        raise ValueError(f"unsupported entity_type '{entity_type}'")
    if role not in ENTITY_ASSET_ROLES:
        raise ValueError(f"unsupported role '{role}'")
    entity = session.get(ENTITY_MODELS[entity_type], entity_id)
    if entity is None or entity.project_id != project_id:
        raise KeyError(f"{entity_type} not found in this project")
    asset = session.get(Asset, asset_id)
    if asset is None or asset.project_id != project_id:
        raise KeyError("asset not found in this project")
    checked_set = None
    if set_id is not _UNSET:
        checked_set = _check_refset_membership(
            session, project_id, entity_type=entity_type,
            entity_id=entity_id, set_id=set_id)
    row = session.execute(
        select(EntityAssetLink).where(
            EntityAssetLink.entity_type == entity_type,
            EntityAssetLink.entity_id == entity_id,
            EntityAssetLink.asset_id == asset_id,
            EntityAssetLink.role == role,
        )
    ).scalar_one_or_none()
    if row is None:
        row = EntityAssetLink(
            id=new_id(), project_id=project_id, entity_type=entity_type,
            entity_id=entity_id, asset_id=asset_id, role=role, label=label,
            weight=weight if weight is not _UNSET else None,
            set_id=checked_set,
            position=_next_position(
                session, EntityAssetLink,
                (EntityAssetLink.entity_type == entity_type) & (EntityAssetLink.entity_id == entity_id),
            ),
        )
        session.add(row)
        session.flush()
    elif weight is not _UNSET:
        # Re-linking the same (entity, asset, role) updates the stored
        # guidance weight rather than creating a duplicate row.
        row.weight = weight
        session.flush()
    return row


def update_entity_asset_link(session: Session, project_id: str, link_id: str, *,
                             label=_UNSET, weight=_UNSET,
                             set_id=_UNSET) -> EntityAssetLink:
    row = session.get(EntityAssetLink, link_id)
    if row is None or row.project_id != project_id:
        raise KeyError("link not found in this project")
    if label is not _UNSET:
        row.label = label
    if weight is not _UNSET:
        row.weight = weight  # may be None — explicit clear
    if set_id is not _UNSET:
        row.set_id = _check_refset_membership(
            session, project_id, entity_type=row.entity_type,
            entity_id=row.entity_id, set_id=set_id)
    session.flush()
    return row


def reorder_entity_asset_links(session: Session, project_id: str, *,
                               entity_type: str, entity_id: str,
                               ordered_link_ids: list[str],
                               set_id: str | None = None) -> list[EntityAssetLink]:
    """Assign positions 0..n-1 in the given order. The id list must be a
    COMPLETE permutation of the entity's links — a partial order would
    leave the rest of the set ambiguous. With `set_id` the permutation
    scopes to that set's members only."""
    links = session.execute(
        select(EntityAssetLink).where(
            EntityAssetLink.project_id == project_id,
            EntityAssetLink.entity_type == entity_type,
            EntityAssetLink.entity_id == entity_id,
        )
    ).scalars().all()
    if set_id:
        links = [l for l in links if l.set_id == set_id]
    by_id = {l.id: l for l in links}
    if set(ordered_link_ids) != set(by_id) or len(ordered_link_ids) != len(by_id):
        raise ValueError(
            "ordered_link_ids must be the complete set of this entity's links")
    for pos, lid in enumerate(ordered_link_ids):
        by_id[lid].position = pos
    session.flush()
    return [by_id[lid] for lid in ordered_link_ids]


def unlink_entity_asset(session: Session, project_id: str, link_id: str) -> EntityAssetLink:
    row = session.get(EntityAssetLink, link_id)
    if row is None or row.project_id != project_id:
        raise KeyError("link not found in this project")
    session.delete(row)
    session.flush()
    return row


def list_entity_assets(session: Session, project_id: str, *, entity_type: str | None = None,
                       entity_id: str | None = None) -> list[EntityAssetLink]:
    q = select(EntityAssetLink).where(EntityAssetLink.project_id == project_id)
    if entity_type:
        q = q.where(EntityAssetLink.entity_type == entity_type)
    if entity_id:
        q = q.where(EntityAssetLink.entity_id == entity_id)
    return session.execute(q.order_by(EntityAssetLink.position)).scalars().all()


# ---------- character library ----------

def duplicate_character(session: Session, character: Character, *, name: str | None,
                        command_id: str | None) -> Character:
    from app.domain import bibles
    dup = bibles.create_entity(
        session, character.project_id, "character",
        name=(name or f"{character.name} (copy)"), summary=character.summary,
        data=dict(character.profile_json or {}), command_id=command_id,
    )
    # C4-A — duplicate the character's reference sets first so member
    # links can be remapped to the new set ids.
    from app.db.models import ReferenceSet
    set_map: dict[str, str] = {}
    for rs in session.execute(
        select(ReferenceSet).where(
            ReferenceSet.character_id == character.id,
            ReferenceSet.status != "archived",
        )
    ).scalars():
        dup_rs = ReferenceSet(
            id=new_id(), project_id=character.project_id,
            character_id=dup.id, name=rs.name, seed=rs.seed,
            notes=rs.notes, position=rs.position, revision=1,
        )
        session.add(dup_rs)
        session.flush()
        set_map[rs.id] = dup_rs.id
    for link in session.execute(
        select(EntityAssetLink).where(
            EntityAssetLink.entity_type == "character",
            EntityAssetLink.entity_id == character.id,
        )
    ).scalars():
        session.add(EntityAssetLink(
            id=new_id(), project_id=character.project_id, entity_type="character",
            entity_id=dup.id, asset_id=link.asset_id, role=link.role,
            label=link.label, position=link.position, weight=link.weight,
            set_id=set_map.get(link.set_id),
        ))
    session.flush()
    return dup


# ---------- storyboard read model ----------

def storyboard(session: Session, project_id: str, *, include_archived: bool = True) -> list[dict]:
    """Full board: scenes (ordered) → shots (ordered) → cast with names."""
    q = select(Scene).where(Scene.project_id == project_id).order_by(Scene.position)
    if not include_archived:
        q = q.where(Scene.status == "active")
    scenes = session.execute(q).scalars().all()
    if not scenes:
        return []
    scene_ids = [s.id for s in scenes]
    shots = session.execute(
        select(Shot).where(Shot.scene_id.in_(scene_ids)).order_by(Shot.position)
    ).scalars().all()
    shot_ids = [s.id for s in shots]
    scene_cast = session.execute(
        select(SceneCharacter).where(SceneCharacter.scene_id.in_(scene_ids))
        .order_by(SceneCharacter.position)
    ).scalars().all()
    shot_cast = session.execute(
        select(ShotCharacter).where(ShotCharacter.shot_id.in_(shot_ids))
        .order_by(ShotCharacter.position)
    ).scalars().all() if shot_ids else []
    char_ids = {c.character_id for c in scene_cast} | {c.character_id for c in shot_cast}
    chars = {
        c.id: c for c in session.execute(
            select(Character).where(Character.id.in_(char_ids))
        ).scalars().all()
    } if char_ids else {}
    loc_ids = {s.location_id for s in scenes if s.location_id} | {
        s.location_id for s in shots if s.location_id}
    locs = {
        l.id: l for l in session.execute(select(Location).where(Location.id.in_(loc_ids))).scalars().all()
    } if loc_ids else {}

    sc_by_scene: dict[str, list] = {}
    for c in scene_cast:
        sc_by_scene.setdefault(c.scene_id, []).append(c)
    sh_by_scene: dict[str, list] = {}
    for s in shots:
        sh_by_scene.setdefault(s.scene_id, []).append(s)
    sc_by_shot: dict[str, list] = {}
    for c in shot_cast:
        sc_by_shot.setdefault(c.shot_id, []).append(c)

    def char_brief(cid: str) -> dict:
        c = chars.get(cid)
        return {"id": cid, "name": c.name if c else "?", "status": c.status if c else "missing"}

    out = []
    for s in scenes:
        scene_cast_rows = sc_by_scene.get(s.id, [])

        def shot_entry(sh: Shot) -> dict:
            own = sc_by_shot.get(sh.id, [])
            own_ids = {c.character_id for c in own}
            # Shots inherit scene cast unless they hold their own row for that
            # character — an own row is the per-shot override.
            inherited = [
                {**_cols(c), "character": char_brief(c.character_id), "source": "scene"}
                for c in scene_cast_rows if c.character_id not in own_ids
            ]
            return {
                "shot": sh,
                "location_name": locs.get(sh.location_id or "", None)
                                 and locs[sh.location_id].name or "",
                "cast": [
                    {**_cols(c), "character": char_brief(c.character_id), "source": "shot"}
                    for c in own
                ],
                "inherited": inherited,
            }

        out.append({
            "scene": s,
            "location_name": locs.get(s.location_id or "", None) and locs[s.location_id].name or "",
            "cast": [
                {**_cols(c), "character": char_brief(c.character_id), "source": "scene"}
                for c in scene_cast_rows
            ],
            "shots": [shot_entry(sh) for sh in sh_by_scene.get(s.id, [])],
        })
    return out


# ---------- reference sets (C4-A) ----------

def get_refset(session: Session, set_id: str, project_id: str):
    """Project-scoped ReferenceSet lookup — raises KeyError like every
    other get_* helper here (commands turn it into 404)."""
    from app.db.models import ReferenceSet
    rs = session.get(ReferenceSet, set_id or "")
    if rs is None or rs.project_id != project_id or rs.status != "active":
        raise KeyError("reference set not found in this project")
    return rs


def create_refset(session: Session, project_id: str, character: Character, *,
                  name: str, seed=None, notes: str = ""):
    from app.db.models import ReferenceSet
    rs = ReferenceSet(
        id=new_id(), project_id=project_id, character_id=character.id,
        name=name, seed=seed, notes=notes or "", revision=1,
        position=_next_position(
            session, ReferenceSet,
            (ReferenceSet.project_id == project_id)
            & (ReferenceSet.character_id == character.id)),
    )
    session.add(rs)
    session.flush()
    return rs


def list_refsets(session: Session, project_id: str, *,
                 character_id: str | None = None):
    from app.db.models import ReferenceSet
    q = select(ReferenceSet).where(
        ReferenceSet.project_id == project_id,
        ReferenceSet.status != "archived")
    if character_id:
        q = q.where(ReferenceSet.character_id == character_id)
    return session.execute(q.order_by(ReferenceSet.position)).scalars().all()


def set_members(session: Session, set_id: str, project_id: str):
    """Ordered links belonging to a set (position order)."""
    return session.execute(
        select(EntityAssetLink).where(
            EntityAssetLink.set_id == set_id,
            EntityAssetLink.project_id == project_id,
        ).order_by(EntityAssetLink.position)
    ).scalars().all()


def delete_refset(session: Session, rs) -> None:
    """Deleting a set keeps its links — they fall back to ungrouped
    references rather than disappearing from the character."""
    for link in set_members(session, rs.id, rs.project_id):
        link.set_id = None
    rs.status = "archived"
    rs.revision += 1
    session.flush()
