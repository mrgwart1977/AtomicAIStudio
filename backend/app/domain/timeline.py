"""C5-A/N1-A — timeline assembly domain.

A Timeline is a set of TimelineTracks holding TimelineClips. Every
clip pins one immutable asset version (id + sha256 + the source's
frame geometry at insert time). Trims are integer SOURCE frames:
``in_frame`` inclusive, ``out_frame`` exclusive, validated against the
pinned ``src_frames`` — never against whatever the asset's current
version might become later.

N1-A placement model: each clip sits on a ``timeline_tracks`` lane at
integer ``start_frames`` in TIMELINE frames (the timeline's
fps_num/fps_den) — authoritative for time. ``position`` is the dense
within-track order index, maintained as rank-by-(start_frames, id);
on a single contiguous track it is exactly the C5-A sequence index.

Timebase conversion is exact-rational: a clip's timeline extent is
``(out-in) * src_fps_den * tl_fps_num / (src_fps_num * tl_fps_den)``
as a Fraction — floats never appear in edit data. N1-A refuses
same-track overlap; gaps are representable (v1 export refuses them
honestly, v2 emits them explicitly). Ripple/split/overlap resolution
arrive in N1-C.

Revisions ride entity_revisions (entity_type ``timeline``); each
snapshot embeds tracks + the full clip list so revert restores the
edit exactly (new clip ids — clip identity is not stable across
revert, the edit decision is what persists)."""
from __future__ import annotations

import json
import math
from fractions import Fraction
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (Asset, AssetVersion, EntityRevision, Scene, Shot,
                           Timeline, TimelineClip, TimelineCrossfade,
                           TimelineTrack, new_id, utcnow)

#: Bounded rational timebases — keeps UI inputs honest without
#: pretending to support arbitrary rates.
# Timeline frame-rate bounds. The numerator is intentionally large
# (480000) so exact broadcast rates like 24000/1001 and 30000/1001 are
# representable — matching the ProjectCreate schema's bound — while the
# *rate* itself is capped at 480 fps, strictly extending the earlier
# numerator-only rule (num ≤ 240 → rate ≤ 240).
MAX_FPS_NUM = 480000
MAX_FPS_DEN = 1001
MAX_FPS_RATE = 480

EDL_FORMAT = "ade-edl/1"
EDL_FORMAT_V2 = "ade-edl/2"
TRACK_KINDS = ("video", "audio", "overlay")


class TimelineError(ValueError):
    """Validation failure — command layer maps to 422."""


def _check_fps(num: int, den: int) -> None:
    if (isinstance(num, bool) or not isinstance(num, int)
            or not 1 <= num <= MAX_FPS_NUM):
        raise TimelineError(f"fps numerator must be an integer 1..{MAX_FPS_NUM}")
    if (isinstance(den, bool) or not isinstance(den, int)
            or not 1 <= den <= MAX_FPS_DEN):
        raise TimelineError(f"fps denominator must be an integer 1..{MAX_FPS_DEN}")
    if Fraction(num, den) > MAX_FPS_RATE:
        raise TimelineError(
            f"frame rate must be at most {MAX_FPS_RATE} fps")


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


def list_tracks(session: Session, timeline_id: str) -> list[TimelineTrack]:
    return session.execute(
        select(TimelineTrack).where(
            TimelineTrack.timeline_id == timeline_id)
        .order_by(TimelineTrack.position, TimelineTrack.id)).scalars().all()


def get_track(session: Session, tl: Timeline, track_id: str) -> TimelineTrack:
    tr = session.get(TimelineTrack, track_id)
    if tr is None or tr.timeline_id != tl.id:
        raise KeyError("track not found on this timeline")
    return tr


def first_video_track(session: Session, timeline_id: str) -> TimelineTrack | None:
    """The lowest-positioned video lane — the default destination for
    inserts that don't name a track (and the migration's V1)."""
    return session.execute(
        select(TimelineTrack).where(
            TimelineTrack.timeline_id == timeline_id,
            TimelineTrack.kind == "video")
        .order_by(TimelineTrack.position, TimelineTrack.id)
        .limit(1)).scalar_one_or_none()


def list_clips(session: Session, timeline_id: str) -> list[TimelineClip]:
    """All clips in timeline-time order: track stack position, then
    start_frames, then id — deterministic even for same-start clips."""
    track_pos = {t.id: (t.position, t.id)
                 for t in list_tracks(session, timeline_id)}
    clips = session.execute(
        select(TimelineClip).where(
            TimelineClip.timeline_id == timeline_id)).scalars().all()
    return sorted(clips, key=lambda c: (
        track_pos.get(c.track_id, (1 << 30, "")),
        c.start_frames, c.position, c.id))


def track_clips(session: Session, track_id: str) -> list[TimelineClip]:
    return session.execute(
        select(TimelineClip).where(TimelineClip.track_id == track_id)
        .order_by(TimelineClip.start_frames, TimelineClip.position,
                  TimelineClip.id)).scalars().all()


def clip_tl_extent(clip: TimelineClip, tl: Timeline) -> Fraction:
    """Timeline frames a clip occupies — exact rational:
    duration_s = (out-in) ÷ src fps, timeline frames = duration × tl fps."""
    return Fraction(clip.out_frame - clip.in_frame) * Fraction(
        clip.src_fps_den, clip.src_fps_num) * Fraction(
            tl.fps_num, tl.fps_den)


def _renumber(session: Session, timeline_id: str) -> None:
    """Position = within-track rank by (start_frames, id). Dense 0..n-1
    per lane — identical to the C5-A sequence on a single contiguous
    track, and the only deterministic meaning across lanes."""
    for t in list_tracks(session, timeline_id):
        clips = track_clips(session, t.id)
        for i, c in enumerate(clips):
            if c.position != i:
                c.position = i
    # Trackless clips (defensive — every op assigns a lane): park at
    # the end of the index space so they never collide with real lanes.
    orphans = session.execute(
        select(TimelineClip).where(
            TimelineClip.timeline_id == timeline_id,
            TimelineClip.track_id.is_(None))
        .order_by(TimelineClip.id)).scalars().all()
    for i, c in enumerate(orphans):
        c.position = (1 << 20) + i
    session.flush()


def _check_track_unlocked(track: TimelineTrack | None) -> None:
    """Locked lanes refuse clip edits — the flag is an application-level
    edit guard, not a render decision."""
    if track is not None and track.locked:
        raise TimelineError(
            f"track '{track.name or track.id}' is locked")


def _check_track_overlap(session: Session, track: TimelineTrack,
                         tl: Timeline, *, start: int, extent: Fraction,
                         ignore_clip_id: str | None = None) -> None:
    """N1-A refuses same-track overlap — resolution semantics (split,
    ripple, replace) are N1-C. Gaps are fine; only intersection is
    rejected, measured with exact rational extents."""
    if extent <= 0:
        raise TimelineError("clip extent must be positive")
    end = Fraction(start) + extent
    for c in track_clips(session, track.id):
        if c.id == ignore_clip_id:
            continue
        c_end = Fraction(c.start_frames) + clip_tl_extent(c, tl)
        if Fraction(start) < c_end and Fraction(c.start_frames) < end:
            raise TimelineError(
                "placement overlaps an existing clip on this track — "
                "choose a free position (overlap resolution is not "
                "implemented yet)")


