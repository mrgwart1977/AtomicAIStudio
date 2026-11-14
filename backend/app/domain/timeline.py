"""C5-A — timeline assembly domain.

A Timeline is an ordered list of TimelineClips. Every clip pins one
immutable asset version (id + sha256 + the source's frame geometry at
insert time). Trims are integer SOURCE frames: ``in_frame`` inclusive,
``out_frame`` exclusive, validated against the pinned ``src_frames`` —
never against whatever the asset's current version might become later.

Revisions ride entity_revisions (entity_type ``timeline``); each
snapshot embeds the full ordered clip list so revert restores the edit
exactly (new clip ids — clip identity is not stable across revert, the
edit decision is what persists).

Ordering is a dense integer ``position`` per timeline, renormalized
after every mutation — ordering is therefore always deterministic.
"""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (Asset, AssetVersion, EntityRevision, Scene, Shot,
                           Timeline, TimelineClip, new_id, utcnow)

#: Bounded rational timebases — keeps UI inputs honest without
#: pretending to support arbitrary rates.
MAX_FPS_NUM = 240
MAX_FPS_DEN = 1001

EDL_FORMAT = "ade-edl/1"


class TimelineError(ValueError):
    """Validation failure — command layer maps to 422."""


def _check_fps(num: int, den: int) -> None:
    if (isinstance(num, bool) or not isinstance(num, int)
            or not 1 <= num <= MAX_FPS_NUM):
        raise TimelineError(f"fps numerator must be an integer 1..{MAX_FPS_NUM}")
    if (isinstance(den, bool) or not isinstance(den, int)
            or not 1 <= den <= MAX_FPS_DEN):
        raise TimelineError(f"fps denominator must be an integer 1..{MAX_FPS_DEN}")


