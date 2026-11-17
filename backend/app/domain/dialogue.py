"""A5 — dialogue segments: speakable beats bound to screenplay
scenes/elements and storyboard scenes/shots with a Character speaker.

A segment joins optional screenplay-element/storyboard refs, the
spoken text + delivery direction, voice-resolution overrides layered
on the speaker's Character ``profile_json['voice']`` (the identity of
record — this module never duplicates voice identity), language +
pronunciation metadata, planned rational timing, generation settings,
and a mock-produced audio result asset placeable on an audio timeline
lane. Every mutation flows through the command system.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Asset,
    Character,
    DialogueSegment,
    EntityRevision,
    GenerationJob,
    Scene,
    ScreenplayElement,
    ScreenplayScene,
    Shot,
    Timeline,
    new_id,
    utcnow,
)

TEXT_MAX = 4000
DIRECTION_MAX = 500
_VOICE_FIELDS = ("profile_override", "style", "delivery", "emotion")
_HINTS_MAX = 64
_LANG_MAX = 20
MAX_DURATION_S = 600.0


def _reject(msg: str) -> None:
    raise ValueError(msg)


def _str(v: Any, field: str, max_len: int) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str):
        _reject(f"{field} must be a string")
    return v.strip()[:max_len] or None


def parse_voice(value: Any) -> dict[str, Any]:
    """{profile_override, style, delivery, emotion} — optional
    per-segment overrides layered on the speaker's character voice
    profile. Unknown keys dropped; explicit null clears."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        _reject("voice must be an object")
    out: dict[str, Any] = {}
    for key in _VOICE_FIELDS:
        if key not in value:
            continue
        out[key] = _str(value[key], f"voice.{key}", 200)
    return out


