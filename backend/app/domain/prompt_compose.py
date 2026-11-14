"""C5-C — deterministic prompt composition for shot generation.

Composes a provider prompt from the user-authored shot prompt plus
project context: character identities, reference-set guidance, scene
style, location, wardrobe, expression/emotion, action, camera framing,
lighting, and continuity notes.

Determinism contract
--------------------
- Section order is fixed (SECTION_ORDER below).
- Cast order is deterministic: the shot's own ShotCharacter rows sorted
  by (position, character_id), then scene-inherited members sorted by
  (SceneCharacter.position, character_id).
- Within each scope the FIRST non-empty field wins; a lower-scope value
  for the same semantic field produces a `conflicts` entry — it is
  surfaced, never silently merged.
- The user prompt is NEVER truncated or rewritten. If it alone exceeds
  the model budget a warning is emitted and it is still sent in full.

Budgets
-------
Model-specific prompt budgets come from the model catalog capability
``prompt_max_chars``; absent → DEFAULT_MAX_CHARS. After the user prompt
is reserved, the character pool receives CHAR_POOL_SHARE of the
remaining budget, divided equally across cast members. Sections are
filled in precedence order; a field that does not fit is omitted and
recorded in ``omitted`` — nothing is silently dropped.

The composed context is guidance for the model, never a claim of
guaranteed identity preservation, and only fields the shot/scene
actually carries are mentioned — no capability is implied that the
contract does not declare.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import (Character, Location, Scene, SceneCharacter,
                           Shot, ShotCharacter, World)

# Model-facing prompt budget when the catalog spec declares none.
DEFAULT_MAX_CHARS = 1500
# Share of the post-user budget reserved for cast blocks.
CHAR_POOL_SHARE = 0.6

# Semantic slot → section. When two scopes supply the same slot with
# different non-empty text, the higher-precedence scope wins and the
# loser is recorded as a conflict.
SECTION_ORDER = ("user", "scene", "location", "camera", "cast", "continuity")

_CONTINUITY_KEYS = ("lighting", "wardrobe", "appearance", "style", "notes")


def _t(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def _profile_text(char: Character, key: str) -> str:
    p = char.profile_json or {}
    return _t(p.get(key))


def _cast_rows(session: Session, shot: Shot,
               warnings: list[str]) -> list[dict[str, Any]]:
    """Effective cast in deterministic order: own shot rows by
    (position, character_id), then scene-inherited rows by the same
    key. Each entry carries the character row + optional shot state.
    Members whose character is missing, archived, or from another
    project are skipped with a warning — never composed silently."""
    own = session.execute(
        select(ShotCharacter).where(ShotCharacter.shot_id == shot.id)
    ).scalars().all()
    own_ids = {r.character_id for r in own}
    inherited = session.execute(
        select(SceneCharacter).where(SceneCharacter.scene_id == shot.scene_id)
    ).scalars().all()
    members: list[dict[str, Any]] = []

    def _add(r, shot_row, scene_row):
        c = session.get(Character, r.character_id)
        if c is None or c.project_id != shot.project_id:
            warnings.append(
                "a cast member references an unavailable character — "
                "skipped in prompt composition")
            return
        if c.status != "active":
            warnings.append(
                f"{_t(c.name) or 'A character'} is archived — its "
                "guidance was skipped")
            return
        members.append({"character": c, "shot": shot_row,
                        "scene": scene_row})

    for r in sorted(own, key=lambda r: (r.position, r.character_id)):
        _add(r, r, None)
    for r in sorted(inherited, key=lambda r: (r.position, r.character_id)):
        if r.character_id not in own_ids:
            _add(r, None, r)
    return members


def _location_text(session: Session, scene: Scene | None,
                   shot: Shot) -> str:
    loc_id = shot.location_id or (scene.location_id if scene else None)
    loc = session.get(Location, loc_id) if loc_id else None
    if loc is None:
        return ""
    parts = [_t(loc.name), _t(loc.summary)]
    mood = _t((loc.details_json or {}).get("mood"))
    if mood:
        parts.append(f"{mood} mood")
    return "; ".join(p for p in parts if p)


def _scene_text(session: Session, scene: Scene | None) -> str:
    if scene is None:
        return ""
    parts = [_t(scene.intent) or _t(scene.description)]
    if scene.world_id:
        w = session.get(World, scene.world_id)
        if w is not None:
            lore = w.lore_json or {}
            for k in ("aesthetic", "tone"):
                if _t(lore.get(k)):
                    parts.append(_t(lore[k]))
            if not parts[0] and _t(w.summary):
                parts.insert(0, _t(w.summary))
    return "; ".join(p for p in parts if p)


def _camera_text(shot: Shot, cont: dict) -> tuple[str, dict[str, str]]:
    """Camera framing + lighting. Returns (text, used) — used records
    which continuity keys were consumed so the trailing continuity
    section does not repeat them."""
    used: dict[str, str] = {}
    parts = []
    if _t(shot.shot_type) and shot.shot_type != "medium":
        parts.append(f"{shot.shot_type} shot")
    lighting = _t(cont.get("lighting"))
    if lighting:
        parts.append(f"{lighting} lighting")
        used["lighting"] = lighting
    return ", ".join(parts), used


def _character_block(member: dict[str, Any], refset) -> dict[str, Any]:
    """One cast member's guidance fields, in fixed sub-order. Only
    non-empty fields appear; every field is individually omittable."""
    c: Character = member["character"]
    sc: ShotCharacter | None = member["shot"]
    fields: list[tuple[str, str]] = []
    ident = "; ".join(t for t in (
        _t(c.name), _profile_text(c, "appearance")) if t)
    if ident:
        fields.append(("identity", ident))
    if refset is not None and refset.character_id == c.id:
        notes = _t(refset.notes)
        if notes:
            fields.append(("reference guidance", notes))
    if sc is not None:
        for label, v in (
            ("wardrobe", sc.wardrobe_variant),
            ("expression", sc.expression),
            ("emotion", sc.emotion),
            ("action", sc.action_instructions),
            ("blocking", sc.blocking),
        ):
            if _t(v):
                fields.append((label, _t(v)))
        cont = sc.continuity_json or {}
        if _t(cont.get("notes")):
            fields.append(("continuity", _t(cont["notes"])))
    return {"name": _t(c.name), "fields": fields}


def compose_shot_prompt(session: Session, project, shot: Shot, *,
                        user_prompt: str, refset=None,
                        model_max_chars: int | None = None) -> dict[str, Any]:
    """Compose the provider prompt for a shot generation call.

    ``refset`` is the already-validated ReferenceSet bound to this
    generation (or None). Returns a dict with the final ``prompt``, the
    verbatim ``user_prompt``, structured ``sections`` (for provenance
    and the UI preview), ``conflicts``/``warnings``/``omitted`` lists,
    ``budget`` accounting, and ``prompt_sha256`` — the fingerprint an
    approval pins via expected_prompt.
    """
    limit = model_max_chars or DEFAULT_MAX_CHARS
    user_prompt = _t(user_prompt)
    scene = session.get(Scene, shot.scene_id)
    shot_cont = dict(shot.continuity_json or {})
    scene_cont = dict(scene.continuity_json or {}) if scene else {}

    warnings: list[str] = []
    conflicts: list[str] = []
    omitted: list[str] = []
    sections: list[dict[str, Any]] = []

    # ---- conflict detection across scopes --------------------------
    # Same semantic key, different non-empty values, different scopes:
    # shot-local wins, the loser is surfaced.
    for key in _CONTINUITY_KEYS:
        sv, cv = _t(shot_cont.get(key)), _t(scene_cont.get(key))
        if sv and cv and sv != cv:
            conflicts.append(
                f"{key}: shot says '{sv[:60]}', scene says '{cv[:60]}' "
                "— using the shot value")
            scene_cont.pop(key, None)
    members = _cast_rows(session, shot, warnings)
    for m in members:
        sc = m["shot"]
        if sc is None:
            continue
        cc = sc.continuity_json or {}
        if _t(sc.wardrobe_variant) and _t(cc.get("wardrobe")) \
                and _t(sc.wardrobe_variant) != _t(cc["wardrobe"]):
            conflicts.append(
                f"{_t(m['character'].name) or 'Character'} wardrobe: "
                f"shot '{sc.wardrobe_variant[:50]}' "
                f"vs continuity '{_t(cc['wardrobe'])[:50]}' — using shot")

    # ---- build section payloads in precedence order ----------------
    cam_text, cam_used = _camera_text(shot, shot_cont)
    cont_parts = []
    for key in _CONTINUITY_KEYS:
        v = _t(shot_cont.get(key)) or _t(scene_cont.get(key))
        if v and key not in cam_used:
            cont_parts.append(f"{key}: {v}")

    cast_blocks = []
    for m in members:
        block = _character_block(m, refset)
        if block["fields"]:
            cast_blocks.append(block)

    # ---- budget ----------------------------------------------------
    used = len(user_prompt)
    budget = {"limit": limit, "used": used, "user": len(user_prompt)}
    user_over_budget = len(user_prompt) > limit
    if user_over_budget:
        warnings.append(
            f"prompt is {len(user_prompt)} chars — over the model's "
            f"{limit}-char budget; it will be sent in full but the model "
            "may ignore trailing context")
    pool = max(0, limit - used)
    char_pool = int(pool * CHAR_POOL_SHARE)
    fixed_pool = pool - char_pool

    def fits(text: str, spend_from: str) -> bool:
        nonlocal used
        extra = len(text) + (2 if text else 0)  # separator
        if used + extra <= limit:
            used += extra
            return True
        return False

    fixed_used = 0
    def fits_fixed(label: str, text: str) -> bool:
        nonlocal fixed_used
        if not text:
            return False
        if fixed_used + len(text) <= fixed_pool and fits(text, "fixed"):
            fixed_used += len(text)
            return True
        omitted.append(label)
        return False

    scene_text = _scene_text(session, scene)
    if fits_fixed("scene style", scene_text):
        sections.append({"id": "scene", "text": scene_text})
    loc_text = _location_text(session, scene, shot)
    if fits_fixed("location", loc_text):
        sections.append({"id": "location", "text": loc_text})
    if fits_fixed("camera/lighting", cam_text):
        sections.append({"id": "camera", "text": cam_text})

    # Per-character share — equal slice of the cast pool so no single
    # member starves the others.
    per_char = (char_pool // len(cast_blocks)) if cast_blocks else 0
    for block in cast_blocks:
        spent = 0
        kept = []
        for label, text in block["fields"]:
            extra = len(text) + 2
            if spent + extra <= per_char and used + extra <= limit:
                spent += extra
                used += extra
                kept.append({"label": label, "text": text})
            else:
                omitted.append(f"{block['name']}: {label}")
        if kept:
            sections.append({"id": "cast", "character": block["name"],
                             "fields": kept})
        else:
            warnings.append(
                f"{block['name']}: no guidance fit the prompt budget")

    cont_text = "; ".join(cont_parts)
    if cont_text:
        if fits(cont_text, "fixed"):
            sections.append({"id": "continuity", "text": cont_text})
        else:
            omitted.append("continuity notes")

    if omitted:
        warnings.append(
            f"{len(omitted)} context item(s) omitted — prompt budget "
            "reached")

    context_parts = []
    for s in sections:
        if s["id"] == "cast":
            context_parts.append(
                f"{s['character']}: " + "; ".join(
                    f["text"] for f in s["fields"]))
        else:
            context_parts.append(s["text"])
    context_text = ". ".join(context_parts)
    prompt = (user_prompt + ". " + context_text).strip(". ") \
        if context_text else user_prompt
    budget["used"] = used

    out = {
        "prompt": prompt,
        "user_prompt": user_prompt,
        "context_text": context_text,
        "sections": sections,
        "conflicts": conflicts,
        "warnings": warnings,
        "omitted": omitted,
        "budget": budget,
        "user_over_budget": user_over_budget,
    }
    out["prompt_sha256"] = hashlib.sha256(
        prompt.encode("utf-8")).hexdigest()
    return out