def _check_int(value: Any, name: str, lo: int, hi: int) -> int:
    """Integer time value — rejects bools, floats, NaN/inf (which arrive
    as non-int JSON anyway), and out-of-range values."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimelineError(f"{name} must be an integer frame number")
    if not lo <= value <= hi:
        raise TimelineError(f"{name} must be between {lo} and {hi}")
    return value


def list_timelines(session: Session, project_id: str) -> list[Timeline]:
    return session.execute(
        select(Timeline).where(Timeline.project_id == project_id)
        .order_by(Timeline.created_at, Timeline.id)).scalars().all()


def get_timeline(session: Session, project_id: str, timeline_id: str) -> Timeline:
    tl = session.get(Timeline, timeline_id)
    if tl is None or tl.project_id != project_id:
        raise KeyError("timeline not found in this project")
    return tl


def list_clips(session: Session, timeline_id: str) -> list[TimelineClip]:
    return session.execute(
        select(TimelineClip).where(TimelineClip.timeline_id == timeline_id)
        .order_by(TimelineClip.position, TimelineClip.id)).scalars().all()


def _renumber(session: Session, timeline_id: str) -> None:
    for i, c in enumerate(list_clips(session, timeline_id)):
        if c.position != i:
            c.position = i
    session.flush()


def _touch(session: Session, tl: Timeline, *, command_id: str | None,
           summary: str) -> None:
    tl.revision += 1
    tl.updated_at = utcnow()
    session.flush()
    write_timeline_revision(session, tl, command_id=command_id, summary=summary)


# ---------- revisions ----------

def clip_snapshot(c: TimelineClip) -> dict[str, Any]:
    return {
        "position": c.position,
        "source_asset_id": c.source_asset_id,
        "source_version_id": c.source_version_id,
        "source_sha256": c.source_sha256,
        "in_frame": c.in_frame, "out_frame": c.out_frame,
        "src_frames": c.src_frames,
        "src_fps_num": c.src_fps_num, "src_fps_den": c.src_fps_den,
        "src_duration_s": c.src_duration_s,
        "shot_id": c.shot_id, "scene_id": c.scene_id,
    }


def timeline_snapshot(session: Session, tl: Timeline) -> dict[str, Any]:
    return {
        "name": tl.name, "description": tl.description,
        "fps_num": tl.fps_num, "fps_den": tl.fps_den,
        "status": tl.status,
        "clips": [clip_snapshot(c) for c in list_clips(session, tl.id)],
    }


def write_timeline_revision(session: Session, tl: Timeline, *,
                            command_id: str | None, summary: str) -> EntityRevision:
    row = EntityRevision(
        project_id=tl.project_id, entity_type="timeline", entity_id=tl.id,
        revision=tl.revision, snapshot_json=timeline_snapshot(session, tl),
        summary=summary, command_id=command_id)
    session.add(row)
    session.flush()
    return row


def revision_history(session: Session, timeline_id: str) -> list[EntityRevision]:
    return session.execute(
        select(EntityRevision)
        .where(EntityRevision.entity_type == "timeline",
               EntityRevision.entity_id == timeline_id)
        .order_by(EntityRevision.revision.desc())).scalars().all()


# ---------- timeline ops ----------

def create_timeline(session: Session, project_id: str, *, name: str,
                    description: str = "", fps_num: int = 24,
                    fps_den: int = 1, command_id: str | None) -> Timeline:
    name = (name or "").strip()
    if not name:
        raise TimelineError("timeline name is required")
    _check_fps(fps_num, fps_den)
    tl = Timeline(id=new_id(), project_id=project_id, name=name,
                  description=(description or "").strip(),
                  fps_num=fps_num, fps_den=fps_den)
    session.add(tl)
    session.flush()
    write_timeline_revision(session, tl, command_id=command_id,
                            summary="Created timeline")
    return tl


def update_timeline(session: Session, tl: Timeline, *,
                    fields: dict, command_id: str | None,
                    change_summary: str) -> dict:
    """Rename/description/timebase. Timebase changes are refused once
    clips exist — clip durations are rational in their own source fps,
    so a changed timebase would silently re-grid every cut point."""
    before = {"name": tl.name, "description": tl.description,
              "fps_num": tl.fps_num, "fps_den": tl.fps_den}
    if "name" in fields:
        name = (fields["name"] or "").strip()
        if not name:
            raise TimelineError("timeline name is required")
        tl.name = name
    if "description" in fields:
        tl.description = (fields["description"] or "").strip()
    if "fps_num" in fields or "fps_den" in fields:
        num = fields.get("fps_num", tl.fps_num)
        den = fields.get("fps_den", tl.fps_den)
        _check_fps(num, den)
        if (num != tl.fps_num or den != tl.fps_den) and list_clips(
                session, tl.id):
            raise TimelineError(
                "timebase is locked once the timeline has clips")
        tl.fps_num, tl.fps_den = num, den
    _touch(session, tl, command_id=command_id, summary=change_summary)
    return before


def set_timeline_archived(session: Session, tl: Timeline, *,
                          archived: bool, command_id: str | None) -> str:
    before = tl.status
    tl.status = "archived" if archived else "active"
    _touch(session, tl, command_id=command_id,
           summary=("Archived" if archived else "Restored") + " timeline")
    return before


def duplicate_timeline(session: Session, tl: Timeline, *,
                       name: str | None, command_id: str | None) -> Timeline:
    dup = Timeline(id=new_id(), project_id=tl.project_id,
                   name=(name or f"{tl.name} (copy)").strip() or "Timeline copy",
                   description=tl.description,
                   fps_num=tl.fps_num, fps_den=tl.fps_den)
    session.add(dup)
    session.flush()
    for c in list_clips(session, tl.id):
        session.add(TimelineClip(
            id=new_id(), project_id=tl.project_id, timeline_id=dup.id,
            position=c.position, source_asset_id=c.source_asset_id,
            source_version_id=c.source_version_id,
            source_sha256=c.source_sha256,
            in_frame=c.in_frame, out_frame=c.out_frame,
            src_frames=c.src_frames, src_fps_num=c.src_fps_num,
            src_fps_den=c.src_fps_den, src_duration_s=c.src_duration_s,
            shot_id=c.shot_id, scene_id=c.scene_id))
    session.flush()
    write_timeline_revision(session, dup, command_id=command_id,
                            summary="Duplicated timeline")
    return dup


def revert_timeline(session: Session, tl: Timeline, to_revision: int, *,
                    command_id: str | None) -> None:
    row = session.execute(
        select(EntityRevision).where(
            EntityRevision.entity_type == "timeline",
            EntityRevision.entity_id == tl.id,
            EntityRevision.revision == to_revision)).scalar_one_or_none()
    if row is None:
        raise KeyError(f"revision {to_revision} not found")
    snap = row.snapshot_json or {}
    for f in ("name", "description", "fps_num", "fps_den", "status"):
        if f in snap:
            setattr(tl, f, snap[f])
    session.execute(
        delete(TimelineClip).where(TimelineClip.timeline_id == tl.id))
    session.flush()
    for i, c in enumerate(snap.get("clips") or []):
        session.add(TimelineClip(
            id=new_id(), project_id=tl.project_id, timeline_id=tl.id,
            position=i, source_asset_id=c["source_asset_id"],
            source_version_id=c["source_version_id"],
            source_sha256=c["source_sha256"],
            in_frame=c["in_frame"], out_frame=c["out_frame"],
            src_frames=c["src_frames"], src_fps_num=c["src_fps_num"],
            src_fps_den=c["src_fps_den"],
            src_duration_s=c.get("src_duration_s", 0.0),
            shot_id=c.get("shot_id"), scene_id=c.get("scene_id")))
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary=f"Reverted to revision {to_revision}")


# ---------- clip ops ----------

def source_geometry(version: AssetVersion) -> dict[str, Any]:
    """Frame geometry from the version's probe metadata. A clip can only
    pin a source with a MEASURED duration+fps — anything less fails
    safe, since trims can't be validated without it."""
    probe = (version.media_json or {}).get("probe") or {}
    fps = probe.get("fps")
    dur = probe.get("duration_s") or probe.get("duration")
    if not probe.get("ok") or not isinstance(fps, (int, float)) \
            or not isinstance(dur, (int, float)) or fps <= 0 or dur <= 0:
        raise TimelineError(
            "source video has no measured duration/fps — "
            "it cannot be trimmed safely")
    num, den = _fps_to_rational(float(fps))
    frames = probe.get("frames")
    if not isinstance(frames, int) or frames <= 0:
        frames = max(1, round(float(dur) * float(fps)))
    return {"frames": frames, "fps_num": num, "fps_den": den,
            "duration_s": float(dur), "width": probe.get("width"),
            "height": probe.get("height"), "codec": probe.get("codec")}


