"""A6 — first-cut assembly: a deterministic, reviewable proposal that
maps storyboard scenes/shots onto timeline lanes.

A plan walks SCREENPLAY scene order (each screenplay scene binds a
storyboard scene), then unlinked storyboard scenes by their own
position; inside each scene, shots run in storyboard position order.
Every shot produces:

- a ``shot`` item — the shot's selected motion clip (lowest-position
  active MotionClip carrying a result asset) on the video lane;
- ``dialogue`` items — active DialogueSegments bound to the shot,
  placed on the dialogue audio lane at the shot's start plus the
  segment's planned timing offset;
- ``audio`` items — audio assets linked to the shot through
  entity_assets (role 'voice' is the audio-capable link), placed on
  the music lane at the shot's start.

The plan is a proposal, never a silent rewrite. ``plan_json`` stores
the previewed items verbatim; ``sources_json`` pins a deterministic
fingerprint of every input the plan was computed from (scene/shot
order + revisions, clip/dialogue results + revisions, linked audio,
and the timeline's own lane occupancy so manual edits between plan
and apply refuse). ``assembly.apply`` checks the fingerprint and is
idempotent: a recorded item whose clip still exists at the recorded
geometry is untouched; a recorded clip that was trimmed/moved by the
user counts as a manual edit and is preserved unless
``resolve_conflicts: 'replace'`` is explicitly approved.
"""
from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (
    AssemblyItem,
    AssemblyPlan,
    Asset,
    AssetVersion,
    DialogueSegment,
    EntityAssetLink,
    EntityRevision,
    MotionClip,
    Scene,
    ScreenplayScene,
    Shot,
    Timeline,
    TimelineClip,
    TimelineTrack,
    new_id,
    utcnow,
)

ITEM_KINDS = ("shot", "dialogue", "audio")
REVIEW_STATUSES = ("pending", "approved", "rejected", "skipped")
VIDEO_LANE = "V1"
DIALOGUE_LANE = "DX"
MUSIC_LANE = "MX"

NAME_MAX = 200
NOTE_MAX = 4000


def _reject(msg: str) -> None:
    raise ValueError(msg)


