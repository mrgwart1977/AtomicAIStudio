"""Versioned shot-cast state schema (M2.1).

The per-character shot state is stored across typed shot_characters columns;
the *wire* form is the `state` object inside shot.cast.* command payloads.
This module is the single parser for that object:

- known fields are type-checked with clear per-field messages,
- legacy aliases (`action`, `voice`, `lora_refs`, `continuity_notes`, …)
  are normalised onto the canonical names,
- keys this build doesn't model are preserved in `ext` (forward compat —
  a newer client's fields survive a round-trip through an older build),
- `schema_version` is honoured: missing ⇒ legacy v0 (normalised the same
  way); a newer integer version parses tolerantly (known fields apply,
  everything else lands in `ext`); a non-integer version is a 422.

Storage mapping (shot_characters columns):
    wardrobe_variant, blocking, expression, emotion, action_instructions,
    dialogue, voice_override, model_ref  → typed columns
    loras                                → lora_json
    continuity                           → continuity_json
    reference_asset_ids                  → reference_ids_json (project-scoped
                                           asset IDs, validated in commands)
    everything else                      → ext_json
"""

from __future__ import annotations

from typing import Any

CAST_STATE_VERSION = 1

# Canonical string fields → shot_characters columns.
TEXT_FIELDS = (
    "wardrobe_variant", "blocking", "expression", "emotion",
    "action_instructions", "dialogue", "voice_override", "model_ref",
)

# Older wire names → canonical names (pre-schema writers).
ALIASES = {
    "action": "action_instructions",
    "performance": "action_instructions",
    "voice": "voice_override",
    "lora_refs": "loras",
    "lora_json": "loras",
    "continuity_json": "continuity",
    "reference_ids": "reference_asset_ids",
    "reference_ids_json": "reference_asset_ids",
}

# Legacy keys that merge INTO the continuity dict rather than a column.
CONTINUITY_LEGACY = ("continuity_notes", "continuity_changes")

ALL_KNOWN = set(TEXT_FIELDS) | {
    "schema_version", "loras", "continuity", "reference_asset_ids", "position",
} | set(ALIASES) | set(CONTINUITY_LEGACY)


class CastStateError(ValueError):
    """Raised for malformed cast state; carries a beginner-readable message."""


def _err(field: str, want: str, got: Any) -> CastStateError:
    return CastStateError(
        f"cast.state.{field}: expected {want}, got {type(got).__name__} "
        f"({got!r:.80})"
    )


def _text(field: str, value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _err(field, "a string", value)
    return value


def _str_list(field: str, value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _err(field, "a list of strings", value)
    out = []
    for i, x in enumerate(value):
        if not isinstance(x, str):
            raise _err(f"{field}[{i}]", "a string", x)
        out.append(x)
    return out


def parse_shot_cast_state(raw: Any, *, partial: bool = False) -> dict[str, Any]:
    """Validate + normalise a shot.cast.* `state` object.

    Returns {"fields": {col: str}, "loras": [...], "continuity": {...},
             "reference_asset_ids": [...], "ext": {...}, "position": int|None}.
    With partial=True only provided keys appear in `fields` (for update, where
    an absent key must not clobber the stored value).
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise CastStateError(
            f"cast.state: expected an object, got {type(raw).__name__}")

    # schema_version: absent ⇒ legacy v0; any int is accepted tolerantly;
    # anything else is a client bug worth rejecting loudly.
    ver = raw.get("schema_version", CAST_STATE_VERSION)
    if not isinstance(ver, int) or isinstance(ver, bool) or ver < 0:
        raise _err("schema_version", "a non-negative integer", ver)

    # Canonicalise aliases up-front (explicit canonical key wins).
    src = dict(raw)
    for alias, canonical in ALIASES.items():
        if alias in src and canonical not in src:
            src[canonical] = src.pop(alias)

    fields: dict[str, str] = {}
    for f in TEXT_FIELDS:
        if f in src:
            fields[f] = _text(f, src[f])
        elif not partial:
            fields[f] = ""

    loras: list[str] | None = None
    if "loras" in src or not partial:
        loras = _str_list("loras", src.get("loras"))

    continuity: dict[str, Any] = {}
    if "continuity" in src:
        c = src["continuity"]
        if not isinstance(c, dict):
            raise _err("continuity", "an object", c)
        continuity = dict(c)
    for k in CONTINUITY_LEGACY:
        if k in src:
            v = _text(k, src[k])
            # continuity_notes → continuity.notes (canonical shorthand)
            target = "notes" if k == "continuity_notes" else "changes"
            if v:
                continuity[target] = v

    refs: list[str] | None = None
    if "reference_asset_ids" in src or not partial:
        refs = _str_list("reference_asset_ids", src.get("reference_asset_ids"))

    position = None
    if "position" in src:
        p = src["position"]
        if p is not None:
            if not isinstance(p, int) or isinstance(p, bool):
                raise _err("position", "an integer", p)
            position = p

    ext = {k: v for k, v in src.items() if k not in ALL_KNOWN}

    return {
        "fields": fields,
        "loras": loras,
        "continuity": continuity,
        "reference_asset_ids": refs,
        "position": position,
        "ext": ext,
    }


def parse_scene_cast_fields(role: Any = None, notes: Any = None) -> None:
    """Validate the scene.cast.* wire fields (role + notes object)."""
    if role is not None and not isinstance(role, str):
        raise CastStateError(
            f"cast role: expected a string, got {type(role).__name__} ({role!r:.80})")
    if notes is not None and not isinstance(notes, dict):
        raise CastStateError(
            f"cast notes: expected an object, got {type(notes).__name__}")