def _fps_to_rational(fps: float) -> tuple[int, int]:
    """Probe fps → exact rational. NTSC rates map to their canonical
    /1001 forms; any other non-integer rate keeps its rational value
    (denominator ≤ 1001) instead of being rounded to a whole number."""
    known = {24: (24, 1), 25: (25, 1), 30: (30, 1), 48: (48, 1),
             60: (60, 1), 23.976: (24000, 1001), 29.97: (30000, 1001),
             59.94: (60000, 1001)}
    for k, v in known.items():
        if abs(fps - k) < 0.01:
            return v
    f = Fraction(fps).limit_denominator(1001)
    return (max(1, f.numerator), max(1, f.denominator))


def resolve_clip_source(session: Session, project_id: str, asset_id: str,
                        version_id: str | None) -> tuple[Asset, AssetVersion, dict]:
    asset = session.get(Asset, asset_id)
    if asset is None or asset.project_id != project_id:
        raise KeyError("source asset not found in this project")
    if asset.kind != "video":
        raise TimelineError("timeline clips require a video asset")
    if asset.status != "active":
        raise TimelineError(
            f"source asset is {asset.status} — restore it before use")
    ver = (session.get(AssetVersion, version_id) if version_id
           else session.get(AssetVersion, asset.current_version_id))
    if ver is None or ver.asset_id != asset.id:
        raise TimelineError("source version not found on this asset")
    return asset, ver, source_geometry(ver)


def _check_trims(in_frame: int, out_frame: int, src_frames: int) -> tuple[int, int]:
    i = _check_int(in_frame, "in_frame", 0, src_frames - 1)
    o = _check_int(out_frame, "out_frame", 1, src_frames)
    if i >= o:
        raise TimelineError("in_frame must be before out_frame")
    return i, o


def insert_clip(session: Session, tl: Timeline, *, project_id: str,
                asset_id: str, version_id: str | None,
                in_frame: int | None, out_frame: int | None,
                position: int | None, shot_id: str | None,
                scene_id: str | None, allow_duplicate: bool,
                command_id: str | None) -> TimelineClip:
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    asset, ver, geo = resolve_clip_source(
        session, project_id, asset_id, version_id)
    clips = list_clips(session, tl.id)
    if not allow_duplicate and any(
            c.source_asset_id == asset.id
            and c.source_version_id == ver.id for c in clips):
        raise TimelineError(
            "this source is already on the timeline — "
            "pass allow_duplicate to repeat it intentionally")
    src_frames = geo["frames"]
    i = 0 if in_frame is None else _check_int(
        in_frame, "in_frame", 0, src_frames - 1)
    o = src_frames if out_frame is None else _check_int(
        out_frame, "out_frame", 1, src_frames)
    if i >= o:
        raise TimelineError("in_frame must be before out_frame")
    pos = _check_int(position, "position", 0, len(clips)) \
        if position is not None else len(clips)
    if shot_id is not None:
        shot = session.get(Shot, shot_id)
        if shot is None or shot.project_id != project_id:
            raise TimelineError("shot not found in this project")
    if scene_id is not None:
        scene = session.get(Scene, scene_id)
        if scene is None or scene.project_id != project_id:
            raise TimelineError("scene not found in this project")
    clip = TimelineClip(
        id=new_id(), project_id=project_id, timeline_id=tl.id,
        position=pos, source_asset_id=asset.id,
        source_version_id=ver.id, source_sha256=ver.sha256,
        in_frame=i, out_frame=o, src_frames=src_frames,
        src_fps_num=geo["fps_num"], src_fps_den=geo["fps_den"],
        src_duration_s=geo["duration_s"], shot_id=shot_id,
        scene_id=scene_id)
    session.add(clip)
    session.flush()
    _renumber(session, tl.id)
    _touch(session, tl, command_id=command_id, summary="Inserted clip")
    return clip