def _ceil_frame(x: Fraction) -> int:
    """Smallest integer timeline frame ≥ x — the integer-grid packing
    convention. Integer-frame timelines pack exactly as C5-A did;
    rational extents leave a sub-frame gap rather than overlap."""
    return -(-x.numerator // x.denominator)


def _packed_start(session: Session, track: TimelineTrack,
                  tl: Timeline) -> int:
    """Next free integer frame at/after the track's farthest end —
    where a plain 'append' insert goes. Ceil (not round): rounding
    down would place the new clip inside the previous clip's tail."""
    end = Fraction(0)
    for c in track_clips(session, track.id):
        e = Fraction(c.start_frames) + clip_tl_extent(c, tl)
        if e > end:
            end = e
    return _ceil_frame(end)


def _touch(session: Session, tl: Timeline, *, command_id: str | None,
           summary: str, history: bool = True) -> None:
    # N1-E — every user-facing mutation pushes the PRE-edit revision
    # onto the undo stack and clears redo (a divergent edit makes the
    # abandoned branch unreachable). Undo/redo themselves pass
    # history=False and manage the stacks explicitly.
    if history:
        tl.undo_stack_json = [
            *(tl.undo_stack_json or []),
            {"rev": tl.revision, "label": summary}]
        tl.redo_stack_json = []
    tl.revision += 1
    tl.updated_at = utcnow()
    session.flush()
    write_timeline_revision(session, tl, command_id=command_id, summary=summary)


# ---------- revisions ----------

def track_snapshot(t: TimelineTrack) -> dict[str, Any]:
    return {
        "id": t.id, "kind": t.kind, "position": t.position,
        "name": t.name, "visible": t.visible, "locked": t.locked,
        "muted": t.muted,
        "gain_db": t.gain_db, "solo": t.solo,
        "automation_json": getattr(t, "automation_json", "[]"),
        "automation_interp": getattr(t, "automation_interp", "linear"),
    }


def clip_snapshot(c: TimelineClip) -> dict[str, Any]:
    return {
        "id": c.id,
        "track_id": c.track_id,
        "position": c.position,
        "start_frames": c.start_frames,
        "source_asset_id": c.source_asset_id,
        "source_version_id": c.source_version_id,
        "source_sha256": c.source_sha256,
        "in_frame": c.in_frame, "out_frame": c.out_frame,
        "src_frames": c.src_frames,
        "src_fps_num": c.src_fps_num, "src_fps_den": c.src_fps_den,
        "src_duration_s": c.src_duration_s,
        "src_has_audio": getattr(c, "src_has_audio", True),
        "gain_db": c.gain_db,
        "fade_in_units": c.fade_in_units,
        "fade_in_curve": c.fade_in_curve,
        "automation_interp": getattr(c, "automation_interp", "linear"),
        "fade_out_units": c.fade_out_units,
        "fade_out_curve": c.fade_out_curve,
        "automation_json": c.automation_json,
        "shot_id": c.shot_id, "scene_id": c.scene_id,
    }


def timeline_snapshot(session: Session, tl: Timeline) -> dict[str, Any]:
    return {
        "name": tl.name, "description": tl.description,
        "fps_num": tl.fps_num, "fps_den": tl.fps_den,
        "status": tl.status,
        "tracks": [track_snapshot(t) for t in list_tracks(session, tl.id)],
        "clips": [clip_snapshot(c) for c in list_clips(session, tl.id)],
        "crossfades": [{"id": x.id, "out_clip_id": x.out_clip_id,
                        "in_clip_id": x.in_clip_id,
                        "tl_start": x.tl_start, "tl_frames": x.tl_frames,
                        "curve": x.curve}
                       for x in list_crossfades(session, tl.id)],
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
    # N1-A — every timeline carries its default video lane from birth,
    # so clips always have somewhere legal to land (matches the
    # migration's V1 backfill).
    session.add(TimelineTrack(
        id=new_id(), project_id=project_id, timeline_id=tl.id,
        kind="video", position=0, name="V1"))
    session.flush()
    write_timeline_revision(session, tl, command_id=command_id,
                            summary="Created timeline")
    return tl


# ---------- track ops ----------

def add_track(session: Session, tl: Timeline, *, kind: str = "video",
              name: str = "", command_id: str | None) -> TimelineTrack:
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    if kind not in TRACK_KINDS:
        raise TimelineError(
            f"track kind must be one of {', '.join(TRACK_KINDS)}")
    pos = max((t.position for t in list_tracks(session, tl.id)),
              default=-1) + 1
    tr = TimelineTrack(
        id=new_id(), project_id=tl.project_id, timeline_id=tl.id,
        kind=kind, position=pos, name=(name or "").strip())
    session.add(tr)
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary=f"Added {kind} track")
    return tr


# ---------- N2-C gain contract ----------

# Decibels are the single stored representation — linear amplitude is
# derived (10**(db/20)) only at mix time, so nothing can disagree.
GAIN_DB_MIN = -60.0   # practical silence floor (~0.1% amplitude)
GAIN_DB_MAX = 12.0    # headroom cap — beginner-safe


def check_gain_db(v: Any, field: str = "gain_db") -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise TimelineError(f"{field} must be a number of decibels")
    v = float(v)
    if not math.isfinite(v):
        raise TimelineError(f"{field} must be finite")
    if v < GAIN_DB_MIN or v > GAIN_DB_MAX:
        raise TimelineError(
            f"{field} must be within "
            f"[{GAIN_DB_MIN:g}, {GAIN_DB_MAX:g}] dB")
    return v


def db_to_linear(db: float) -> float:
    """Decibels → amplitude ratio. 0 dB = unity (1.0), negative
    attenuates, positive amplifies. -inf is never stored — true
    silence is represented by ``muted``, not a gain value."""
    return 10.0 ** (db / 20.0)


def effective_gain_db(clip_db: float, track_db: float) -> float:
    """Clip gain combines multiplicatively with track gain — dB adds."""
    return (clip_db or 0.0) + (track_db or 0.0)


def lane_audible(track: TimelineTrack, any_solo: bool) -> bool:
    """Audibility precedence, applied in order:
    hidden lanes are render-inert → mute silences → a nonempty solo
    set silences every non-soloed lane. Multiple solos are all
    audible; an empty solo set leaves mute as the only silencer."""
    if not track.visible:
        return False
    if track.muted:
        return False
    if any_solo and not track.solo:
        return False
    return True


def any_solo(session: Session, timeline_id: str) -> bool:
    return any(t.solo for t in list_tracks(session, timeline_id))


def set_clip_gain(session: Session, tl: Timeline, clip: TimelineClip, *,
                  gain_db: float, command_id: str | None) -> None:
    """Per-clip gain, audio-source clips only — video clips carry
    embedded audio whose level is a track/mix decision (N2-E)."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    tr = session.get(TimelineTrack, clip.track_id) if clip.track_id else None
    if tr is None or tr.kind != "audio":
        raise TimelineError(
            "clip gain applies to clips on audio lanes")
    clip.gain_db = check_gain_db(gain_db)
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary="Set clip gain")


# ---------- N2-D: fades / automation / crossfades ----------

#: Fade curve vocabulary. 'linear' ramps amplitude 0→1 linearly over
#: the fade (g(t)=t/N). 'equal_power' uses the constant-power pair —
#: in: g(t)=sin(πt/2N), out: g(t)=cos(πt/2N) — so a paired crossfade
#: sums to unity power (sin²+cos²=1) instead of dipping in the middle.
#: N2-F adds: 's_curve' — the sin²/cos² S-curve (gentle at both ends);
#: 'exponential' — a fixed exponential ramp (perceived-even loudness
#: for fades, maps to afade exp).
FADE_CURVES = ("linear", "equal_power", "s_curve", "exponential")

#: Automation bounds — bounded initial model: at most this many
#: gain keyframes per clip.
AUTOMATION_MAX_POINTS = 64

#: N2-F — automation interpolation modes. 'linear' interpolates in
#: dB between neighbors; 'step' holds each keyframe's value until the
#: next (piecewise-constant, deterministic).
AUTOMATION_INTERPS = ("linear", "step")


def check_fade_curve(v: Any, field: str = "curve") -> str:
    if not isinstance(v, str) or v not in FADE_CURVES:
        raise TimelineError(
            f"{field} must be one of {', '.join(FADE_CURVES)}")
    return v


def _check_fade_units(v: Any, field: str) -> int:
    n = _check_int(v, field, 0, 1 << 40)
    return n


def _require_audio_lane_clip(session: Session, clip: TimelineClip,
                             what: str) -> TimelineTrack:
    tr = session.get(TimelineTrack, clip.track_id) if clip.track_id else None
    if tr is None or tr.kind != "audio":
        raise TimelineError(f"{what} applies to clips on audio lanes")
    return tr


def set_clip_fades(session: Session, tl: Timeline, clip: TimelineClip, *,
                   fields: dict, command_id: str | None) -> None:
    """Per-clip fades in SOURCE units (samples for audio — the N2-A
    unit contract), anchored to the trim edges: fade_in covers
    [in_frame, in_frame+units) and fade_out the last `units` before
    out_frame. Both must fit the clip's extent — a fade can never
    reference source material outside the trim."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    _require_audio_lane_clip(session, clip, "fades")
    fin = (clip.fade_in_units if "fade_in_units" not in fields
           else _check_fade_units(fields["fade_in_units"],
                                  "fade_in_units"))
    fout = (clip.fade_out_units if "fade_out_units" not in fields
            else _check_fade_units(fields["fade_out_units"],
                                   "fade_out_units"))
    fic = (clip.fade_in_curve if "fade_in_curve" not in fields
           else check_fade_curve(fields["fade_in_curve"],
                                 "fade_in_curve"))
    foc = (clip.fade_out_curve if "fade_out_curve" not in fields
           else check_fade_curve(fields["fade_out_curve"],
                                 "fade_out_curve"))
    extent = clip.out_frame - clip.in_frame
    if fin + fout > extent:
        raise TimelineError(
            f"fade_in+fade_out ({fin + fout}) exceed the clip extent "
            f"({extent} source units) — fades cannot reach past the "
            f"trim")
    clip.fade_in_units, clip.fade_in_curve = fin, fic
    clip.fade_out_units, clip.fade_out_curve = fout, foc
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary="Set clip fades")


def fade_gain(curve: str, pos: int, n: int, *, out: bool = False
              ) -> float:
    """Envelope value at source offset `pos` within an `n`-unit fade.
    Deterministic float math — 'linear': in t/n, out 1−t/n;
    'equal_power': in sin(πt/2n), out cos(πt/2n)."""
    check_fade_curve(curve)
    if n <= 0:
        return 1.0
    t = min(max(pos / n, 0.0), 1.0)
    if curve == "linear":
        g = 1.0 - t if out else t
    elif curve == "equal_power":
        g = math.cos(t * math.pi / 2) if out else math.sin(
            t * math.pi / 2)
    elif curve == "s_curve":
        g = math.cos(t * math.pi / 2) ** 2 if out else math.sin(
            t * math.pi / 2) ** 2
    else:  # exponential — afade exp: in e^{-11.5129(1−t)} (≈10^{-5(1−t)}),
        # out e^{-11.5129·t} — a fixed −50 dB/decade-style ramp whose
        # tail reaches unity exactly at the edge. FFmpeg contract.
        g = math.exp(-11.512925464970227 * (t if out else 1 - t))
    return g


