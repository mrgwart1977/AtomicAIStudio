"""A3 — keyframes: shot-associated still-image beats.

A keyframe carries a role (start/end/reference/intermediate), an
optional rational frame position, a user-authored prompt + negative
prompt, generation settings, project-scoped continuity refs, and the
produced result asset. Every mutation flows through the command
system; prompts are user-owned — `keyframe.compose` proposes a
continuity-aware draft (via prompt_compose) that is only written by an
explicit apply, never silently.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Asset,
    Character,
    EntityRevision,
    GenerationJob,
    Keyframe,
    Location,
    ReferenceSet,
    Shot,
    new_id,
    utcnow,
)

KEYFRAME_ROLES = ("start", "end", "reference", "intermediate")
PROMPT_MAX = 4000
NOTES_MAX = 2000
CONTINUITY_MAX_REFS = 32
_CONTINUITY_KINDS = ("asset", "refset", "character", "location")

# Generation settings a keyframe may carry. Everything optional; the
# adapter decides what to honor. Keys not listed are dropped (same
# convention as A2 planning) so a typo'd setting never silently
# reaches the job request.
_SETTINGS_KEYS = ("seed", "model_key", "steps", "cfg", "width", "height", "sampler")


def _reject(msg: str) -> None:
    raise ValueError(msg)


def _nonneg_int(v: Any, field: str) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
        _reject(f"{field} must be a non-negative integer")
    return v


def parse_frame_position(value: Any) -> tuple[int | None, int | None]:
    """Validate {num, den} — both present or both absent."""
    if value in (None, ""):
        return None, None
    if not isinstance(value, dict):
        _reject("frame must be an object {num, den}")
    num, den = value.get("num"), value.get("den")
    if num is None and den is None:
        return None, None
    if num is None or den is None:
        _reject("frame requires both num and den")
    num = _nonneg_int(num, "frame.num")
    den = _nonneg_int(den, "frame.den")
    if den < 1:
        _reject("frame.den must be a positive integer")
    return num, den


def parse_settings(value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        _reject("settings must be an object")
    out: dict[str, Any] = {}
    for k, v in value.items():
        if k not in _SETTINGS_KEYS:
            continue
        if k == "seed":
            if v is not None and (
                    not isinstance(v, int) or isinstance(v, bool)
                    or v < 0 or v > 2**63 - 1):
                _reject("settings.seed must be an integer in [0, 2^63)")
            out[k] = v
        elif k in ("model_key", "sampler"):
            if v is not None and not isinstance(v, str):
                _reject(f"settings.{k} must be a string")
            out[k] = (v or "")[:200] or None
        elif k in ("steps", "width", "height"):
            if v is not None and (
                    not isinstance(v, int) or isinstance(v, bool) or v <= 0):
                _reject(f"settings.{k} must be a positive integer")
            out[k] = v
        elif k == "cfg":
            if v is not None and (
                    not isinstance(v, (int, float)) or isinstance(v, bool)):
                _reject("settings.cfg must be a number")
            out[k] = v
    return out


def check_continuity_refs(s: Session, project_id: str, refs: list[dict]) -> list[dict]:
    """Project-scope every ref; returns normalized list."""
    out = []
    for r in refs[:CONTINUITY_MAX_REFS]:
        if not isinstance(r, dict):
            _reject("continuity ref must be an object")
        kind, rid = r.get("kind"), r.get("id")
        if kind not in _CONTINUITY_KINDS or not isinstance(rid, str) or not rid:
            _reject(f"invalid continuity ref kind/id: {r!r}")
        if kind == "character" and not s.execute(select(Character.id).where(
                Character.id == rid, Character.project_id == project_id)).first():
            _reject("continuity ref: character not in project")
        if kind == "location" and not s.execute(select(Location.id).where(
                Location.id == rid, Location.project_id == project_id)).first():
            _reject("continuity ref: location not in project")
        if kind == "asset" and not s.execute(select(Asset.id).where(
                Asset.id == rid, Asset.project_id == project_id)).first():
            _reject("continuity ref: asset not in project")
        if kind == "refset" and not s.execute(select(ReferenceSet.id).where(
                ReferenceSet.id == rid,
                ReferenceSet.project_id == project_id)).first():
            _reject("continuity ref: reference set not in project")
        out.append({"kind": kind, "id": rid,
                    "label": (r.get("label") or "")[:120]})
    return out


def parse_continuity(s: Session, project_id: str, value: Any) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        _reject("continuity must be an object")
    out: dict[str, Any] = {}
    refs = value.get("refs")
    if isinstance(refs, list):
        out["refs"] = check_continuity_refs(s, project_id, refs)
    notes = value.get("notes")
    if isinstance(notes, str) and notes.strip():
        out["notes"] = notes.strip()[:NOTES_MAX]
    return out


def _active_shot(s: Session, project_id: str, shot_id: str) -> Shot:
    shot = s.get(Shot, shot_id)
    if shot is None or shot.project_id != project_id:
        _reject("shot not found")
    if shot.status != "active":
        _reject("shot is archived")
    return shot


def _get(s: Session, project_id: str, keyframe_id: str) -> Keyframe:
    kf = s.get(Keyframe, keyframe_id)
    if kf is None or kf.project_id != project_id:
        _reject("keyframe not found")
    return kf


def _cols(row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if hasattr(val, "isoformat"):
            val = val.isoformat()
        out[col.name] = val
    return out


def keyframe_snapshot(kf: Keyframe) -> dict:
    return _cols(kf)


def write_keyframe_revision(s: Session, kf: Keyframe, *, command_id: str | None,
                            summary: str) -> EntityRevision:
    row = EntityRevision(
        id=new_id(), project_id=kf.project_id, entity_type="keyframe",
        entity_id=kf.id, revision=kf.revision,
        snapshot_json=keyframe_snapshot(kf),
        summary=summary, command_id=command_id,
    )
    s.add(row)
    return row


def create_keyframe(s: Session, project_id: str, payload: dict,
                    command_id: str | None = None) -> Keyframe:
    shot = _active_shot(s, project_id, payload["shot_id"])
    role = payload.get("role", "start")
    if role not in KEYFRAME_ROLES:
        _reject(f"role must be one of {KEYFRAME_ROLES}")
    num, den = parse_frame_position(payload.get("frame"))
    prompt = (payload.get("prompt") or "").strip()
    neg = (payload.get("negative_prompt") or "").strip()
    if len(prompt) > PROMPT_MAX or len(neg) > PROMPT_MAX:
        _reject(f"prompt exceeds {PROMPT_MAX} chars")
    kf = Keyframe(
        id=new_id(), project_id=project_id, shot_id=shot.id,
        scene_id=shot.scene_id, role=role,
        position=(s.execute(select(func.max(Keyframe.position)).where(
            Keyframe.shot_id == shot.id)).scalar_one() or -1) + 1,
        frame_num=num, frame_den=den,
        prompt=prompt, negative_prompt=neg,
        settings_json=parse_settings(payload.get("settings")),
        continuity_json=parse_continuity(
            s, project_id, payload.get("continuity")),
        prompt_origin="compose" if payload.get("compose") else "user",
        status="active", revision=0,
    )
    s.add(kf)
    s.flush()
    write_keyframe_revision(s, kf, command_id=command_id,
                            summary="Keyframe created")
    return kf


def update_keyframe(s: Session, project_id: str, payload: dict,
                    command_id: str | None = None) -> Keyframe:
    kf = _get(s, project_id, payload["keyframe_id"])
    if kf.status != "active":
        _reject("keyframe is archived")
    if "role" in payload:
        if payload["role"] not in KEYFRAME_ROLES:
            _reject(f"role must be one of {KEYFRAME_ROLES}")
        kf.role = payload["role"]
    if "frame" in payload:
        kf.frame_num, kf.frame_den = parse_frame_position(payload["frame"])
    if "prompt" in payload:
        p = (payload["prompt"] or "").strip()
        if len(p) > PROMPT_MAX:
            _reject(f"prompt exceeds {PROMPT_MAX} chars")
        kf.prompt = p
        if p:
            kf.prompt_origin = "compose" if payload.get("compose") else "user"
    if "negative_prompt" in payload:
        neg = (payload["negative_prompt"] or "").strip()
        if len(neg) > PROMPT_MAX:
            _reject(f"negative_prompt exceeds {PROMPT_MAX} chars")
        kf.negative_prompt = neg
    if "settings" in payload:
        merged = dict(kf.settings_json or {})
        for k, v in parse_settings(payload["settings"]).items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        kf.settings_json = merged
    if "continuity" in payload:
        kf.continuity_json = parse_continuity(
            s, project_id, payload["continuity"])
    kf.revision += 1
    kf.updated_at = utcnow()
    write_keyframe_revision(s, kf, command_id=command_id,
                            summary="Keyframe updated")
    return kf


def archive_keyframe(s: Session, project_id: str, payload: dict,
                     archive: bool = True,
                     command_id: str | None = None) -> Keyframe:
    kf = _get(s, project_id, payload["keyframe_id"])
    want = "archived" if archive else "active"
    if kf.status == want:
        _reject(f"keyframe already {want}")
    kf.status = want
    kf.revision += 1
    kf.updated_at = utcnow()
    write_keyframe_revision(s, kf, command_id=command_id,
                            summary=f"Keyframe {want}")
    return kf


def apply_result(s: Session, project_id: str, payload: dict,
                 command_id: str | None = None) -> Keyframe:
    """keyframe.result — the job's apply_command lands the produced
    asset as a normal audited, undoable command."""
    kf = _get(s, project_id, payload["keyframe_id"])
    asset_id = payload.get("result_asset_id")
    if asset_id is not None:
        asset = s.get(Asset, asset_id)
        if asset is None or asset.project_id != project_id:
            _reject("result asset not in project")
        kf.result_asset_id = asset_id
    kf.revision += 1
    kf.updated_at = utcnow()
    write_keyframe_revision(s, kf, command_id=command_id,
                            summary="Keyframe result attached")
    return kf


def revert_keyframe(s: Session, project_id: str, keyframe_id: str,
                    snapshot: dict) -> Keyframe:
    kf = _get(s, project_id, keyframe_id)
    for col in ("role", "position", "frame_num", "frame_den", "prompt",
                "negative_prompt", "settings_json", "continuity_json",
                "result_asset_id", "prompt_origin", "status"):
        if col in snapshot:
            setattr(kf, col, snapshot[col])
    kf.revision += 1
    kf.updated_at = utcnow()
    return kf


def list_keyframes(s: Session, project_id: str, shot_id: str) -> list[Keyframe]:
    shot = s.get(Shot, shot_id)
    if shot is None or shot.project_id != project_id:
        _reject("shot not found")
    return list(s.execute(
        select(Keyframe)
        .where(Keyframe.shot_id == shot_id,
               Keyframe.project_id == project_id)
        .order_by(Keyframe.position, Keyframe.created_at)
    ).scalars())


def list_shot_keyframes_bulk(s: Session, project_id: str,
                           shot_ids: list[str]) -> dict[str, list[Keyframe]]:
    """shot_id → keyframes (for board serialization)."""
    if not shot_ids:
        return {}
    rows = list(s.execute(
        select(Keyframe)
        .where(Keyframe.project_id == project_id,
               Keyframe.shot_id.in_(shot_ids))
        .order_by(Keyframe.position, Keyframe.created_at)
    ).scalars())
    out: dict[str, list[Keyframe]] = {}
    for kf in rows:
        out.setdefault(kf.shot_id, []).append(kf)
    return out


def serialize_keyframe(s: Session, kf: Keyframe) -> dict:
    out = keyframe_snapshot(kf)
    out.pop("project_id", None)
    out["result_asset_name"] = None
    out["result_asset_status"] = None
    if kf.result_asset_id:
        asset = s.get(Asset, kf.result_asset_id)
        if asset is not None:
            out["result_asset_name"] = asset.name
            out["result_asset_status"] = asset.status
    out["job_status"] = None
    if kf.generate_job_id:
        job = s.get(GenerationJob, kf.generate_job_id)
        if job is not None:
            out["job_status"] = job.status
            out["job_progress"] = job.progress
            if job.status == "succeeded" and job.output_asset_id \
                    and kf.result_asset_id != job.output_asset_id:
                # Self-heal if the post-command failed (still audited —
                # the apply_command wrote a revision when it ran).
                kf.result_asset_id = job.output_asset_id
    return out


def compose_draft(s: Session, project, keyframe_id: str) -> dict:
    """A3.2 — propose a continuity-aware prompt draft for a keyframe
    using the same deterministic composer shots use, seeded with a
    role-scoped lead-in. Read-only: nothing is written until the user
    applies the draft via keyframe.update."""
    from app.domain.prompt_compose import compose_shot_prompt
    kf = _get(s, project.id, keyframe_id)
    shot = s.get(Shot, kf.shot_id)
    if shot is None or shot.project_id != project.id:
        _reject("shot not found")
    lead = {
        "start": "opening frame",
        "end": "closing frame",
        "reference": "reference frame",
        "intermediate": "intermediate frame",
    }[kf.role]
    seed = kf.prompt or shot.prompt or ""
    draft_prompt = f"{lead}: {seed}".strip(": ") if seed else f"{lead} —"
    return compose_shot_prompt(s, project, shot, user_prompt=draft_prompt)


def keyframe_history(s: Session, project_id: str,
                     keyframe_id: str) -> list[dict]:
    _get(s, project_id, keyframe_id)
    rows = s.execute(
        select(EntityRevision)
        .where(EntityRevision.entity_type == "keyframe",
               EntityRevision.entity_id == keyframe_id,
               EntityRevision.project_id == project_id)
        .order_by(EntityRevision.revision.desc())
    ).scalars()
    return [{"revision": r.revision, "action": r.action,
             "summary": r.summary, "created_at": r.created_at.isoformat()}
            for r in rows]
