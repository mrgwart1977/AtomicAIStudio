"""A1 — screenplay documents + deterministic shot-list scaffold.

Same versioning contract as bibles/storyboard: every committed mutation
bumps the screenplay's revision and writes one immutable
EntityRevision snapshot embedding ALL its scenes + elements — a revert
restores the whole document atomically. Per-scene state (cast,
positions, links) lives inside that snapshot.

Shot-list scaffold is deterministic (NOT generation): each screenplay
scene maps to a storyboard Scene; maximal runs of same-kind elements
become suggested shots. Managed shots carry a fingerprint in
continuity_json["shotlist"] — sync compares it to detect manual edits
and never touches a manually-edited shot without explicit per-shot
approval. Removals archive rather than delete, and also require
approval. Every applied change writes the touched entity's own
revision, so individual shots/scenes stay revertable through the
existing storyboard history.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain import storyboard as sb
from app.db.models import (
    Character,
    EntityRevision,
    Location,
    Scene,
    Screenplay,
    ScreenplayElement,
    ScreenplayScene,
    Shot,
    ShotCharacter,
    new_id,
    utcnow,
)

ELEMENT_KINDS = ("action", "dialogue", "note")

# Shot-type mapping for scaffolded shots — deterministic and explainable.
_KIND_SHOT_TYPE = {"action": "wide", "dialogue": "medium"}


class ScreenplayError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------- fetch (project-scoped; KeyError on cross-project access) ----------

def get_screenplay(session: Session, screenplay_id: str, project_id: str) -> Screenplay:
    row = session.get(Screenplay, screenplay_id)
    if row is None or row.project_id != project_id:
        raise KeyError("screenplay not found in this project")
    return row


def get_sp_scene(session: Session, sp_scene_id: str, project_id: str) -> ScreenplayScene:
    row = session.get(ScreenplayScene, sp_scene_id)
    if row is None or row.project_id != project_id:
        raise KeyError("screenplay scene not found in this project")
    return row


def get_element(session: Session, element_id: str, project_id: str) -> ScreenplayElement:
    row = session.get(ScreenplayElement, element_id)
    if row is None or row.project_id != project_id:
        raise KeyError("screenplay element not found in this project")
    return row


def _check_link(session: Session, model, link_id: str | None, project_id: str, label: str) -> None:
    if link_id is None:
        return
    row = session.get(model, link_id)
    if row is None or row.project_id != project_id:
        raise KeyError(f"{label} not found in this project")


# ---------- snapshots / revisions ----------

def _cols(row) -> dict[str, Any]:
    return sb._cols(row)


def sp_scene_elements(session: Session, sp_scene_id: str) -> list[ScreenplayElement]:
    return session.execute(
        select(ScreenplayElement)
        .where(ScreenplayElement.screenplay_scene_id == sp_scene_id)
        .order_by(ScreenplayElement.position)
    ).scalars().all()


def screenplay_scenes(session: Session, screenplay_id: str) -> list[ScreenplayScene]:
    return session.execute(
        select(ScreenplayScene)
        .where(ScreenplayScene.screenplay_id == screenplay_id)
        .order_by(ScreenplayScene.position)
    ).scalars().all()


def sp_scene_snapshot(session: Session, sp_scene: ScreenplayScene) -> dict[str, Any]:
    snap = _cols(sp_scene)
    snap["elements"] = [_cols(e) for e in sp_scene_elements(session, sp_scene.id)]
    return snap


def screenplay_snapshot(session: Session, sp: Screenplay) -> dict[str, Any]:
    snap = _cols(sp)
    snap["scenes"] = [sp_scene_snapshot(session, s)
                      for s in screenplay_scenes(session, sp.id)]
    return snap


def write_screenplay_revision(session: Session, sp: Screenplay, *,
                              command_id: str | None, summary: str) -> EntityRevision:
    row = EntityRevision(
        id=new_id(), project_id=sp.project_id, entity_type="screenplay",
        entity_id=sp.id, revision=sp.revision,
        snapshot_json=screenplay_snapshot(session, sp),
        summary=summary, command_id=command_id,
    )
    session.add(row)
    return row


def _bump(session: Session, sp: Screenplay, *, command_id: str | None, summary: str) -> None:
    sp.revision += 1
    sp.updated_at = utcnow()
    session.flush()
    write_screenplay_revision(session, sp, command_id=command_id, summary=summary)


# ---------- screenplay CRUD ----------

def _next_position(session: Session, model, where) -> int:
    return sb._next_position(session, model, where)


def create_screenplay(session: Session, project_id: str, *, title: str,
                      description: str = "", metadata: dict | None = None,
                      command_id: str | None = None) -> Screenplay:
    if not (title or "").strip():
        raise ValueError("screenplay title is required")
    sp = Screenplay(
        id=new_id(), project_id=project_id, title=title.strip(),
        description=description or "", metadata_json=dict(metadata or {}),
        position=_next_position(session, Screenplay, Screenplay.project_id == project_id),
        revision=1,
    )
    session.add(sp)
    session.flush()
    write_screenplay_revision(session, sp, command_id=command_id,
                              summary="Created screenplay")
    return sp


SCREENPLAY_FIELDS = ("title", "description")


def update_screenplay(session: Session, sp: Screenplay, *, fields: dict,
                      metadata: dict | None = None, command_id: str | None = None,
                      change_summary: str = "Updated screenplay") -> None:
    for f in SCREENPLAY_FIELDS:
        if f in fields and fields[f] is not None:
            setattr(sp, f, fields[f])
    if metadata is not None:
        sp.metadata_json = {**(sp.metadata_json or {}), **metadata}
    _bump(session, sp, command_id=command_id, summary=change_summary)


def set_screenplay_archived(session: Session, sp: Screenplay, *, archived: bool,
                            command_id: str | None) -> None:
    sp.status = "archived" if archived else "active"
    _bump(session, sp, command_id=command_id,
          summary="Archived screenplay" if archived else "Restored screenplay")


def move_screenplay(session: Session, sp: Screenplay, to_index: int,
                    *, command_id: str | None) -> None:
    rows = session.execute(
        select(Screenplay).where(Screenplay.project_id == sp.project_id)
        .order_by(Screenplay.position)
    ).scalars().all()
    rows.remove(sp)
    rows.insert(max(0, min(to_index, len(rows))), sp)
    sb._renumber(rows)
    _bump(session, sp, command_id=command_id, summary="Reordered screenplays")


# ---------- screenplay scenes ----------

def create_sp_scene(session: Session, sp: Screenplay, *, heading: str = "",
                    action: str = "", notes: str = "",
                    location_id: str | None = None, scene_id: str | None = None,
                    position: int | None = None,
                    command_id: str | None = None) -> ScreenplayScene:
    _check_link(session, Location, location_id, sp.project_id, "location")
    _check_link(session, Scene, scene_id, sp.project_id, "scene")
    pos = _next_position(session, ScreenplayScene,
                         ScreenplayScene.screenplay_id == sp.id)
    sp_scene = ScreenplayScene(
        id=new_id(), project_id=sp.project_id, screenplay_id=sp.id,
        position=pos, heading=(heading or "").strip(), action=action or "",
        notes=notes or "", location_id=location_id, scene_id=scene_id,
        revision=1,
    )
    session.add(sp_scene)
    session.flush()
    if position is not None and position != pos:
        move_sp_scene(session, sp_scene, position, bump=False)
    _bump(session, sp, command_id=command_id,
          summary="Added screenplay scene")
    return sp_scene


SP_SCENE_FIELDS = ("heading", "action", "notes")


def update_sp_scene(session: Session, sp: Screenplay, sp_scene: ScreenplayScene, *,
                    fields: dict, location_id=..., scene_id=...,
                    command_id: str | None = None,
                    change_summary: str = "Updated screenplay scene") -> None:
    for f in SP_SCENE_FIELDS:
        if f in fields and fields[f] is not None:
            setattr(sp_scene, f, fields[f])
    if location_id is not ...:
        _check_link(session, Location, location_id, sp.project_id, "location")
        sp_scene.location_id = location_id
    if scene_id is not ...:
        _check_link(session, Scene, scene_id, sp.project_id, "scene")
        sp_scene.scene_id = scene_id
    sp_scene.revision += 1
    sp_scene.updated_at = utcnow()
    session.flush()
    _bump(session, sp, command_id=command_id, summary=change_summary)


def delete_sp_scene(session: Session, sp: Screenplay, sp_scene: ScreenplayScene,
                    *, command_id: str | None = None) -> None:
    session.delete(sp_scene)
    session.flush()
    # renumber remaining + bump screenplay revision
    rows = screenplay_scenes(session, sp.id)
    sb._renumber(rows)
    _bump(session, sp, command_id=command_id, summary="Deleted screenplay scene")


def move_sp_scene(session: Session, sp_scene: ScreenplayScene, to_index: int,
                  *, bump: bool = True, command_id: str | None = None) -> None:
    rows = screenplay_scenes(session, sp_scene.screenplay_id)
    rows.remove(sp_scene)
    rows.insert(max(0, min(to_index, len(rows))), sp_scene)
    sb._renumber(rows)
    if bump:
        sp = session.get(Screenplay, sp_scene.screenplay_id)
        _bump(session, sp, command_id=command_id,
              summary="Reordered screenplay scenes")


# ---------- screenplay elements ----------

def _apply_element(session: Session, sp_scene: ScreenplayElement | None,
                   sp_scene_id: str, project_id: str, data: dict,
                   position: int, elem: ScreenplayElement | None = None) -> ScreenplayElement:
    kind = data.get("kind", "action")
    if kind not in ELEMENT_KINDS:
        raise ValueError(f"unknown element kind '{kind}'")
    character_id = data.get("character_id")
    if kind != "dialogue":
        character_id = None
    _check_link(session, Character, character_id, project_id, "character")
    if elem is None:
        elem = ScreenplayElement(id=new_id(), project_id=project_id,
                                 screenplay_scene_id=sp_scene_id)
        session.add(elem)
    elem.kind = kind
    elem.character_id = character_id
    elem.text = data.get("text", "") or ""
    elem.parenthetical = data.get("parenthetical", "") or ""
    elem.position = position
    elem.updated_at = utcnow()
    return elem


def set_sp_scene_elements(session: Session, sp: Screenplay,
                          sp_scene: ScreenplayScene, elements: list[dict],
                          *, command_id: str | None = None,
                          change_summary: str = "Updated screenplay scene elements") -> None:
    """Replace a scene's element list in one revision bump (the editor
    saves a scene card as a unit). Existing element ids are reused so
    references and sync fingerprints stay stable."""
    existing = {e.id: e for e in sp_scene_elements(session, sp_scene.id)}
    keep: set[str] = set()
    for i, data in enumerate(elements):
        eid = data.get("id")
        row = existing.get(eid) if eid else None
        if eid and row is None:
            raise ValueError(f"element {eid} not found in this scene")
        row = _apply_element(session, None, sp_scene.id, sp.project_id,
                             data, i, elem=row)
        session.flush()  # ensure new row has its id before tracking
        keep.add(row.id)
    for eid, row in existing.items():
        if eid not in keep:
            session.delete(row)
    sp_scene.revision += 1
    sp_scene.updated_at = utcnow()
    session.flush()
    _bump(session, sp, command_id=command_id, summary=change_summary)


def add_element(session: Session, sp: Screenplay, sp_scene: ScreenplayScene, *,
                kind: str, text: str = "", character_id: str | None = None,
                parenthetical: str = "", position: int | None = None,
                command_id: str | None = None) -> ScreenplayElement:
    pos = _next_position(session, ScreenplayElement,
                         ScreenplayElement.screenplay_scene_id == sp_scene.id)
    elem = _apply_element(session, None, sp_scene.id, sp.project_id,
                          {"kind": kind, "text": text,
                           "character_id": character_id,
                           "parenthetical": parenthetical}, pos)
    session.flush()
    if position is not None and position != pos:
        move_element(session, elem, position, bump=False)
    _bump(session, sp, command_id=command_id, summary="Added screenplay element")
    return elem


def update_element(session: Session, sp: Screenplay, elem: ScreenplayElement, *,
                   fields: dict, character_id=...,
                   command_id: str | None = None) -> None:
    if "kind" in fields and fields["kind"] is not None:
        if fields["kind"] not in ELEMENT_KINDS:
            raise ValueError(f"unknown element kind '{fields['kind']}'")
        elem.kind = fields["kind"]
    for f in ("text", "parenthetical"):
        if f in fields and fields[f] is not None:
            setattr(elem, f, fields[f])
    if character_id is not ...:
        if elem.kind == "dialogue":
            _check_link(session, Character, character_id, sp.project_id, "character")
            elem.character_id = character_id
        else:
            elem.character_id = None
    if elem.kind != "dialogue":
        elem.character_id = None
    elem.updated_at = utcnow()
    session.flush()
    _bump(session, sp, command_id=command_id, summary="Updated screenplay element")


def delete_element(session: Session, sp: Screenplay, elem: ScreenplayElement,
                   *, command_id: str | None = None) -> None:
    sp_scene_id = elem.screenplay_scene_id
    session.delete(elem)
    session.flush()
    rows = session.execute(
        select(ScreenplayElement)
        .where(ScreenplayElement.screenplay_scene_id == sp_scene_id)
        .order_by(ScreenplayElement.position)
    ).scalars().all()
    sb._renumber(rows)
    _bump(session, sp, command_id=command_id, summary="Deleted screenplay element")


def move_element(session: Session, elem: ScreenplayElement, to_index: int,
                 *, bump: bool = True, command_id: str | None = None) -> None:
    rows = session.execute(
        select(ScreenplayElement)
        .where(ScreenplayElement.screenplay_scene_id == elem.screenplay_scene_id)
        .order_by(ScreenplayElement.position)
    ).scalars().all()
    rows.remove(elem)
    rows.insert(max(0, min(to_index, len(rows))), elem)
    sb._renumber(rows)
    if bump:
        sp_scene = session.get(ScreenplayScene, elem.screenplay_scene_id)
        sp = session.get(Screenplay, sp_scene.screenplay_id)
        _bump(session, sp, command_id=command_id,
              summary="Reordered screenplay elements")


# ---------- revert ----------

def revert_screenplay(session: Session, sp: Screenplay, to_revision: int,
                      *, command_id: str | None = None) -> None:
    """Restore the whole document (scenes + elements) from a snapshot.
    Storyboard Scenes/Shots the scaffold created are NEVER touched —
    they own their own revision history."""
    row = session.execute(
        select(EntityRevision).where(
            EntityRevision.entity_type == "screenplay",
            EntityRevision.entity_id == sp.id,
            EntityRevision.revision == to_revision,
        )
    ).scalar_one_or_none()
    if row is None:
        raise KeyError(f"revision {to_revision} not found")
    snap = row.snapshot_json
    for f in SCREENPLAY_FIELDS + ("status", "position", "metadata_json"):
        if f in snap:
            setattr(sp, f, snap[f])
    _restore_scenes(session, sp, snap.get("scenes") or [])
    _bump(session, sp, command_id=command_id,
          summary=f"Reverted to revision {to_revision}")


def _restore_scenes(session: Session, sp: Screenplay, scene_snaps: list[dict]) -> None:
    current = {s.id: s for s in screenplay_scenes(session, sp.id)}
    keep: set[str] = set()
    for i, ssnap in enumerate(scene_snaps):
        sid = ssnap.get("id")
        srow = current.get(sid)
        if srow is None:
            srow = ScreenplayScene(id=sid or new_id(), project_id=sp.project_id,
                                   screenplay_id=sp.id)
            session.add(srow)
        for f in SP_SCENE_FIELDS + ("location_id", "scene_id"):
            if f in ssnap:
                setattr(srow, f, ssnap[f])
        srow.position = i
        srow.revision = ssnap.get("revision", srow.revision)
        srow.updated_at = utcnow()
        keep.add(srow.id)
        _restore_elements(session, srow, ssnap.get("elements") or [])
    for sid, srow in current.items():
        if sid not in keep:
            session.delete(srow)


def _restore_elements(session: Session, sp_scene: ScreenplayScene,
                      elem_snaps: list[dict]) -> None:
    current = {e.id: e for e in sp_scene_elements(session, sp_scene.id)}
    keep: set[str] = set()
    for i, esnap in enumerate(elem_snaps):
        eid = esnap.get("id")
        erow = current.get(eid)
        if erow is None:
            erow = ScreenplayElement(id=eid or new_id(),
                                     project_id=sp_scene.project_id,
                                     screenplay_scene_id=sp_scene.id)
            session.add(erow)
        for f in ("kind", "character_id", "text", "parenthetical"):
            if f in esnap:
                setattr(erow, f, esnap[f])
        erow.position = i
        erow.updated_at = utcnow()
        keep.add(erow.id)
    for eid, erow in current.items():
        if eid not in keep:
            session.delete(erow)


# ---------- payloads ----------

def element_payload(elem: ScreenplayElement) -> dict[str, Any]:
    return {
        "id": elem.id, "position": elem.position, "kind": elem.kind,
        "character_id": elem.character_id, "text": elem.text,
        "parenthetical": elem.parenthetical,
    }


def sp_scene_payload(session: Session, sp_scene: ScreenplayScene) -> dict[str, Any]:
    out = _cols(sp_scene)
    out["elements"] = [element_payload(e)
                       for e in sp_scene_elements(session, sp_scene.id)]
    return out


def screenplay_payload(session: Session, sp: Screenplay, *,
                       include_scenes: bool = True) -> dict[str, Any]:
    out = _cols(sp)
    if include_scenes:
        out["scenes"] = [sp_scene_payload(session, s)
                         for s in screenplay_scenes(session, sp.id)]
    return out


def list_screenplays(session: Session, project_id: str, *,
                     include_archived: bool = False) -> list[dict[str, Any]]:
    q = select(Screenplay).where(Screenplay.project_id == project_id)
    if not include_archived:
        q = q.where(Screenplay.status == "active")
    rows = session.execute(q.order_by(Screenplay.position)).scalars().all()
    return [screenplay_payload(session, sp, include_scenes=False) for sp in rows]


# ---------- shot-list scaffold & sync ----------

def _element_groups(elements: list[ScreenplayElement]) -> list[list[ScreenplayElement]]:
    """Maximal runs of consecutive same-kind elements; 'note' runs are
    excluded (they're writer annotations, not screen beats)."""
    groups: list[list[ScreenplayElement]] = []
    for e in elements:
        if e.kind == "note":
            continue
        if groups and groups[-1] and groups[-1][0].kind == e.kind \
                and e.kind == "dialogue":
            groups[-1].append(e)
        elif groups and groups[-1] and groups[-1][0].kind == e.kind \
                and e.kind == "action":
            groups[-1].append(e)
        else:
            groups.append([e])
    return groups


def _group_key(group: list[ScreenplayElement]) -> str:
    # sorted: shot identity is the SET of elements in the beat —
    # reordering dialogue inside a run keeps the shot (content
    # signature still shifts, producing a normal update, not a
    # remove+add identity churn)
    return ",".join(sorted(e.id for e in group))


def _group_signature(group: list[ScreenplayElement]) -> str:
    """Content hash of the screenplay-side source — what the suggested
    shot is derived FROM. Element edits change the signature → sync
    proposes an update (subject to manual-edit check)."""
    parts = []
    for e in group:
        parts.append("|".join([e.id, e.kind, e.character_id or "",
                               e.text, e.parenthetical]))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _shot_fingerprint(shot: Shot, session: Session) -> str:
    """Hash of ONLY the scaffold-managed fields — title, shot_type,
    description, location + the dialogue-carrying cast rows. Compared
    against continuity_json['shotlist']['fingerprint'] to detect manual
    edits to content a sync would overwrite. Fields sync never writes
    (prompt, intent, duration, thumbnails, non-dialogue cast fields)
    are deliberately excluded — editing them alone must not flag a
    shot as manually edited."""
    cast = session.execute(
        select(ShotCharacter).where(ShotCharacter.shot_id == shot.id)
        .order_by(ShotCharacter.position)
    ).scalars().all()
    parts = ["|".join([shot.title, shot.shot_type, shot.description,
                       shot.location_id or ""])]
    for c in cast:
        parts.append("|".join([str(c.position), c.character_id, c.dialogue]))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _spec_fingerprint(sug: dict[str, Any]) -> str:
    """The fingerprint a shot will carry AFTER applying `sug` — same
    field set as _shot_fingerprint, computed from the spec so the stamp
    can ride inside the shot's revision snapshot at write time."""
    parts = ["|".join([sug["title"], sug["shot_type"], sug["description"],
                       sug.get("location_id") or ""])]
    for i, c in enumerate(sug.get("cast") or []):
        parts.append("|".join([str(i), c["character_id"],
                               c.get("dialogue") or ""]))
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _managed_shots(session: Session, scene_id: str) -> dict[str, Shot]:
    """Shots under a storyboard Scene that carry a shotlist fingerprint,
    keyed by the element-group key they were scaffolded from."""
    shots = session.execute(
        select(Shot).where(Shot.scene_id == scene_id)
    ).scalars().all()
    out: dict[str, Shot] = {}
    for sh in shots:
        meta = (sh.continuity_json or {}).get("shotlist") or {}
        gk = meta.get("group_key")
        if gk:
            out[gk] = sh
    return out


def _suggested_shot(session: Session, sp_scene: ScreenplayScene,
                    group: list[ScreenplayElement],
                    char_names: dict[str, str]) -> dict[str, Any]:
    """Pure function: group → proposed shot spec."""
    kind = group[0].kind
    shot_type = _KIND_SHOT_TYPE.get(kind, "medium")
    if kind == "dialogue":
        names = []
        seen = set()
        for e in group:
            n = char_names.get(e.character_id) or "(unresolved speaker)"
            if e.character_id not in seen:
                seen.add(e.character_id)
                names.append(n)
        speakers = "+".join(names)
        title = f"{speakers}"[:240]
        description = " · ".join(
            (e.text or "").strip().split("\n", 1)[0][:80] for e in group[:4])
        # merge repeated speakers: shot_characters is unique on
        # (shot_id, character_id), so a character with several lines in
        # one beat collapses to a single row with all their lines
        merged: dict[str, dict] = {}
        for e in group:
            if not e.character_id:
                continue
            row = merged.get(e.character_id)
            if row is None:
                merged[e.character_id] = {
                    "character_id": e.character_id,
                    "dialogue": e.text or "",
                    "parenthetical": e.parenthetical}
            else:
                row["dialogue"] = (row["dialogue"] + "\n" + (e.text or "")).strip()
        cast = list(merged.values())
    else:
        title = (group[0].text or "Action beat").strip().split("\n", 1)[0][:80]
        description = "\n".join((e.text or "").strip() for e in group)[:2000]
        cast = []
    return {
        "group_key": _group_key(group),
        "signature": _group_signature(group),
        "title": title or "(untitled beat)",
        "shot_type": shot_type,
        "description": description,
        "location_id": sp_scene.location_id,
        "cast": cast,
        "camera": {"mode": None, "placeholder": True},  # A1: placeholder only
    }


def scaffold_plan(session: Session, sp: Screenplay) -> dict[str, Any]:
    """Compute the FULL desired state (deterministic). Used by both
    scaffold (first apply) and sync (diff vs current)."""
    chars = {c.id: c.name for c in session.execute(
        select(Character).where(Character.project_id == sp.project_id)
    ).scalars()}
    scenes: list[dict[str, Any]] = []
    for sp_scene in screenplay_scenes(session, sp.id):
        elements = sp_scene_elements(session, sp_scene.id)
        suggestions = []
        # scene-level action block becomes its own leading shot if present
        if (sp_scene.action or "").strip():
            pseudo = ScreenplayElement(
                id=f"{sp_scene.id}::action", kind="action",
                text=sp_scene.action.strip(), parenthetical="")
            suggestions.append(_suggested_shot(session, sp_scene, [pseudo], chars))
        for group in _element_groups(elements):
            suggestions.append(_suggested_shot(session, sp_scene, group, chars))
        scenes.append({
            "sp_scene_id": sp_scene.id,
            "heading": sp_scene.heading,
            "scene_id": sp_scene.scene_id,
            "location_id": sp_scene.location_id,
            "suggestions": suggestions,
        })
    return {"screenplay_id": sp.id, "scenes": scenes}


def _diff_scene(session: Session, project_id: str,
                sp_scene: ScreenplayScene,
                scene_plan: dict[str, Any]) -> dict[str, Any]:
    """Diff one screenplay scene's suggestions against existing managed
    shots. Returns classified proposals; nothing mutates here."""
    out = {"sp_scene_id": sp_scene.id, "heading": sp_scene.heading,
           "scene_id": sp_scene.scene_id, "scene_created": False,
           "add": [], "update": [], "remove": [], "manual_edit": [],
           "unchanged": []}
    scene_id = sp_scene.scene_id
    managed: dict[str, Shot] = {}
    if scene_id:
        scene = session.get(Scene, scene_id)
        if scene is None or scene.project_id != project_id:
            scene_id = None  # stale link — treated as unlinked
        else:
            managed = _managed_shots(session, scene_id)
    archived_managed = {k: v for k, v in managed.items()
                        if v.status != "active"}
    managed = {k: v for k, v in managed.items() if v.status == "active"}
    out["skipped_archived"] = []
    wanted_keys = set()
    for sug in scene_plan["suggestions"]:
        gk = sug["group_key"]
        wanted_keys.add(gk)
        shot = managed.get(gk)
        if shot is None:
            if gk in archived_managed:
                # user archived the scaffolded shot — report it, NEVER
                # silently recreate a shot the user put away
                out["skipped_archived"].append({
                    "shot_id": archived_managed[gk].id, "group_key": gk,
                    "shot_title": archived_managed[gk].title})
                continue
            out["add"].append(sug)
            continue
        meta = (shot.continuity_json or {}).get("shotlist") or {}
        if meta.get("signature") == sug["signature"]:
            out["unchanged"].append({"shot_id": shot.id, "group_key": gk})
            continue
        fingerprint_now = _shot_fingerprint(shot, session)
        if fingerprint_now != meta.get("fingerprint"):
            out["manual_edit"].append({
                "shot_id": shot.id, "group_key": gk,
                "shot_title": shot.title, "proposed": sug})
        else:
            out["update"].append({"shot_id": shot.id, "group_key": gk,
                                  "proposed": sug})
    # shots whose groups are gone → proposed removals (archive only)
    for gk, shot in managed.items():
        if gk not in wanted_keys and shot.status == "active":
            fingerprint_now = _shot_fingerprint(shot, session)
            meta = (shot.continuity_json or {}).get("shotlist") or {}
            manual = fingerprint_now != meta.get("fingerprint")
            out["remove"].append({"shot_id": shot.id, "group_key": gk,
                                  "shot_title": shot.title,
                                  "manually_edited": manual})
    return out


def shotlist_plan(session: Session, project_id: str,
                  screenplay_id: str) -> dict[str, Any]:
    """The previewable sync diff: additions / updates / removals /
    manual-edit conflicts / unchanged, per screenplay scene."""
    sp = get_screenplay(session, screenplay_id, project_id)
    plan = scaffold_plan(session, sp)
    scene_diffs = []
    for scene_plan in plan["scenes"]:
        sp_scene = get_sp_scene(session, scene_plan["sp_scene_id"], project_id)
        scene_diffs.append(_diff_scene(session, project_id, sp_scene, scene_plan))
    return {
        "screenplay_id": sp.id,
        "scenes": scene_diffs,
        "counts": {
            "add": sum(len(d["add"]) for d in scene_diffs),
            "update": sum(len(d["update"]) for d in scene_diffs),
            "remove": sum(len(d["remove"]) for d in scene_diffs),
            "manual_edit": sum(len(d["manual_edit"]) for d in scene_diffs),
            "unchanged": sum(len(d["unchanged"]) for d in scene_diffs),
        },
    }


def _apply_suggestion(session: Session, sp_scene: ScreenplayScene,
                      scene: Scene, sug: dict[str, Any], *,
                      shot: Shot | None, command_id: str | None) -> Shot:
    """Write one suggested spec into a shot (create or update), then
    stamp the post-write fingerprint into its shotlist metadata."""
    cast_spec = {c["character_id"]: c.get("dialogue") or ""
                 for c in (sug.get("cast") or [])}
    continuity = {"shotlist": {
        "screenplay_scene_id": sp_scene.id,
        "group_key": sug["group_key"],
        "signature": sug["signature"],
        "fingerprint": _spec_fingerprint(sug),
        "cast_spec": cast_spec,
        "camera": sug["camera"],
    }}
    created = shot is None
    if created:
        shot = sb.create_shot(
            session, scene, title=sug["title"],
            shot_type=sug["shot_type"], description=sug["description"],
            intent="", prompt="", duration_sec=0.0,
            location_id=sug["location_id"], continuity=continuity,
            command_id=command_id)
    else:
        # cast rows first so the update's revision snapshot carries them
        old_spec = ((shot.continuity_json or {}).get("shotlist") or {}) \
            .get("cast_spec") or {}
        _apply_suggestion_cast(session, shot, sug["cast"], old_spec,
                               command_id=command_id)
        cont = dict(shot.continuity_json or {})
        cont["shotlist"] = {**(cont.get("shotlist") or {}), **continuity["shotlist"]}
        sb.update_shot(
            session, shot,
            fields={"title": sug["title"], "shot_type": sug["shot_type"],
                    "description": sug["description"]},
            location_id=sug["location_id"], continuity=cont,
            command_id=command_id,
            change_summary="Shot-list sync update")
        return shot
    _apply_suggestion_cast(session, shot, sug["cast"], {},
                           command_id=command_id)
    if created:
        # the create-path revision was written before the cast rows —
        # one more bump so the latest snapshot carries them faithfully
        sb.update_shot(session, shot, fields={}, command_id=command_id,
                       change_summary="Shot-list scaffold")
    return shot


def _apply_suggestion_cast(session: Session, shot: Shot,
                           cast_spec: list[dict], old_spec: dict, *,
                           command_id: str | None) -> None:
    """Align the shot's dialogue-carrying cast rows to the suggestion.

    Removal rule: a cast row whose character left the spec is deleted
    ONLY when it is provably scaffold-owned and untouched — its
    dialogue equals what the last spec wrote (`old_spec`) — or carries
    no content at all. Any row holding user-authored state (edited
    dialogue, blocking, wardrobe, overrides, refs) is preserved; the
    shot's fingerprint already flags content edits as manual_edit at
    the diff level, so a preserved row survives even an approved
    overwrite of the managed fields."""
    have = {c.character_id: c for c in session.execute(
        select(ShotCharacter).where(ShotCharacter.shot_id == shot.id)
    ).scalars()}
    want_ids = [c["character_id"] for c in cast_spec]
    pos = 0
    for spec in cast_spec:
        cid = spec["character_id"]
        row = have.pop(cid, None)
        if row is None:
            row = ShotCharacter(id=new_id(), project_id=shot.project_id,
                                shot_id=shot.id, character_id=cid)
            session.add(row)
        row.dialogue = spec.get("dialogue") or ""
        row.position = pos
        pos += 1
    for cid, row in have.items():
        if cid in want_ids:
            continue
        scaffold_owned = (row.dialogue == old_spec.get(cid, "\x00")
                          and row.dialogue != "")
        empty = not any([row.blocking, row.expression, row.emotion,
                         row.action_instructions, row.voice_override,
                         row.model_ref, row.lora_json,
                         row.reference_ids_json, row.dialogue])
        if scaffold_owned or empty:
            session.delete(row)
    session.flush()


def apply_shotlist(session: Session, project_id: str, screenplay_id: str,
                   *, approve_overwrite: list[str] | None = None,
                   approve_remove: list[str] | None = None,
                   command_id: str | None = None) -> dict[str, Any]:
    """Commit-side: apply the diff with explicit approvals. Returns a
    batch record (created/updated/archived/unlinked + ids) that
    shotlist.undo reverses and shotlist.redo re-applies."""
    overwrite = set(approve_overwrite or [])
    remove_ok = set(approve_remove or [])
    plan = shotlist_plan(session, project_id, screenplay_id)
    sp = get_screenplay(session, screenplay_id, project_id)
    batch = {"created_scenes": [], "created_shots": [],
             "updated_shots": [], "archived_shots": [],
             "linked_scenes": [], "skipped_manual_edit": [],
             "skipped_remove": []}
    char_cache: dict[str, Character] = {}
    for scene_diff, scene_plan in zip(plan["scenes"], scaffold_plan(session, sp)["scenes"]):
        sp_scene = get_sp_scene(session, scene_diff["sp_scene_id"], project_id)
        scene = None
        if sp_scene.scene_id:
            scene = session.get(Scene, sp_scene.scene_id)
            if scene is None or scene.project_id != project_id:
                scene = None
        needs_scene = (scene_diff["add"] or scene_diff["update"]
                       or scene_diff["manual_edit"])
        if scene is None and needs_scene:
            scene = sb.create_scene(
                session, project_id,
                title=sp_scene.heading or f"Scene {sp_scene.position + 1}",
                description=sp_scene.action, intent="",
                location_id=sp_scene.location_id,
                command_id=command_id)
            sp_scene.scene_id = scene.id
            sp_scene.revision += 1
            session.flush()
            batch["created_scenes"].append(scene.id)
            batch["linked_scenes"].append(
                {"sp_scene_id": sp_scene.id, "scene_id": scene.id})
        if scene is None:
            continue
        managed = _managed_shots(session, scene.id)
        # additions
        for sug in scene_diff["add"]:
            shot = _apply_suggestion(session, sp_scene, scene, sug,
                                     shot=None, command_id=command_id)
            batch["created_shots"].append(shot.id)
        # approved-content updates (fingerprint still managed)
        for item in scene_diff["update"]:
            shot = session.get(Shot, item["shot_id"])
            if shot is None or shot.project_id != project_id:
                continue
            prev = shot.revision
            _apply_suggestion(session, sp_scene, scene, item["proposed"],
                              shot=shot, command_id=command_id)
            batch["updated_shots"].append(
                {"shot_id": shot.id, "prev_revision": prev,
                 "post_revision": shot.revision})
        # manual-edit overwrites — explicit per-shot approval required
        for item in scene_diff["manual_edit"]:
            if item["shot_id"] not in overwrite:
                batch["skipped_manual_edit"].append(item["shot_id"])
                continue
            shot = session.get(Shot, item["shot_id"])
            if shot is None or shot.project_id != project_id:
                continue
            prev = shot.revision
            _apply_suggestion(session, sp_scene, scene, item["proposed"],
                              shot=shot, command_id=command_id)
            batch["updated_shots"].append(
                {"shot_id": shot.id, "prev_revision": prev,
                 "post_revision": shot.revision,
                 "was_manual_edit": True})
        # removals — archive only, explicit approval required
        for item in scene_diff["remove"]:
            if item["shot_id"] not in remove_ok:
                batch["skipped_remove"].append(item["shot_id"])
                continue
            shot = session.get(Shot, item["shot_id"])
            if shot is None or shot.project_id != project_id:
                continue
            prev_status = shot.status
            sb.set_shot_archived(session, shot, archived=True,
                                 command_id=command_id)
            batch["archived_shots"].append(
                {"shot_id": shot.id, "prev_status": prev_status})
    session.flush()
    return batch


def undo_shotlist(session: Session, project_id: str, batch: dict,
                  *, command_id: str | None = None) -> dict[str, Any]:
    """Reverse an apply batch: created shots/scenes archived (never
    deleted — recoverable), updated shots reverted to their
    pre-sync snapshots, archived shots restored."""
    undone = {"archived": [], "reverted": [], "restored": [],
              "skipped_stale": []}
    for shot_id in batch.get("created_shots") or []:
        shot = session.get(Shot, shot_id)
        if shot is None or shot.project_id != project_id:
            continue
        if shot.status == "active":
            sb.set_shot_archived(session, shot, archived=True,
                                 command_id=command_id)
        undone["archived"].append(shot_id)
    for scene_id in batch.get("created_scenes") or []:
        scene = session.get(Scene, scene_id)
        if scene is None or scene.project_id != project_id:
            continue
        if scene.status == "active":
            sb.set_scene_archived(session, scene, archived=True,
                                  command_id=command_id)
        undone["archived"].append(scene_id)
        # unlink any screenplay scene pointing at it
        for sps in session.execute(
                select(ScreenplayScene).where(
                    ScreenplayScene.scene_id == scene_id)).scalars():
            sps.scene_id = None
    for item in batch.get("updated_shots") or []:
        shot = session.get(Shot, item["shot_id"])
        if shot is None or shot.project_id != project_id:
            continue
        post = item.get("post_revision")
        # fail-safe staleness: if the shot was edited after the batch
        # applied, reverting would destroy the newer work — skip it.
        if post is not None and shot.revision != post:
            undone["skipped_stale"].append(item["shot_id"])
            continue
        try:
            sb.revert_shot(session, shot, item["prev_revision"],
                           command_id=command_id)
            undone["reverted"].append(item["shot_id"])
            # annotate the batch (this mutated copy is what the undo
            # command's audit persists — redo reads it back): the exact
            # revision the undo produced, so redo can verify the shot
            # is still in the undone state before re-applying
            item["undone_revision"] = shot.revision
        except KeyError:
            pass
    for item in batch.get("archived_shots") or []:
        shot = session.get(Shot, item["shot_id"])
        if shot is None or shot.project_id != project_id:
            continue
        if item.get("prev_status") == "active" and shot.status == "archived":
            sb.set_shot_archived(session, shot, archived=False,
                                 command_id=command_id)
            undone["restored"].append(item["shot_id"])
    # restore screenplay-scene → scene links recorded in the batch
    for link in batch.get("linked_scenes") or []:
        sps = session.get(ScreenplayScene, link["sp_scene_id"])
        scene = session.get(Scene, link["scene_id"])
        if sps is not None and scene is not None \
                and scene.project_id == project_id:
            sps.scene_id = scene.id
    session.flush()
    return undone


def redo_shotlist(session: Session, project_id: str, batch: dict,
                  *, command_id: str | None = None) -> dict[str, Any]:
    """Re-apply an undone batch: unarchive created rows, re-apply the
    forward revisions by re-running apply semantics on recorded ids."""
    redone = {"restored": [], "archived": [], "reapplied": [],
              "skipped_stale": []}
    for shot_id in batch.get("created_shots") or []:
        shot = session.get(Shot, shot_id)
        if shot is not None and shot.project_id == project_id \
                and shot.status == "archived":
            sb.set_shot_archived(session, shot, archived=False,
                                 command_id=command_id)
            redone["restored"].append(shot_id)
    for item in batch.get("updated_shots") or []:
        shot = session.get(Shot, item["shot_id"])
        if shot is None or shot.project_id != project_id:
            continue
        post = item.get("post_revision")
        # re-apply only while the shot still sits at the exact revision
        # the undo produced (annotated on this batch by undo_shotlist);
        # anything else means intervening work — never clobber it.
        # Batches undone before this annotation existed have no marker
        # and fail safe by skipping.
        undone_rev = item.get("undone_revision")
        if post is not None and undone_rev is not None \
                and shot.revision == undone_rev:
            try:
                sb.revert_shot(session, shot, post, command_id=command_id)
                redone["reapplied"].append(item["shot_id"])
            except KeyError:
                pass
        else:
            redone["skipped_stale"].append(item["shot_id"])
    for scene_id in batch.get("created_scenes") or []:
        scene = session.get(Scene, scene_id)
        if scene is not None and scene.project_id == project_id \
                and scene.status == "archived":
            sb.set_scene_archived(session, scene, archived=False,
                                  command_id=command_id)
            redone["restored"].append(scene_id)
    for item in batch.get("archived_shots") or []:
        shot = session.get(Shot, item["shot_id"])
        if shot is not None and shot.project_id == project_id \
                and shot.status == "active":
            sb.set_shot_archived(session, shot, archived=True,
                                 command_id=command_id)
            redone["archived"].append(item["shot_id"])
    session.flush()
    return redone