def automation_points(clip: TimelineClip) -> list[dict[str, Any]]:
    """Stored keyframes [{id, at_frames, gain_db}] — `at_frames` is a
    TIMELINE-frame offset from clip start (clip-local time position,
    so moves never drift it)."""
    try:
        pts = json.loads(clip.automation_json or "[]")
    except (TypeError, ValueError):
        return []
    return pts if isinstance(pts, list) else []


def set_clip_automation(session: Session, tl: Timeline,
                        clip: TimelineClip, *, points: list,
                        interp: str | None = None,
                        command_id: str | None) -> None:
    """Replace the clip's gain keyframes. Bounded model: ≤64 points,
    integer `at_frames` within the clip's integer occupancy [0,
    ceil(extent)), unique positions, gain bounds as clip gain. Points
    are stored sorted by position — deterministic serialization.
    Interpolation is `clip.automation_interp`: 'linear' in dB between
    neighbors, or 'step' (hold each value until the next point); flat
    outside the first/last point either way."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    _require_audio_lane_clip(session, clip, "gain automation")
    if interp is not None:
        if interp not in AUTOMATION_INTERPS:
            raise TimelineError(
                f"interp must be one of {', '.join(AUTOMATION_INTERPS)}")
        clip.automation_interp = interp
    if not isinstance(points, list):
        raise TimelineError("points must be a list")
    if len(points) > AUTOMATION_MAX_POINTS:
        raise TimelineError(
            f"at most {AUTOMATION_MAX_POINTS} automation points")
    occ = _ceil_frame(clip_tl_extent(clip, tl))
    cleaned: list[dict[str, Any]] = []
    seen: set[int] = set()
    for i, p in enumerate(points):
        if not isinstance(p, dict):
            raise TimelineError("automation points must be objects")
        at = _check_int(p.get("at_frames"), "at_frames", 0, occ - 1)
        if at in seen:
            raise TimelineError(
                f"duplicate automation point at frame {at}")
        seen.add(at)
        cleaned.append({
            "id": (p.get("id") if isinstance(p.get("id"), str)
                   and p["id"] else f"p{i}"),
            "at_frames": at,
            "gain_db": check_gain_db(p.get("gain_db"))})
    cleaned.sort(key=lambda p: p["at_frames"])
    clip.automation_json = json.dumps(cleaned, separators=(",", ":"))
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary="Set clip automation")


def automation_gain_db(points: list[dict], at_frames: float, *,
                       interp: str = "linear") -> float:
    """Interpolated gain in dB at a clip-local position — linear in
    dB between neighbors for 'linear', held constant for 'step';
    flat before the first/after the last point. Deterministic."""
    if not points:
        return 0.0
    if at_frames <= points[0]["at_frames"]:
        return float(points[0]["gain_db"])
    if at_frames >= points[-1]["at_frames"]:
        return float(points[-1]["gain_db"])
    for a, b in zip(points, points[1:]):
        if a["at_frames"] <= at_frames <= b["at_frames"]:
            if interp == "step":
                return float(a["gain_db"])
            span = b["at_frames"] - a["at_frames"]
            t = 0.0 if span == 0 else (at_frames - a["at_frames"]) / span
            return float(a["gain_db"]) + t * (float(b["gain_db"])
                                              - float(a["gain_db"]))
    return float(points[-1]["gain_db"])


# ---------- N3-A: per-track gain automation ----------

#: Bounded model for lane-level keyframes. Track points are keyed in
#: ABSOLUTE timeline frames and span the whole program — the bound is
#: larger than the clip bound (whose points cover only one clip's
#: occupancy) but still finite and deterministic.
TRACK_AUTOMATION_MAX_POINTS = 256

#: Track keyframes may be positioned anywhere on the timeline — same
#: ceiling other timeline-frame fields use. Points past the program
#: end are valid and inert (the flat tail holds the last value).
TRACK_AUTOMATION_MAX_FRAME = 1 << 30


def track_automation_points(track: TimelineTrack) -> list[dict[str, Any]]:
    """Stored lane keyframes [{id, at_tl_frames, gain_db}] — keyed in
    ABSOLUTE timeline frames (unlike clip automation's clip-local
    offsets; lane envelopes are a function of timeline position, not
    of whichever clips happen to cover it)."""
    try:
        pts = json.loads(getattr(track, "automation_json", "[]") or "[]")
    except (TypeError, ValueError):
        return []
    return pts if isinstance(pts, list) else []


def set_track_automation(session: Session, tl: Timeline,
                         track: TimelineTrack, *, points: list,
                         interp: str | None = None,
                         command_id: str | None) -> None:
    """Replace the lane's gain keyframes — same stored representation
    as clip automation ([{id, position, gain_db}] sorted, unique
    positions, gain bounds as clip/track gain, missing ids assigned
    p{i}) but `at_tl_frames` is an absolute timeline frame: clip moves,
    trims, and splits can never orphan it. One commit, one undo step."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    if track.kind == "overlay":
        raise TimelineError(
            "track automation is meaningless on overlay lanes "
            "(no audio output)")
    if interp is not None:
        if interp not in AUTOMATION_INTERPS:
            raise TimelineError(
                f"interp must be one of {', '.join(AUTOMATION_INTERPS)}")
        track.automation_interp = interp
    if not isinstance(points, list):
        raise TimelineError("points must be a list")
    if len(points) > TRACK_AUTOMATION_MAX_POINTS:
        raise TimelineError(
            f"at most {TRACK_AUTOMATION_MAX_POINTS} automation points")
    cleaned: list[dict[str, Any]] = []
    seen: set[int] = set()
    for i, p in enumerate(points):
        if not isinstance(p, dict):
            raise TimelineError("automation points must be objects")
        at = _check_int(p.get("at_tl_frames"), "at_tl_frames",
                        0, TRACK_AUTOMATION_MAX_FRAME - 1)
        if at in seen:
            raise TimelineError(
                f"duplicate automation point at timeline frame {at}")
        seen.add(at)
        cleaned.append({
            "id": (p.get("id") if isinstance(p.get("id"), str)
                   and p["id"] else f"p{i}"),
            "at_tl_frames": at,
            "gain_db": check_gain_db(p.get("gain_db"))})
    cleaned.sort(key=lambda p: p["at_tl_frames"])
    track.automation_json = json.dumps(cleaned, separators=(",", ":"))
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary="Set track automation")


def track_automation_gain_db(points: list[dict], tl_frames: float, *,
                             interp: str = "linear") -> float:
    """Lane gain in dB at an ABSOLUTE timeline frame — the same
    piecewise evaluator as clip automation (`linear` dB ramp | `step`
    hold, flat outside the first/last point), keyed by `at_tl_frames`
    instead of the clip-local `at_frames`. One interpolation
    implementation, two position domains."""
    return automation_gain_db(
        [{"at_frames": p["at_tl_frames"], "gain_db": p["gain_db"]}
         for p in points], tl_frames, interp=interp)


def list_crossfades(session: Session, timeline_id: str
                    ) -> list[TimelineCrossfade]:
    return session.execute(
        select(TimelineCrossfade)
        .where(TimelineCrossfade.timeline_id == timeline_id)
        .order_by(TimelineCrossfade.tl_start,
                  TimelineCrossfade.id)).scalars().all()


def _xf_handle_needs(session: Session, tl: Timeline,
                     out_clip: TimelineClip, in_clip: TimelineClip,
                     win_start: int, win_frames: int
                     ) -> tuple[int, int] | None:
    """Source-unit handle requirements for a window, or None if the
    window can't apply to this pair at all. The window [win_start,
    win_start+win_frames) must contain the lane's seam region
    [out_end, in_start]: win_start ≤ in_clip's start (so the in-clip
    always enters from silence at the window start — never dips out
    of native playback) and win_end ≥ out_clip's end (so the
    out-clip's fade completes before its occupancy would continue).
    The out-clip covers its far side via tail handle (out_frame→),
    the in-clip covers its near side via head handle (←in_frame).
    Returns (tail_src_needed, head_src_needed)."""
    win_end = win_start + win_frames
    out_end = Fraction(out_clip.start_frames) + clip_tl_extent(
        out_clip, tl)
    in_end = Fraction(in_clip.start_frames) + clip_tl_extent(
        in_clip, tl)
    if Fraction(in_clip.start_frames) <= Fraction(
            out_clip.start_frames):
        return None  # pair must be ordered out → in on the lane
    # The window must contain the seam and stay within the pair's
    # combined reach — an interval where only one clip could sound
    # is not a crossfade.
    if Fraction(win_start) < Fraction(out_clip.start_frames) or \
            Fraction(win_start) > Fraction(in_clip.start_frames) or \
            Fraction(win_end) < out_end or \
            Fraction(win_end) > in_end:
        return None
    tl_rate = Fraction(tl.fps_num, tl.fps_den)

    def tl_to_src(frames: Fraction, c: TimelineClip) -> int:
        u = frames * Fraction(c.src_fps_num, c.src_fps_den) / tl_rate
        return _ceil_frame(u)  # conservative: whole source units

    tail = max(Fraction(0), Fraction(win_end) - out_end)
    head = max(Fraction(0),
               Fraction(in_clip.start_frames) - Fraction(win_start))
    return tl_to_src(tail, out_clip), tl_to_src(head, in_clip)