def get_clip(session: Session, tl: Timeline, clip_id: str) -> TimelineClip:
    clip = session.get(TimelineClip, clip_id)
    if clip is None or clip.timeline_id != tl.id:
        raise KeyError("clip not found on this timeline")
    return clip


def remove_clip(session: Session, tl: Timeline, clip: TimelineClip, *,
                command_id: str | None) -> None:
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    session.delete(clip)
    session.flush()
    _renumber(session, tl.id)
    _touch(session, tl, command_id=command_id, summary="Removed clip")


def move_clip(session: Session, tl: Timeline, clip: TimelineClip, *,
              to_index: int, command_id: str | None) -> None:
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    clips = list_clips(session, tl.id)
    _check_int(to_index, "to_index", 0, len(clips) - 1)
    clips.remove(clip)
    clips.insert(to_index, clip)
    for i, c in enumerate(clips):
        c.position = i
    session.flush()
    _touch(session, tl, command_id=command_id, summary="Moved clip")


def trim_clip(session: Session, tl: Timeline, clip: TimelineClip, *,
              in_frame: int | None, out_frame: int | None,
              command_id: str | None) -> None:
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    i, o = _check_trims(
        clip.in_frame if in_frame is None else in_frame,
        clip.out_frame if out_frame is None else out_frame,
        clip.src_frames)
    clip.in_frame, clip.out_frame = i, o
    session.flush()
    _touch(session, tl, command_id=command_id, summary="Trimmed clip")


# ---------- edit decision list ----------

def clip_duration_frac(clip: TimelineClip) -> Fraction:
    """Exact rational clip duration in seconds: trimmed source frames
    ÷ source fps as (num/den). All timeline math uses this — floats only
    ever appear at the JSON boundary."""
    return Fraction(clip.out_frame - clip.in_frame, 1) * Fraction(
        clip.src_fps_den, clip.src_fps_num)


def clip_duration_s(clip: TimelineClip) -> float:
    return float(clip_duration_frac(clip))


def total_duration_frac(clips: list[TimelineClip]) -> Fraction:
    return sum((clip_duration_frac(c) for c in clips), Fraction(0))


def edl_json(session: Session, tl: Timeline) -> dict[str, Any]:
    """Deterministic `ade-edl/1` serialization — same timeline state
    produces byte-identical JSON (no timestamps, no random ordering).

    Timebase rules: each clip's trims are integer frames in ITS OWN
    source rate (src_fps_num/src_fps_den, preserved exactly — 30000/1001
    stays 30000/1001). Clip seconds = (out-in)·den/num as an exact
    Fraction; the timeline total is the exact sum across mixed rates.
    Floats appear only at the boundary, rounded to 6 dp — the integer
    frames in the document are always the authoritative cut points.
    """
    clips_out = []
    total = Fraction(0)
    for c in list_clips(session, tl.id):
        dur = clip_duration_frac(c)
        total += dur
        ver = session.get(AssetVersion, c.source_version_id)
        if ver is not None and ver.sha256 != c.source_sha256:
            raise TimelineError(
                f"clip {c.id}: pinned sha does not match its version — "
                "timeline data is inconsistent")
        media = (ver.media_json or {}) if ver else {}
        probe = media.get("probe") or {}
        clips_out.append({
            "order": c.position, "clip_id": c.id,
            "asset_id": c.source_asset_id,
            "version_id": c.source_version_id,
            "sha256": c.source_sha256,
            "in_frame": c.in_frame, "out_frame": c.out_frame,
            "duration_s": round(float(dur), 6),
            "source": {
                "fps_num": c.src_fps_num, "fps_den": c.src_fps_den,
                "frames": c.src_frames, "duration_s": c.src_duration_s,
                "width": probe.get("width"), "height": probe.get("height"),
                "codec": probe.get("codec"),
            },
            "shot_id": c.shot_id, "scene_id": c.scene_id,
        })
    return {
        "format": EDL_FORMAT,
        "timeline_id": tl.id,
        "revision": tl.revision,
        "name": tl.name,
        "timebase": {"num": tl.fps_num, "den": tl.fps_den},
        "clips": clips_out,
        "total_duration_s": round(float(total), 6),
    }