def parse_pronunciation(value: Any) -> dict[str, Any]:
    """{hints: [{word, say_as}]} — verbatim pronunciation metadata;
    stored, never interpreted by the mock."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        _reject("pronunciation must be an object")
    hints = value.get("hints")
    if hints is None:
        return {}
    if not isinstance(hints, list) or len(hints) > _HINTS_MAX:
        _reject(f"pronunciation.hints must be a list ≤ {_HINTS_MAX}")
    out = []
    for i, h in enumerate(hints):
        if not isinstance(h, dict):
            _reject(f"pronunciation.hints[{i}] must be an object")
        w = _str(h.get("word"), f"pronunciation.hints[{i}].word", 120)
        sa = _str(h.get("say_as"), f"pronunciation.hints[{i}].say_as",
                  240)
        if w is None or sa is None:
            _reject(f"pronunciation.hints[{i}] needs word + say_as")
        out.append({"word": w, "say_as": sa})
    return {"hints": out}


def parse_language(value: Any) -> str:
    if value in (None, ""):
        return "en"
    if not isinstance(value, str) or len(value.strip()) > _LANG_MAX \
            or not value.strip():
        _reject(f"language must be a string ≤ {_LANG_MAX} chars")
    return value.strip()


def parse_timing(s: Session, project_id: str, value: Any) -> dict[str, Any]:
    """{start_frames, fps_num, fps_den, timeline_id, duration_s} —
    planned rational timing. start_frames is an integer at the stated
    rational rate (the timeline's own unit contract); the TimelineClip
    row remains the actual placement record."""
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        _reject("timing must be an object")
    out: dict[str, Any] = {}
    sf = value.get("start_frames")
    if sf is not None:
        if not isinstance(sf, int) or isinstance(sf, bool) or sf < 0:
            _reject("timing.start_frames must be an integer ≥ 0")
        out["start_frames"] = sf
    fps = value.get("fps")
    if fps is not None:
        if not isinstance(fps, dict):
            _reject("timing.fps must be an object {num, den}")
        num, den = fps.get("num"), fps.get("den")
        if not isinstance(num, int) or not isinstance(den, int) \
                or isinstance(num, bool) or isinstance(den, bool) \
                or num < 1 or den < 1:
            _reject("timing.fps needs positive integer num/den")
        if num / den > 480:
            _reject("timing fps must be at most 480")
        out["fps_num"], out["fps_den"] = num, den
    dur = value.get("duration_s")
    if dur is not None:
        if not isinstance(dur, (int, float)) or isinstance(dur, bool) \
                or dur <= 0 or dur > MAX_DURATION_S:
            _reject(f"timing.duration_s must be in (0, {MAX_DURATION_S}]")
        out["duration_s"] = float(dur)
    tid = value.get("timeline_id")
    if tid is not None:
        tl = s.get(Timeline, tid)
        if tl is None or tl.project_id != project_id:
            _reject("timing.timeline_id: timeline not in project")
        out["timeline_id"] = tl.id
    return out


def _get(s: Session, project_id: str, seg_id: str) -> DialogueSegment:
    d = s.get(DialogueSegment, seg_id)
    if d is None or d.project_id != project_id:
        _reject("dialogue segment not found")
    return d


def _link(s: Session, project_id: str, model, link_id: Any,
          field: str, *, scene_id: str | None = None) -> str | None:
    """Every optional ref must resolve inside the same project — never
    a cross-project link. Elements must belong to the bound sp_scene;
    shots must live on the bound storyboard scene when both are set."""
    if link_id in (None, ""):
        return None
    row = s.get(model, link_id)
    if row is None or row.project_id != project_id:
        _reject(f"{field}: not in project")
    if model is Shot and scene_id is not None \
            and row.scene_id != scene_id:
        _reject("shot_id: shot belongs to a different scene")
    return row.id


def _element(s: Session, project_id: str, sp_scene_id: str | None,
             elem_id: Any) -> str | None:
    if elem_id in (None, ""):
        return None
    el = s.get(ScreenplayElement, elem_id)
    if el is None or el.project_id != project_id:
        _reject("element_id: not in project")
    if el.kind != "dialogue":
        _reject("element_id: only dialogue elements may be bound")
    if sp_scene_id is not None \
            and el.screenplay_scene_id != sp_scene_id:
        _reject("element_id: element belongs to a different scene")
    return el.id


def _cols(row) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        val = getattr(row, col.name)
        if hasattr(val, "isoformat"):
            val = val.isoformat()
        out[col.name] = val
    return out


def segment_snapshot(d: DialogueSegment) -> dict:
    return _cols(d)


def write_segment_revision(s: Session, d: DialogueSegment, *,
                           command_id: str | None,
                           summary: str) -> EntityRevision:
    row = EntityRevision(
        id=new_id(), project_id=d.project_id,
        entity_type="dialogue_segment", entity_id=d.id,
        revision=d.revision, snapshot_json=segment_snapshot(d),
        summary=summary, command_id=command_id)
    s.add(row)
    return row


def _validate_links(s: Session, project_id: str, payload: dict) -> dict:
    """Resolve every optional ref; at least one anchor
    (sp_scene/scene/shot) is required so the segment lives somewhere
    the UI can list it."""
    out = {
        "sp_scene_id": _link(s, project_id, ScreenplayScene,
                             payload.get("sp_scene_id"), "sp_scene_id"),
        "scene_id": _link(s, project_id, Scene,
                          payload.get("scene_id"), "scene_id"),
    }
    out["shot_id"] = _link(s, project_id, Shot,
                           payload.get("shot_id"), "shot_id",
                           scene_id=out["scene_id"])
    out["element_id"] = _element(s, project_id, out["sp_scene_id"],
                                 payload.get("element_id"))
    out["character_id"] = _link(s, project_id, Character,
                                payload.get("character_id"),
                                "character_id")
    if not any(out[k] for k in ("sp_scene_id", "scene_id", "shot_id")):
        _reject("a segment needs an anchor — sp_scene_id, scene_id, "
                "or shot_id")
    return out


def create_segment(s: Session, project_id: str, payload: dict,
                   command_id: str | None = None) -> DialogueSegment:
    links = _validate_links(s, project_id, payload)
    text = (payload.get("text") or "").strip()
    if len(text) > TEXT_MAX:
        _reject(f"text exceeds {TEXT_MAX} chars")
    direction = (payload.get("direction") or "").strip()
    if len(direction) > DIRECTION_MAX:
        _reject(f"direction exceeds {DIRECTION_MAX} chars")
    seed = payload.get("seed")
    if seed is not None and (not isinstance(seed, int)
                             or isinstance(seed, bool) or seed < 0):
        _reject("seed must be an integer ≥ 0")
    scope = (links["sp_scene_id"] or links["shot_id"]
             or links["scene_id"])
    d = DialogueSegment(
        id=new_id(), project_id=project_id,
        sp_scene_id=links["sp_scene_id"],
        element_id=links["element_id"],
        scene_id=links["scene_id"], shot_id=links["shot_id"],
        character_id=links["character_id"],
        position=(s.execute(select(func.max(DialogueSegment.position))
                            .where(DialogueSegment.project_id
                                   == project_id,
                                   DialogueSegment.sp_scene_id
                                   == links["sp_scene_id"],
                                   DialogueSegment.shot_id
                                   == links["shot_id"],
                                   DialogueSegment.scene_id
                                   == links["scene_id"]))
                  .scalar_one() or -1) + 1 if scope else 0,
        text=text, direction=direction,
        voice_json={k: v for k, v in parse_voice(
            payload.get("voice")).items() if v is not None},
        language=parse_language(payload.get("language")),
        pronunciation_json=parse_pronunciation(
            payload.get("pronunciation")),
        timing_json=parse_timing(s, project_id, payload.get("timing")),
        seed=seed,
        model_key=(payload.get("model_key") or "")[:200] or None,
        settings_json=dict(payload.get("settings") or {}),
        status="active", revision=0,
    )
    s.add(d)
    s.flush()
    write_segment_revision(s, d, command_id=command_id,
                           summary="Dialogue segment created")
    return d


def update_segment(s: Session, project_id: str, payload: dict,
                   command_id: str | None = None) -> DialogueSegment:
    d = _get(s, project_id, payload["segment_id"])
    if d.status != "active":
        _reject("dialogue segment is archived")
    if any(k in payload for k in
           ("sp_scene_id", "element_id", "scene_id", "shot_id")):
        merged = {
            "sp_scene_id": payload.get("sp_scene_id", d.sp_scene_id),
            "element_id": payload.get("element_id", d.element_id),
            "scene_id": payload.get("scene_id", d.scene_id),
            "shot_id": payload.get("shot_id", d.shot_id),
            "character_id": d.character_id,
        }
        links = _validate_links(s, project_id, merged)
        d.sp_scene_id = links["sp_scene_id"]
        d.element_id = links["element_id"]
        d.scene_id = links["scene_id"]
        d.shot_id = links["shot_id"]
    if "character_id" in payload:
        d.character_id = _link(s, project_id, Character,
                               payload["character_id"], "character_id")
    if "text" in payload:
        t = (payload["text"] or "").strip()
        if len(t) > TEXT_MAX:
            _reject(f"text exceeds {TEXT_MAX} chars")
        d.text = t
    if "direction" in payload:
        dr = (payload["direction"] or "").strip()
        if len(dr) > DIRECTION_MAX:
            _reject(f"direction exceeds {DIRECTION_MAX} chars")
        d.direction = dr
    if "voice" in payload:
        merged = dict(d.voice_json or {})
        for k, v in parse_voice(payload["voice"]).items():
            if v is None:
                merged.pop(k, None)
            else:
                merged[k] = v
        d.voice_json = merged
    if "language" in payload:
        d.language = parse_language(payload["language"])
    if "pronunciation" in payload:
        d.pronunciation_json = parse_pronunciation(
            payload["pronunciation"])
    if "timing" in payload:
        merged = dict(d.timing_json or {})
        for k, v in parse_timing(s, project_id,
                                 payload["timing"]).items():
            merged[k] = v
        d.timing_json = merged
    if "seed" in payload:
        sd = payload["seed"]
        if sd is not None and (not isinstance(sd, int)
                               or isinstance(sd, bool) or sd < 0):
            _reject("seed must be an integer ≥ 0")
        d.seed = sd
    if "model_key" in payload:
        d.model_key = (payload["model_key"] or "")[:200] or None
    if "settings" in payload:
        d.settings_json = dict(payload["settings"] or {})
    d.revision += 1
    d.updated_at = utcnow()
    write_segment_revision(s, d, command_id=command_id,
                           summary="Dialogue segment updated")
    return d


def archive_segment(s: Session, project_id: str, payload: dict,
                    archive: bool = True,
                    command_id: str | None = None) -> DialogueSegment:
    d = _get(s, project_id, payload["segment_id"])
    want = "archived" if archive else "active"
    if d.status == want:
        _reject(f"dialogue segment already {want}")
    d.status = want
    d.revision += 1
    d.updated_at = utcnow()
    write_segment_revision(s, d, command_id=command_id,
                           summary=f"Dialogue segment {want}")
    return d


def resolved_voice(s: Session, d: DialogueSegment) -> dict:
    """The speaker's character voice profile (identity of record)
    merged under the segment's own overrides. ``missing`` flags let
    the UI show a clear no-voice indicator instead of guessing."""
    voice: dict[str, Any] = {}
    if d.character_id:
        ch = s.get(Character, d.character_id)
        if ch is not None:
            prof = ch.profile_json or {}
            base = prof.get("voice")
            if isinstance(base, dict):
                voice.update(base)
            elif isinstance(base, str) and base.strip():
                voice["profile"] = base.strip()
    for k, v in (d.voice_json or {}).items():
        if v is not None:
            voice[k] = v
    voice["missing"] = not any(
        v for k, v in voice.items() if k != "missing")
    return voice


def segment_fingerprint(d: DialogueSegment) -> dict:
    """Generation-relevant authored fields — identical key set to the
    approval pin's fingerprint minus ``segment_id``/``revision``, so a
    revision-only bump (result attach, enqueue bookkeeping) never
    flags a result stale while a text/voice/timing/settings edit does."""
    return {
        "text": d.text,
        "direction": d.direction,
        "voice": d.voice_json or {},
        "language": d.language,
        "character_id": d.character_id,
        "timing": d.timing_json or {},
        "settings": d.settings_json or {},
    }


def sources_fingerprint(s: Session, d: DialogueSegment) -> dict:
    """A5 — publish-state pin for the segment's source refs: a
    mid-flight speaker/voice/element/scene change alters what this
    generation was approved against, so the produced audio is stale."""
    out: dict[str, Any] = {}
    ch = s.get(Character, d.character_id) if d.character_id else None
    out["character"] = (None if ch is None else {
        "revision": ch.revision, "status": ch.status,
        "voice_sha": hashlib.sha256(json.dumps(
            (ch.profile_json or {}).get("voice"),
            sort_keys=True).encode()).hexdigest()})
    el = s.get(ScreenplayElement, d.element_id) if d.element_id else None
    out["element"] = (None if el is None else {
        "text_sha": hashlib.sha256(el.text.encode()).hexdigest(),
        "parenthetical_sha": hashlib.sha256(
            el.parenthetical.encode()).hexdigest(),
        "character_id": el.character_id})
    sp = (s.get(ScreenplayScene, d.sp_scene_id)
          if d.sp_scene_id else None)
    out["sp_scene"] = (None if sp is None else {
        "revision": sp.revision, "scene_id": sp.scene_id})
    shot = s.get(Shot, d.shot_id) if d.shot_id else None
    out["shot"] = (None if shot is None else {
        "revision": shot.revision, "status": shot.status})
    return out


def check_result_fresh(s: Session, d: DialogueSegment,
                       payload: dict) -> None:
    """A5 — same stale-result contract as keyframes/clips: only the
    segment's current generation, on an unchanged segment with
    unchanged speaker/element/scene sources, may attach a result.
    Never a silent overwrite."""
    if d.status != "active":
        _reject("dialogue segment is archived")
    jid = payload.get("job_id")
    if jid is not None and d.generate_job_id != jid:
        _reject("result job is not the segment's current generation"
                " — superseded")
    erev = payload.get("expected_segment_revision")
    if erev is not None and erev != d.revision:
        _reject("dialogue segment changed since generation"
                " — result is stale")
    esrc = payload.get("expected_sources")
    if esrc is not None and sources_fingerprint(s, d) != esrc:
        _reject("dialogue sources changed since generation"
                " — result is stale")


def apply_result(s: Session, project_id: str, payload: dict,
                 command_id: str | None = None) -> DialogueSegment:
    d = _get(s, project_id, payload["segment_id"])
    check_result_fresh(s, d, payload)
    asset_id = payload.get("result_asset_id")
    if asset_id is not None:
        asset = s.get(Asset, asset_id)
        if asset is None or asset.project_id != project_id:
            _reject("result asset not in project")
        d.result_asset_id = asset_id
    d.revision += 1
    d.updated_at = utcnow()
    write_segment_revision(s, d, command_id=command_id,
                           summary="Dialogue result attached")
    return d


def revert_segment(s: Session, project_id: str, seg_id: str,
                   snapshot: dict) -> DialogueSegment:
    d = _get(s, project_id, seg_id)
    for col in ("position", "sp_scene_id", "element_id", "scene_id",
                "shot_id", "character_id", "text", "direction",
                "voice_json", "language", "pronunciation_json",
                "timing_json", "seed", "model_key", "settings_json",
                "result_asset_id", "status"):
        if col in snapshot:
            setattr(d, col, snapshot[col])
    d.revision += 1
    d.updated_at = utcnow()
    return d


def list_segments(s: Session, project_id: str, *,
                  sp_scene_id: str | None = None,
                  scene_id: str | None = None,
                  shot_id: str | None = None) -> list[DialogueSegment]:
    q = select(DialogueSegment).where(
        DialogueSegment.project_id == project_id)
    if sp_scene_id is not None:
        q = q.where(DialogueSegment.sp_scene_id == sp_scene_id)
    if scene_id is not None:
        q = q.where(DialogueSegment.scene_id == scene_id)
    if shot_id is not None:
        q = q.where(DialogueSegment.shot_id == shot_id)
    return list(s.execute(
        q.order_by(DialogueSegment.position, DialogueSegment.created_at)
    ).scalars())


def serialize_segment(s: Session, d: DialogueSegment) -> dict:
    out = segment_snapshot(d)
    out.pop("project_id", None)
    out["speaker_name"] = None
    if d.character_id:
        ch = s.get(Character, d.character_id)
        if ch is not None:
            out["speaker_name"] = ch.name
    out["resolved_voice"] = resolved_voice(s, d)
    out["result_asset_name"] = None
    out["result_asset_status"] = None
    if d.result_asset_id:
        asset = s.get(Asset, d.result_asset_id)
        if asset is not None:
            out["result_asset_name"] = asset.name
            out["result_asset_status"] = asset.status
    out["job_status"] = None
    out["result_stale"] = False
    if d.generate_job_id:
        job = s.get(GenerationJob, d.generate_job_id)
        if job is not None:
            out["job_status"] = job.status
            out["job_progress"] = job.progress
            if job.status == "succeeded" and job.output_asset_id \
                    and d.generate_job_id == job.id \
                    and d.result_asset_id != job.output_asset_id:
                d.result_asset_id = job.output_asset_id
            # F2 — the audio was approved against this exact source
            # fingerprint + segment fingerprint; when the speaker/
            # voice/element/scene or the authored fields drift
            # afterwards the result is flagged stale, never silently
            # replaced or deleted.
            # The snapshot (out) still holds the STORED result id —
            # check staleness against it, not the transient reconcile.
            if out.get("result_asset_id"):
                req = job.request_json or {}
                pinned = req.get("expected_sources")
                if pinned is not None:
                    out["result_stale"] = \
                        sources_fingerprint(s, d) != pinned
                exp = req.get("expected_dialogue")
                if not out["result_stale"] and \
                        isinstance(exp, dict):
                    live = {k: v for k, v in exp.items()
                            if k not in ("segment_id", "revision")}
                    out["result_stale"] = \
                        live != segment_fingerprint(d)
    return out


def est_duration_s(text: str, direction: str = "") -> float:
    """Deterministic predicted duration the UI, preview, and the mock
    adapter all agree on: 0.34 s/word + 0.12 s per punctuation pause,
    with direction modifiers — never measured, never a guess that can
    drift between preview and commit."""
    words = [w for w in (text or "").split() if w.strip()]
    base = len(words) * 0.34
    pauses = sum(0.12 for ch in (text or "") if ch in ",;:")
    pauses += sum(0.22 for ch in (text or "") if ch in ".!?…")
    mult = 1.0
    dl = (direction or "").lower()
    if any(k in dl for k in ("slow", "deliberate", "whisper")):
        mult = 1.35
    elif any(k in dl for k in ("fast", "urgent", "rushed")):
        mult = 0.75
    return round(max(0.5, base + pauses) * mult + 0.4, 3)


def segment_history(s: Session, project_id: str,
                    seg_id: str) -> list[dict]:
    _get(s, project_id, seg_id)
    rows = s.execute(
        select(EntityRevision)
        .where(EntityRevision.entity_type == "dialogue_segment",
               EntityRevision.entity_id == seg_id,
               EntityRevision.project_id == project_id)
        .order_by(EntityRevision.revision.desc())
    ).scalars()
    return [{"revision": r.revision, "action": r.action,
             "summary": r.summary, "created_at": r.created_at.isoformat()}
            for r in rows]