def crossfade_satisfied(session: Session, tl: Timeline,
                        xf: TimelineCrossfade) -> bool:
    """Read-time feasibility — a record whose clips moved/trimmed past
    the window reports False instead of silently rendering wrong."""
    out_c = session.get(TimelineClip, xf.out_clip_id)
    in_c = session.get(TimelineClip, xf.in_clip_id)
    if out_c is None or in_c is None or \
            out_c.track_id != in_c.track_id:
        return False
    tr = session.get(TimelineTrack, out_c.track_id or "")
    if tr is None or tr.kind != "audio":
        return False
    needs = _xf_handle_needs(session, tl, out_c, in_c,
                             xf.tl_start, xf.tl_frames)
    if needs is None:
        return False
    tail, head = needs
    return (out_c.out_frame + tail <= out_c.src_frames
            and in_c.in_frame - head >= 0)


def set_crossfade(session: Session, tl: Timeline, *,
                  out_clip_id: str, in_clip_id: str, tl_start: Any,
                  tl_frames: Any, curve: Any,
                  command_id: str | None) -> TimelineCrossfade:
    """Create/replace the pair's crossfade. Lanes still refuse grid
    overlap — the window borrows source handles at render time:
    out-clip extends `win_end − out_end` past its occupancy into its
    tail handle, in-clip reaches `in_start − win_start` into its head
    handle. Both must exist or the command refuses (fail closed)."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    out_c = session.get(TimelineClip, out_clip_id)
    in_c = session.get(TimelineClip, in_clip_id)
    for c, label in ((out_c, "out_clip"), (in_c, "in_clip")):
        if c is None or c.timeline_id != tl.id:
            raise TimelineError(f"{label} not found on this timeline")
    if out_c.id == in_c.id:
        raise TimelineError("a clip cannot crossfade with itself")
    if out_c.track_id is None or out_c.track_id != in_c.track_id:
        raise TimelineError(
            "crossfades join two clips on the same audio lane")
    tr = session.get(TimelineTrack, out_c.track_id)
    if tr is None or tr.kind != "audio":
        raise TimelineError(
            "crossfades join two clips on the same audio lane")
    _check_track_unlocked(tr)
    start = _check_int(tl_start, "tl_start", 0, 1 << 30)
    frames = _check_int(tl_frames, "tl_frames", 1, 1 << 30)
    curve = check_fade_curve(curve if curve is not None else
                             "equal_power")
    needs = _xf_handle_needs(session, tl, out_c, in_c, start, frames)
    if needs is None:
        raise TimelineError(
            "crossfade window must overlap both clips' spans with "
            "the out-clip placed before the in-clip")
    tail, head = needs
    if out_c.out_frame + tail > out_c.src_frames:
        raise TimelineError(
            "crossfade needs more tail material than the out-clip "
            "has — extend its trim first")
    if in_c.in_frame - head < 0:
        raise TimelineError(
            "crossfade needs more head material than the in-clip "
            "has — extend its trim first")
    xf = session.execute(select(TimelineCrossfade).where(
        TimelineCrossfade.out_clip_id == out_c.id,
        TimelineCrossfade.in_clip_id == in_c.id)).scalar_one_or_none()
    if xf is None:
        xf = TimelineCrossfade(
            id=new_id(), project_id=tl.project_id, timeline_id=tl.id,
            out_clip_id=out_c.id, in_clip_id=in_c.id)
        session.add(xf)
    xf.tl_start, xf.tl_frames, xf.curve = start, frames, curve
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary="Set crossfade")
    return xf


def clear_crossfade(session: Session, tl: Timeline, *,
                    crossfade_id: str, command_id: str | None) -> None:
    xf = session.get(TimelineCrossfade, crossfade_id)
    if xf is None or xf.timeline_id != tl.id:
        raise TimelineError("crossfade not found on this timeline")
    session.delete(xf)
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary="Cleared crossfade")


def update_track(session: Session, tl: Timeline, track: TimelineTrack, *,
                 fields: dict, command_id: str | None) -> None:
    """name/visible/locked/muted/solo/gain_db. Flags are deliberately
    settable on a locked track — locking is the clip-edit guard, not a
    mix-control lock; unlock must remain reachable."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    if "name" in fields:
        track.name = (fields["name"] or "").strip()
    for f in ("visible", "locked", "muted", "solo"):
        if f in fields:
            v = fields[f]
            if not isinstance(v, bool):
                raise TimelineError(f"{f} must be a boolean")
            if f == "solo" and v and track.kind == "overlay":
                raise TimelineError(
                    "solo is meaningless on overlay lanes "
                    "(no audio output)")
            setattr(track, f, v)
    if "gain_db" in fields:
        if track.kind == "overlay":
            raise TimelineError(
                "gain_db is meaningless on overlay lanes "
                "(no audio output)")
        track.gain_db = check_gain_db(fields["gain_db"])
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary="Updated track")


def reorder_track(session: Session, tl: Timeline, track: TimelineTrack, *,
                  to_position: int, command_id: str | None) -> None:
    """Stack order — 0 is the bottom lane, higher composites on top."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    tracks = list_tracks(session, tl.id)
    to_position = _check_int(to_position, "to_position", 0,
                             len(tracks) - 1)
    tracks.remove(track)
    tracks.insert(to_position, track)
    for i, t in enumerate(tracks):
        t.position = i
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary="Reordered track")


def remove_track(session: Session, tl: Timeline, track: TimelineTrack, *,
                 command_id: str | None) -> None:
    """Refuses non-empty lanes — clip loss is never a side effect."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    if track_clips(session, track.id):
        raise TimelineError(
            "track still holds clips — remove them first "
            "(visibility hides a lane; it never deletes its clips)")
    session.delete(track)
    session.flush()
    for i, t in enumerate(list_tracks(session, tl.id)):
        if t.position != i:
            t.position = i
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary="Removed track")


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
    track_map: dict[str, str] = {}
    for t in list_tracks(session, tl.id):
        nt = TimelineTrack(
            id=new_id(), project_id=tl.project_id, timeline_id=dup.id,
            kind=t.kind, position=t.position, name=t.name,
            visible=t.visible, locked=t.locked, muted=t.muted)
        session.add(nt)
        track_map[t.id] = nt.id
    session.flush()
    clip_map: dict[str, str] = {}
    for c in list_clips(session, tl.id):
        nid = new_id()
        clip_map[c.id] = nid
        session.add(TimelineClip(
            id=nid, project_id=tl.project_id, timeline_id=dup.id,
            track_id=track_map.get(c.track_id or ""),
            position=c.position, start_frames=c.start_frames,
            source_asset_id=c.source_asset_id,
            source_version_id=c.source_version_id,
            source_sha256=c.source_sha256,
            in_frame=c.in_frame, out_frame=c.out_frame,
            src_frames=c.src_frames, src_fps_num=c.src_fps_num,
            src_fps_den=c.src_fps_den, src_duration_s=c.src_duration_s,
            src_has_audio=c.src_has_audio,
            gain_db=c.gain_db,
            fade_in_units=c.fade_in_units,
            fade_in_curve=c.fade_in_curve,
            fade_out_units=c.fade_out_units,
            fade_out_curve=c.fade_out_curve,
            automation_json=c.automation_json,
            automation_interp=c.automation_interp,
            shot_id=c.shot_id, scene_id=c.scene_id))
    session.flush()
    for x in list_crossfades(session, tl.id):
        session.add(TimelineCrossfade(
            id=new_id(), project_id=tl.project_id, timeline_id=dup.id,
            out_clip_id=clip_map[x.out_clip_id],
            in_clip_id=clip_map[x.in_clip_id],
            tl_start=x.tl_start, tl_frames=x.tl_frames,
            curve=x.curve))
    session.flush()
    write_timeline_revision(session, dup, command_id=command_id,
                            summary="Duplicated timeline")
    return dup


def _src_has_audio(session: Session, version_id: str | None) -> bool:
    """Re-derive the flag for snapshot rows that predate the column:
    audio probes pin `sample_rate`, video probes pin `audio_streams`
    — either present means the source decodes audio."""
    ver = session.get(AssetVersion, version_id) if version_id else None
    if ver is None:
        return False
    probe = (ver.media_json or {}).get("probe") or {}
    return bool(probe.get("audio_streams") or probe.get("sample_rate"))