def _sha(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def item_key(kind: str, source_id: str) -> str:
    return f"{kind}:{source_id}"


def _timeline(session: Session, project_id: str, timeline_id: str) -> Timeline:
    tl = session.get(Timeline, timeline_id)
    if tl is None or tl.project_id != project_id:
        _reject("timeline not found in this project")
    if tl.status != "active":
        _reject("timeline is archived")
    return tl


def get_plan(session: Session, project_id: str, plan_id: str) -> AssemblyPlan:
    plan = session.get(AssemblyPlan, plan_id)
    if plan is None or plan.project_id != project_id:
        _reject("assembly plan not found in this project")
    return plan


def plan_for_timeline(session: Session, project_id: str,
                      timeline_id: str) -> AssemblyPlan | None:
    return session.execute(
        select(AssemblyPlan).where(
            AssemblyPlan.project_id == project_id,
            AssemblyPlan.timeline_id == timeline_id)
    ).scalar_one_or_none()


def plan_items(session: Session, plan_id: str) -> list[AssemblyItem]:
    return session.execute(
        select(AssemblyItem).where(AssemblyItem.plan_id == plan_id)
        .order_by(AssemblyItem.position)
    ).scalars().all()


# ---------- source gathering ----------

def _ordered_scenes(session: Session, project_id: str) -> list[dict]:
    """Screenplay scene order first (via their bound storyboard
    scenes), then unlinked active storyboard scenes by position.
    Returns [{scene, sp_scene}] — deterministic."""
    sp_scenes = session.execute(
        select(ScreenplayScene).where(
            ScreenplayScene.project_id == project_id,
            ScreenplayScene.scene_id.isnot(None))
        .order_by(ScreenplayScene.position)
    ).scalars().all()
    seen: set[str] = set()
    out: list[dict] = []
    for sp in sp_scenes:
        scene = session.get(Scene, sp.scene_id)
        if scene is None or scene.project_id != project_id \
                or scene.id in seen or scene.status != "active":
            continue
        seen.add(scene.id)
        out.append({"scene": scene, "sp_scene": sp})
    for scene in session.execute(
            select(Scene).where(
                Scene.project_id == project_id,
                Scene.status == "active")
            .order_by(Scene.position)).scalars().all():
        if scene.id not in seen:
            out.append({"scene": scene, "sp_scene": None})
    return out


def _shot_source(session: Session, shot: Shot) -> dict:
    """Everything a shot contributes to the plan, fingerprinted."""
    clips = session.execute(
        select(MotionClip).where(
            MotionClip.shot_id == shot.id,
            MotionClip.status == "active")
        .order_by(MotionClip.position)
    ).scalars().all()
    segments = session.execute(
        select(DialogueSegment).where(
            DialogueSegment.shot_id == shot.id,
            DialogueSegment.status == "active")
        .order_by(DialogueSegment.position)
    ).scalars().all()
    links = session.execute(
        select(EntityAssetLink).where(
            EntityAssetLink.project_id == shot.project_id,
            EntityAssetLink.entity_type == "shot",
            EntityAssetLink.entity_id == shot.id)
        .order_by(EntityAssetLink.position)
    ).scalars().all()
    audio_links: list[tuple[EntityAssetLink, Asset | None]] = []
    for link in links:
        asset = session.get(Asset, link.asset_id)
        if asset is not None and asset.project_id == shot.project_id \
                and asset.kind == "audio":
            audio_links.append((link, asset))
    return {"clips": clips, "segments": segments,
            "audio_links": audio_links}


def _take(clips: list[MotionClip]) -> tuple[MotionClip | None, str | None]:
    """Selected take = lowest-position active clip with a result.
    Returns (clip, warning)."""
    with_result = [c for c in clips if c.result_asset_id]
    if not with_result:
        return None, ("no generated motion clip" if clips
                      else "shot has no motion clips")
    take = with_result[0]
    warn = (f"{len(with_result)} candidate takes — lowest-position "
            f"'{take.id[:8]}' selected") if len(with_result) > 1 else None
    return take, warn


def _audio_extent_frac(session: Session, asset: Asset,
                       tl: Timeline) -> Fraction | None:
    """Timeline frames an audio asset occupies — exact rational from
    the current version's probed geometry."""
    from app.domain import timeline as tld
    if not asset.current_version_id:
        return None
    ver = session.get(AssetVersion, asset.current_version_id)
    if ver is None:
        return None
    try:
        geo = tld.source_geometry_audio(ver)
    except Exception:
        return None
    samples = geo.get("frames")
    rate = geo.get("fps_num")
    if not samples or not rate:
        return None
    return Fraction(int(samples), int(rate)) * Fraction(
        tl.fps_num, tl.fps_den)


def _video_extent_frac(session: Session, clip: MotionClip,
                       tl: Timeline) -> Fraction | None:
    """Timeline frames a motion clip occupies: its own rational
    duration converted at the timeline rate; falls back to the
    probed asset geometry when duration_frames is unset."""
    from app.domain import timeline as tld
    if clip.duration_frames:
        return Fraction(clip.duration_frames) * Fraction(
            clip.fps_den, clip.fps_num) * Fraction(
                tl.fps_num, tl.fps_den)
    asset = session.get(Asset, clip.result_asset_id) \
        if clip.result_asset_id else None
    if asset is None or not asset.current_version_id:
        return None
    ver = session.get(AssetVersion, asset.current_version_id)
    if ver is None:
        return None
    try:
        geo = tld.source_geometry(ver)
    except Exception:
        return None
    frames, num, den = (geo.get("frames"), geo.get("fps_num"),
                        geo.get("fps_den"))
    if not frames or not num or not den:
        return None
    return Fraction(int(frames)) * Fraction(int(den), int(num)) * Fraction(
        tl.fps_num, tl.fps_den)


def _lane_map(session: Session, tl: Timeline) -> dict:
    """Resolve the three lane roles. Deterministic: video → first
    video track; dialogue → 'DX'-named audio else first audio;
    music → 'MX'-named audio else a second audio track. `None`
    entries mean apply creates the lane."""
    from app.domain import timeline as tld
    tracks = tld.list_tracks(session, tl.id)
    videos = [t for t in tracks if t.kind == "video"]
    audios = [t for t in tracks if t.kind == "audio"]
    video = videos[0] if videos else None
    dx = next((t for t in audios
               if (t.name or "").strip().upper() == DIALOGUE_LANE), None)
    if dx is None and audios:
        dx = audios[0]
    mx = next((t for t in audios
               if (t.name or "").strip().upper() == MUSIC_LANE), None)
    if mx is None:
        mx = next((t for t in audios if dx is None or t.id != dx.id), None)
    return {"video": video, "dialogue": dx, "music": mx}


def _lane_spec(lane: dict) -> dict:
    """JSON-facing lane resolution for the plan preview."""
    def spec(track, kind: str, name: str) -> dict:
        return {"track_id": track.id if track else None,
                "track_name": track.name if track else None,
                "kind": kind,
                "create": None if track else {"kind": kind, "name": name}}
    return {"video": spec(lane["video"], "video", VIDEO_LANE),
            "dialogue": spec(lane["dialogue"], "audio", DIALOGUE_LANE),
            "music": spec(lane["music"], "audio", MUSIC_LANE)}


def _overflow_track(session, tl, base: dict, role: str,
                    index: int, assigned: set[str]):
    """An extra audio lane for stacked dialogue: 'DX{index}'-named,
    else the next audio track not already claimed by a role, else a
    create-on-demand marker."""
    from app.domain import timeline as tld
    name = f"{DIALOGUE_LANE}{index}" if role == "dialogue" \
        else f"{MUSIC_LANE}{index}"
    tracks = tld.list_tracks(session, tl.id)
    named = next((t for t in tracks if t.kind == "audio"
                  and (t.name or "").strip().upper() == name), None)
    if named is not None:
        return named, name
    free = next((t for t in tracks if t.kind == "audio"
                 and t.id not in assigned), None)
    return free, name


def _applied_map(plan: AssemblyPlan) -> dict[str, dict]:
    return dict((plan.applied_json or {}).get("items") or {})


def _managed_clips(session: Session, plan: AssemblyPlan) -> dict[str, dict]:
    """Timeline clips an earlier apply recorded, per item key:
    {clip, clean} — `clean` is False when the clip's live geometry
    drifted from what apply recorded (i.e. the user edited it → it is
    a manual edit, preserved by default)."""
    from app.domain import timeline as tld
    tl = session.get(Timeline, plan.timeline_id)
    out: dict[str, dict] = {}
    for key, rec in _applied_map(plan).items():
        clip_id = (rec or {}).get("timeline_clip_id")
        clip = session.get(TimelineClip, clip_id) if clip_id else None
        entry = {"clip": None, "state": "removed"}
        if clip is not None and tl is not None \
                and clip.timeline_id == tl.id:
            clean = (
                clip.track_id == rec.get("track_id")
                and clip.start_frames == rec.get("start_frames")
                and clip.in_frame == rec.get("in_frame")
                and clip.out_frame == rec.get("out_frame"))
            entry = {"clip": clip,
                     "state": "clean" if clean else "edited"}
        out[key] = entry
    return out


def _overlap(a_start: int, a_end: Fraction, b_start: int,
             b_end: Fraction) -> bool:
    return a_start < b_end and b_start < a_end


def _item_extent_frames(extent: Fraction | None) -> int:
    """Grid footprint for scheduling/preview — ceil, never zero."""
    if extent is None or extent <= 0:
        return 0
    return max(1, -(-extent.numerator // extent.denominator))


def _applied_kept(session: Session, plan: AssemblyPlan) -> bool:
    """True when the last apply's records all still hold — the only
    live-vs-plan drift is the apply itself, so re-apply is a no-op."""
    applied = plan.applied_json or {}
    proposal = plan.plan_json or {}
    if applied.get("fingerprint") != _sha(proposal):
        return False
    for rec in (applied.get("items") or {}).values():
        cid = (rec or {}).get("timeline_clip_id")
        clip = session.get(TimelineClip, cid) if cid else None
        if clip is None or not (
                clip.track_id == rec.get("track_id")
                and clip.start_frames == rec.get("start_frames")
                and clip.in_frame == rec.get("in_frame")
                and clip.out_frame == rec.get("out_frame")):
            return False
    return True


def _is_stale(session: Session, plan: AssemblyPlan) -> bool:
    """Source-drift test for a persisted plan. Scene-side inputs
    (ordering, revisions, results, links) always stale the plan;
    timeline-side drift is expected only when it exactly equals the
    state the last apply recorded — anything else is a manual edit
    between plan and apply."""
    tl = session.get(Timeline, plan.timeline_id)
    if tl is None or not plan.sources_json:
        return False
    live = sources_fingerprint(session, plan.project_id, tl)
    src = plan.sources_json
    if live["scenes"] != src.get("scenes"):
        return True
    tl_sig = live["timeline"]
    if tl_sig == src.get("timeline"):
        return False
    return tl_sig != (plan.applied_json or {}).get("timeline_after")


# ---------- plan compute ----------

def sources_fingerprint(session: Session, project_id: str,
                        tl: Timeline) -> dict:
    """Deterministic signature of every input a plan is computed from:
    scene ordering + revisions, per-shot sources (clips, dialogue,
    audio links + their revisions), and the timeline's own occupancy
    (tracks + live clips) so a manual edit between plan and apply is
    caught as drift. `assembly.apply` compares this verbatim."""
    from app.domain import timeline as tld
    scenes: list[dict] = []
    for entry in _ordered_scenes(session, project_id):
        scene, sp = entry["scene"], entry["sp_scene"]
        shots = session.execute(
            select(Shot).where(
                Shot.scene_id == scene.id, Shot.status == "active")
            .order_by(Shot.position)).scalars().all()
        shot_sigs = []
        for shot in shots:
            src = _shot_source(session, shot)
            shot_sigs.append({
                "shot_id": shot.id, "position": shot.position,
                "revision": shot.revision,
                "duration_sec": shot.duration_sec,
                "clips": [{
                    "id": c.id, "position": c.position,
                    "revision": c.revision,
                    "result_asset_id": c.result_asset_id,
                    "duration_frames": c.duration_frames,
                    "fps": [c.fps_num, c.fps_den]}
                    for c in src["clips"]],
                "dialogue": [{
                    "id": d.id, "position": d.position,
                    "revision": d.revision,
                    "result_asset_id": d.result_asset_id,
                    "timing": d.timing_json or {}}
                    for d in src["segments"]],
                "audio": [{
                    "link_id": l.id, "asset_id": l.asset_id,
                    "asset_status": a.status if a else "missing",
                    "asset_version": (a.current_version_id if a else None)}
                    for l, a in src["audio_links"]]})
        scenes.append({
            "scene_id": scene.id, "position": scene.position,
            "revision": scene.revision,
            "sp_scene_id": sp.id if sp else None,
            "sp_revision": sp.revision if sp else None,
            "shots": shot_sigs})
    clip_sigs = []
    for c in tld.list_clips(session, tl.id):
        clip_sigs.append({
            "id": c.id, "track_id": c.track_id,
            "start": c.start_frames,
            "in": c.in_frame, "out": c.out_frame,
            "asset": c.source_asset_id,
            "shot_id": c.shot_id})
    track_sigs = [{"id": t.id, "kind": t.kind, "position": t.position,
                   "name": t.name, "locked": t.locked, "muted": t.muted,
                   "solo": t.solo}
                  for t in tld.list_tracks(session, tl.id)]
    return {"scenes": scenes,
            "timeline": {"id": tl.id, "fps": [tl.fps_num, tl.fps_den],
                         "status": tl.status,
                         "tracks": track_sigs, "clips": clip_sigs}}


def compute_plan(session: Session, project_id: str, tl: Timeline, *,
                 plan: AssemblyPlan | None = None) -> dict:
    """Build the deterministic proposal. `plan` (the persisted row, if
    any) supplies prior applied records + review state so a re-plan is
    a revision-impact analysis, not a fresh cut."""
    from app.domain import timeline as tld

    lanes = _lane_map(session, tl)
    managed = _managed_clips(session, plan) if plan else {}
    prior_items = ({it.item_key: it for it in plan_items(session, plan.id)}
                   if plan else {})
    items: list[dict] = []
    warnings: list[str] = []
    shot_start = 0  # running video start, integer frames

    for entry in _ordered_scenes(session, project_id):
        scene, sp = entry["scene"], entry["sp_scene"]
        scene_start = shot_start
        shots = session.execute(
            select(Shot).where(
                Shot.scene_id == scene.id, Shot.status == "active")
            .order_by(Shot.position)).scalars().all()
        for shot in shots:
            src = _shot_source(session, shot)
            take, take_warn = _take(src["clips"])
            extent = _video_extent_frac(session, take, tl) if take else None
            if extent is None or extent <= 0:
                # Fallback: shot's authored duration at the timeline rate.
                fallback = Fraction(str(shot.duration_sec or 0)) * Fraction(
                    tl.fps_num, tl.fps_den)
                extent = fallback if fallback > 0 else Fraction(tl.fps_num,
                                                                tl.fps_den)
            shot_span = _item_extent_frames(extent)
            base_flags: dict[str, Any] = {"warnings": [], "conflicts": []}
            if take_warn:
                base_flags["warnings"].append(take_warn)
            key = item_key("shot", shot.id)
            items.append({
                "key": key, "kind": "shot",
                "scene_id": scene.id, "shot_id": shot.id,
                "sp_scene_id": sp.id if sp else None,
                "motion_clip_id": take.id if take else None,
                "asset_id": take.result_asset_id if take else None,
                "lane": "video", "start_frames": shot_start,
                "extent_frames": shot_span,
                "in_frame": 0,
                "out_units": take.duration_frames if take else None,
                "title": shot.title or shot.id[:8],
                "flags": {**base_flags,
                          "missing": (None if take else
                                      "no motion-clip result")},
                "source": {"shot_revision": shot.revision,
                           "clip_id": take.id if take else None,
                           "clip_revision": take.revision if take else None,
                           "result_asset_id": (take.result_asset_id
                                               if take else None)},
            })

            # Dialogue — start offset from the segment's planned timing
            # when it targets this timeline (or any timeline: the first
            # cut treats start_frames as the in-scene offset).
            for seg in src["segments"]:
                dkey = item_key("dialogue", seg.id)
                asset = (session.get(Asset, seg.result_asset_id)
                         if seg.result_asset_id else None)
                dext = (_audio_extent_frac(session, asset, tl)
                        if asset else None)
                timing = seg.timing_json or {}
                offset = int(timing.get("start_frames") or 0)
                flags: dict[str, Any] = {"warnings": [], "conflicts": []}
                if asset is None:
                    flags["missing"] = "no audio result — generate dialogue first"
                elif asset.status != "ready":
                    flags["missing"] = f"audio result is {asset.status}"
                elif dext is None:
                    flags["missing"] = "audio result has no usable geometry"
                items.append({
                    "key": dkey, "kind": "dialogue",
                    "scene_id": scene.id, "shot_id": shot.id,
                    "dialogue_id": seg.id,
                    "asset_id": asset.id if asset else None,
                    "lane": "dialogue",
                    "start_frames": shot_start + offset,
                    "extent_frames": _item_extent_frames(dext),
                    "in_frame": 0,
                    "out_units": None,
                    "title": (seg.text or seg.id)[:40],
                    "flags": flags,
                    "source": {"dialogue_id": seg.id,
                               "revision": seg.revision,
                               "result_asset_id": seg.result_asset_id,
                               "timing": timing},
                })

            # Shot-linked audio on the music lane.
            for link, asset in src["audio_links"]:
                akey = item_key("audio", link.id)
                aext = (_audio_extent_frac(session, asset, tl)
                        if asset else None)
                flags = {"warnings": [], "conflicts": []}
                if asset is None or asset.status != "ready":
                    flags["missing"] = ("linked audio asset is "
                                        f"{asset.status if asset else 'gone'}")
                elif aext is None:
                    flags["missing"] = "linked audio has no usable geometry"
                items.append({
                    "key": akey, "kind": "audio",
                    "scene_id": scene.id, "shot_id": shot.id,
                    "link_id": link.id,
                    "asset_id": asset.id if asset else None,
                    "lane": "music",
                    "start_frames": shot_start,
                    "extent_frames": _item_extent_frames(aext),
                    "in_frame": 0,
                    "out_units": None,
                    "title": asset.name if asset else link.id[:8],
                    "flags": flags,
                    "source": {"link_id": link.id,
                               "asset_id": link.asset_id,
                               "asset_status": asset.status if asset else None,
                               "asset_version": (asset.current_version_id
                                                 if asset else None)},
                })
            shot_start += shot_span

        # Scene-level dialogue (bound to the scene, no shot anchor)
        # lands at the scene's start on the dialogue lane.
        for seg in session.execute(
                select(DialogueSegment).where(
                    DialogueSegment.scene_id == scene.id,
                    DialogueSegment.shot_id.is_(None),
                    DialogueSegment.status == "active")
                .order_by(DialogueSegment.position)).scalars().all():
            dkey = item_key("dialogue", seg.id)
            asset = (session.get(Asset, seg.result_asset_id)
                     if seg.result_asset_id else None)
            dext = (_audio_extent_frac(session, asset, tl)
                    if asset else None)
            timing = seg.timing_json or {}
            flags = {"warnings": [], "conflicts": []}
            if asset is None:
                flags["missing"] = "no audio result — generate dialogue first"
            elif asset.status != "ready":
                flags["missing"] = f"audio result is {asset.status}"
            elif dext is None:
                flags["missing"] = "audio result has no usable geometry"
            items.append({
                "key": dkey, "kind": "dialogue",
                "scene_id": scene.id, "shot_id": None,
                "dialogue_id": seg.id,
                "asset_id": asset.id if asset else None,
                "lane": "dialogue",
                "start_frames": scene_start + int(
                    timing.get("start_frames") or 0),
                "extent_frames": _item_extent_frames(dext),
                "in_frame": 0, "out_units": None,
                "title": (seg.text or seg.id)[:40],
                "flags": flags,
                "source": {"dialogue_id": seg.id,
                           "revision": seg.revision,
                           "result_asset_id": seg.result_asset_id,
                           "timing": timing},
            })

    # Lane assignment + conflict detection. Dialogue items that would
    # overlap an already-planned dialogue clip overflow onto stacked
    # lanes (DX2, DX3…) — real NLE behavior instead of dropping lines.
    live = tld.list_clips(session, tl.id)
    current_keys = {it["key"] for it in items}
    # Clean managed clips still in-plan are assembly-owned — a re-plan
    # repositions them through their own items, so they never count as
    # manual conflicts. Edited or orphaned managed clips do.
    managed_ok_ids = {e["clip"].id for k, e in managed.items()
                      if e["clip"] is not None and e["state"] == "clean"
                      and k in current_keys}
    lane_claimed = {t.id for t in lanes.values() if t is not None}
    placed_spans: dict[str, list[tuple[Fraction, Fraction]]] = {}
    overflow_lanes: dict[str, dict] = {}

    def _resolve_track(it: dict) -> tuple[Any, str]:
        """Pick the item's lane track; stacked dialogue overflows to
        extra lanes when it collides with an already-planned plan
        item. Returns (track, lane_key) — lane_key names the proposal's
        lane map entry: 'video'/'dialogue'/'music' or 'dialogue2'…"""
        role = it["lane"]
        track = lanes[role]
        if role not in ("dialogue", "music") \
                or it["flags"].get("missing"):
            return track, role
        start = Fraction(it["start_frames"])
        end = start + Fraction(it["extent_frames"])
        idx = 1
        lane_key = role
        while True:
            spans = placed_spans.setdefault(lane_key, [])
            if not any(_overlap(start, end, s, e) for s, e in spans):
                spans.append((start, end))
                return track, lane_key
            idx += 1
            lane_key = f"{role}{idx}"
            if lane_key not in overflow_lanes:
                track2, name2 = _overflow_track(
                    session, tl, lanes, role, idx,
                    lane_claimed | {t.id for t in overflow_lanes.values()
                                    if t is not None})
                overflow_lanes[lane_key] = {
                    "track": track2, "name": name2, "kind": "audio"}
            entry = overflow_lanes[lane_key]
            track = entry["track"]

    for it in items:
        track, lane_key = _resolve_track(it)
        it["track_id"] = track.id if track else None
        it["lane_key"] = lane_key
        prior = prior_items.get(it["key"])
        it["review_status"] = (prior.review_status
                               if prior else "pending")
        it["notes"] = prior.notes if prior else ""
        rec = managed.get(it["key"])
        if rec is not None:
            it["applied"] = {"clip_id": (rec["clip"].id
                                         if rec["clip"] else None),
                             "state": rec["state"]}
            if rec["state"] == "edited":
                it["flags"]["conflicts"].append({
                    "clip_id": rec["clip"].id,
                    "reason": "clip was manually edited after apply"})
            elif rec["state"] == "removed":
                it["flags"]["warnings"].append(
                    "previously applied clip was removed — "
                    "manual edit preserved")
        else:
            it["applied"] = None
        if it["flags"].get("missing"):
            warnings.append(
                f"{it['kind']} item '{it['title']}': "
                f"{it['flags']['missing']}")
        # Overlap vs manual clips on the same lane.
        if track is None or it["flags"].get("missing"):
            continue
        start = it["start_frames"]
        end = Fraction(start) + Fraction(it["extent_frames"])
        own = (it["applied"] or {}).get("clip_id")
        for c in live:
            if c.track_id != track.id or c.id == own \
                    or c.id in managed_ok_ids:
                continue
            c_end = Fraction(c.start_frames) + tld.clip_tl_extent(c, tl)
            if _overlap(Fraction(start), end, Fraction(c.start_frames),
                        c_end):
                it["flags"]["conflicts"].append({
                    "clip_id": c.id,
                    "reason": "overlaps an existing clip on this lane"})
        if it["flags"]["conflicts"]:
            warnings.append(
                f"{it['kind']} item '{it['title']}' conflicts with manual "
                "timeline edits — preserved unless replace is approved")

    # Prior items whose sources vanished — flagged, never silently dropped.
    removed = []
    for key, prior in prior_items.items():
        if key not in current_keys:
            rec = managed.get(key)
            removed.append({
                "key": key, "kind": prior.kind,
                "review_status": prior.review_status,
                "notes": prior.notes,
                "managed_clip_id": (rec["clip"].id
                                    if rec and rec["clip"] else None),
                "managed_state": (rec["state"] if rec else None)})
            warnings.append(
                f"{prior.kind} item '{key}' no longer has a source — "
                "its applied clip is kept unless replace is approved")
    lane_out = _lane_spec(lanes)
    for lane_key, entry in overflow_lanes.items():
        lane_out[lane_key] = {
            "track_id": entry["track"].id if entry["track"] else None,
            "track_name": entry["track"].name if entry["track"] else None,
            "kind": "audio",
            "create": (None if entry["track"] else
                       {"kind": "audio", "name": entry["name"]})}
    return {"timeline_id": tl.id, "lanes": lane_out,
            "items": items, "removed": removed,
            "warnings": warnings,
            "program_frames": shot_start,
            "counts": {
                "items": len(items),
                "missing": sum(1 for i in items
                               if i["flags"].get("missing")),
                "conflicts": sum(1 for i in items
                                 if i["flags"]["conflicts"]),
                "removed": len(removed)}}


def persist_plan(session: Session, project_id: str, tl: Timeline, *,
                 name: str | None, command_id: str | None) -> AssemblyPlan:
    """Compute + persist the plan for a timeline. The row is keyed
    one-per-timeline; re-planning updates it in place and preserves
    per-item review state via stable item keys."""
    plan = plan_for_timeline(session, project_id, tl.id)
    proposal = compute_plan(session, project_id, tl, plan=plan)
    fp = sources_fingerprint(session, project_id, tl)
    if plan is None:
        plan = AssemblyPlan(
            id=new_id(), project_id=project_id, timeline_id=tl.id,
            name=(name or "First cut").strip()[:NAME_MAX] or "First cut")
        session.add(plan)
        session.flush()
    elif name:
        plan.name = name.strip()[:NAME_MAX] or plan.name
    plan.status = "draft"  # a recompute is a fresh proposal
    plan.plan_json = proposal
    plan.sources_json = fp
    plan.revision += 1
    plan.updated_at = utcnow()

    prior = {it.item_key: it for it in plan_items(session, plan.id)}
    for pos, it in enumerate(proposal["items"]):
        row = prior.pop(it["key"], None)
        if row is None:
            row = AssemblyItem(
                id=new_id(), project_id=project_id, plan_id=plan.id,
                item_key=it["key"], kind=it["kind"])
            session.add(row)
        row.position = pos
        row.kind = it["kind"]
        row.scene_id = it.get("scene_id")
        row.shot_id = it.get("shot_id")
        row.dialogue_id = it.get("dialogue_id")
        row.motion_clip_id = it.get("motion_clip_id")
        row.asset_id = it.get("asset_id")
        row.track_id = it.get("track_id")
        # timeline_clip_id: keep the applied record — the applied map
        # stays authoritative until the next apply.
        rec = (plan.applied_json or {}).get("items", {}).get(it["key"])
        row.timeline_clip_id = (rec or {}).get("timeline_clip_id")
        row.start_frames = it["start_frames"]
        row.extent_frames = it["extent_frames"]
        row.flags_json = it["flags"]
        row.source_json = it["source"]
        row.revision = (row.revision or 0) + 1
        row.updated_at = utcnow()
    # Orphaned rows keep their review record with a removed flag.
    for key, row in prior.items():
        row.flags_json = {**(row.flags_json or {}),
                          "removed": "source no longer in the cut"}
        row.revision = (row.revision or 0) + 1
        row.updated_at = utcnow()
    session.flush()
    return plan


def _item_src_geometry(session: Session, it: dict) -> tuple[int, int] | None:
    """(in_frame, out_frame) in SOURCE units for an item's asset —
    0 → probed frames (video) or samples (audio)."""
    from app.domain import timeline as tld
    asset_id = it.get("asset_id")
    if not asset_id:
        return None
    asset = session.get(Asset, asset_id)
    if asset is None or not asset.current_version_id:
        return None
    ver = session.get(AssetVersion, asset.current_version_id)
    if ver is None:
        return None
    try:
        geo = (tld.source_geometry_audio(ver) if asset.kind == "audio"
               else tld.source_geometry(ver))
    except Exception:
        return None
    frames = geo.get("frames")
    return (0, int(frames)) if frames else None


def apply_plan(session: Session, project_id: str, plan: AssemblyPlan, *,
               resolve_conflicts: str = "skip",
               command_id: str | None = None) -> dict:
    """Apply the current proposal. Fail-closed: refuses when the live
    sources drifted from the fingerprint pinned at plan time —
    re-run assembly.plan for a fresh proposal. Idempotent: a re-apply
    whose recorded clips still match the recorded geometry is an
    audited no-op. Manual edits are preserved by default —
    resolve_conflicts='replace' is the explicit approval to remove
    them."""
    from app.domain import timeline as tld

    if resolve_conflicts not in ("skip", "replace"):
        _reject("resolve_conflicts must be 'skip' or 'replace'")
    tl = session.get(Timeline, plan.timeline_id)
    if tl is None:
        _reject("plan's timeline is gone")
    proposal = plan.plan_json or {}
    items = proposal.get("items") or []
    lane_spec = proposal.get("lanes") or {}
    prior_items = {it.item_key: it for it in plan_items(session, plan.id)}

    # Source drift refuses before anything else — the recorded
    # post-apply timeline state is exempted inside _is_stale so the
    # apply's own inserts never trip it.
    if _is_stale(session, plan):
        _reject("sources changed since this plan was computed — "
                "re-run assembly.plan for a fresh proposal")

    # Idempotent replay: the recorded plan fingerprint matches AND
    # every recorded clip still sits at its recorded geometry.
    applied = plan.applied_json or {}
    if applied.get("fingerprint") == _sha(proposal) \
            and _applied_kept(session, plan):
        recs = applied.get("items") or {}
        return {"applied": False, "idempotent": True,
                "created_clips": 0, "skipped": 0, "replaced": 0,
                "kept": len(recs), "removed_clips": 0}

    undo_before = len(tl.undo_stack_json or [])
    managed = _managed_clips(session, plan)
    applied_items: dict[str, dict] = {}
    stats = {"applied": True, "idempotent": False, "created_clips": 0,
             "skipped": 0, "replaced": 0, "kept": 0, "removed_clips": 0}
    skip_notes: list[dict] = []
    created_tracks: dict[str, str] = {}

    def _track_for(it: dict):
        lane_key = it.get("lane_key") or it["lane"]
        spec = lane_spec.get(lane_key) or {}
        if it.get("track_id"):
            return session.get(TimelineTrack, it["track_id"])
        if lane_key in created_tracks:
            return session.get(TimelineTrack, created_tracks[lane_key])
        create = spec.get("create")
        if create is None:
            return None
        track = tld.add_track(session, tl, kind=create["kind"],
                              name=create["name"], command_id=command_id)
        created_tracks[lane_key] = track.id
        return track

    # ---------- decisions ----------
    current_keys = {it["key"] for it in items}
    removals: list[TimelineClip] = []
    inserts: list[tuple[dict, Any]] = []

    def _remove(clip) -> None:
        if clip is not None and all(c.id != clip.id for c in removals):
            removals.append(clip)

    # Stale managed clips (item left the plan): clean ones yield only
    # to explicit replace approval; edited ones are always preserved.
    for key, rec in managed.items():
        if key in current_keys or rec["clip"] is None:
            continue
        if rec["state"] == "clean" and resolve_conflicts == "replace":
            _remove(rec["clip"])
            stats["removed_clips"] += 1
        else:
            stats["kept"] += 1

    for it in items:
        key = it["key"]
        row = prior_items.get(key)
        status = (row.review_status if row else it.get("review_status")) \
            or "pending"
        flags = it["flags"] or {}
        rec = managed.get(key)
        clip_rec = rec["clip"] if rec else None
        if status in ("rejected", "skipped"):
            stats["skipped"] += 1
            skip_notes.append({"key": key, "reason": f"review:{status}"})
            continue
        if flags.get("missing"):
            stats["skipped"] += 1
            skip_notes.append({"key": key, "reason": flags["missing"]})
            continue
        track = _track_for(it)
        if track is None:
            stats["skipped"] += 1
            skip_notes.append({"key": key, "reason": "no lane"})
            continue
        if flags.get("conflicts"):
            if resolve_conflicts == "skip":
                stats["skipped"] += 1
                skip_notes.append({"key": key,
                                   "reason": "conflict with manual edits"})
                continue
            for conflict in flags["conflicts"]:
                c = session.get(TimelineClip, conflict.get("clip_id"))
                if c is not None:
                    _remove(c)
                    stats["replaced"] += 1
            if clip_rec is not None:
                _remove(clip_rec)
            inserts.append((it, track))
            continue
        if clip_rec is not None:
            same_geometry = (
                clip_rec.track_id == track.id
                and clip_rec.start_frames == it["start_frames"])
            if rec["state"] == "clean" and same_geometry:
                stats["kept"] += 1
                applied_items[key] = {
                    "timeline_clip_id": clip_rec.id,
                    "track_id": clip_rec.track_id,
                    "start_frames": clip_rec.start_frames,
                    "in_frame": clip_rec.in_frame,
                    "out_frame": clip_rec.out_frame}
                continue
            if rec["state"] == "edited":
                stats["skipped"] += 1
                skip_notes.append({
                    "key": key,
                    "reason": "clip manually edited — preserved"})
                continue
            # Clean managed clip with changed geometry → reposition.
            _remove(clip_rec)
            stats["removed_clips"] += 1
        geo = _item_src_geometry(session, it)
        if geo is None:
            stats["skipped"] += 1
            skip_notes.append({"key": key,
                               "reason": "source has no usable geometry"})
            continue
        inserts.append((it, track))

    # ---------- removals before inserts ----------
    for clip in removals:
        tld.remove_clip(session, tl, clip, command_id=command_id)

    for it, track in inserts:
        geo = _item_src_geometry(session, it)
        key = it["key"]
        try:
            clip = tld.insert_clip(
                session, tl, project_id=project_id,
                asset_id=it["asset_id"], version_id=None,
                in_frame=geo[0], out_frame=geo[1],
                position=None, shot_id=it.get("shot_id"),
                scene_id=it.get("scene_id"), allow_duplicate=True,
                track_id=track.id, start_frames=it["start_frames"],
                command_id=command_id)
        except (tld.TimelineError, KeyError) as exc:
            stats["skipped"] += 1
            skip_notes.append({"key": key,
                               "reason": str(exc).strip("'")})
            continue
        stats["created_clips"] += 1
        applied_items[key] = {"timeline_clip_id": clip.id,
                              "track_id": clip.track_id,
                              "start_frames": clip.start_frames,
                              "in_frame": clip.in_frame,
                              "out_frame": clip.out_frame}

    # One undo step for the whole apply: keep the first pushed entry
    # (pre-apply revision), drop the per-mutation entries, relabel.
    stack = list(tl.undo_stack_json or [])
    if len(stack) > undo_before:
        first = dict(stack[undo_before])
        first["label"] = "Applied first-cut assembly"
        tl.undo_stack_json = stack[:undo_before] + [first]
    plan.status = "applied"
    plan.revision += 1
    plan.updated_at = utcnow()
    for key, rec in applied_items.items():
        row = prior_items.get(key)
        if row is not None:
            row.timeline_clip_id = rec["timeline_clip_id"]
            row.revision += 1
            row.updated_at = utcnow()
    session.flush()
    # Pin the exact post-apply timeline signature — _is_stale exempts
    # it so the apply's own inserts never read as drift.
    plan.applied_json = {
        "fingerprint": _sha(proposal),
        "created_track_ids": list(created_tracks.values()),
        "items": applied_items,
        "skipped": skip_notes,
        "timeline_after": sources_fingerprint(
            session, project_id, tl)["timeline"]}
    return stats


def preview_apply(session: Session, plan: AssemblyPlan, *,
                  resolve_conflicts: str = "skip") -> dict:
    """Read-only walk of the apply decision table — what the approval
    sees before committing: per-item disposition (keep/insert/skip/
    replace) plus the stale-source refusal flag. Must stay in lockstep
    with apply_plan's decision block; tests assert parity."""
    proposal = plan.plan_json or {}
    items = proposal.get("items") or []
    prior_items = {it.item_key: it for it in plan_items(session, plan.id)}
    managed = _managed_clips(session, plan)
    tl = session.get(Timeline, plan.timeline_id)
    stale = tl is not None and _is_stale(session, plan)
    dispositions: list[dict] = []
    for it in items:
        key = it["key"]
        row = prior_items.get(key)
        status = (row.review_status if row else it.get("review_status")) \
            or "pending"
        flags = it["flags"] or {}
        rec = managed.get(key)
        clip_rec = rec["clip"] if rec else None
        if status in ("rejected", "skipped"):
            action, reason = "skip", f"review:{status}"
        elif flags.get("missing"):
            action, reason = "skip", flags["missing"]
        elif flags.get("conflicts"):
            if resolve_conflicts == "skip":
                action, reason = "skip", "conflict with manual edits"
            else:
                action, reason = "replace", "manual clips removed"
        elif clip_rec is not None:
            # 'clean' already proves track/in/out match the recorded
            # apply; proposal track_id is None for apply-created lanes
            # so only the start anchors the comparison here.
            same_geometry = clip_rec.start_frames == it["start_frames"]
            if rec["state"] == "clean" and same_geometry:
                action, reason = "keep", "already applied"
            elif rec["state"] == "edited":
                action, reason = "skip", "clip manually edited — preserved"
            else:
                action, reason = "insert", "geometry changed — repositioned"
        else:
            action, reason = "insert", "new clip"
        dispositions.append({"key": key, "kind": it["kind"],
                             "title": it.get("title") or key,
                             "action": action, "reason": reason})
    return {"plan_id": plan.id, "timeline_id": plan.timeline_id,
            "sources_stale": stale,
            "resolve_conflicts": resolve_conflicts,
            "items": dispositions,
            "counts": {
                "insert": sum(1 for d in dispositions
                              if d["action"] == "insert"),
                "keep": sum(1 for d in dispositions
                            if d["action"] == "keep"),
                "skip": sum(1 for d in dispositions
                            if d["action"] == "skip"),
                "replace": sum(1 for d in dispositions
                               if d["action"] == "replace")}}


def review_items(session: Session, plan: AssemblyPlan, *,
                 item_ids: list[str] | None = None,
                 scene_id: str | None = None,
                 shot_id: str | None = None,
                 status: str, note: str | None,
                 command_id: str | None = None) -> list[str]:
    """Set review_status (+optional note) on matching items. Returns
    the touched item ids. Rejected/skipped items are not applied —
    the review vote is real."""
    if status not in REVIEW_STATUSES:
        _reject(f"status must be one of {', '.join(REVIEW_STATUSES)}")
    rows = plan_items(session, plan.id)
    touched: list[str] = []
    for row in rows:
        if item_ids is not None and row.id not in item_ids:
            continue
        if scene_id is not None and row.scene_id != scene_id:
            continue
        if shot_id is not None and row.shot_id != shot_id:
            continue
        if item_ids is None and scene_id is None and shot_id is None:
            continue  # a target is required — never blanket-update
        row.review_status = status
        if note is not None:
            row.notes = note[:NOTE_MAX]
        row.revision += 1
        row.updated_at = utcnow()
        touched.append(row.id)
    if not touched:
        _reject("no plan items matched the review target")
    session.flush()
    return touched


# ---------- serialization + history ----------

def plan_snapshot(session: Session, plan: AssemblyPlan) -> dict:
    return {"id": plan.id, "timeline_id": plan.timeline_id,
            "name": plan.name, "status": plan.status,
            "plan_json": plan.plan_json or {},
            "sources_json": plan.sources_json or {},
            "applied_json": plan.applied_json or {},
            "items": [{"id": it.id, "item_key": it.item_key,
                       "kind": it.kind, "position": it.position,
                       "scene_id": it.scene_id, "shot_id": it.shot_id,
                       "dialogue_id": it.dialogue_id,
                       "motion_clip_id": it.motion_clip_id,
                       "asset_id": it.asset_id, "track_id": it.track_id,
                       "timeline_clip_id": it.timeline_clip_id,
                       "start_frames": it.start_frames,
                       "extent_frames": it.extent_frames,
                       "review_status": it.review_status,
                       "notes": it.notes,
                       "flags_json": it.flags_json or {},
                       "source_json": it.source_json or {}}
                      for it in plan_items(session, plan.id)]}


def write_plan_revision(session: Session, plan: AssemblyPlan, *,
                        command_id: str | None, summary: str) -> EntityRevision:
    row = EntityRevision(
        project_id=plan.project_id, entity_type="assembly_plan",
        entity_id=plan.id, revision=plan.revision,
        snapshot_json=plan_snapshot(session, plan),
        summary=summary, command_id=command_id)
    session.add(row)
    session.flush()
    return row


def restore_plan(session: Session, plan: AssemblyPlan,
                 snapshot: dict) -> None:
    """Restore a plan snapshot — items keyed stable so review state
    written after the snapshot is intentionally left alone only when
    the item still exists in the snapshot's key set."""
    plan.name = snapshot.get("name") or plan.name
    plan.status = snapshot.get("status") or plan.status
    plan.plan_json = snapshot.get("plan_json") or {}
    plan.sources_json = snapshot.get("sources_json") or {}
    plan.applied_json = snapshot.get("applied_json") or {}
    snap_items = {it["item_key"]: it
                  for it in (snapshot.get("items") or [])}
    for row in plan_items(session, plan.id):
        src = snap_items.get(row.item_key)
        if src is None:
            row.flags_json = {**(row.flags_json or {}),
                              "removed": "source no longer in the cut"}
        else:
            row.position = src["position"]
            row.kind = src["kind"]
            row.scene_id = src.get("scene_id")
            row.shot_id = src.get("shot_id")
            row.dialogue_id = src.get("dialogue_id")
            row.motion_clip_id = src.get("motion_clip_id")
            row.asset_id = src.get("asset_id")
            row.track_id = src.get("track_id")
            row.timeline_clip_id = src.get("timeline_clip_id")
            row.start_frames = src["start_frames"]
            row.extent_frames = src["extent_frames"]
            row.review_status = src.get("review_status") or "pending"
            row.notes = src.get("notes") or ""
            row.flags_json = src.get("flags_json") or {}
            row.source_json = src.get("source_json") or {}
        row.revision += 1
        row.updated_at = utcnow()
    plan.revision += 1
    plan.updated_at = utcnow()
    session.flush()


def _live_item_source(session: Session, it: AssemblyItem) -> dict | None:
    """The signature compute_plan would record for this item's source
    right now — serializer-side staleness check."""
    if it.kind == "shot" and it.shot_id:
        shot = session.get(Shot, it.shot_id)
        if shot is None or shot.status != "active":
            return None
        take, _ = _take(_shot_source(session, shot)["clips"])
        return {"shot_revision": shot.revision,
                "clip_id": take.id if take else None,
                "clip_revision": take.revision if take else None,
                "result_asset_id": take.result_asset_id if take else None}
    if it.kind == "dialogue" and it.dialogue_id:
        seg = session.get(DialogueSegment, it.dialogue_id)
        if seg is None or seg.status != "active":
            return None
        return {"dialogue_id": seg.id, "revision": seg.revision,
                "result_asset_id": seg.result_asset_id,
                "timing": seg.timing_json or {}}
    if it.kind == "audio" and it.source_json.get("link_id"):
        link = session.get(EntityAssetLink, it.source_json["link_id"])
        if link is None:
            return None
        asset = session.get(Asset, link.asset_id)
        return {"link_id": link.id, "asset_id": link.asset_id,
                "asset_status": asset.status if asset else None,
                "asset_version": asset.current_version_id if asset else None}
    return None


def serialize_plan(session: Session, plan: AssemblyPlan, *,
                   detail: bool = True) -> dict:
    out = {"id": plan.id, "project_id": plan.project_id,
           "timeline_id": plan.timeline_id, "name": plan.name,
           "status": plan.status, "revision": plan.revision,
           "created_at": plan.created_at.isoformat(),
           "updated_at": plan.updated_at.isoformat()}
    if not detail:
        return out
    out["sources_stale"] = _is_stale(session, plan)
    out["proposal"] = plan.plan_json or {}
    items = []
    for it in plan_items(session, plan.id):
        live_src = _live_item_source(session, it)
        clip = (session.get(TimelineClip, it.timeline_clip_id)
                if it.timeline_clip_id else None)
        items.append({
            "id": it.id, "key": it.item_key, "kind": it.kind,
            "position": it.position,
            "scene_id": it.scene_id, "shot_id": it.shot_id,
            "dialogue_id": it.dialogue_id,
            "motion_clip_id": it.motion_clip_id,
            "asset_id": it.asset_id, "track_id": it.track_id,
            "timeline_clip_id": it.timeline_clip_id,
            "on_timeline": clip is not None,
            "start_frames": it.start_frames,
            "extent_frames": it.extent_frames,
            "review_status": it.review_status, "notes": it.notes,
            "flags": it.flags_json or {},
            "stale": live_src is not None
            and live_src != (it.source_json or {}),
            "source_gone": live_src is None})
    out["items"] = items
    out["counts"] = {"items": len(items),
                     "missing": sum(1 for i in items
                                    if i["flags"].get("missing")),
                     "conflicts": sum(1 for i in items
                                      if i["flags"].get("conflicts")),
                     "stale": sum(1 for i in items if i["stale"])}
    return out


def list_plans(session: Session, project_id: str,
               timeline_id: str | None = None) -> list[dict]:
    q = select(AssemblyPlan).where(AssemblyPlan.project_id == project_id)
    if timeline_id:
        q = q.where(AssemblyPlan.timeline_id == timeline_id)
    return [serialize_plan(session, p, detail=False)
            for p in session.execute(q).scalars().all()]
