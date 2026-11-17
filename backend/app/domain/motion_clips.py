"""A4 — motion clips: shot-associated keyframe-to-video beats.

A motion clip joins optional start/end keyframes with a motion-aware
prompt, camera movement/follow settings (same validated vocabulary as
A2 shot planning), rational duration + fps, resolution, generation
settings, and a mock-produced video result asset placeable on the
existing timeline. Every mutation flows through the command system;
prompts and motion settings are user-owned — `clip.compose` proposes
a draft that is only written by an explicit apply.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Asset,
    EntityRevision,
    GenerationJob,
    Keyframe,
    MotionClip,
    Shot,
    new_id,
    utcnow,
)
from app.domain import storyboard as sb
from app.domain.keyframes import (
    PROMPT_MAX,
    check_continuity_refs,
)

MOTION_NOTES_MAX = 2000
MAX_DURATION_FRAMES = 480 * 60  # bounded: ≤1 min at max timeline rate
_MAX_DIM = 4096
_INTENSITIES = ("subtle", "moderate", "strong")


def _reject(msg: str) -> None:
    raise ValueError(msg)


def _nonneg_int(v: Any, field: str, lo: int = 0) -> int:
    if not isinstance(v, int) or isinstance(v, bool) or v < lo:
        _reject(f"{field} must be an integer ≥ {lo}")
    return v


def parse_motion(value: Any) -> dict[str, Any]:
    """{instructions, intensity, temporal_consistency} — all optional;
    unknown keys dropped like every other A-series document."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        _reject("motion must be an object")
    out: dict[str, Any] = {}
    for key in ("instructions", "temporal_consistency"):
        if key not in value:
            continue
        v = value[key]
        if v is None:
            out[key] = None  # explicit null clears
            continue
        if not isinstance(v, str):
            _reject(f"motion.{key} must be a string")
        out[key] = v.strip()[:MOTION_NOTES_MAX] or None
    inten = value.get("intensity")
    if inten is not None:
        if inten not in _INTENSITIES:
            _reject(f"motion.intensity must be one of {_INTENSITIES}")
        out["intensity"] = inten
    elif "intensity" in value:
        out["intensity"] = None  # explicit null clears
    return out


def parse_camera(s: Session, project_id: str, value: Any) -> dict[str, Any]:
    """Clip camera spec {movement, follow_mode, follow_targets,
    follow_params, framing, subject_placement} — validated through the
    A2 planning parser so the vocabulary can never drift."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        _reject("camera must be an object")
    planning = {"camera": {
        k: v for k, v in value.items()
        if k in ("movement", "follow_mode", "follow_targets",
                 "follow_params", "angle")},
        "framing": value.get("framing"),
        "subject_placement": value.get("subject_placement"),
    }
    # Strip absent keys so explicit nulls still clear but missing keys
    # don't manufacture clears.
    planning["camera"] = {k: v for k, v in planning["camera"].items()
                          if k in value}
    for k in ("framing", "subject_placement"):
        if k not in value:
            planning.pop(k)
    parsed = sb.parse_shot_planning(s, project_id, planning)
    out: dict[str, Any] = {}
    cam = parsed.get("camera")
    if isinstance(cam, dict):
        out.update(cam)
    elif cam is None and "camera" in parsed:
        out["camera_cleared"] = True
    for k in ("framing", "subject_placement"):
        if k in parsed:
            out[k] = parsed[k]
    return out


def parse_clip_timing(payload: dict) -> tuple[int | None, int, int]:
    """(duration_frames, fps_num, fps_den) — rational and bounded the
    same way the timeline's own timebase is."""
    frames = payload.get("duration_frames")
    if frames is not None:
        frames = _nonneg_int(frames, "duration_frames", 1)
        if frames > MAX_DURATION_FRAMES:
            _reject(f"duration_frames must be ≤ {MAX_DURATION_FRAMES}")
    fps = payload.get("fps")
    num, den = 24, 1
    if fps is not None:
        if not isinstance(fps, dict):
            _reject("fps must be an object {num, den}")
        num, den = fps.get("num"), fps.get("den")
        if not isinstance(num, int) or not isinstance(den, int) \
                or isinstance(num, bool) or isinstance(den, bool) \
                or num < 1 or den < 1:
            _reject("fps needs positive integer num/den")
        if num / den > 480:
            _reject("frame rate must be at most 480 fps")
    return frames, num, den