def _restore_snapshot(session: Session, tl: Timeline,
                      snap: dict[str, Any]) -> None:
    """Apply a snapshot's edit state (tracks + clips + meta). The
    caller owns revision bookkeeping — this only rewrites rows."""
    for f in ("name", "description", "fps_num", "fps_den", "status"):
        if f in snap:
            setattr(tl, f, snap[f])
    snap_tracks = snap.get("tracks") or []
    snap_track_ids = {st["id"] for st in snap_tracks}
    have = {t.id: t for t in list_tracks(session, tl.id)}
    for st in snap_tracks:
        if st["id"] not in have:
            session.add(TimelineTrack(
                id=st["id"], project_id=tl.project_id,
                timeline_id=tl.id, kind=st.get("kind", "video"),
                position=st.get("position", 0), name=st.get("name", ""),
                visible=st.get("visible", True),
                locked=st.get("locked", False),
                muted=st.get("muted", False),
                gain_db=st.get("gain_db", 0.0),
                solo=st.get("solo", False),
                automation_json=st.get("automation_json", "[]"),
                automation_interp=st.get("automation_interp",
                                         "linear")))
        else:
            # N1-E — restore mutable lane fields too, or undoing a
            # reorder/rename/visibility/lock/mute edit silently keeps
            # the post-edit lane state. Fields absent from an older
            # snapshot restore to their column defaults (pre-0019
            # snapshots carry no automation fields — keep-if-absent
            # would leak post-edit automation across an undo).
            tr = have[st["id"]]
            for f, d in (("position", 0), ("name", ""),
                         ("visible", True), ("locked", False),
                         ("muted", False), ("gain_db", 0.0),
                         ("solo", False), ("automation_json", "[]"),
                         ("automation_interp", "linear")):
                setattr(tr, f, st.get(f, d))
    session.flush()
    # N1-E — restore clips by STABLE IDENTITY, not delete-all/recreate:
    # rows whose id is in the snapshot are updated in place, rows
    # created after the snapshot are deleted, and deleted clips come
    # back with their ORIGINAL ids (nothing external references clip
    # ids, but stable ids keep EDL docs and audit trails continuous).
    # Pre-N1-E snapshots carry no clip ids — every row is recreated.
    live_clips = {c.id: c for c in list_clips(session, tl.id)}
    snap_clips = snap.get("clips") or []
    want_ids = {c["id"] for c in snap_clips if c.get("id")}
    for cid, row in live_clips.items():
        if cid not in want_ids:
            session.delete(row)
    session.flush()
    if "tracks" in snap:
        # N1-A snapshot: restore the lane set exactly. Tracks absent
        # from the snapshot are empty by construction (their clips
        # were just deleted) — dropping them can't orphan anything.
        for t in list_tracks(session, tl.id):
            if t.id not in snap_track_ids:
                session.delete(t)
        session.flush()
    # Pre-N1 snapshots carry no lane set: keep the existing lanes and
    # repack clips onto the default video lane (migration's rule).
    # If the timeline somehow has no video lane at all, clips would
    # orphan — recreate V1 first.
    default_lane = first_video_track(session, tl.id)
    if default_lane is None and snap.get("clips"):
        default_lane = TimelineTrack(
            id=new_id(), project_id=tl.project_id, timeline_id=tl.id,
            kind="video", position=0, name="V1")
        session.add(default_lane)
        session.flush()
    acc = Fraction(0)  # legacy-snapshot repack (exact rational)
    for i, c in enumerate(snap_clips):
        legacy = "start_frames" not in c
        start = (c.get("start_frames") if not legacy
                 else _ceil_frame(acc))
        track_id = (default_lane.id
                    if (legacy or not c.get("track_id"))
                    and default_lane is not None else c.get("track_id"))
        fields = dict(
            track_id=track_id, position=i, start_frames=start,
            source_asset_id=c["source_asset_id"],
            source_version_id=c["source_version_id"],
            source_sha256=c["source_sha256"],
            in_frame=c["in_frame"], out_frame=c["out_frame"],
            src_frames=c["src_frames"], src_fps_num=c["src_fps_num"],
            src_fps_den=c["src_fps_den"],
            src_duration_s=c.get("src_duration_s", 0.0),
            src_has_audio=(c["src_has_audio"] if "src_has_audio" in c
                           else _src_has_audio(session,
                                               c["source_version_id"])),
            gain_db=c.get("gain_db", 0.0),
            fade_in_units=c.get("fade_in_units", 0),
            fade_in_curve=c.get("fade_in_curve", "linear"),
            fade_out_units=c.get("fade_out_units", 0),
            fade_out_curve=c.get("fade_out_curve", "linear"),
            automation_json=c.get("automation_json", "[]"),
            automation_interp=c.get("automation_interp", "linear"),
            shot_id=c.get("shot_id"), scene_id=c.get("scene_id"))
        row = live_clips.get(c.get("id")) if c.get("id") else None
        if row is not None:
            for k, v in fields.items():
                setattr(row, k, v)
            clip = row
        else:
            clip = TimelineClip(
                id=c.get("id") or new_id(), project_id=tl.project_id,
                timeline_id=tl.id, **fields)
            session.add(clip)
        if legacy:
            acc = Fraction(start) + clip_tl_extent(clip, tl)
    session.flush()
    _renumber(session, tl.id)
    # N2-D — restore the crossfade set to the snapshot exactly:
    # records absent from the snapshot are dropped, missing ones are
    # recreated with their original ids (pre-N2-D snapshots carry no
    # "crossfades" key → the set clears, matching "no crossfades
    # existed" semantics of older revisions).
    snap_xfs = snap.get("crossfades") or []
    want_xf = {x["id"]: x for x in snap_xfs}
    for x in list_crossfades(session, tl.id):
        if x.id not in want_xf:
            session.delete(x)
        else:
            sx = want_xf[x.id]
            for f in ("out_clip_id", "in_clip_id", "tl_start",
                      "tl_frames", "curve"):
                if f in sx:
                    setattr(x, f, sx[f])
    for sx in snap_xfs:
        if session.get(TimelineCrossfade, sx["id"]) is None:
            session.add(TimelineCrossfade(
                id=sx["id"], project_id=tl.project_id,
                timeline_id=tl.id, out_clip_id=sx["out_clip_id"],
                in_clip_id=sx["in_clip_id"], tl_start=sx["tl_start"],
                tl_frames=sx["tl_frames"], curve=sx["curve"]))
    session.flush()


def revert_timeline(session: Session, tl: Timeline, to_revision: int, *,
                    command_id: str | None) -> None:
    """Restore a snapshot's edit state. N1-A: the snapshot's lane set
    is restored exactly — tracks listed by id are recreated when
    missing, and tracks added after the snapshot (empty once their
    clips are deleted) are removed. Pre-N1 snapshots (no track
    fields) restore onto the first video lane with ceil-packed starts
    — matching the migration's conversion rule."""
    row = session.execute(
        select(EntityRevision).where(
            EntityRevision.entity_type == "timeline",
            EntityRevision.entity_id == tl.id,
            EntityRevision.revision == to_revision)).scalar_one_or_none()
    if row is None:
        raise KeyError(f"revision {to_revision} not found")
    _restore_snapshot(session, tl, row.snapshot_json or {})
    _touch(session, tl, command_id=command_id,
           summary=f"Reverted to revision {to_revision}")


# ---------- N1-E — undo / redo ----------

def undo_tip(tl: Timeline) -> dict | None:
    return (tl.undo_stack_json or [])[-1] if tl.undo_stack_json else None


def redo_tip(tl: Timeline) -> dict | None:
    return (tl.redo_stack_json or [])[-1] if tl.redo_stack_json else None


def _step(session: Session, tl: Timeline, src: str, dst: str,
          verb: str, command_id: str | None) -> dict:
    stack = list(getattr(tl, src) or [])
    if not stack:
        raise TimelineError(f"nothing to {verb}")
    entry = stack.pop()
    row = session.execute(
        select(EntityRevision).where(
            EntityRevision.entity_type == "timeline",
            EntityRevision.entity_id == tl.id,
            EntityRevision.revision == entry["rev"])).scalar_one_or_none()
    if row is None:
        raise TimelineError(
            f"revision {entry['rev']} for {verb} is missing")
    setattr(tl, src, stack)
    setattr(tl, dst, [*(getattr(tl, dst) or []),
                      {"rev": tl.revision, "label": entry["label"]}])
    _restore_snapshot(session, tl, row.snapshot_json or {})
    _touch(session, tl, command_id=command_id, history=False,
           summary=f"{verb.capitalize()} {entry['label']}")
    return entry


def undo_timeline(session: Session, tl: Timeline, *,
                  command_id: str | None) -> dict:
    """Restore the previous edit state; the abandoned state moves to
    the redo stack. Atomic within the caller's transaction."""
    return _step(session, tl, "undo_stack_json", "redo_stack_json",
                 "undo", command_id)


def redo_timeline(session: Session, tl: Timeline, *,
                  command_id: str | None) -> dict:
    """Re-apply the most recently undone edit."""
    return _step(session, tl, "redo_stack_json", "undo_stack_json",
                 "redo", command_id)


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
            "height": probe.get("height"), "codec": probe.get("codec"),
            "has_audio": bool(probe.get("audio_streams"))}


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


def source_geometry_audio(version: AssetVersion) -> dict[str, Any]:
    """N2-A — audio geometry in SAMPLES: the clip's src_fps_* pins the
    source sample rate (rate/1), src_frames pins total samples, and
    in/out trims are sample indices — sample-exact with no new units.
    Requires a measured rate+duration+sample count, same fail-closed
    stance as video."""
    probe = (version.media_json or {}).get("probe") or {}
    rate = probe.get("sample_rate")
    dur = probe.get("duration_s")
    samples = probe.get("samples")
    if not probe.get("ok") or not isinstance(rate, int) or rate <= 0 \
            or not isinstance(dur, (int, float)) or dur <= 0 \
            or not isinstance(samples, int) or samples <= 0:
        raise TimelineError(
            "source audio has no measured rate/duration/samples — "
            "it cannot be trimmed safely")
    return {"frames": samples, "fps_num": rate, "fps_den": 1,
            "duration_s": float(dur), "width": None, "height": None,
            "codec": probe.get("codec"), "has_audio": True,
            "sample_rate": rate, "channels": probe.get("channels"),
            "channel_layout": probe.get("channel_layout")}


# Lane ↔ asset kind rule (N2-A): audio lanes accept only audio-kind
# assets; video and overlay lanes accept video-kind assets. A video
# file's embedded audio is edited as a video asset — detaching it
# onto an audio lane is out of scope.
def check_track_asset_kind(track: TimelineTrack, asset: Asset) -> None:
    if track.kind == "audio":
        if asset.kind != "audio":
            raise TimelineError(
                f"audio lanes accept audio assets only — "
                f"'{asset.name}' is kind '{asset.kind}'")
    elif asset.kind != "video":
        raise TimelineError(
            f"{track.kind} lanes accept video assets only — "
            f"'{asset.name}' is kind '{asset.kind}'")


def resolve_clip_source(session: Session, project_id: str, asset_id: str,
                        version_id: str | None) -> tuple[Asset, AssetVersion, dict]:
    asset = session.get(Asset, asset_id)
    if asset is None or asset.project_id != project_id:
        raise KeyError("source asset not found in this project")
    if asset.kind not in ("video", "audio"):
        raise TimelineError(
            "timeline clips require a video or audio asset")
    # Asset vocabulary is ready|missing|deleted — 'ready' is the only
    # usable state; archived/missing/deleted sources refuse.
    if asset.status != "ready":
        raise TimelineError(
            f"source asset is {asset.status} — restore it before use")
    ver = (session.get(AssetVersion, version_id) if version_id
           else session.get(AssetVersion, asset.current_version_id))
    if ver is None or ver.asset_id != asset.id:
        raise TimelineError("source version not found on this asset")
    geo = (source_geometry_audio(ver) if asset.kind == "audio"
           else source_geometry(ver))
    return asset, ver, geo


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
                track_id: str | None = None,
                start_frames: int | None = None,
                ripple_scope: str | None = None,
                command_id: str | None) -> TimelineClip:
    """Insert a clip on a lane. Defaults preserve C5-A semantics:
    first video track, appended after the contiguous run. An explicit
    ``start_frames`` places it at a timeline-time position — gaps are
    allowed, same-track overlap is refused (N1-A)."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    asset, ver, geo = resolve_clip_source(
        session, project_id, asset_id, version_id)
    if track_id is not None:
        try:
            track = get_track(session, tl, track_id)
        except KeyError:
            raise TimelineError(
                "track not found on this timeline") from None
    else:
        track = first_video_track(session, tl.id)
        if track is None:
            raise TimelineError(
                "timeline has no video track — add one before "
                "inserting clips")
    _check_track_unlocked(track)
    check_track_asset_kind(track, asset)
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
        track_id=track.id, source_asset_id=asset.id,
        source_version_id=ver.id, source_sha256=ver.sha256,
        in_frame=i, out_frame=o, src_frames=src_frames,
        src_fps_num=geo["fps_num"], src_fps_den=geo["fps_den"],
        src_duration_s=geo["duration_s"],
        src_has_audio=bool(geo.get("has_audio", True)),
        shot_id=shot_id,
        scene_id=scene_id)
    extent = clip_tl_extent(clip, tl)
    if start_frames is not None:
        clip.start_frames = _check_int(
            start_frames, "start_frames", 0, 1 << 30)
        if ripple_scope is not None:
            # Ripple insert: push in-scope clips at/after the point
            # right by the clip's integer footprint instead of
            # refusing the collision. A point strictly inside another
            # clip still refuses — split it first, explicitly.
            scope = _check_ripple_scope(ripple_scope)
            for c in track_clips(session, track.id):
                e = Fraction(c.start_frames) + clip_tl_extent(c, tl)
                if Fraction(c.start_frames) < Fraction(
                        clip.start_frames) < e:
                    raise TimelineError(
                        "insert point is inside a clip — "
                        "split it first")
            tracks = _ripple_tracks(
                session, tl, scope, track,
                after_start=clip.start_frames)
            _shift_following(
                session, tl, tracks, after_start=clip.start_frames,
                delta=_ceil_frame(extent), keep={clip.id})
        else:
            _check_track_overlap(
                session, track, tl, start=clip.start_frames,
                extent=extent)
    elif position is not None:
        # Legacy positional insert: repack the lane's contiguous run so
        # the clip lands at that index — identical to C5-A insert-at
        # when extents are integral. Gapped lanes have no faithful
        # index↔time mapping: refuse.
        tc = track_clips(session, track.id)
        position = _check_int(position, "position", 0, len(tc))
        run_end = Fraction(0)
        for c in tc:
            if c.start_frames != _ceil_frame(run_end):
                raise TimelineError(
                    "positional insert needs a contiguous track — "
                    "use explicit start_frames on gapped lanes")
            run_end = Fraction(c.start_frames) + clip_tl_extent(c, tl)
        # Repack by ceil-accumulation so index k's start is the packed
        # end of the merged prefix — stable for rational extents.
        merged = tc[:position] + [clip] + tc[position:]
        run_end = Fraction(0)
        for c in merged:
            c.start_frames = _ceil_frame(run_end)
            run_end = Fraction(c.start_frames) + clip_tl_extent(c, tl)
    else:
        clip.start_frames = _packed_start(session, track, tl)
        _check_track_overlap(
            session, track, tl, start=clip.start_frames, extent=extent)
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
                ripple_scope: str | None = None,
                command_id: str | None) -> None:
    """Non-ripple delete leaves a gap. ``ripple_scope`` shifts clips
    starting at/after the removed clip's grid end left by its integer
    footprint — 'track' limits that to the clip's own lane (default,
    disclosed), 'all' applies it to every unlocked lane."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    track = (get_track(session, tl, clip.track_id)
             if clip.track_id else None)
    _check_track_unlocked(track)
    if ripple_scope is not None and track is not None:
        scope = _check_ripple_scope(ripple_scope)
        occ_end = _ceil_end(clip, tl)
        tracks = _ripple_tracks(session, tl, scope, track,
                                after_start=occ_end)
        session.delete(clip)
        session.flush()
        _shift_following(session, tl, tracks, after_start=occ_end,
                         delta=-(occ_end - clip.start_frames))
    else:
        session.delete(clip)
        session.flush()
    _renumber(session, tl.id)
    _touch(session, tl, command_id=command_id,
           summary="Ripple-deleted clip" if ripple_scope is not None
           else "Removed clip")


def move_clip(session: Session, tl: Timeline, clip: TimelineClip, *,
              to_index: int | None = None,
              to_track_id: str | None = None,
              start_frames: int | None = None,
              command_id: str | None) -> None:
    """Two legal forms (not both):
    - ``to_index`` — C5-A sequence reorder on a contiguous track:
      repacks the lane so the clip takes that index. Refuses gapped
      lanes (index has no faithful meaning across holes).
    - ``to_track_id``/``start_frames`` — explicit placement: retimes
      and/or relanes the clip; same-track overlap refuses (N1-A)."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    src_track = (get_track(session, tl, clip.track_id)
                 if clip.track_id else None)
    _check_track_unlocked(src_track)
    if to_index is not None and (to_track_id is not None
                                 or start_frames is not None):
        raise TimelineError(
            "pass to_index or {to_track_id, start_frames}, not both")
    if to_track_id is None and start_frames is None:
        # Legacy sequence reorder — only meaningful on a contiguous
        # lane; on gaps it would silently retime the clip.
        track = src_track
        if track is None:
            raise TimelineError("clip has no track")
        tc = track_clips(session, track.id)
        to_index = _check_int(to_index, "to_index", 0, len(tc) - 1)
        run_end = Fraction(0)
        for c in tc:
            if c.start_frames != _ceil_frame(run_end):
                raise TimelineError(
                    "index move needs a contiguous track — "
                    "use explicit start_frames on gapped lanes")
            run_end = Fraction(c.start_frames) + clip_tl_extent(c, tl)
        tc.remove(clip)
        tc.insert(to_index, clip)
        acc = Fraction(0)
        for c in tc:
            c.start_frames = _ceil_frame(acc)
            acc = Fraction(c.start_frames) + clip_tl_extent(c, tl)
    else:
        if to_track_id is not None:
            try:
                dst_track = get_track(session, tl, to_track_id)
            except KeyError:
                raise TimelineError(
                    "track not found on this timeline") from None
            _check_track_unlocked(dst_track)
        else:
            dst_track = src_track
        if dst_track is None:
            raise TimelineError("clip has no track")
        if dst_track.id != clip.track_id:
            asset = session.get(Asset, clip.source_asset_id)
            if asset is not None:
                check_track_asset_kind(dst_track, asset)
        clip.start_frames = _check_int(
            start_frames if start_frames is not None
            else clip.start_frames, "start_frames", 0, 1 << 30)
        clip.track_id = dst_track.id
        _check_track_overlap(
            session, dst_track, tl, start=clip.start_frames,
            extent=clip_tl_extent(clip, tl), ignore_clip_id=clip.id)
    session.flush()
    _renumber(session, tl.id)
    _touch(session, tl, command_id=command_id, summary="Moved clip")


def trim_clip(session: Session, tl: Timeline, clip: TimelineClip, *,
              in_frame: int | None, out_frame: int | None,
              ripple_scope: str | None = None,
              command_id: str | None) -> None:
    """Source-frame trims, start_frames anchored. Non-ripple keeps
    N1-A semantics: a growing extent must not reach a neighbor.
    ``ripple_scope`` instead shifts clips starting at/after the clip's
    old grid end by the change in integer occupancy — the ripple is
    measured in timeline frames, source trims stay exact."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    track = (get_track(session, tl, clip.track_id)
             if clip.track_id else None)
    _check_track_unlocked(track)
    i, o = _check_trims(
        clip.in_frame if in_frame is None else in_frame,
        clip.out_frame if out_frame is None else out_frame,
        clip.src_frames)
    old_i, old_o = clip.in_frame, clip.out_frame
    old_end = _ceil_end(clip, tl)
    clip.in_frame, clip.out_frame = i, o
    new_end = _ceil_end(clip, tl)
    delta = new_end - old_end
    # N2-D — a trim can never make stored fades exceed the new
    # extent, and can never pull source material a crossfade window
    # relies on. Both refuse closed — the user clears the fade /
    # crossfade first.
    if clip.fade_in_units + clip.fade_out_units > o - i:
        raise TimelineError(
            "trim would leave less room than the clip's fades need — "
            "shorten fade_in/fade_out first")
    new_occ = _ceil_frame(clip_tl_extent(clip, tl))
    stray = [p["at_frames"] for p in automation_points(clip)
             if p["at_frames"] >= new_occ]
    if stray:
        raise TimelineError(
            "trim would orphan automation points — remove points "
            "beyond the new clip end first")
    for xf in list_crossfades(session, tl.id):
        if clip.id not in (xf.out_clip_id, xf.in_clip_id):
            continue
        if not crossfade_satisfied(session, tl, xf):
            raise TimelineError(
                "trim would break a crossfade's source handles — "
                "clear the crossfade first")
    if ripple_scope is not None:
        if delta != 0 and track is not None:
            scope = _check_ripple_scope(ripple_scope)
            tracks = _ripple_tracks(session, tl, scope, track,
                                    after_start=old_end)
            _shift_following(session, tl, tracks,
                             after_start=old_end, delta=delta)
    # A trim that grows the extent must not push into a neighbor —
    # same-track overlap is refused everywhere in N1-A.
    elif o - i > old_o - old_i and clip.track_id:
        _check_track_overlap(
            session, track, tl, start=clip.start_frames,
            extent=clip_tl_extent(clip, tl), ignore_clip_id=clip.id)
    session.flush()
    _touch(session, tl, command_id=command_id,
           summary="Ripple-trimmed clip" if ripple_scope is not None
           else "Trimmed clip")