def _get(s: Session, project_id: str, clip_id: str) -> MotionClip:
    c = s.get(MotionClip, clip_id)
    if c is None or c.project_id != project_id:
        _reject("motion clip not found")
    return c


def _active_shot(s: Session, project_id: str, shot_id: str) -> Shot:
    shot = s.get(Shot, shot_id)
    if shot is None or shot.project_id != project_id:
        _reject("shot not found")
    if shot.status != "active":
        _reject("shot is archived")
    return shot


def _kf(s: Session, project_id: str, shot_id: str, kf_id: Any,
        field: str) -> str | None:
    """A clip's keyframe refs must live on the same shot in the same
    project — never a cross-shot/cross-project link."""
    if kf_id in (None, ""):
        return None
    kf = s.get(Keyframe, kf_id)
    if kf is None or kf.project_id != project_id:
        _reject(f"{field}: keyframe not in project")
    if kf.shot_id != shot_id:
        _reject(f"{field}: keyframe belongs to a different shot")
    return kf.id


def _cols(row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if hasattr(val, "isoformat"):
            val = val.isoformat()
        out[col.name] = val
    return out


def clip_snapshot(c: MotionClip) -> dict:
    return _cols(c)


def write_clip_revision(s: Session, c: MotionClip, *, command_id: str | None,
                        summary: str) -> EntityRevision:
    row = EntityRevision(
        id=new_id(), project_id=c.project_id, entity_type="motion_clip",
        entity_id=c.id, revision=c.revision,
        snapshot_json=clip_snapshot(c),
        summary=summary, command_id=command_id,
    )
    s.add(row)
    return row


def create_clip(s: Session, project_id: str, payload: dict,
                command_id: str | None = None) -> MotionClip:
    shot = _active_shot(s, project_id, payload["shot_id"])
    prompt = (payload.get("prompt") or "").strip()
    neg = (payload.get("negative_prompt") or "").strip()
    if len(prompt) > PROMPT_MAX or len(neg) > PROMPT_MAX:
        _reject(f"prompt exceeds {PROMPT_MAX} chars")
    frames, num, den = parse_clip_timing(payload)
    w = payload.get("width"); h = payload.get("height")
    for v, f in ((w, "width"), (h, "height")):
        if v is not None:
            _nonneg_int(v, f, 1)
            if v > _MAX_DIM:
                _reject(f"{f} must be ≤ {_MAX_DIM}")
    seed = payload.get("seed")
    if seed is not None:
        seed = _nonneg_int(seed, "seed")
    c = MotionClip(
        id=new_id(), project_id=project_id, shot_id=shot.id,
        scene_id=shot.scene_id,
        position=(s.execute(select(func.max(MotionClip.position)).where(
            MotionClip.shot_id == shot.id)).scalar_one() or -1) + 1,
        start_keyframe_id=_kf(s, project_id, shot.id,
                              payload.get("start_keyframe_id"),
                              "start_keyframe_id"),
        end_keyframe_id=_kf(s, project_id, shot.id,
                            payload.get("end_keyframe_id"),
                            "end_keyframe_id"),
        prompt=prompt, negative_prompt=neg,
        motion_json={k: v for k, v in parse_motion(
            payload.get("motion")).items() if v is not None},
        camera_json=parse_camera(s, project_id, payload.get("camera")),
        duration_frames=frames, fps_num=num, fps_den=den,
        width=w or 512, height=h or 512,
        model_key=(payload.get("model_key") or "")[:200] or None,
        seed=seed,
        continuity_json={"refs": check_continuity_refs(
            s, project_id,
            (payload.get("continuity") or {}).get("refs") or [])},
        prompt_origin="compose" if payload.get("compose") else "user",
        status="active", revision=0,
    )
    s.add(c)
    s.flush()
    write_clip_revision(s, c, command_id=command_id,
                        summary="Motion clip created")
    return c


def update_clip(s: Session, project_id: str, payload: dict,
                command_id: str | None = None) -> MotionClip:
    c = _get(s, project_id, payload["clip_id"])
    if c.status != "active":
        _reject("motion clip is archived")
    if "start_keyframe_id" in payload:
        c.start_keyframe_id = _kf(s, project_id, c.shot_id,
                                  payload["start_keyframe_id"],
                                  "start_keyframe_id")
    if "end_keyframe_id" in payload:
        c.end_keyframe_id = _kf(s, project_id, c.shot_id,
                                payload["end_keyframe_id"],
                                "end_keyframe_id")
    if "prompt" in payload:
        p = (payload["prompt"] or "").strip()
        if len(p) > PROMPT_MAX:
            _reject(f"prompt exceeds {PROMPT_MAX} chars")
        c.prompt = p
        if p:
            c.prompt_origin = "compose" if payload.get("compose") else "user"
    if "negative_prompt" in payload:
        neg = (payload["negative_prompt"] or "").strip()
        if len(neg) > PROMPT_MAX:
            _reject(f"negative_prompt exceeds {PROMPT_MAX} chars")
        c.negative_prompt = neg
    if "motion" in payload:
        merged = dict(c.motion_json or {})
        for k, v in parse_motion(payload["motion"]).items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        c.motion_json = merged
    if "camera" in payload:
        merged = dict(c.camera_json or {})
        parsed = parse_camera(s, project_id, payload["camera"])
        if parsed.pop("camera_cleared", False):
            for k in ("movement", "follow_mode", "follow_targets",
                      "follow_params", "angle"):
                merged.pop(k, None)
        for k, v in parsed.items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        c.camera_json = merged
    if any(k in payload for k in ("duration_frames", "fps")):
        frames, num, den = parse_clip_timing({
            "duration_frames": payload.get("duration_frames",
                                           c.duration_frames),
            "fps": payload.get("fps",
                               {"num": c.fps_num, "den": c.fps_den})})
        c.duration_frames, c.fps_num, c.fps_den = frames, num, den
    for f in ("width", "height"):
        if f in payload:
            v = _nonneg_int(payload[f], f, 1)
            if v > _MAX_DIM:
                _reject(f"{f} must be ≤ {_MAX_DIM}")
            setattr(c, f, v)
    if "seed" in payload:
        c.seed = (None if payload["seed"] is None
                  else _nonneg_int(payload["seed"], "seed"))
    if "model_key" in payload:
        c.model_key = (payload["model_key"] or "")[:200] or None
    if "continuity" in payload:
        c.continuity_json = {"refs": check_continuity_refs(
            s, project_id,
            (payload["continuity"] or {}).get("refs") or [])}
    c.revision += 1
    c.updated_at = utcnow()
    write_clip_revision(s, c, command_id=command_id,
                        summary="Motion clip updated")
    return c


def archive_clip(s: Session, project_id: str, payload: dict,
                 archive: bool = True,
                 command_id: str | None = None) -> MotionClip:
    c = _get(s, project_id, payload["clip_id"])
    want = "archived" if archive else "active"
    if c.status == want:
        _reject(f"motion clip already {want}")
    c.status = want
    c.revision += 1
    c.updated_at = utcnow()
    write_clip_revision(s, c, command_id=command_id,
                        summary=f"Motion clip {want}")
    return c


def planning_sha(shot: Shot | None) -> str:
    return hashlib.sha256(json.dumps(
        (shot.planning_json if shot else None) or {},
        sort_keys=True).encode()).hexdigest()


def check_result_fresh(s: Session, c: MotionClip, payload: dict) -> None:
    """A4 — same stale-result contract as keyframes: only the clip's
    current generation, on an unchanged clip, with unchanged shot
    planning, may attach a result. Never a silent overwrite."""
    if c.status != "active":
        _reject("motion clip is archived")
    jid = payload.get("job_id")
    if jid is not None and c.generate_job_id != jid:
        _reject("result job is not the clip's current generation"
                " — superseded")
    erev = payload.get("expected_clip_revision")
    if erev is not None and erev != c.revision:
        _reject("motion clip changed since generation — result is stale")
    psha = payload.get("expected_planning_sha")
    if psha is not None and planning_sha(s.get(Shot, c.shot_id)) != psha:
        _reject("shot planning changed since generation — result is stale")


def apply_result(s: Session, project_id: str, payload: dict,
                 command_id: str | None = None) -> MotionClip:
    c = _get(s, project_id, payload["clip_id"])
    check_result_fresh(s, c, payload)
    asset_id = payload.get("result_asset_id")
    if asset_id is not None:
        asset = s.get(Asset, asset_id)
        if asset is None or asset.project_id != project_id:
            _reject("result asset not in project")
        c.result_asset_id = asset_id
    c.revision += 1
    c.updated_at = utcnow()
    write_clip_revision(s, c, command_id=command_id,
                        summary="Motion clip result attached")
    return c


def revert_clip(s: Session, project_id: str, clip_id: str,
                snapshot: dict) -> MotionClip:
    c = _get(s, project_id, clip_id)
    for col in ("position", "start_keyframe_id", "end_keyframe_id",
                "prompt", "negative_prompt", "motion_json", "camera_json",
                "duration_frames", "fps_num", "fps_den", "width", "height",
                "model_key", "seed", "settings_json", "continuity_json",
                "result_asset_id", "prompt_origin", "status"):
        if col in snapshot:
            setattr(c, col, snapshot[col])
    c.revision += 1
    c.updated_at = utcnow()
    return c


def list_clips(s: Session, project_id: str, shot_id: str) -> list[MotionClip]:
    shot = s.get(Shot, shot_id)
    if shot is None or shot.project_id != project_id:
        _reject("shot not found")
    return list(s.execute(
        select(MotionClip)
        .where(MotionClip.shot_id == shot_id,
               MotionClip.project_id == project_id)
        .order_by(MotionClip.position, MotionClip.created_at)
    ).scalars())


def list_shot_clips_bulk(s: Session, project_id: str,
                         shot_ids: list[str]) -> dict[str, list[MotionClip]]:
    if not shot_ids:
        return {}
    rows = list(s.execute(
        select(MotionClip)
        .where(MotionClip.project_id == project_id,
               MotionClip.shot_id.in_(shot_ids))
        .order_by(MotionClip.position, MotionClip.created_at)
    ).scalars())
    out: dict[str, list[MotionClip]] = {}
    for c in rows:
        out.setdefault(c.shot_id, []).append(c)
    return out


def serialize_clip(s: Session, c: MotionClip) -> dict:
    out = clip_snapshot(c)
    out.pop("project_id", None)
    out["result_asset_name"] = None
    out["result_asset_status"] = None
    if c.result_asset_id:
        asset = s.get(Asset, c.result_asset_id)
        if asset is not None:
            out["result_asset_name"] = asset.name
            out["result_asset_status"] = asset.status
    out["job_status"] = None
    if c.generate_job_id:
        job = s.get(GenerationJob, c.generate_job_id)
        if job is not None:
            out["job_status"] = job.status
            out["job_progress"] = job.progress
            if job.status == "succeeded" and job.output_asset_id \
                    and c.generate_job_id == job.id \
                    and c.result_asset_id != job.output_asset_id:
                c.result_asset_id = job.output_asset_id
    return out


def compose_draft(s: Session, project, clip_id: str) -> dict:
    """A4.2 — propose a motion-aware prompt draft through the same
    deterministic composer shots/keyframes use, seeded with the clip's
    motion context. Read-only until applied."""
    from app.domain.prompt_compose import compose_shot_prompt
    c = _get(s, project.id, clip_id)
    shot = s.get(Shot, c.shot_id)
    if shot is None or shot.project_id != project.id:
        _reject("shot not found")
    seed = c.prompt or shot.prompt or ""
    motion = c.motion_json or {}
    cam = c.camera_json or {}
    bits = [motion.get("instructions") or ""]
    bits += [f"camera {cam[k]}" for k in ("movement", "follow_mode")
             if cam.get(k)]
    lead = " ".join(b for b in bits if b).strip()
    draft_prompt = f"{seed} {lead}".strip() or "motion clip"
    return compose_shot_prompt(s, project, shot, user_prompt=draft_prompt)


def clip_history(s: Session, project_id: str,
                 clip_id: str) -> list[dict]:
    _get(s, project_id, clip_id)
    rows = s.execute(
        select(EntityRevision)
        .where(EntityRevision.entity_type == "motion_clip",
               EntityRevision.entity_id == clip_id,
               EntityRevision.project_id == project_id)
        .order_by(EntityRevision.revision.desc())
    ).scalars()
    return [{"revision": r.revision, "action": r.action,
             "summary": r.summary, "created_at": r.created_at.isoformat()}
            for r in rows]