# ---------- N1-C: split / ripple / gap ops ----------

#: Ripple scope choices — 'track' (the safest default: only the edited
#: lane's later clips move) or 'all' (every lane's later clips; a
#: locked lane that would be affected refuses the whole op). 'all'
#: treats lane kinds uniformly — video, overlay, and audio lanes all
#: shift by the same integer timeline-frame delta.
RIPPLE_SCOPES = ("track", "all")


def _check_ripple_scope(scope: Any) -> str:
    if scope is None:
        return "track"
    if scope not in RIPPLE_SCOPES:
        raise TimelineError(
            f"ripple scope must be one of {', '.join(RIPPLE_SCOPES)}")
    return scope


def _ceil_end(clip: TimelineClip, tl: Timeline) -> int:
    """Integer-grid occupancy end: start + ceil(exact extent)."""
    return clip.start_frames + _ceil_frame(clip_tl_extent(clip, tl))


def _ripple_tracks(session: Session, tl: Timeline, scope: str,
                   anchor: TimelineTrack, *, after_start: int
                   ) -> list[TimelineTrack]:
    """In-scope lanes for a ripple. 'track' is just the anchor lane.
    'all' is every lane — but a locked lane holding clips that the
    shift would move refuses the whole operation (fail closed)."""
    if scope == "track":
        return [anchor]
    tracks = list_tracks(session, tl.id)
    for tr in tracks:
        if tr.locked and any(c.start_frames >= after_start
                             for c in track_clips(session, tr.id)):
            raise TimelineError(
                f"ripple would move clips on locked track "
                f"'{tr.name or tr.id}' — unlock it or use "
                f"scope 'track'")
    return tracks


def _shift_following(session: Session, tl: Timeline,
                     tracks: list[TimelineTrack], *, after_start: int,
                     delta: int, keep: set[str] | None = None) -> int:
    """Move clips on `tracks` starting at/after `after_start` by `delta`
    frames. Order-preserving, so a uniform shift cannot overlap.
    Returns how many clips moved."""
    skip = keep or set()
    moved = 0
    for tr in tracks:
        for c in track_clips(session, tr.id):
            if c.id in skip or c.start_frames < after_start:
                continue
            ns = c.start_frames + delta
            if ns < 0:
                raise TimelineError(
                    "shift would push a clip before frame 0")
            c.start_frames = ns
            moved += 1
    return moved


def split_clip(session: Session, tl: Timeline, clip: TimelineClip, *,
               at_frame: int, command_id: str | None) -> TimelineClip:
    """Frame-accurate split at a TIMELINE frame, strictly inside the
    clip's occupancy. The cut must land on a SOURCE-frame boundary —
    the timeline-frame offset into the clip must map to an integer
    source-frame offset, else we refuse rather than cut mid-frame
    (honest accuracy for mixed rates). Both fragments keep the full
    source pin (asset, version, sha256, geometry, shot/scene links)."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    track = (get_track(session, tl, clip.track_id)
             if clip.track_id else None)
    _check_track_unlocked(track)
    if track is None:
        raise TimelineError("clip has no track")
    at = _check_int(at_frame, "at_frame", 0, 1 << 30)
    end = Fraction(clip.start_frames) + clip_tl_extent(clip, tl)
    if not (Fraction(clip.start_frames) < Fraction(at) < end):
        raise TimelineError(
            f"split must land strictly inside the clip "
            f"(frames {clip.start_frames + 1}..{_ceil_frame(end) - 1})")
    # Timeline-frame offset → source-frame offset, exact rational:
    off = Fraction(at - clip.start_frames) * Fraction(
        tl.fps_den, tl.fps_num) * Fraction(
            clip.src_fps_num, clip.src_fps_den)
    if off.denominator != 1:
        raise TimelineError(
            "split point doesn't land on a source frame — "
            "choose a timeline frame that maps to a whole source frame")
    off_i = int(off)
    if not 0 < off_i < clip.out_frame - clip.in_frame:
        raise TimelineError("split would create a zero-length fragment")
    # N2-D — a split can never land inside a crossfade window (the
    # record's semantics would be ambiguous) nor inside a fade region
    # (a fade must stay edge-anchored and whole).
    for xf in list_crossfades(session, tl.id):
        if clip.id in (xf.out_clip_id, xf.in_clip_id) and \
                xf.tl_start <= at < xf.tl_start + xf.tl_frames:
            raise TimelineError(
                "split point falls inside a crossfade window — "
                "clear the crossfade or split outside it")
    left_extent = off_i
    right_extent = clip.out_frame - (clip.in_frame + off_i)
    if clip.fade_in_units > left_extent:
        raise TimelineError(
            "split lands inside the fade-in — split outside the fade "
            "or shorten the fade first")
    if clip.fade_out_units > right_extent:
        raise TimelineError(
            "split lands inside the fade-out — split outside the "
            "fade or shorten the fade first")
    # Automation points are clip-local (offset from clip start): the
    # cut at timeline frame `at` divides them — left keeps the points
    # before the cut, right gets the rest rebased.
    cut_off = at - clip.start_frames
    pts = automation_points(clip)
    left_pts = [p for p in pts if p["at_frames"] < cut_off]
    right_pts = [dict(p, at_frames=p["at_frames"] - cut_off)
                 for p in pts if p["at_frames"] >= cut_off]
    right = TimelineClip(
        id=new_id(), project_id=clip.project_id, timeline_id=tl.id,
        track_id=clip.track_id, position=clip.position,
        source_asset_id=clip.source_asset_id,
        source_version_id=clip.source_version_id,
        source_sha256=clip.source_sha256,
        in_frame=clip.in_frame + off_i, out_frame=clip.out_frame,
        src_frames=clip.src_frames,
        src_fps_num=clip.src_fps_num, src_fps_den=clip.src_fps_den,
        src_duration_s=clip.src_duration_s,
        src_has_audio=clip.src_has_audio,
        gain_db=clip.gain_db,
        # N2-D — fades are edge-anchored: the left fragment keeps the
        # IN fade (its in edge is unchanged; its new out edge is the
        # cut), the right fragment keeps the OUT fade.
        fade_in_units=0, fade_in_curve=clip.fade_in_curve,
        fade_out_units=clip.fade_out_units,
        fade_out_curve=clip.fade_out_curve,
        automation_json=json.dumps(right_pts, separators=(",", ":")),
        automation_interp=clip.automation_interp,
        shot_id=clip.shot_id, scene_id=clip.scene_id,
        start_frames=at)
    clip.out_frame = clip.in_frame + off_i
    clip.fade_out_units = 0
    clip.automation_json = json.dumps(left_pts, separators=(",", ":"))
    session.add(right)
    session.flush()
    _renumber(session, tl.id)
    _touch(session, tl, command_id=command_id, summary="Split clip")
    return right


def close_gap(session: Session, tl: Timeline, *, track_id: str,
              at_frame: int, command_id: str | None) -> int:
    """Move-to-close-gap: shift the lane's clips after the gap left by
    the exact integer gap length, so the next clip abuts the previous
    clip's grid end. The leading gap (0 → first clip) counts too.
    Returns the gap length closed."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    try:
        track = get_track(session, tl, track_id)
    except KeyError:
        raise TimelineError("track not found on this timeline") from None
    _check_track_unlocked(track)
    at = _check_int(at_frame, "at_frame", 0, 1 << 30)
    prev_end = Fraction(0)
    gap_len = 0
    follower_start = 0
    for c in track_clips(session, track.id):
        if Fraction(at) < Fraction(c.start_frames):
            gap_len = c.start_frames - _ceil_frame(prev_end)
            follower_start = c.start_frames
            break
        e = Fraction(c.start_frames) + clip_tl_extent(c, tl)
        if Fraction(at) < e:
            raise TimelineError(
                "that frame is inside a clip, not a gap")
        prev_end = e
    if gap_len <= 0:
        raise TimelineError("no gap at that frame")
    _shift_following(session, tl, [track],
                     after_start=follower_start, delta=-gap_len)
    session.flush()
    _renumber(session, tl.id)
    _touch(session, tl, command_id=command_id, summary="Closed gap")
    return gap_len


def insert_gap(session: Session, tl: Timeline, *, track_id: str,
               at_frame: int, frames: int,
               command_id: str | None) -> int:
    """Insert-space: shift the lane's clips at/after `at_frame` right
    by `frames`. Refuses when `at_frame` lands strictly inside a clip —
    splitting first keeps the intent explicit."""
    if tl.status != "active":
        raise TimelineError("timeline is archived")
    try:
        track = get_track(session, tl, track_id)
    except KeyError:
        raise TimelineError("track not found on this timeline") from None
    _check_track_unlocked(track)
    at = _check_int(at_frame, "at_frame", 0, 1 << 30)
    n = _check_int(frames, "frames", 1, 1 << 30)
    for c in track_clips(session, track.id):
        e = Fraction(c.start_frames) + clip_tl_extent(c, tl)
        if Fraction(c.start_frames) < Fraction(at) < e:
            raise TimelineError(
                "insert point is inside a clip — split it first")
    moved = _shift_following(session, tl, [track],
                             after_start=at, delta=n)
    if moved == 0:
        raise TimelineError("no clips after that frame — nothing to shift")
    session.flush()
    _renumber(session, tl.id)
    _touch(session, tl, command_id=command_id, summary="Inserted gap")
    return moved


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


def program_end_frac(session: Session, tl: Timeline) -> Fraction:
    """Timeline-time end of the last clip — the honest program length
    whether the edit is contiguous, gapped, or multitrack."""
    end = Fraction(0)
    for c in list_clips(session, tl.id):
        e = Fraction(c.start_frames) + clip_tl_extent(c, tl)
        if e > end:
            end = e
    return end


def v1_layout_reason(session: Session, tl: Timeline) -> str | None:
    """Why a timeline can't be serialized as ade-edl/1, or None if it
    can. v1 knows exactly one unmuted visible video lane of contiguous
    clips starting at 0 — anything else would silently change the
    picture, so we refuse instead of flattening."""
    tracks = list_tracks(session, tl.id)
    clipped = [t for t in tracks if track_clips(session, t.id)]
    if not clipped:
        return None  # empty timeline serializes as an empty clip list
    if len(clipped) > 1:
        return ("multiple tracks hold clips — ade-edl/1 can only "
                "represent a single sequential video lane")
    t = clipped[0]
    if t.kind != "video":
        return ("the only populated track is "
                f"'{t.kind}' — ade-edl/1 represents video lanes only")
    if not t.visible:
        return "the populated track is hidden — refusing an empty v1 picture"
    if t.muted:
        return ("the populated track is muted — ade-edl/1 has no "
                "mute flag and would export sound")
    run_end = Fraction(0)
    for c in track_clips(session, t.id):
        if Fraction(c.start_frames) < run_end:
            return "clips overlap on the track"
        if c.start_frames != _ceil_frame(run_end):
            return ("clips are not contiguous — ade-edl/1 cannot "
                    "represent gaps; use ade-edl/2")
        run_end = Fraction(c.start_frames) + clip_tl_extent(c, tl)
    return None


def edl_json(session: Session, tl: Timeline) -> dict[str, Any]:
    """Deterministic `ade-edl/1` serialization — same timeline state
    produces byte-identical JSON (no timestamps, no random ordering).

    Refuses layouts v1 can't express exactly: multiple clipped tracks,
    hidden/muted sole lanes, gaps, or overlaps — never flattens them
    silently (N1-A).

    Timebase rules: each clip's trims are integer frames in ITS OWN
    source rate (src_fps_num/src_fps_den, preserved exactly — 30000/1001
    stays 30000/1001). Clip seconds = (out-in)·den/num as an exact
    Fraction; the timeline total is the exact sum across mixed rates.
    Floats appear only at the boundary, rounded to 6 dp — the integer
    frames in the document are always the authoritative cut points.
    """
    reason = v1_layout_reason(session, tl)
    if reason is not None:
        raise TimelineError(
            f"timeline cannot be represented as ade-edl/1: {reason}")
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


def edl_json_v2(session: Session, tl: Timeline) -> dict[str, Any]:
    """Deterministic `ade-edl/2` — full-fidelity multitrack document:
    lanes, integer timeline-frame placement, source trims, pinned
    versions/hashes, per-lane gaps, provenance.

    Invariant: same timeline state → byte-identical JSON. Clip order is
    (track position, start_frames, id); gaps are computed per lane as
    integer frames between consecutive clip ends."""
    tracks = list_tracks(session, tl.id)
    tracks_out = [{
        "id": t.id, "kind": t.kind, "position": t.position,
        "name": t.name, "visible": t.visible, "locked": t.locked,
        "muted": t.muted,
        # N2-C — lane mix controls. dB only; the mixer converts.
        "gain_db": t.gain_db, "solo": t.solo,
        # N3-A — lane gain keyframes keyed in absolute timeline
        # frames; the exporter interpolates them in the shared dB
        # sum per contributing stream.
        "automation": track_automation_points(t),
        "automation_interp": getattr(t, "automation_interp",
                                     "linear")} for t in tracks]
    clips_out, gaps_out = [], []
    tl_fps = Fraction(tl.fps_num, tl.fps_den)
    for t in tracks:
        run_end = Fraction(0)
        for c in track_clips(session, t.id):
            ver = session.get(AssetVersion, c.source_version_id)
            if ver is not None and ver.sha256 != c.source_sha256:
                raise TimelineError(
                    f"clip {c.id}: pinned sha does not match its "
                    "version — timeline data is inconsistent")
            media = (ver.media_json or {}) if ver else {}
            probe = media.get("probe") or {}
            if Fraction(c.start_frames) > run_end:
                gaps_out.append({
                    "track_id": t.id,
                    "start_frames": int(run_end),
                    "duration_frames": int(
                        Fraction(c.start_frames) - run_end),
                })
            extent = clip_tl_extent(c, tl)
            run_end = max(run_end,
                          Fraction(c.start_frames) + extent)
            clips_out.append({
                "clip_id": c.id, "track_id": c.track_id,
                "order": c.position,
                "start_frames": c.start_frames,
                "start_s": round(float(
                    Fraction(c.start_frames) / tl_fps), 6),
                "timeline_frames": {
                    "num": extent.numerator, "den": extent.denominator},
                "asset_id": c.source_asset_id,
                "version_id": c.source_version_id,
                "sha256": c.source_sha256,
                "in_frame": c.in_frame, "out_frame": c.out_frame,
                "duration_s": round(float(clip_duration_frac(c)), 6),
                # N2-C — per-clip gain in dB (0 = unity). Effective
                # level = clip + track dB when the lane is audible.
                "gain_db": c.gain_db,
                # N2-D — fades in source units + bounded gain
                # automation (clip-local tl-frame offsets, linear-in-
                # dB interpolation). Both are metadata until N2-E
                # renders them.
                "fades": {
                    "in": {"units": c.fade_in_units,
                           "curve": c.fade_in_curve},
                    "out": {"units": c.fade_out_units,
                            "curve": c.fade_out_curve},
                },
                "automation": automation_points(c),
                "automation_interp": getattr(c, "automation_interp",
                                             "linear"),
                "source": {
                    "fps_num": c.src_fps_num, "fps_den": c.src_fps_den,
                    "frames": c.src_frames,
                    "duration_s": c.src_duration_s,
                    "width": probe.get("width"),
                    "height": probe.get("height"),
                    "codec": probe.get("codec"),
                    # N2-A — audio sources carry stream identity so the
                    # document alone reveals rate/layout; null on video.
                    # samples_exact distinguishes an exact container
                    # count (duration_ts @ 1/rate — WAV/AIFF) from a
                    # duration×rate estimate (compressed formats) —
                    # never claim exactness we don't have.
                    "sample_rate": probe.get("sample_rate"),
                    "channels": probe.get("channels"),
                    "channel_layout": probe.get("channel_layout"),
                    "samples": probe.get("samples"),
                    "samples_exact": probe.get("samples_exact"),
                },
                "shot_id": c.shot_id, "scene_id": c.scene_id,
            })
    return {
        "format": EDL_FORMAT_V2,
        "timeline_id": tl.id,
        "revision": tl.revision,
        "name": tl.name,
        "timebase": {"num": tl.fps_num, "den": tl.fps_den},
        "tracks": tracks_out,
        "clips": clips_out,
        "gaps": gaps_out,
        # N2-D — crossfade records, ordered by window start. Each
        # reports read-time feasibility via `satisfied` — a record
        # whose window detached (moves, etc.) stays honest, never
        # silently dropped.
        "crossfades": [{
            "id": x.id, "out_clip_id": x.out_clip_id,
            "in_clip_id": x.in_clip_id,
            "tl_start": x.tl_start, "tl_frames": x.tl_frames,
            "curve": x.curve,
            "satisfied": crossfade_satisfied(session, tl, x)}
            for x in list_crossfades(session, tl.id)],
        "total_duration_s": round(
            float(program_end_frac(session, tl) / tl_fps), 6),
    }
