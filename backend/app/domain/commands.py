"""Unified Creative Command System — M1.

One canonical mutation pathway for every interface. Each change is
validated into a dry-run proposal (human-readable diff, affected IDs,
capability warnings), committed transactionally with optimistic
concurrency and an idempotency key, revisioned, audited, and — where
the change is safely reversible — undoable. Generation-triggering and
file-moving commands enqueue durable jobs only at commit, never at
preview.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (
    Asset,
    AssetVersion,
    AuditEvent,
    CommandTransaction,
    CreativeCommand,
    GenerationJob,
    Project,
    ProjectRevision,
    PromptDraft,
    SharedAssetLink,
    SpendReservation,
    StorageLocation,
    new_id,
    utcnow,
)
from app.domain import bibles
from app.domain import storyboard as sb

SUPPORTED_COMMANDS = {
    "prompt.set_text",
    # projects
    "project.rename",
    "project.archive",
    "project.restore",
    "project.update_settings",
    "project.duplicate",
    "project.delete",
    # generation (mock + real local inference since M3)
    "generate.image.mock", "generate.image", "generate.image.persistent",
    "generate.video", "generate.text.cloud", "generate.image.cloud",
    "generate.image.img2img", "generate.image.edit",
    # bibles
    "character.create", "character.update", "character.archive", "character.unarchive", "character.revert",
    "world.create", "world.update", "world.archive", "world.unarchive", "world.revert",
    "location.create", "location.update", "location.archive", "location.unarchive", "location.revert",
    # assets / jobs
    "asset.import", "asset.share", "asset.link", "asset.relink", "assets.verify",
    # C1.5 access bridge — user-driven external dispatch only
    "bridge.prepare", "bridge.launch", "bridge.import",
    "bridge.watch.scan", "bridge.watch.enable", "bridge.watch.disable",
    "bridge.cli.detect",
    # backups
    "backup.export", "backup.import", "backup.restore",
    # C2.0b spend ledger — budget config + explicit reconciliation
    "spend.budget_set", "spend.budget_remove", "spend.reconcile",
    # C2 exit gate — Stage A live-connectivity safety controls
    "cloud.live.enable", "cloud.live.disable",
    "cloud.live.disable_all", "cloud.live.resume",
    # C2 exit gate — Stage B2a single-use DeepSeek validation permit
    "cloud.live.validate_arm", "cloud.live.validate_run",
    # scenes & storyboard (M2)
    "scene.create", "scene.update", "scene.archive", "scene.unarchive",
    "scene.revert", "scene.duplicate", "scene.move",
    "scene.cast.assign", "scene.cast.update", "scene.cast.remove",
    "shot.create", "shot.update", "shot.archive", "shot.unarchive",
    "shot.revert", "shot.duplicate", "shot.move",
    "shot.cast.assign", "shot.cast.update", "shot.cast.remove",
    "character.duplicate",
    "entity.assets.link", "entity.assets.unlink",
    "entity.assets.reorder", "entity.assets.update",
    # C4-A — named character reference sets + identity guidance
    "refset.create", "refset.update", "refset.delete",
    # C4-B — shot-scoped image generation (composes existing pipelines)
    "shot.generate",
    # C4-D — shot-scoped video generation (composes generate.video)
    "shot.video",
}
GENERATION_COMMANDS = {"generate.image.mock", "generate.image",
                       "generate.image.persistent", "generate.video",
                       "generate.text.cloud", "generate.image.cloud",
                       "generate.image.img2img", "generate.image.edit",
                       # C4-B — shot-scoped composition of the same
                       # simulated image pipelines (img2img when a
                       # source is bound, else cloud text-to-image).
                       "shot.generate",
                       # C4-D — shot-scoped composition of generate.video
                       # (local installed video models, i2v optional).
                       "shot.video"}

# Commands whose commit transaction must begin BEGIN IMMEDIATE — they
# check-then-act on financial state, so the budget-check SELECT must
# run after the write lock is held (scoped; see api/routers.py
# _spend_isolation). Everything else stays deferred.
SPEND_GUARDED_COMMANDS = {
    "generate.text.cloud", "generate.image.cloud",
    "generate.image.img2img", "generate.image.edit", "shot.generate",
    "spend.budget_set", "spend.budget_remove", "spend.reconcile",
    # live-gate mutations check-then-act on budgets + gate state
    "cloud.live.enable", "cloud.live.disable",
    "cloud.live.disable_all", "cloud.live.resume",
    # B2a permit arm/run — check-then-act on permit + financial state
    "cloud.live.validate_arm", "cloud.live.validate_run",
    # project.delete checks for unresolved spend holds — the check must
    # hold the write lock or a concurrent admission could slip an open
    # reservation between check and delete (unreconcilable hold left
    # counting against global budgets forever).
    "project.delete",
}

# Commands that enqueue durable jobs (visible progress, restart-safe).
JOB_COMMANDS = {
    "generate.image.mock": "mock.image",
    "generate.image": "gen.image.sdcpp",
    "generate.image.persistent": "gen.image.sdcpp-server",
    "generate.video": "gen.video.sdcpp",
    # Default cloud-text adapter — generate.text.cloud resolves per
    # provider_key (REST contract impls route to cloud.text.rest).
    "generate.text.cloud": "cloud.text.mock",
    # C3-A — cloud image contract adapter; per-provider contract
    # coverage is checked at commit (only implemented contracts run).
    "generate.image.cloud": "cloud.image.sim",
    # C3-D2 — simulated image-to-image on documented edits contracts;
    # routed through its own adapter id so contract coverage and mode
    # are negotiated per provider/model, never from catalog flags alone.
    "generate.image.img2img": "cloud.image.img2img.sim",
    # C3-D3 — simulated inpaint/outpaint on documented contracts.
    "generate.image.edit": "cloud.image.edit.sim",
    # C4-B — maps to the base sim adapter; the composed request
    # overrides to img2img when a source image is bound.
    "shot.generate": "cloud.image.sim",
    # C4-D — local installed video models; the composed request is
    # exactly generate.video's shape plus shot provenance.
    "shot.video": "gen.video.sdcpp",
    "asset.import": "ops.asset.import",
    "bridge.import": "ops.asset.import",
    "asset.relink": "ops.asset.relink",
    "assets.verify": "ops.assets.verify",
    "backup.export": "ops.backup.export",
    "backup.import": "ops.backup.import",
    "backup.restore": "ops.backup.restore",
}

# Commands that can be safely undone (jobs/compute can never be unspent).
UNDOABLE = {
    "prompt.set_text", "project.rename", "project.update_settings",
    "character.update", "world.update", "location.update",
    "character.archive", "character.unarchive",
    "world.archive", "world.unarchive",
    "location.archive", "location.unarchive",
    # scenes & storyboard — structural moves and cast state are reversible
    "scene.update", "scene.archive", "scene.unarchive", "scene.move",
    "scene.cast.assign", "scene.cast.update", "scene.cast.remove",
    "shot.update", "shot.archive", "shot.unarchive", "shot.move",
    "shot.cast.assign", "shot.cast.update", "shot.cast.remove",
    "entity.assets.link", "entity.assets.unlink",
    "entity.assets.reorder", "entity.assets.update",
    "refset.create", "refset.update", "refset.delete",
}

PROJECT_FORMAT_FIELDS = ("kind", "aspect_ratio", "width", "height", "fps_num", "fps_den", "style")
# Direct project columns writable via project.update_settings (beyond formats).
PROJECT_SETTING_FIELDS = PROJECT_FORMAT_FIELDS + ("storage_location_id", "backup_dir")
ENTITY_OPS = ("create", "update", "archive", "unarchive", "revert")


class CommandError(Exception):
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def _entity_parts(command_type: str) -> tuple[str, str] | None:
    t, _, op = command_type.partition(".")
    if t in ("character", "world", "location") and op in ENTITY_OPS:
        return t, op
    return None


def _summary(command_type: str, payload: dict) -> str:
    if command_type == "prompt.set_text":
        return f"Update prompt draft ({len((payload.get('text') or '').strip())} chars)"
    if command_type == "project.rename":
        return f"Rename project to '{payload.get('name', '')}'"
    if command_type == "project.archive":
        return "Archive project"
    if command_type == "project.restore":
        return "Restore project"
    if command_type == "project.update_settings":
        keys = ", ".join(sorted(k for k in payload if k in PROJECT_FORMAT_FIELDS or k == "settings"))
        return f"Update project settings ({keys})"
    if command_type == "project.duplicate":
        return f"Duplicate project as '{payload.get('name', '')}'"
    if command_type == "project.delete":
        return "Delete project permanently"
    if command_type == "generate.image.mock":
        return f"Generate image via mock adapter — '{(payload.get('prompt') or '')[:60]}'"
    if command_type == "generate.image":
        return f"Generate image ({payload.get('model_key', '?')}) — '{(payload.get('prompt') or '')[:60]}'"
    if command_type == "generate.image.persistent":
        return f"Generate image via persistent worker ({payload.get('model_key', '?')}) — '{(payload.get('prompt') or '')[:60]}'"
    if command_type == "generate.video":
        return f"Generate video ({payload.get('model_key', '?')}) — '{(payload.get('prompt') or '')[:60]}'"
    if command_type == "shot.video":
        return f"Generate video for shot — '{(payload.get('motion_prompt') or '')[:60]}'"
    if command_type == "generate.text.cloud":
        return (f"Simulate cloud text job for {payload.get('provider_key', '?')} "
                f"— '{(payload.get('prompt') or '')[:60]}'")
    if command_type == "generate.image.cloud":
        return (f"Simulate cloud image job for {payload.get('provider_key', '?')} "
                f"— '{(payload.get('prompt') or '')[:60]}'")
    if command_type == "generate.image.img2img":
        return (f"Simulate image-to-image edit via {payload.get('provider_key', '?')} "
                f"— '{(payload.get('prompt') or '')[:60]}'")
    if command_type == "generate.image.edit":
        return (f"Simulate {payload.get('operation', 'image edit')} via "
                f"{payload.get('provider_key', '?')} "
                f"— '{(payload.get('prompt') or '')[:60]}'")
    if command_type == "asset.import":
        return f"Import file '{payload.get('source_path', '')}'"
    if command_type == "bridge.prepare":
        return f"Prepare prompt for {payload.get('provider_key', '?')} — you paste it yourself"
    if command_type == "bridge.launch":
        return f"Open the documented {payload.get('provider_key', '?')} site"
    if command_type == "bridge.import":
        return f"Import user-provided file tagged from {payload.get('provider_key', '?')}"
    if command_type == "asset.share":
        return "Share asset to the shared library"
    if command_type == "asset.link":
        return f"Link shared asset into project ({payload.get('mode', 'link')})"
    if command_type == "asset.relink":
        return "Relink asset to a file on disk"
    if command_type == "assets.verify":
        return "Verify asset files on disk"
    if command_type == "backup.export":
        return f"{payload.get('kind', 'backup').capitalize()} project to .atomicpkg"
    if command_type == "backup.import":
        return f"Import package '{payload.get('package_path', '')}'"
    if command_type == "backup.restore":
        return "Restore project from a backup run"
    if command_type == "spend.budget_set":
        return (f"Set {payload.get('target', 'project')} "
                f"{payload.get('scope', '?')} spend budget to "
                f"${payload.get('limit_usd', '?')}")
    if command_type == "spend.budget_remove":
        return (f"Remove {payload.get('target', 'project')} "
                f"{payload.get('scope', '?')} spend budget")
    if command_type == "spend.reconcile":
        return (f"Reconcile remote spend for job "
                f"{(payload.get('job_id') or '')[:8]} — "
                f"{payload.get('outcome', '?')}")
    if command_type == "cloud.live.enable":
        return (f"Enable live lane for {payload.get('provider_key', '?')} / "
                f"{payload.get('model_id', '?')} (Stage A — no dispatch)")
    if command_type == "cloud.live.disable":
        return f"Disable live lane for {payload.get('provider_key', '?')}"
    if command_type == "cloud.live.disable_all":
        return "Disable ALL live execution (kill switch)"
    if command_type == "cloud.live.resume":
        return "Lift the global live kill switch"
    if command_type == "cloud.live.validate_arm":
        return ("Arm the single-use DeepSeek validation permit "
                "(B2a — one request, audited, consumed on dispatch)")
    if command_type == "cloud.live.validate_run":
        return "Run the armed DeepSeek validation request"
    if command_type.startswith("scene."):
        if command_type == "scene.create":
            return f"Create scene '{payload.get('title', '')}'"
        if command_type == "scene.duplicate":
            return "Duplicate scene (with its shots and cast)"
        if command_type == "scene.move":
            return f"Move scene to position {(payload.get('to_index') or 0) + 1}"
        if command_type == "scene.revert":
            return f"Revert scene to revision {payload.get('to_revision')}"
        if command_type.startswith("scene.cast."):
            op = command_type.rsplit(".", 1)[1]
            return f"{op.capitalize()} character in scene cast"
        return f"{command_type.split('.', 1)[1].capitalize()} scene"
    if command_type.startswith("shot."):
        if command_type == "shot.create":
            return f"Create shot '{payload.get('title') or payload.get('shot_type', 'medium')}'"
        if command_type == "shot.duplicate":
            return "Duplicate shot (with its cast)"
        if command_type == "shot.move":
            return f"Move shot to position {(payload.get('to_index') or 0) + 1}"
        if command_type == "shot.revert":
            return f"Revert shot to revision {payload.get('to_revision')}"
        if command_type.startswith("shot.cast."):
            op = command_type.rsplit(".", 1)[1]
            return f"{op.capitalize()} character in shot cast"
        return f"{command_type.split('.', 1)[1].capitalize()} shot"
    if command_type == "shot.generate":
        return "Generate image for shot (simulated)"
    if command_type == "character.duplicate":
        return "Duplicate character (with its linked assets)"
    if command_type == "entity.assets.link":
        return f"Link asset to {payload.get('entity_type', 'entity')} ({payload.get('role', 'reference')})"
    if command_type == "entity.assets.unlink":
        return "Unlink asset from entity"
    if command_type.startswith("refset."):
        op = command_type.rsplit(".", 1)[1]
        if op == "create":
            return f"Create reference set '{payload.get('name', '')}'"
        return f"{op.capitalize()} reference set"
    parts = _entity_parts(command_type)
    if parts:
        etype, op = parts
        if op == "create":
            return f"Create {etype} '{payload.get('name', '')}'"
        if op == "revert":
            return f"Revert {etype} to revision {payload.get('to_revision')}"
        return f"{op.capitalize()} {etype}"
    return command_type


def _validate_refset_seed(seed) -> None:
    """Optional pinned seed — integer ≥ 0, or None to clear. Guidance
    only; never marketed as an identity guarantee."""
    if seed is None:
        return
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise CommandError(422, "seed must be a non-negative integer or null")


def _refset_refs(session: Session, project: Project, payload: dict) -> tuple:
    """C4-A — resolve reference_set_id to (rs, entries, weights).

    ``entries`` is the ordered fingerprint a preview publishes and a
    commit re-verifies — asset_id, link weight, and the CURRENT content
    hash of each member — so membership, ordering, weight, and
    underlying-content changes all invalidate a stale approval.
    ``weights`` is the explicit provider-facing list: if ANY member
    carries a weight, the whole list is explicit (missing members
    default to 1.0) so provider weight semantics stay aligned; if none
    do, weights is []."""
    rs = sb.get_refset(session, payload.get("reference_set_id") or "",
                       project.id)
    members = sb.set_members(session, rs.id, project.id)
    entries = []
    for l in members:
        a = session.get(Asset, l.asset_id)
        if a is None or a.project_id != project.id:
            raise CommandError(404, "reference image asset not found "
                                    "in project")
        if a.kind != "image":
            raise CommandError(422, "reference asset must be an image")
        entries.append({"asset_id": l.asset_id, "weight": l.weight,
                        "sha256": a.sha256})
    if any(l.weight is not None for l in members):
        weights = [l.weight if l.weight is not None else 1.0
                   for l in members]
    else:
        weights = []
    return rs, entries, weights


def _shot_cast_ids(session: Session, shot) -> set[str]:
    """C4-B — a shot's effective cast: its own shot_characters UNION
    the scene-level cast it inherits (mirrors the storyboard payload's
    cast + inherited lists)."""
    from app.db.models import SceneCharacter, ShotCharacter
    own = {r.character_id for r in session.execute(
        select(ShotCharacter).where(ShotCharacter.shot_id == shot.id)
    ).scalars()}
    inherited = {r.character_id for r in session.execute(
        select(SceneCharacter).where(SceneCharacter.scene_id == shot.scene_id)
    ).scalars()}
    return own | inherited


def _check_shot_bindings(session: Session, project: Project, shot,
                         payload: dict, *, cast_known: bool) -> None:
    """C4-B — validate reference_set_id / source_asset_id bindings on
    shot.create/shot.update. The set's character must be in the shot's
    effective cast whenever that cast is non-empty; empty cast stays
    permissive (beginner flow — nothing to contradict)."""
    if "reference_set_id" in payload and payload["reference_set_id"] is not None:
        try:
            rs = sb.get_refset(session, payload["reference_set_id"],
                               project.id)
        except KeyError:
            raise CommandError(
                404, "reference set not found in this project") from None
        if cast_known:
            cast = _shot_cast_ids(session, shot)
            if cast and rs.character_id not in cast:
                raise CommandError(
                    422, "reference set's character is not in this "
                         "shot's cast")
    if "source_asset_id" in payload and payload["source_asset_id"] is not None:
        a = session.get(Asset, payload["source_asset_id"])
        if a is None or a.project_id != project.id:
            raise CommandError(404, "source asset not found in this project")
        if a.kind != "image":
            raise CommandError(422, "shot source asset must be an image")


def _compose_shot_generate(session: Session, project: Project,
                           payload: dict) -> tuple:
    """C4-B — resolve a shot.generate call into the equivalent existing
    image command + effective payload. Returns (shot, inner_type,
    composed). Composition is server-side only: bindings come from the
    shot row, never the client."""
    try:
        shot = sb.get_shot(session, payload.get("shot_id") or "", project.id)
    except KeyError:
        raise CommandError(404, "shot not found in this project") from None
    if shot.status != "active":
        raise CommandError(422, "cannot generate from an archived shot")
    # Payload may explicitly override the bound set; otherwise use the
    # shot's binding. A deleted/archived set resolves nowhere — 404.
    rs_id = payload.get("reference_set_id") or shot.reference_set_id
    char_id = None
    if rs_id:
        try:
            rs = sb.get_refset(session, rs_id, project.id)
        except KeyError:
            raise CommandError(
                404, "reference set not found in this project") from None
        # Generate-time is STRICT, unlike bind-time: a bound set whose
        # character was removed from the effective cast (own ∪ scene
        # inherited) must refuse — never fall back to unrelated refs.
        cast = _shot_cast_ids(session, shot)
        if rs.character_id not in cast:
            raise CommandError(
                422, "reference set's character is not in this shot's "
                     "cast")
        char_id = rs.character_id
    src = payload.get("source_asset_id") or shot.source_asset_id
    prompt = (payload.get("prompt") or shot.prompt or "").strip()
    if not prompt:
        raise CommandError(
            422, "shot has no prompt — write one or pass prompt")
    inner = ("generate.image.img2img" if src
             else "generate.image.cloud")
    composed = {
        "provider_key": payload.get("provider_key"),
        "model_id": payload.get("model_id"),
        "prompt": prompt,
        "negative_prompt": payload.get("negative_prompt"),
        "execution_target": "cloud",
        "size": payload.get("size"),
        "quality": payload.get("quality"),
        "seed": payload.get("seed"),
        "max_cost_usd": payload.get("max_cost_usd"),
        "scene_id": shot.scene_id,
        "shot_id": shot.id,
        "character_id": char_id or payload.get("character_id"),
        "location_id": shot.location_id,
    }
    if src:
        composed["source_asset_id"] = src
    if rs_id:
        composed["reference_set_id"] = rs_id
        # Forward the caller's stale-approval pins untouched — the
        # inner pipeline re-resolves and compares them.
        for k in ("expected_references", "expected_set"):
            if k in payload:
                composed[k] = payload[k]
    # Shot-state staleness: expected_references fingerprints the SET,
    # but a prompt/source/cast edit between preview and approve would
    # otherwise sail through. The approval pins the shot fingerprint
    # (revision + effective cast + composed bindings); commit recompiles
    # and refuses a mismatch — composition runs again in _job_request,
    # so the check also gates job creation.
    expected_shot = payload.get("expected_shot")
    if expected_shot is not None and \
            expected_shot != _shot_gen_fingerprint(session, shot, composed):
        raise CommandError(
            422, "shot changed since preview — re-preview shot.generate")
    return shot, inner, composed


def _shot_gen_fingerprint(session: Session, shot, composed: dict) -> dict:
    """C4-B — the generation-relevant shot state an approval pins.
    shot.revision catches prompt/source/continuity edits, but cast
    membership lives in shot_characters and doesn't bump the row —
    it is fingerprinted explicitly. The source key covers both image
    (source_asset_id) and video (image_asset_id) compositions."""
    return {
        "shot_id": shot.id,
        "revision": shot.revision,
        "prompt": composed.get("prompt"),
        "source_asset_id": (composed.get("source_asset_id")
                            or composed.get("image_asset_id")),
        "reference_set_id": composed.get("reference_set_id"),
        "continuity": shot.continuity_json or {},
        "cast": sorted(_shot_cast_ids(session, shot)),
    }


def _compose_shot_video(session: Session, project: Project,
                        payload: dict) -> tuple:
    """C4-D — resolve a shot.video call into the equivalent
    generate.video call + effective payload. Returns (shot,
    "generate.video", composed). Composition is server-side only:
    bindings come from the shot row, never the client.

    Reference sets/cast/location are NOT video-contract inputs — the
    contract takes a prompt + an optional source image, so that is all
    the composed request carries. Descriptive continuity guidance is
    merged INTO the prompt (the only channel the contract supports) —
    never claimed as enforced continuity."""
    try:
        shot = sb.get_shot(session, payload.get("shot_id") or "", project.id)
    except KeyError:
        raise CommandError(404, "shot not found in this project")
    if shot.status != "active":
        raise CommandError(422, "cannot generate video from an archived shot")
    src = (payload.get("image_asset_id")
           or payload.get("source_asset_id") or shot.source_asset_id)
    parts = [str(p).strip() for p in
             (shot.prompt, payload.get("motion_prompt"),
              payload.get("prompt")) if p and str(p).strip()]
    cont = shot.continuity_json or {}
    cont_txt = ", ".join(f"{k}: {v}" for k, v in cont.items()
                         if str(v or "").strip())
    if cont_txt:
        parts.append(f"continuity guidance: {cont_txt}"[:300])
    prompt = " — ".join(parts)
    if not prompt and not src:
        raise CommandError(
            422, "shot has no prompt and no source image — "
                 "write a prompt or bind a source first")
    composed = {
        "model_key": payload.get("model_key"),
        "prompt": prompt,
        "negative_prompt": payload.get("negative_prompt"),
        "width": payload.get("width"),
        "height": payload.get("height"),
        "frames": payload.get("frames"),
        "fps": payload.get("fps"),
        "steps": payload.get("steps"),
        "cfg": payload.get("cfg"),
        "seed": payload.get("seed"),
        "device": payload.get("device", "auto"),
        "image_asset_id": src,
        "shot_id": shot.id,
        "scene_id": shot.scene_id,
        "residency_mode": payload.get("residency_mode"),
        "max_vram_gib": payload.get("max_vram_gib"),
        "memory_risk_acknowledged": payload.get("memory_risk_acknowledged"),
    }
    # generate.video's request builder applies int()/default fallbacks
    # to ABSENT keys — a present-but-None value would crash them, so
    # unset fields are simply omitted.
    composed = {k: v for k, v in composed.items() if v is not None}
    # Same approval-pin contract as shot.generate — a shot edit between
    # preview and commit refuses rather than silently regenerating.
    expected_shot = payload.get("expected_shot")
    if expected_shot is not None and \
            expected_shot != _shot_gen_fingerprint(session, shot, composed):
        raise CommandError(
            422, "shot changed since preview — re-preview shot.video")
    return shot, "generate.video", composed


def _validate_reference_weight(weight, *, allow_none: bool = False) -> None:
    """C3-D1 — optional per-link guidance strength must be a real
    number inside the declared range; bools and non-numerics refuse."""
    if weight is None:
        if allow_none:
            return
        return  # absent weight is fine on link (defaults to NULL)
    if isinstance(weight, bool) or not isinstance(weight, (int, float)):
        raise CommandError(422, "weight must be a number")
    if not (sb.REFERENCE_WEIGHT_MIN <= weight <= sb.REFERENCE_WEIGHT_MAX):
        raise CommandError(
            422, f"weight must be {sb.REFERENCE_WEIGHT_MIN}–"
                 f"{sb.REFERENCE_WEIGHT_MAX} (got {weight})")


def _resolve_project_image(session: Session, storage, project: Project,
                           aid: str, label: str):
    """Project-owned image asset → (row, resolved CAS path) after the
    byte-level safety decode. Shared by every image-input commit path."""
    from app.db.models import Asset
    from app.domain import image_safety
    a = session.get(Asset, aid or "")
    if a is None or a.project_id != project.id:
        raise CommandError(404, f"{label} asset not found in project")
    if a.kind != "image":
        raise CommandError(422, f"{label} asset must be an image")
    if storage is None:
        raise CommandError(500, "storage unavailable")
    p = storage.asset_path(session, a)
    if p is None or not p.exists():
        raise CommandError(422, f"{label} asset bytes missing")
    try:
        image_safety.inspect_image(p.read_bytes())
    except Exception as exc:
        raise CommandError(
            422, f"{label} image failed safety inspection: {exc}")
    return a, p


def _validate(session: Session, project: Project, command_type: str, payload: dict) -> list[str]:
    warnings: list[str] = []
    if command_type == "generate.image.mock":
        if not (payload.get("prompt") or "").strip():
            raise CommandError(422, "prompt is required for image generation")
        warnings.append("Uses the mock adapter — output is a placeholder, not AI inference")
    if command_type == "generate.image":
        _check_generation(session, project, payload, "gen.image.sdcpp")
    if command_type == "generate.image.persistent":
        _check_generation(session, project, payload, "gen.image.sdcpp-server")
        warnings.append(
            "Experimental persistent worker — keeps the model resident between jobs; "
            "not yet validated for production use")
    if command_type == "generate.video":
        _check_video_generation(session, project, payload)
    if command_type == "generate.text.cloud":
        # C2.0a — simulated remote lifecycle. Eligibility is catalog-
        # driven: the provider must document a T1 official-API text path;
        # execution_target must be explicitly "cloud" (local stays the
        # default). No credentials are read and nothing billable runs —
        # the adapter is a mock. Estimates are labeled estimates.
        from app.domain import bridge as _bridge
        from app.domain import cloud_cost as _cc
        provider = _bridge.get_provider(payload.get("provider_key"))
        if provider is None:
            raise CommandError(404, "provider not found in catalog")
        if payload.get("execution_target") != "cloud":
            raise CommandError(422, "cloud generation requires explicit "
                                    "execution_target 'cloud' — local is "
                                    "the default and must be opted out of")
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise CommandError(422, "prompt is required")
        if len(prompt) > 8000:
            raise CommandError(422, "prompt exceeds 8000 characters")
        strat = _cc.t1_text_strategy(provider)
        if strat is None:
            raise CommandError(422, "no documented T1 text API path — "
                                    "provider not eligible for cloud text")
        if _bridge.record_stale(strat):
            warnings.append("Documented strategy evidence needs re-check — "
                            "this simulation proceeds on stale evidence only")
        model = _cc.pick_text_model(provider, payload.get("model_id"))
        if payload.get("model_id") and model is None:
            raise CommandError(422, f"unknown text model '{payload['model_id']}'")
        if model is None:
            raise CommandError(422, "no text-capable model for this provider")
        est = _cc.estimate_text_cost(model, prompt)
        cost_txt = (f"~${est['est_cost_usd']:.4f}" if est["est_cost_usd"]
                    is not None else "unknown (no catalog rates)")
        warnings.append(f"Cloud execution sends: {_cc.data_out_summary(provider, prompt)}")
        warnings.append(
            f"Cost is an ESTIMATE only ({cost_txt}) — no durable spend "
            "ledger or budget enforcement exists yet, and no billable "
            "execution happens in this slice")
        from app.domain.workers.rest_providers import REST_PROVIDER_KEYS
        if provider.get("key") in REST_PROVIDER_KEYS:
            warnings.append(
                "Runs through the REST contract adapter over a SIMULATED "
                "transport — documented request/response shapes with no "
                "network egress; output can never be verified_remote")
        else:
            warnings.append("Runs on the MOCK remote provider — simulated "
                            "lifecycle only; output can never be verified_remote")
    if command_type == "generate.image.cloud":
        # C3-A — simulated cloud image lifecycle. Eligibility is
        # catalog-driven (T1 official-API image strategy + image model);
        # execution_target must be explicitly "cloud". No credentials
        # are read and nothing billable runs — the transport is a
        # MockTransport fixture; output can never be verified_remote.
        from app.domain import bridge as _bridge
        from app.domain import cloud_cost as _cc
        from app.domain.workers.rest_image_providers import (
            IMAGE_REST_KEYS)
        provider = _bridge.get_provider(payload.get("provider_key"))
        if provider is None:
            raise CommandError(404, "provider not found in catalog")
        if payload.get("execution_target") != "cloud":
            raise CommandError(422, "cloud generation requires explicit "
                                    "execution_target 'cloud' — local is "
                                    "the default and must be opted out of")
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise CommandError(422, "prompt is required")
        if len(prompt) > 8000:
            raise CommandError(422, "prompt exceeds 8000 characters")
        strat = _cc.t1_image_strategy(provider)
        if strat is None:
            raise CommandError(422, "no documented T1 image API path — "
                                    "provider not eligible for cloud image")
        if _bridge.record_stale(strat):
            warnings.append("Documented strategy evidence needs re-check — "
                            "this simulation proceeds on stale evidence only")
        model = _cc.pick_image_model(provider, payload.get("model_id"))
        if payload.get("model_id") and model is None:
            raise CommandError(
                422, f"unknown image model '{payload['model_id']}'")
        if model is None:
            raise CommandError(422, "no image-capable model for this provider")
        if payload.get("provider_key") not in IMAGE_REST_KEYS:
            raise CommandError(
                422, f"no image contract implemented for provider "
                     f"'{payload.get('provider_key')}' — C3-A covers: "
                     + ", ".join(IMAGE_REST_KEYS))
        est = _cc.estimate_image_cost(
            model, count=1, size=payload.get("size"),
            quality=payload.get("quality"), prompt=prompt)
        if est["est_cost_usd"] is None:
            raise CommandError(
                422, {"error": "pricing_unavailable",
                      "detail": "catalog pricing for this model is missing, "
                                "stale, or not estimable — refusing rather "
                                "than guessing a billable amount"})
        warnings.append(
            f"Cloud execution sends: {_cc.data_out_summary(provider, prompt)}")
        warnings.append(
            f"Cost is an ESTIMATE only (~${est['est_cost_usd']:.4f}, "
            f"{est['unit']}) — simulated accounting on this run; "
            "no billable execution happens in C3-A")
        if int(payload.get("count") or 1) != 1:
            raise CommandError(422, "C3-A supports exactly one image per job")
        for ref in ("scene_id", "shot_id", "character_id", "world_id",
                    "location_id", "reference_asset_id"):
            if payload.get(ref) is not None:
                warnings.append(
                    f"Filmmaking metadata attached: {ref}")
        warnings.append(
            "Runs on a SIMULATED transport — documented request/response "
            "shapes with no network egress; output is a deterministic "
            "test PNG and can never be verified_remote")
    if command_type == "generate.image.img2img":
        # C3-D2 — simulated image-to-image. Capability negotiation is
        # fail-closed at two layers: the catalog model must declare
        # editing.img2img AND the provider must have an implemented
        # edits contract (spec.edit_path). Catalog metadata alone can
        # never enable the mode.
        from app.domain import bridge as _bridge
        from app.domain import cloud_cost as _cc
        from app.domain.providers import catalog as _pc
        from app.domain.workers.rest_image_providers import IMAGE_SPECS
        provider = _bridge.get_provider(payload.get("provider_key"))
        if provider is None:
            raise CommandError(404, "provider not found in catalog")
        if payload.get("execution_target") != "cloud":
            raise CommandError(422, "cloud generation requires explicit "
                                    "execution_target 'cloud' — local is "
                                    "the default and must be opted out of")
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise CommandError(422, "prompt is required")
        if len(prompt) > 8000:
            raise CommandError(422, "prompt exceeds 8000 characters")
        strat = _cc.t1_image_strategy(provider)
        if strat is None:
            raise CommandError(422, "no documented T1 image API path — "
                                    "provider not eligible for cloud image")
        if _bridge.record_stale(strat):
            warnings.append("Documented strategy evidence needs re-check — "
                            "this simulation proceeds on stale evidence only")
        model = _cc.pick_image_model(provider, payload.get("model_id"))
        if payload.get("model_id") and model is None:
            raise CommandError(
                422, f"unknown image model '{payload['model_id']}'")
        if model is None:
            raise CommandError(422, "no image-capable model for this provider")
        caps = _pc.model_editing_caps(provider["key"], model["id"])
        if not caps.get("img2img"):
            raise CommandError(
                422, f"model '{model['id']}' does not declare img2img "
                     "editing support — unsupported modes fail closed")
        spec = IMAGE_SPECS.get(provider["key"])
        if spec is None or spec.edit_path is None:
            raise CommandError(
                422, f"no img2img edits contract implemented for provider "
                     f"'{provider['key']}' — a catalog declaration alone "
                     "does not enable an unimplemented provider operation")
        if int(payload.get("count") or 1) != 1:
            raise CommandError(422, "C3-D2 supports exactly one image per job")
        src = session.get(Asset, payload.get("source_asset_id") or "")
        if src is None or src.project_id != project.id:
            raise CommandError(404, "source image asset not found in project")
        if src.kind != "image":
            raise CommandError(422, "source asset must be an image")
        if payload.get("reference_set_id"):
            if payload.get("reference_asset_ids"):
                raise CommandError(
                    422, "reference_set_id and reference_asset_ids "
                         "are mutually exclusive")
            try:
                _, entries, refset_weights = _refset_refs(
                    session, project, payload)
            except KeyError:
                raise CommandError(404, "reference set not found in this project") from None
            refs = [e["asset_id"] for e in entries]
            weights = refset_weights
        else:
            refs = list(payload.get("reference_asset_ids") or [])
            weights = payload.get("reference_weights")
        if len(refs) > int(caps.get("max_reference_images") or 0):
            raise CommandError(
                422, f"model accepts at most "
                     f"{caps.get('max_reference_images') or 0} "
                     "reference image(s)")
        seen = {src.id}
        for rid in refs:
            if rid in seen:
                raise CommandError(422, "duplicate asset in source/references")
            seen.add(rid)
            r = session.get(Asset, rid)
            if r is None or r.project_id != project.id:
                raise CommandError(404, "reference image asset not found "
                                        "in project")
            if r.kind != "image":
                raise CommandError(422, "reference asset must be an image")
        if weights:
            if not caps.get("reference_weights"):
                raise CommandError(
                    422, "this model does not implement reference weights")
            if len(weights) != len(refs):
                raise CommandError(
                    422, "reference_weights must match reference_asset_ids")
            for w in weights:
                _validate_reference_weight(w)
        est = _cc.estimate_image_cost(
            model, count=1, size=payload.get("size"),
            quality=payload.get("quality"), prompt=prompt)
        if est["est_cost_usd"] is None:
            raise CommandError(
                422, {"error": "pricing_unavailable",
                      "detail": "catalog pricing for this model is missing, "
                                "stale, or not estimable — refusing rather "
                                "than guessing a billable amount"})
        warnings.append(
            f"Cloud execution sends: {_cc.data_out_summary(provider, prompt)} "
            "+ source image bytes"
            + (" + reference image bytes" if refs else ""))
        warnings.append(
            f"Cost is an ESTIMATE only (~${est['est_cost_usd']:.4f}, "
            f"{est['unit']}) — simulated accounting on this run; "
            "no billable execution happens in C3-D2")
        warnings.append(
            "Runs on a SIMULATED edits transport — output is a new asset; "
            "the source image is never modified")
    if command_type == "generate.image.edit":
        # C3-D3 — simulated inpaint/outpaint. Same two-layer fail-closed
        # negotiation as img2img: catalog capability AND implemented
        # contract (spec.inpaint_path / spec.outpaint_path).
        from app.domain import bridge as _bridge
        from app.domain import cloud_cost as _cc
        from app.domain import image_edit
        from app.domain.providers import catalog as _pc
        from app.domain.workers.rest_image_providers import IMAGE_SPECS
        operation = payload.get("operation")
        if operation not in ("inpaint", "outpaint"):
            raise CommandError(
                422, "operation must be 'inpaint' or 'outpaint'")
        cap_key = "mask_inpaint" if operation == "inpaint" else "outpaint"
        provider = _bridge.get_provider(payload.get("provider_key"))
        if provider is None:
            raise CommandError(404, "provider not found in catalog")
        if payload.get("execution_target") != "cloud":
            raise CommandError(422, "cloud generation requires explicit "
                                    "execution_target 'cloud' — local is "
                                    "the default and must be opted out of")
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise CommandError(422, "prompt is required")
        if len(prompt) > 8000:
            raise CommandError(422, "prompt exceeds 8000 characters")
        strat = _cc.t1_image_strategy(provider)
        if strat is None:
            raise CommandError(422, "no documented T1 image API path — "
                                    "provider not eligible for cloud image")
        model = _cc.pick_image_model(provider, payload.get("model_id"))
        if payload.get("model_id") and model is None:
            raise CommandError(
                422, f"unknown image model '{payload['model_id']}'")
        if model is None:
            raise CommandError(422, "no image-capable model for this provider")
        caps = _pc.model_editing_caps(provider["key"], model["id"])
        if not caps.get(cap_key):
            raise CommandError(
                422, f"model '{model['id']}' does not declare {operation} "
                     "editing support — unsupported modes fail closed")
        spec = IMAGE_SPECS.get(provider["key"])
        spec_path = (spec.inpaint_path if operation == "inpaint"
                     else spec.outpaint_path) if spec else None
        if spec_path is None:
            raise CommandError(
                422, f"no {operation} contract implemented for provider "
                     f"'{provider['key']}' — a catalog declaration alone "
                     "does not enable an unimplemented provider operation")
        if int(payload.get("count") or 1) != 1:
            raise CommandError(422, "C3-D3 supports exactly one image per job")
        src = session.get(Asset, payload.get("source_asset_id") or "")
        if src is None or src.project_id != project.id:
            raise CommandError(404, "source image asset not found in project")
        if src.kind != "image":
            raise CommandError(422, "source asset must be an image")
        if operation == "inpaint":
            mask = session.get(Asset, payload.get("mask_asset_id") or "")
            if mask is None or mask.project_id != project.id:
                raise CommandError(
                    404, "mask asset not found in project")
            if mask.kind != "image":
                raise CommandError(422, "mask asset must be an image")
            if mask.id == src.id:
                raise CommandError(
                    422, "mask must be a distinct asset from the source")
        else:
            try:
                out_w = int(payload.get("out_width"))
                out_h = int(payload.get("out_height"))
            except (TypeError, ValueError):
                raise CommandError(
                    422, "outpaint requires explicit integer "
                         "out_width/out_height")
            if not (1 <= out_w <= image_edit.MAX_OUT_DIM
                    and 1 <= out_h <= image_edit.MAX_OUT_DIM):
                raise CommandError(
                    422, f"outpaint dimensions must be 1–"
                         f"{image_edit.MAX_OUT_DIM} px")
            if payload.get("placement") not in image_edit.PLACEMENTS:
                raise CommandError(
                    422, "placement must be one of: "
                         + ", ".join(image_edit.PLACEMENTS))
        refs = list(payload.get("reference_asset_ids") or [])
        if len(refs) > int(caps.get("max_reference_images") or 0):
            raise CommandError(
                422, f"model accepts at most "
                     f"{caps.get('max_reference_images') or 0} "
                     "reference image(s)")
        seen = {src.id}
        for rid in refs:
            if rid in seen:
                raise CommandError(422, "duplicate asset in source/references")
            seen.add(rid)
            r = session.get(Asset, rid)
            if r is None or r.project_id != project.id:
                raise CommandError(404, "reference image asset not found "
                                        "in project")
            if r.kind != "image":
                raise CommandError(422, "reference asset must be an image")
        est = _cc.estimate_image_cost(
            model, count=1, size=payload.get("size"),
            quality=payload.get("quality"), prompt=prompt)
        if est["est_cost_usd"] is None:
            raise CommandError(
                422, {"error": "pricing_unavailable",
                      "detail": "catalog pricing for this model is missing, "
                                "stale, or not estimable — refusing rather "
                                "than guessing a billable amount"})
        warnings.append(
            f"Cloud execution sends: {_cc.data_out_summary(provider, prompt)} "
            "+ source image bytes"
            + (" + mask bytes" if operation == "inpaint" else ""))
        warnings.append(
            f"Cost is an ESTIMATE only (~${est['est_cost_usd']:.4f}, "
            f"{est['unit']}) — simulated accounting on this run; "
            "no billable execution happens in C3-D3")
        warnings.append(
            "Runs on a SIMULATED edit transport — output is a new asset; "
            "source and mask assets are never modified")
    if command_type in ("spend.budget_set", "spend.budget_remove"):
        if payload.get("target", "project") not in ("project", "global"):
            raise CommandError(422, "target must be 'project' or 'global'")
        if payload.get("scope") not in ("session", "day", "month", "lifetime"):
            raise CommandError(422, "scope must be session, day, month, or lifetime")
        if command_type == "spend.budget_set":
            if payload.get("limit_usd") is None:
                raise CommandError(422, "limit_usd is required")
            warnings.append(
                "Budget edits are forward-only — existing open "
                "reservations are never reduced or released")
        else:
            warnings.append(
                "Removing a budget leaves that scope unconstrained")
        if payload.get("target") == "global":
            warnings.append("Global budgets apply to ALL projects")
    if command_type == "cloud.live.enable":
        for f in ("provider_key", "model_id", "credential_ref"):
            if not str(payload.get(f) or "").strip():
                raise CommandError(422, f"{f} is required")
        warnings.append(
            "Stage A — this grants only the opt-in flag. No live "
            "transport exists in this build; dispatch is unreachable.")
    if command_type == "cloud.live.disable":
        if not payload.get("provider_key"):
            raise CommandError(422, "provider_key is required")
        warnings.append(
            "Open financial reservations are never released by "
            "disabling — they settle or reconcile normally")
    if command_type == "cloud.live.disable_all":
        warnings.append(
            "Kills ALL live dispatch and revokes every provider grant. "
            "Already-submitted provider work cannot be un-submitted — "
            "open reservations stay until settled or reconciled")
    if command_type == "cloud.live.resume":
        warnings.append(
            "Lifting the kill switch does not re-enable providers — "
            "each must be granted again explicitly")
    if command_type == "cloud.live.validate_arm":
        for f in ("credential_ref", "prompt", "max_output_tokens",
                  "max_cost_usd"):
            if payload.get(f) is None or str(payload.get(f)) == "":
                raise CommandError(422, f"{f} is required")
        warnings.append(
            "B2a — arms a single-use permit for one DeepSeek request. "
            "It is consumed the moment dispatch is attempted; nothing "
            "is sent until cloud.live.validate_run.")
    if command_type == "cloud.live.validate_run":
        warnings.append(
            "Consumes the armed permit — exactly one dispatch attempt. "
            "Ambiguous outcomes hold the reservation for explicit "
            "reconcile; nothing is retried automatically.")
    if command_type == "spend.reconcile":
        if not payload.get("job_id"):
            raise CommandError(422, "job_id is required")
        if payload.get("outcome") not in ("confirmed_charge",
                                          "confirmed_no_charge",
                                          "still_unknown"):
            raise CommandError(422, "outcome must be confirmed_charge, "
                                    "confirmed_no_charge, or still_unknown")
        warnings.append(
            "Reconciliation is your attestation about remote billing — "
            "ambiguous exposure stays reserved until confirmed")
    if command_type in {"project.archive", "project.restore", "project.duplicate"}:
        warnings.append("Applies to the whole project")
    if command_type == "project.delete":
        if (payload.get("confirm_name") or "") != project.name:
            raise CommandError(422, "type the project name to confirm deletion")
        open_holds = session.execute(
            select(func.count()).select_from(SpendReservation).where(
                SpendReservation.project_id == project.id,
                SpendReservation.status == "open")).scalar_one()
        if open_holds:
            warnings.append(
                f"{open_holds} unresolved spend reservation(s) — "
                "deletion will be refused until they are reconciled")
        warnings.append("Permanent — assets and history are removed; only backups survive")
    if command_type == "project.update_settings":
        for k in payload:
            if k not in PROJECT_SETTING_FIELDS and k != "settings":
                raise CommandError(422, f"unknown settings field '{k}'")
        if "storage_location_id" in payload and payload["storage_location_id"]:
            if session.get(StorageLocation, payload["storage_location_id"]) is None:
                raise CommandError(404, "storage location not found")
        if "backup_dir" in payload and payload["backup_dir"]:
            from pathlib import Path as _P
            if not _P(payload["backup_dir"]).is_absolute():
                raise CommandError(422, "backup_dir must be an absolute path")
            from app.domain import backup_dest as _bd
            res = _bd.validate_destination(payload["backup_dir"])
            for e in res["errors"]:
                warnings.append(f"backup_dir: {e}")
    if command_type == "asset.import":
        if not (payload.get("source_path") or "").strip():
            raise CommandError(422, "source_path is required")
    if command_type.startswith("bridge."):
        from app.domain import bridge as _bridge
        warnings += _bridge.validate_dispatch(
            session, project, command_type, payload)[0]
    if command_type == "asset.share":
        asset = session.get(Asset, payload.get("asset_id") or "")
        if asset is None or asset.project_id != project.id:
            raise CommandError(404, "asset not found in this project")
        if asset.status != "ready":
            raise CommandError(409, "only verified assets can be shared")
    if command_type == "asset.link":
        asset = session.get(Asset, payload.get("asset_id") or "")
        if asset is None or asset.scope != "shared":
            raise CommandError(404, "shared asset not found")
        if asset.project_id == project.id:
            raise CommandError(409, "asset already belongs to this project")
        if payload.get("mode", "link") not in ("link", "copy"):
            raise CommandError(422, "mode must be 'link' or 'copy'")
    if command_type == "backup.restore":
        from app.db.models import BackupRun
        run = session.get(BackupRun, payload.get("backup_run_id") or "")
        if run is None or run.status != "succeeded" or run.project_id != project.id:
            raise CommandError(404, "successful backup run not found for this project")
    if command_type == "backup.import":
        if not (payload.get("package_path") or "").strip():
            raise CommandError(422, "package_path is required")
    if command_type == "asset.relink":
        relink_asset = session.get(Asset, payload.get("asset_id") or "")
        if relink_asset is None or relink_asset.project_id != project.id:
            raise CommandError(404, "asset not found in this project")
        if not (payload.get("source_path") or "").strip():
            raise CommandError(422, "source_path is required")
    # ---- M2: scenes / shots / cast / entity assets ----
    if command_type == "scene.create":
        if not (payload.get("title") or "").strip():
            raise CommandError(422, "scene title is required")
        _check_storyboard_links(session, project, payload)
    if command_type in ("scene.update", "scene.archive", "scene.unarchive",
                        "scene.revert", "scene.duplicate", "scene.move"):
        _get_scene(session, project, payload)
        if command_type == "scene.update":
            _check_storyboard_links(session, project, payload)
    if command_type.startswith("scene.cast."):
        _get_scene(session, project, payload)
        _get_character(session, project, payload)
        try:
            sb.parse_scene_cast_fields(payload.get("role"), payload.get("notes"))
        except sb.CastStateError as e:
            raise CommandError(422, str(e)) from None
        if command_type != "scene.cast.assign":
            _require_scene_cast(session, project, payload)
    if command_type == "shot.create":
        _get_scene(session, project, payload)
        _check_storyboard_links(session, project, payload)
        _check_shot_fields(payload)
        _check_shot_bindings(session, project, None, payload,
                             cast_known=False)
    if command_type in ("shot.update", "shot.archive", "shot.unarchive",
                        "shot.revert", "shot.duplicate", "shot.move"):
        shot = _get_shot(session, project, payload)
        if command_type == "shot.update":
            _check_storyboard_links(session, project, payload)
            _check_shot_fields(payload)
            _check_shot_bindings(session, project, shot, payload,
                                 cast_known=True)
    if command_type == "shot.generate":
        # C4-B — compose bindings → effective payload, then run the
        # REAL inner-pipeline validation (caps, refs, cost, seed).
        _shot, _inner, _composed = _compose_shot_generate(
            session, project, payload)
        warnings += _validate(session, project, _inner, _composed)
    if command_type == "shot.video":
        # C4-D — same discipline for video: compose → the REAL
        # generate.video validation (modality, install state, i2v
        # support, frames/fps/device bounds).
        _vshot, _vinner, _vcomposed = _compose_shot_video(
            session, project, payload)
        warnings += _validate(session, project, _vinner, _vcomposed)
        if command_type == "shot.move" and payload.get("to_scene_id"):
            _get_scene(session, project, {"scene_id": payload["to_scene_id"]})
    if command_type.startswith("shot.cast."):
        _get_shot(session, project, payload)
        _get_character(session, project, payload)
        _check_cast_state(session, project, payload, partial=command_type != "shot.cast.assign")
        if command_type != "shot.cast.assign":
            _require_shot_cast(session, project, payload)
    if command_type == "character.duplicate":
        _get_character(session, project, payload)
    if command_type == "entity.assets.link":
        for k in ("entity_type", "entity_id", "asset_id"):
            if not payload.get(k):
                raise CommandError(422, f"{k} is required")
        if payload["entity_type"] not in sb.ENTITY_ASSET_TYPES:
            raise CommandError(422, f"unsupported entity_type '{payload['entity_type']}'")
        if payload.get("role", "reference") not in sb.ENTITY_ASSET_ROLES:
            raise CommandError(422, f"unsupported role '{payload.get('role')}'")
        ent = session.get(sb.ENTITY_MODELS[payload["entity_type"]], payload["entity_id"])
        if ent is None or ent.project_id != project.id:
            raise CommandError(404, f"{payload['entity_type']} not found in this project")
        asset = session.get(Asset, payload["asset_id"])
        if asset is None or asset.project_id != project.id:
            raise CommandError(404, "asset not found in this project")
        # C3-D1 — visual roles must point at an image asset; the
        # byte-level safe-decode gate runs at commit (storage is not
        # reachable from preview validation).
        role = payload.get("role", "reference")
        if role in sb.ENTITY_ASSET_IMAGE_ROLES and asset.kind != "image":
            raise CommandError(
                422, f"role '{role}' requires an image asset "
                     f"(got kind '{asset.kind}')")
        _validate_reference_weight(payload.get("weight"))
        if payload.get("set_id"):
            try:
                rs = sb.get_refset(session, payload["set_id"], project.id)
            except KeyError:
                raise CommandError(404, "reference set not found in this project") from None
            if payload["entity_type"] != "character" or rs.character_id != payload["entity_id"]:
                raise CommandError(
                    422, "reference set does not belong to this entity")
    if command_type == "entity.assets.unlink":
        from app.db.models import EntityAssetLink
        link = session.get(EntityAssetLink, payload.get("link_id") or "")
        if link is None or link.project_id != project.id:
            raise CommandError(404, "link not found in this project")
    if command_type == "entity.assets.update":
        from app.db.models import EntityAssetLink
        link = session.get(EntityAssetLink, payload.get("link_id") or "")
        if link is None or link.project_id != project.id:
            raise CommandError(404, "link not found in this project")
        label = payload.get("label")
        if label is not None and len(label) > 200:
            raise CommandError(422, "label must be ≤ 200 characters")
        if "weight" in payload:
            _validate_reference_weight(payload["weight"], allow_none=True)
        if "set_id" in payload and payload["set_id"]:
            try:
                rs = sb.get_refset(session, payload["set_id"], project.id)
            except KeyError:
                raise CommandError(404, "reference set not found in this project") from None
            if link.entity_type != "character" or rs.character_id != link.entity_id:
                raise CommandError(
                    422, "reference set does not belong to this entity")
    if command_type == "entity.assets.reorder":
        from app.db.models import EntityAssetLink
        etype = payload.get("entity_type")
        if etype not in sb.ENTITY_ASSET_TYPES:
            raise CommandError(422, f"unsupported entity_type '{etype}'")
        ent = session.get(sb.ENTITY_MODELS[etype], payload.get("entity_id") or "")
        if ent is None or ent.project_id != project.id:
            raise CommandError(404, f"{etype} not found in this project")
        ids = payload.get("ordered_link_ids")
        if (not isinstance(ids, list) or not ids
                or not all(isinstance(i, str) for i in ids)):
            raise CommandError(422, "ordered_link_ids must be a non-empty "
                                    "list of link ids")
        existing = {
            l.id for l in session.execute(
                select(EntityAssetLink).where(
                    EntityAssetLink.project_id == project.id,
                    EntityAssetLink.entity_type == etype,
                    EntityAssetLink.entity_id == ent.id)).scalars()
        }
        if payload.get("set_id"):
            try:
                rs = sb.get_refset(session, payload["set_id"], project.id)
            except KeyError:
                raise CommandError(404, "reference set not found in this project") from None
            existing = {
                l.id for l in sb.set_members(session, rs.id, project.id)}
        if len(set(ids)) != len(ids) or set(ids) != existing:
            raise CommandError(422, "ordered_link_ids must be exactly the "
                                    "complete set of this entity's links")
    # ---- C4-A: character reference sets ----
    if command_type == "refset.create":
        if not (payload.get("name") or "").strip():
            raise CommandError(422, "name is required")
        if len(payload["name"].strip()) > 120:
            raise CommandError(422, "name must be ≤ 120 characters")
        try:
            sb.get_character(session, payload.get("character_id") or "",
                             project.id)
        except KeyError:
            raise CommandError(404, "character not found in this project") from None
        _validate_refset_seed(payload.get("seed"))
    if command_type == "refset.update":
        try:
            sb.get_refset(session, payload.get("set_id") or "", project.id)
        except KeyError:
            raise CommandError(404, "reference set not found in this project") from None
        if "name" in payload and not (payload["name"] or "").strip():
            raise CommandError(422, "name may not be empty")
        if "seed" in payload:
            _validate_refset_seed(payload["seed"])
    if command_type == "refset.delete":
        try:
            sb.get_refset(session, payload.get("set_id") or "", project.id)
        except KeyError:
            raise CommandError(404, "reference set not found in this project") from None
    parts = _entity_parts(command_type)
    if parts:
        etype, op = parts
        if op == "create" and not (payload.get("name") or "").strip():
            raise CommandError(422, f"{etype} name is required")
        if op in ("update", "archive", "unarchive", "revert"):
            try:
                bibles.get_entity(session, etype, payload.get(f"{etype}_id") or "", project.id)
            except KeyError:
                raise CommandError(404, f"{etype} not found in this project") from None
    return warnings


# ---- M2 validation helpers (project isolation is the invariant) ----

def _get_scene(session: Session, project: Project, payload: dict):
    from app.domain import storyboard as sb
    try:
        return sb.get_scene(session, payload.get("scene_id") or "", project.id)
    except KeyError:
        raise CommandError(404, "scene not found in this project") from None


def _get_shot(session: Session, project: Project, payload: dict):
    from app.domain import storyboard as sb
    try:
        return sb.get_shot(session, payload.get("shot_id") or "", project.id)
    except KeyError:
        raise CommandError(404, "shot not found in this project") from None


def _get_character(session: Session, project: Project, payload: dict):
    from app.domain import storyboard as sb
    try:
        return sb.get_character(session, payload.get("character_id") or "", project.id)
    except KeyError:
        raise CommandError(404, "character not found in this project") from None


def _require_scene_cast(session: Session, project: Project, payload: dict) -> None:
    from app.db.models import SceneCharacter
    row = session.execute(
        select(SceneCharacter).where(
            SceneCharacter.scene_id == payload.get("scene_id"),
            SceneCharacter.character_id == payload.get("character_id"),
            SceneCharacter.project_id == project.id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise CommandError(404, "character is not in this scene's cast")


def _require_shot_cast(session: Session, project: Project, payload: dict) -> None:
    from app.db.models import ShotCharacter
    row = session.execute(
        select(ShotCharacter).where(
            ShotCharacter.shot_id == payload.get("shot_id"),
            ShotCharacter.character_id == payload.get("character_id"),
            ShotCharacter.project_id == project.id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise CommandError(404, "character is not in this shot's cast")


def _check_cast_state(session: Session, project: Project, payload: dict, *, partial: bool) -> None:
    """Validate cast.state: schema types, then project-scope on every
    reference_asset_id (rejects foreign + dead IDs before any write)."""
    from app.domain import cast_state
    try:
        st = cast_state.parse_shot_cast_state(payload.get("state"), partial=partial)
    except cast_state.CastStateError as e:
        raise CommandError(422, str(e)) from None
    for aid in st["reference_asset_ids"] or []:
        a = session.get(Asset, aid)
        if a is None or a.project_id != project.id:
            raise CommandError(404, "reference asset not found in this project")


def _check_generation(session: Session, project: Project, payload: dict,
                      adapter_id: str = "gen.image.sdcpp") -> None:
    """generate.image / .persistent: model must be known, licensed, declared
    compatible with the selected adapter, and actually installed; referenced
    shot/scene must belong to the project; device selection must be sane."""
    from app.domain import model_catalog, model_files

    from app.db.models import Scene, Shot

    if not (payload.get("prompt") or "").strip():
        raise CommandError(422, "prompt is required for image generation")
    spec = model_catalog.get(payload.get("model_key") or "")
    if spec is None:
        raise CommandError(422, f"unknown model '{payload.get('model_key')}' — pick an installed model")
    if spec.modality != "image" or "text2image" not in spec.modes:
        raise CommandError(
            422, f"{spec.display_name} is a {spec.modality} model — generate.image requires a text2image model")
    if spec.support not in ("production", "tested"):
        raise CommandError(
            422, f"{spec.display_name} is {spec.support} — catalog-visible but not usable for generation yet")
    if not spec.license.get("commercial_ok", True):
        raise CommandError(422, f"{spec.display_name} has a restricted license and cannot generate")
    if not model_catalog.serves_adapter(spec, adapter_id):
        raise CommandError(
            422, f"{spec.display_name} is not declared compatible with adapter "
                 f"'{adapter_id}' — its request contract is unverified for that path")
    with session.no_autoflush:
        weight = model_files.installed_file(session, None, spec)
    if weight is None:
        raise CommandError(
            422, f"{spec.display_name} is not installed — install it from the Model Manager first")

    device = payload.get("device", "auto")
    if isinstance(device, str) and device not in ("auto", "cpu"):
        raise CommandError(422, f"invalid device '{device}' — use 'auto', 'cpu', or a GPU index")
    if isinstance(device, bool) or (isinstance(device, (int, float)) and int(device) < 0):
        raise CommandError(422, "invalid GPU index")

    shot_id = payload.get("shot_id")
    if shot_id:
        shot = session.get(Shot, shot_id)
        if shot is None or shot.project_id != project.id:
            raise CommandError(404, "shot not found in this project")
        if payload.get("scene_id") and shot.scene_id != payload["scene_id"]:
            raise CommandError(422, "shot does not belong to the given scene")
    if payload.get("scene_id") and session.get(Scene, payload["scene_id"]) is None:
        raise CommandError(404, "scene not found in this project")


def _experimental_ok() -> bool:
    """Dev/validation escape hatch: ATOMIC_ALLOW_EXPERIMENTAL_GEN=1 lets
    'experimental' catalog entries generate so a candidate can be validated
    end-to-end before its support tier is raised. Off by default; never a
    shipped default — the UI shows the honest tier regardless."""
    import os
    return os.environ.get("ATOMIC_ALLOW_EXPERIMENTAL_GEN") == "1"


def _check_video_generation(session: Session, project: Project, payload: dict) -> None:
    """generate.video: multi-component video model installed, prompt or a
    project image asset for i2v, sane frames/fps/device."""
    from app.domain import model_catalog, model_files

    from app.db.models import Asset, Scene, Shot

    if not (payload.get("prompt") or "").strip() and not payload.get("image_asset_id"):
        raise CommandError(422, "a prompt or a source image is required for video generation")
    spec = model_catalog.get(payload.get("model_key") or "")
    if spec is None:
        raise CommandError(422, f"unknown model '{payload.get('model_key')}' — pick an installed model")
    if spec.modality != "video":
        raise CommandError(
            422, f"{spec.display_name} is a {spec.modality} model — generate.video requires a video model")
    if spec.support not in ("production", "tested") and not _experimental_ok():
        raise CommandError(
            422, f"{spec.display_name} is {spec.support} — catalog-visible but not usable for generation yet")
    if not spec.license.get("commercial_ok", True):
        raise CommandError(422, f"{spec.display_name} has a restricted license and cannot generate")
    with session.no_autoflush:
        weight = model_files.installed_file(session, None, spec)
        vae = model_files.installed_component(session, None, spec, "vae") if weight else None
        tenc = model_files.installed_component(session, None, spec, "text_encoder") if weight else None
    if weight is None:
        raise CommandError(
            422, f"{spec.display_name} is not installed — install it from the Model Manager first")
    if vae is None or tenc is None:
        raise CommandError(
            422, f"{spec.display_name} is missing components — reinstall it from the Model Manager")

    image_asset_id = payload.get("image_asset_id")
    if image_asset_id:
        if "image2video" not in spec.modes:
            raise CommandError(422, f"{spec.display_name} does not support image-to-video")
        a = session.get(Asset, image_asset_id)
        if a is None or a.project_id != project.id or a.kind != "image":
            raise CommandError(404, "source image not found in this project")

    frames = int(payload.get("frames") or spec.capabilities.get("frames", {}).get("default", 33))
    fc = spec.capabilities.get("frames", {})
    if not fc.get("min", 1) <= frames <= fc.get("max", 129):
        raise CommandError(422, f"frames {frames} outside {fc.get('min', 1)}–{fc.get('max', 129)} for this model")

    device = payload.get("device", "auto")
    if isinstance(device, str) and device not in ("auto", "cpu"):
        raise CommandError(422, f"invalid device '{device}' — use 'auto', 'cpu', or a GPU index")
    if isinstance(device, bool) or (isinstance(device, (int, float)) and int(device) < 0):
        raise CommandError(422, "invalid GPU index")

    shot_id = payload.get("shot_id")
    if shot_id:
        shot = session.get(Shot, shot_id)
        if shot is None or shot.project_id != project.id:
            raise CommandError(404, "shot not found in this project")
        if payload.get("scene_id") and shot.scene_id != payload["scene_id"]:
            raise CommandError(422, "shot does not belong to the given scene")
    if payload.get("scene_id") and session.get(Scene, payload["scene_id"]) is None:
        raise CommandError(404, "scene not found in this project")


def _check_storyboard_links(session: Session, project: Project, payload: dict) -> None:
    from app.db.models import Location, World
    for key, model, label in (
        ("world_id", World, "world"), ("location_id", Location, "location"),
    ):
        lid = payload.get(key)
        if lid is None:
            continue
        row = session.get(model, lid)
        if row is None or row.project_id != project.id:
            raise CommandError(404, f"{label} not found in this project")
    tid = payload.get("thumbnail_asset_id")
    if tid:
        a = session.get(Asset, tid)
        if a is None or a.project_id != project.id:
            raise CommandError(404, "thumbnail asset not found in this project")


def _check_shot_fields(payload: dict) -> None:
    from app.domain import storyboard as sb
    st = payload.get("shot_type")
    if st is not None and st not in sb.SHOT_TYPES:
        raise CommandError(422, f"unknown shot_type '{st}'")
    dur = payload.get("duration_sec")
    if dur is not None and float(dur) < 0:
        raise CommandError(422, "duration_sec cannot be negative")


def _diff_for(command_type: str, payload: dict) -> dict[str, Any]:
    """Small human-readable diff for previews."""
    return {"summary": _summary(command_type, payload)}


def _preview_cost_estimate(command_type: str, payload: dict) -> dict | None:
    """Structured authoritative estimate for the preview response.

    Recomputed through the same pure catalog functions _validate applied
    (pick model → estimate) so the returned figure IS the number the
    commit path validates against — the UI must render this value, and
    any client-side hint is preliminary only. None for commands without
    a catalog estimate or when the estimate is not computable."""
    if command_type not in ("generate.text.cloud", "generate.image.cloud",
                            "generate.image.img2img", "generate.image.edit"):
        return None
    from app.domain import bridge as _bridge
    from app.domain import cloud_cost as _cc
    provider = _bridge.get_provider(payload.get("provider_key"))
    if provider is None:
        return None
    prompt = (payload.get("prompt") or "").strip()
    if command_type == "generate.text.cloud":
        model = _cc.pick_text_model(provider, payload.get("model_id"))
        if model is None:
            return None
        est = _cc.estimate_text_cost(model, prompt)
    else:
        model = _cc.pick_image_model(provider, payload.get("model_id"))
        if model is None:
            return None
        est = _cc.estimate_image_cost(
            model, count=1, size=payload.get("size"),
            quality=payload.get("quality"), prompt=prompt)
    return {"est_cost_usd": est["est_cost_usd"],
            "unit": est.get("unit"), "currency": "USD",
            "is_estimate": True, "simulated": True,
            "settlement": "usage-based",
            "rates_checked": est.get("rates_checked")}


def preview(session: Session, project: Project, *, command_type: str, payload: dict,
            scope: str, origin: str, target_type: str | None, target_id: str | None) -> dict:
    if command_type not in SUPPORTED_COMMANDS:
        raise CommandError(422, f"unsupported command_type '{command_type}'")
    warnings = _validate(session, project, command_type, payload)
    # C4-B — shot.generate composes an inner image command; estimate and
    # resolved-reference surfacing run on the COMPOSED payload so the
    # approval reflects exactly what commit will build.
    eff, est_type = payload, command_type
    _sh = None
    if command_type == "shot.generate":
        try:
            _sh, est_type, eff = _compose_shot_generate(
                session, project, payload)
        except CommandError:
            est_type, eff = command_type, payload
    elif command_type == "shot.video":
        # C4-D — compose for the cost/vram estimate + resolved_shot pin.
        try:
            _sh, est_type, eff = _compose_shot_video(
                session, project, payload)
        except CommandError:
            est_type, eff = command_type, payload
    out = {
        "command": {
            "project_id": project.id,
            "command_type": command_type,
            "scope": scope,
            "origin": origin,
            "target_type": target_type,
            "target_id": target_id,
            "payload": payload,
        },
        "summary": _summary(command_type, payload),
        "affected": [{"type": "project", "id": project.id, "change": _summary(command_type, payload)}],
        "warnings": warnings,
        "diff": _diff_for(command_type, payload),
        "estimated_cost": {"compute": "local", "currency_usd": 0.0, "requires_approval": False},
        "cost_estimate": _preview_cost_estimate(est_type, eff),
        "current_revision": project.revision,
        "launches_generation": command_type in GENERATION_COMMANDS,
    }
    # C4-A — surface the resolved set contents so a UI approval can
    # fingerprint the ACTUAL assets+weights, not merely the set id; a
    # later set edit then changes the fingerprint. Commit re-resolves
    # and refuses a mismatch against expected_references.
    if eff.get("reference_set_id"):
        try:
            _rs, _entries, _ws = _refset_refs(session, project, eff)
            out["resolved_references"] = _entries
            # Seed + revision ride alongside: a pinned-seed or metadata
            # edit changes neither the member list nor the fingerprint,
            # so the approval pins the set identity itself.
            out["resolved_set"] = {"id": _rs.id, "seed": _rs.seed,
                                   "revision": _rs.revision}
        except KeyError:
            pass  # _validate already reported the missing set
    if (command_type in ("shot.generate", "shot.video")
            and _sh is not None):
        # C4-B/D — the approval also pins the SHOT's generation-relevant
        # state (prompt, source, cast, continuity); set fingerprints
        # alone can't see those edits.
        out["resolved_shot"] = _shot_gen_fingerprint(session, _sh, eff)
    return out


# ---------- commit helpers ----------

def _commit_entity(session: Session, project: Project, command: CreativeCommand,
                   etype: str, op: str, payload: dict, audit: dict) -> None:
    if op == "create":
        e = bibles.create_entity(
            session, project.id, etype,
            name=payload["name"], summary=payload.get("summary", ""),
            data=payload.get("data") or payload.get("fields"),
            world_id=payload.get("world_id"), command_id=command.id,
        )
        audit["after"][f"{etype}_id"] = e.id
        return
    try:
        e = bibles.get_entity(session, etype, payload.get(f"{etype}_id") or "", project.id)
    except KeyError:
        raise CommandError(404, f"{etype} not found in this project") from None
    if op == "update":
        before = bibles.update_entity(
            session, e,
            name=payload.get("name"), summary=payload.get("summary"),
            data=payload.get("data") or payload.get("fields"),
            world_id=payload.get("world_id", ...),
            command_id=command.id,
            change_summary=payload.get("change_summary") or _summary("x.update", payload),
        )
        audit["before"][f"{etype}_id"] = e.id
        audit["before"]["snapshot"] = before
        audit["after"][f"{etype}_id"] = e.id
    elif op in ("archive", "unarchive"):
        before_status = bibles.archive_entity(
            session, e, archived=(op == "archive"), command_id=command.id
        )
        audit["before"]["status"] = before_status
        audit["after"]["status"] = e.status
        audit["after"][f"{etype}_id"] = e.id
    elif op == "revert":
        try:
            bibles.revert_entity(
                session, e, to_revision=int(payload["to_revision"]), command_id=command.id
            )
        except KeyError:
            raise CommandError(404, "revision not found") from None
        audit["after"][f"{etype}_id"] = e.id
        audit["after"]["reverted_to"] = int(payload["to_revision"])


def _duplicate_project(session: Session, project: Project, new_name: str, command: CreativeCommand) -> Project:
    """Copy all project-owned rows to a new project id. Asset blobs are NOT
    copied — new asset rows/versions reference the same immutable CAS
    files, so duplication is O(rows) regardless of media size."""
    from app.db.models import Character, Location, PromptDraft as PD, World

    clone = Project(
        id=new_id(), name=new_name, status="active", template=project.template,
        kind=project.kind, aspect_ratio=project.aspect_ratio, width=project.width,
        height=project.height, fps_num=project.fps_num, fps_den=project.fps_den,
        style=project.style, storage_location_id=project.storage_location_id,
        settings_json={**(project.settings_json or {}), "duplicated_from": project.id},
        revision=1,
    )
    session.add(clone)
    session.flush()

    draft = session.execute(select(PD).where(PD.project_id == project.id)).scalar_one_or_none()
    if draft is not None:
        session.add(PD(id=new_id(), project_id=clone.id, text=draft.text, scope=draft.scope))

    world_map: dict[str, str] = {}
    for w in session.execute(select(World).where(World.project_id == project.id)).scalars():
        nw = World(
            id=new_id(), project_id=clone.id, name=w.name, summary=w.summary,
            lore_json=dict(w.lore_json or {}), status=w.status, revision=w.revision,
        )
        session.add(nw)
        world_map[w.id] = nw.id
    char_map: dict[str, str] = {}
    for c in session.execute(select(Character).where(Character.project_id == project.id)).scalars():
        nc = Character(
            id=new_id(), project_id=clone.id, name=c.name, summary=c.summary,
            profile_json=dict(c.profile_json or {}), status=c.status, revision=c.revision,
        )
        session.add(nc)
        char_map[c.id] = nc.id
    loc_map: dict[str, str] = {}
    for loc in session.execute(select(Location).where(Location.project_id == project.id)).scalars():
        nl = Location(
            id=new_id(), project_id=clone.id,
            world_id=world_map.get(loc.world_id or "", None),
            name=loc.name, summary=loc.summary,
            details_json=dict(loc.details_json or {}), status=loc.status, revision=loc.revision,
        )
        session.add(nl)
        loc_map[loc.id] = nl.id
    asset_map: dict[str, str] = {}
    for a in session.execute(
        select(Asset).where(Asset.project_id == project.id, Asset.status != "deleted")
    ).scalars():
        na = Asset(
            id=new_id(), project_id=clone.id, name=a.name, kind=a.kind, origin=a.origin,
            scope=a.scope, status=a.status, uri=a.uri, sha256=a.sha256,
            byte_size=a.byte_size, provenance_json={**(a.provenance_json or {}), "duplicated_from": a.id},
        )
        session.add(na)
        session.flush()
        asset_map[a.id] = na.id
        # Re-point each version at the same immutable blob.
        for v in session.execute(
            select(AssetVersion).where(AssetVersion.asset_id == a.id).order_by(AssetVersion.version_no)
        ).scalars():
            nv = AssetVersion(
                id=new_id(), asset_id=na.id, version_no=v.version_no, sha256=v.sha256,
                media_json=dict(v.media_json or {}), command_id=command.id,
            )
            session.add(nv)
            session.flush()
            if a.current_version_id == v.id:
                na.current_version_id = nv.id

    # M2 storyboard graph — scenes → scene cast → shots → shot cast,
    # then entity↔asset links. All references remap through the maps above.
    from app.db.models import EntityAssetLink, Scene, SceneCharacter, Shot, ShotCharacter

    scene_map: dict[str, str] = {}
    for s in session.execute(select(Scene).where(Scene.project_id == project.id)).scalars():
        ns = Scene(
            id=new_id(), project_id=clone.id, title=s.title, description=s.description,
            intent=s.intent, position=s.position, status=s.status,
            world_id=world_map.get(s.world_id or "", None),
            location_id=loc_map.get(s.location_id or "", None),
            continuity_json=dict(s.continuity_json or {}), revision=s.revision,
        )
        session.add(ns)
        session.flush()
        scene_map[s.id] = ns.id
    for scc in session.execute(
        select(SceneCharacter).where(SceneCharacter.project_id == project.id)
    ).scalars():
        if scc.scene_id not in scene_map or scc.character_id not in char_map:
            continue
        session.add(SceneCharacter(
            id=new_id(), project_id=clone.id, scene_id=scene_map[scc.scene_id],
            character_id=char_map[scc.character_id], position=scc.position,
            role=scc.role, notes_json=dict(scc.notes_json or {}),
        ))
    shot_map: dict[str, str] = {}
    for sh in session.execute(select(Shot).where(Shot.project_id == project.id)).scalars():
        if sh.scene_id not in scene_map:
            continue
        nsh = Shot(
            id=new_id(), project_id=clone.id, scene_id=scene_map[sh.scene_id],
            title=sh.title, description=sh.description, intent=sh.intent,
            prompt=sh.prompt, shot_type=sh.shot_type, duration_sec=sh.duration_sec,
            position=sh.position, status=sh.status,
            location_id=loc_map.get(sh.location_id or "", None),
            thumbnail_asset_id=asset_map.get(sh.thumbnail_asset_id or "", None),
            continuity_json=dict(sh.continuity_json or {}), revision=sh.revision,
        )
        session.add(nsh)
        session.flush()
        shot_map[sh.id] = nsh.id
    for shc in session.execute(
        select(ShotCharacter).where(ShotCharacter.project_id == project.id)
    ).scalars():
        if shc.shot_id not in shot_map or shc.character_id not in char_map:
            continue
        session.add(ShotCharacter(
            id=new_id(), project_id=clone.id, shot_id=shot_map[shc.shot_id],
            character_id=char_map[shc.character_id], position=shc.position,
            wardrobe_variant=shc.wardrobe_variant, blocking=shc.blocking,
            expression=shc.expression, emotion=shc.emotion,
            action_instructions=shc.action_instructions, dialogue=shc.dialogue,
            voice_override=shc.voice_override, model_ref=shc.model_ref,
            lora_json=list(shc.lora_json or []), continuity_json=dict(shc.continuity_json or {}),
        ))
    entity_id_map = {**char_map, **world_map, **loc_map}
    for link in session.execute(
        select(EntityAssetLink).where(EntityAssetLink.project_id == project.id)
    ).scalars():
        new_ent = entity_id_map.get(link.entity_id)
        new_asset = asset_map.get(link.asset_id)
        if not new_ent or not new_asset:
            continue
        session.add(EntityAssetLink(
            id=new_id(), project_id=clone.id, entity_type=link.entity_type,
            entity_id=new_ent, asset_id=new_asset, role=link.role,
            label=link.label, position=link.position, weight=link.weight,
        ))

    session.add(
        AuditEvent(
            id=new_id(), project_id=clone.id, event_type="project.duplicated_from",
            payload_json={"source_project_id": project.id, "command_id": command.id},
        )
    )
    return clone


def _inspect_reference_image(session: Session, asset_id: str, storage) -> None:
    """C3-D1 — byte-level safe-decode gate for visual reference links.
    Reads at most MAX_IMAGE_BYTES+1 of the current asset bytes and runs
    the same inspection a provider image must pass; ImageRejected
    becomes a 422."""
    from app.domain import image_safety
    from app.db.models import Asset

    asset = session.get(Asset, asset_id)
    if asset is None:
        raise CommandError(404, "asset not found in this project")
    if storage is None:
        raise CommandError(500, "storage unavailable for image validation")
    path = storage.asset_path(session, asset)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CommandError(422, f"asset bytes unreadable: {exc}") from exc
    try:
        image_safety.inspect_image(data[:image_safety.MAX_IMAGE_BYTES + 1])
    except image_safety.ImageRejected as exc:
        raise CommandError(422, f"reference image rejected: {exc}") from exc


def _commit_storyboard(session: Session, project: Project, command: CreativeCommand,
                       command_type: str, payload: dict, audit: dict,
                       storage=None) -> None:
    """Scene/shot/cast/entity-asset mutations. Cast ops also bump the parent
    scene/shot revision so history shows cast changes."""
    from app.db.models import EntityAssetLink

    def scene() -> "object":
        try:
            return sb.get_scene(session, payload.get("scene_id") or "", project.id)
        except KeyError:
            raise CommandError(404, "scene not found in this project") from None

    def shot() -> "object":
        try:
            return sb.get_shot(session, payload.get("shot_id") or "", project.id)
        except KeyError:
            raise CommandError(404, "shot not found in this project") from None

    def character() -> "object":
        try:
            return sb.get_character(session, payload.get("character_id") or "", project.id)
        except KeyError:
            raise CommandError(404, "character not found in this project") from None

    if command_type == "scene.create":
        s = sb.create_scene(
            session, project.id,
            title=payload["title"], description=payload.get("description", ""),
            intent=payload.get("intent", ""), world_id=payload.get("world_id"),
            location_id=payload.get("location_id"), continuity=payload.get("continuity"),
            command_id=command.id,
        )
        audit["after"]["scene_id"] = s.id
        return

    if command_type == "shot.create":
        sc = scene()
        sh = sb.create_shot(
            session, sc,
            title=payload.get("title", ""), shot_type=payload.get("shot_type", "medium"),
            description=payload.get("description", ""), intent=payload.get("intent", ""),
            prompt=payload.get("prompt", ""), duration_sec=payload.get("duration_sec", 0.0),
            location_id=payload.get("location_id"), continuity=payload.get("continuity"),
            command_id=command.id,
        )
        # C4-B — creation-time generation bindings follow the same
        # validation as shot.update (validated in _check_shot_bindings).
        if payload.get("reference_set_id") is not None \
                or payload.get("source_asset_id") is not None:
            try:
                sb.update_shot(
                    session, sh, fields={},
                    reference_set_id=payload.get("reference_set_id"),
                    source_asset_id=payload.get("source_asset_id"),
                    command_id=command.id,
                    change_summary="Bound generation references",
                )
            except KeyError as exc:
                raise CommandError(404, str(exc).strip("'")) from None
            except ValueError as exc:
                raise CommandError(422, str(exc)) from None
        audit["after"]["shot_id"] = sh.id
        return

    if command_type == "character.duplicate":
        c = character()
        dup = sb.duplicate_character(session, c, name=payload.get("name"), command_id=command.id)
        audit["after"]["character_id"] = dup.id
        return

    if command_type == "entity.assets.link":
        role = payload.get("role", "reference")
        if role in sb.ENTITY_ASSET_IMAGE_ROLES:
            # C3-D1 — byte-level safe-decode gate at commit (preview
            # validated kind only): the linked bytes must survive
            # format/size/pixel inspection like any provider image.
            _inspect_reference_image(session, payload["asset_id"], storage)
        link = sb.link_entity_asset(
            session, project.id,
            entity_type=payload["entity_type"], entity_id=payload["entity_id"],
            asset_id=payload["asset_id"], role=role,
            label=payload.get("label", ""),
            weight=(payload["weight"] if "weight" in payload else sb._UNSET),
            set_id=(payload["set_id"] if "set_id" in payload else sb._UNSET),
        )
        audit["after"]["link_id"] = link.id
        return
    if command_type == "entity.assets.unlink":
        link = session.get(EntityAssetLink, payload.get("link_id") or "")
        if link is None or link.project_id != project.id:
            raise CommandError(404, "link not found in this project")
        audit["before"]["link"] = sb._cols(link)
        session.delete(link)
        return
    if command_type == "entity.assets.update":
        audit["before"]["link"] = sb._cols(
            session.get(EntityAssetLink, payload["link_id"]))
        link = sb.update_entity_asset_link(
            session, project.id, payload["link_id"],
            label=(payload["label"] if "label" in payload else sb._UNSET),
            weight=(payload["weight"] if "weight" in payload else sb._UNSET),
            set_id=(payload["set_id"] if "set_id" in payload else sb._UNSET))
        audit["after"]["link"] = sb._cols(link)
        return
    # ---- C4-A: reference sets ----
    if command_type == "refset.create":
        c = character()
        rs = sb.create_refset(
            session, project.id, c,
            name=payload["name"].strip(),
            seed=payload.get("seed"),
            notes=payload.get("notes") or "")
        audit["after"]["set_id"] = rs.id
        return
    if command_type == "refset.update":
        rs = sb.get_refset(session, payload["set_id"], project.id)
        audit["before"]["set"] = sb._cols(rs)
        for f in ("name", "seed", "notes"):
            if f in payload:
                setattr(rs, f, payload[f])
        rs.revision += 1
        session.flush()
        audit["after"]["set"] = sb._cols(rs)
        return
    if command_type == "refset.delete":
        rs = sb.get_refset(session, payload["set_id"], project.id)
        audit["before"]["set"] = sb._cols(rs)
        sb.delete_refset(session, rs)
        audit["after"]["set_id"] = rs.id
        return
    if command_type == "entity.assets.reorder":
        audit["before"]["order"] = [
            l.id for l in sb.list_entity_assets(
                session, project.id,
                entity_type=payload["entity_type"],
                entity_id=payload["entity_id"])]
        try:
            links = sb.reorder_entity_asset_links(
                session, project.id,
                entity_type=payload["entity_type"],
                entity_id=payload["entity_id"],
                ordered_link_ids=payload["ordered_link_ids"],
                set_id=payload.get("set_id"))
        except ValueError as exc:
            raise CommandError(422, str(exc)) from exc
        audit["after"]["order"] = [l.id for l in links]
        return

    # --- scene-level ops ---
    if command_type == "scene.update":
        s = scene()
        fields = {f: payload[f] for f in sb.SCENE_FIELDS if f in payload}
        try:
            before = sb.update_scene(
                session, s, fields=fields,
                world_id=payload.get("world_id", ...), location_id=payload.get("location_id", ...),
                continuity=payload.get("continuity"), command_id=command.id,
                change_summary=payload.get("change_summary") or "Updated scene",
            )
        except KeyError as exc:
            raise CommandError(404, str(exc).strip("'")) from None
        audit["before"]["snapshot"] = before
        audit["after"]["scene_id"] = s.id
        return
    if command_type in ("scene.archive", "scene.unarchive"):
        s = scene()
        before_status = sb.set_scene_archived(
            session, s, archived=(command_type == "scene.archive"), command_id=command.id,
        )
        audit["before"]["status"] = before_status
        audit["after"]["scene_id"] = s.id
        return
    if command_type == "scene.revert":
        s = scene()
        try:
            sb.revert_scene(session, s, int(payload["to_revision"]), command_id=command.id)
        except KeyError:
            raise CommandError(404, "revision not found") from None
        audit["after"]["scene_id"] = s.id
        audit["after"]["reverted_to"] = int(payload["to_revision"])
        return
    if command_type == "scene.duplicate":
        s = scene()
        dup = sb.duplicate_scene(session, s, title=payload.get("title"), command_id=command.id)
        audit["after"]["scene_id"] = dup.id
        return
    if command_type == "scene.move":
        s = scene()
        before = sb.move_scene(session, s, int(payload.get("to_index", 0)), command_id=command.id)
        audit["before"]["scene_move"] = before
        audit["after"]["scene_id"] = s.id
        return

    # --- scene cast ---
    if command_type.startswith("scene.cast."):
        s = scene()
        c = character()
        if command_type == "scene.cast.assign":
            try:
                row = sb.scene_cast_assign(
                    session, s, c, role=payload.get("role", ""), notes=payload.get("notes"),
                )
            except ValueError as exc:
                raise CommandError(409, str(exc)) from None
            audit["after"]["cast_row"] = sb._cols(row)
            summary = f"Cast {c.name} in scene"
        elif command_type == "scene.cast.update":
            audit["before"]["cast_row"] = sb._cols(
                _scene_cast_row(session, s.id, c.id))
            row = sb.scene_cast_update(
                session, s, c.id,
                role=payload.get("role"), notes=payload.get("notes"),
                position=payload.get("position"),
            )
            audit["after"]["cast_row"] = sb._cols(row)
            summary = f"Updated {c.name}'s scene role"
        else:  # remove
            audit["before"]["cast_row"] = sb._cols(
                _scene_cast_row(session, s.id, c.id))
            sb.scene_cast_remove(session, s, c.id)
            summary = f"Removed {c.name} from scene cast"
        s.revision += 1
        s.updated_at = utcnow()
        session.flush()
        sb.write_scene_revision(session, s, command_id=command.id, summary=summary)
        return

    # --- shot-level ops ---
    if command_type == "shot.update":
        sh = shot()
        fields = {f: payload[f] for f in sb.SHOT_FIELDS if f in payload}
        try:
            before = sb.update_shot(
                session, sh, fields=fields,
                location_id=payload.get("location_id", ...),
                thumbnail_asset_id=payload.get("thumbnail_asset_id", ...),
                reference_set_id=payload.get("reference_set_id", ...),
                source_asset_id=payload.get("source_asset_id", ...),
                continuity=payload.get("continuity"), command_id=command.id,
                change_summary=payload.get("change_summary") or "Updated shot",
            )
        except KeyError as exc:
            raise CommandError(404, str(exc).strip("'")) from None
        except ValueError as exc:
            raise CommandError(422, str(exc)) from None
        audit["before"]["snapshot"] = before
        audit["after"]["shot_id"] = sh.id
        return
    if command_type in ("shot.archive", "shot.unarchive"):
        sh = shot()
        before_status = sb.set_shot_archived(
            session, sh, archived=(command_type == "shot.archive"), command_id=command.id,
        )
        audit["before"]["status"] = before_status
        audit["after"]["shot_id"] = sh.id
        return
    if command_type == "shot.revert":
        sh = shot()
        try:
            sb.revert_shot(session, sh, int(payload["to_revision"]), command_id=command.id)
        except KeyError:
            raise CommandError(404, "revision not found") from None
        audit["after"]["shot_id"] = sh.id
        audit["after"]["reverted_to"] = int(payload["to_revision"])
        return
    if command_type == "shot.duplicate":
        sh = shot()
        dup = sb.duplicate_shot(session, sh, title=payload.get("title"), command_id=command.id)
        audit["after"]["shot_id"] = dup.id
        return
    if command_type == "shot.move":
        sh = shot()
        before = sb.move_shot(
            session, sh, int(payload.get("to_index", 0)),
            to_scene_id=payload.get("to_scene_id"), command_id=command.id,
        )
        audit["before"]["shot_move"] = before
        audit["after"]["shot_id"] = sh.id
        return

    # --- shot cast ---
    if command_type.startswith("shot.cast."):
        sh = shot()
        c = character()
        if command_type == "shot.cast.assign":
            try:
                row = sb.shot_cast_assign(session, sh, c, state=payload.get("state") or {})
            except ValueError as exc:
                raise CommandError(409, str(exc)) from None
            audit["after"]["cast_row"] = sb._cols(row)
            summary = f"Cast {c.name} in shot"
        elif command_type == "shot.cast.update":
            audit["before"]["cast_row"] = sb._cols(
                _shot_cast_row(session, sh.id, c.id))
            try:
                row = sb.shot_cast_update(session, sh, c.id, state=payload.get("state") or {})
            except KeyError as exc:
                raise CommandError(404, str(exc)) from None
            audit["after"]["cast_row"] = sb._cols(row)
            summary = f"Updated {c.name}'s shot state"
        else:  # remove
            audit["before"]["cast_row"] = sb._cols(
                _shot_cast_row(session, sh.id, c.id))
            sb.shot_cast_remove(session, sh, c.id)
            summary = f"Removed {c.name} from shot cast"
        sh.revision += 1
        sh.updated_at = utcnow()
        session.flush()
        sb.write_shot_revision(session, sh, command_id=command.id, summary=summary)
        return

    raise CommandError(422, f"unsupported command_type '{command_type}'")


def _scene_cast_row(session: Session, scene_id: str, character_id: str):
    from app.db.models import SceneCharacter
    row = session.execute(
        select(SceneCharacter).where(
            SceneCharacter.scene_id == scene_id, SceneCharacter.character_id == character_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise CommandError(404, "character is not in this scene's cast")
    return row


def _shot_cast_row(session: Session, shot_id: str, character_id: str):
    from app.db.models import ShotCharacter
    row = session.execute(
        select(ShotCharacter).where(
            ShotCharacter.shot_id == shot_id, ShotCharacter.character_id == character_id
        )
    ).scalar_one_or_none()
    if row is None:
        raise CommandError(404, "character is not in this shot's cast")
    return row


def _share_asset(session: Session, project: Project, asset_id: str, audit: dict) -> Asset:
    asset = session.get(Asset, asset_id)
    audit["before"]["scope"] = asset.scope
    asset.scope = "shared"
    audit["after"]["scope"] = "shared"
    audit["after"]["asset_id"] = asset.id
    return asset


def _link_asset(session: Session, project: Project, asset: Asset, mode: str, audit: dict) -> Asset:
    if mode == "copy":
        clone_asset = Asset(
            id=new_id(), project_id=project.id, name=asset.name, kind=asset.kind,
            origin="shared", scope="project", status=asset.status, uri=asset.uri,
            sha256=asset.sha256, byte_size=asset.byte_size,
            provenance_json={"copied_from": asset.id, "source_project_id": asset.project_id},
        )
        session.add(clone_asset)
        session.flush()
        from app.db.models import AssetVersion
        for v in session.execute(
            select(AssetVersion).where(AssetVersion.asset_id == asset.id)
        ).scalars():
            nv = AssetVersion(
                id=new_id(), asset_id=clone_asset.id, version_no=v.version_no,
                sha256=v.sha256, media_json=dict(v.media_json or {}),
            )
            session.add(nv)
            session.flush()
            if asset.current_version_id == v.id:
                clone_asset.current_version_id = nv.id
        link = SharedAssetLink(
            id=new_id(), project_id=project.id, asset_id=clone_asset.id,
            source_project_id=asset.project_id, mode="copy",
        )
        session.add(link)
        audit["after"]["asset_id"] = clone_asset.id
        return clone_asset
    link = session.execute(
        select(SharedAssetLink).where(
            SharedAssetLink.project_id == project.id, SharedAssetLink.asset_id == asset.id
        )
    ).scalar_one_or_none()
    if link is None:
        session.add(SharedAssetLink(
            id=new_id(), project_id=project.id, asset_id=asset.id,
            source_project_id=asset.project_id, mode="link",
        ))
    audit["after"]["asset_id"] = asset.id
    return asset


def commit(session: Session, project: Project, *, command_type: str, payload: dict,
           scope: str, origin: str, target_type: str | None, target_id: str | None,
           expected_revision: int, idempotency_key: str,
           job_engine=None, storage=None, vault=None,
           session_factory=None, data_dir=None) -> dict:
    existing = session.execute(
        select(CreativeCommand).where(CreativeCommand.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        # Idempotent replay returns the recorded outcome and NEVER
        # re-runs the handler — but a reused key with a different
        # command type, project, or payload fails closed.
        same = (
            existing.command_type == command_type
            and existing.project_id == project.id
            and json.dumps(existing.payload_json, sort_keys=True,
                           default=str) == json.dumps(
                               payload, sort_keys=True, default=str))
        if not same:
            raise CommandError(
                409, "idempotency_key already committed with a "
                     "different payload — refusing to replay")
        return {"command": existing, "idempotent_replay": True}

    if project.status == "archived" and command_type != "project.restore":
        raise CommandError(409, "project is archived; restore it before making changes")
    if expected_revision != project.revision:
        raise CommandError(409, f"revision conflict: expected {expected_revision}, current {project.revision}")

    warnings = _validate(session, project, command_type, payload)
    before_revision = project.revision
    audit: dict[str, Any] = {"before": {}, "after": {}, "warnings": warnings}
    job: GenerationJob | None = None
    # B2a — validate_run must dispatch AFTER the command transaction
    # commits: the handler runs while this session holds the write lock,
    # and live dispatch books its own BEGIN IMMEDIATE transaction on a
    # separate session — dispatching here would deadlock on the same
    # database file.
    post_commit: Any | None = None
    # Full bridge payloads may be richer than the durable audit record —
    # e.g. watch.scan returns candidate paths to the live session while the
    # audit keeps only counts (data minimization).
    bridge_response: dict | None = None

    command = CreativeCommand(
        id=new_id(),
        project_id=project.id,
        origin=origin,
        command_type=command_type,
        scope=scope,
        target_type=target_type,
        target_id=target_id,
        payload_json=payload,
        expected_revision=expected_revision,
        idempotency_key=idempotency_key,
        status="committed",
    )
    session.add(command)
    session.flush()

    parts = _entity_parts(command_type)
    if command_type == "prompt.set_text":
        draft = session.execute(
            select(PromptDraft).where(PromptDraft.project_id == project.id)
        ).scalar_one_or_none()
        if draft is None:
            draft = PromptDraft(id=new_id(), project_id=project.id, text="", scope=scope)
            session.add(draft)
            session.flush()
        audit["before"]["text"] = draft.text
        audit["after"]["text"] = payload.get("text", "")
        draft.text = payload.get("text", "")

    elif command_type == "project.rename":
        name = (payload.get("name") or "").strip()
        if not name:
            raise CommandError(422, "name is required")
        audit["before"]["name"] = project.name
        audit["after"]["name"] = name
        project.name = name

    elif command_type in {"project.archive", "project.restore"}:
        new_status = "archived" if command_type == "project.archive" else "active"
        audit["before"]["status"] = project.status
        audit["after"]["status"] = new_status
        project.status = new_status

    elif command_type == "project.update_settings":
        for f in PROJECT_SETTING_FIELDS:
            if f in payload:
                audit["before"][f] = getattr(project, f)
                setattr(project, f, payload[f])
                audit["after"][f] = payload[f]
        if "settings" in payload:
            audit["before"]["settings"] = project.settings_json
            merged = {**(project.settings_json or {}), **(payload["settings"] or {})}
            project.settings_json = merged
            audit["after"]["settings"] = merged

    elif command_type == "project.duplicate":
        new_name = (payload.get("name") or f"{project.name} (copy)").strip()
        clone = _duplicate_project(session, project, new_name, command)
        audit["after"]["duplicate_project_id"] = clone.id

    elif command_type == "project.delete":
        # Open spend reservations must be reconciled first: deleting
        # now would orphan the hold — it would keep counting against
        # global budgets forever with no reachable reconcile route
        # (financial rows deliberately outlive the project).
        open_holds = session.execute(
            select(func.count()).select_from(SpendReservation).where(
                SpendReservation.project_id == project.id,
                SpendReservation.status == "open")).scalar_one()
        if open_holds:
            raise CommandError(
                422, f"cannot delete: {open_holds} spend reservation(s) "
                     "still open — reconcile them (spend.reconcile) or "
                     "wait for the jobs to finish first")
        audit["before"]["name"] = project.name
        audit["before"]["status"] = project.status
        # Hard delete: FK cascades remove child rows; blobs are immutable
        # and may still be referenced by duplicates/links — only
        # unreferenced files are unlinked.
        _delete_project_files(session, project.id)
        session.delete(project)
        session.add(AuditEvent(
            id=new_id(), project_id=None, event_type="project.deleted",
            payload_json={"project_id": project.id, "name": audit["before"]["name"],
                          "command_id": command.id},
        ))
        # Project row is gone — return early; no revision bump.
        return {"command": command, "job": None, "idempotent_replay": False,
                "warnings": warnings, "deleted": True}

    elif command_type == "asset.share":
        _share_asset(session, project, payload["asset_id"], audit)

    elif command_type in ("bridge.prepare", "bridge.launch"):
        from app.domain import bridge as _bridge
        provider = _bridge.get_provider(payload["provider_key"])
        strat, _err = _bridge.resolve_strategy(
            provider, command_type.rsplit(".", 1)[1],
            payload.get("strategy_id"))
        if command_type == "bridge.prepare":
            prepared, meta = _bridge.prepare_prompt(provider, strat, payload)
            audit["after"]["bridge"] = {
                "action": "prepare", **meta,
                "prepared_text": prepared, "char_count": len(prepared),
            }
        else:
            # Validate + audit only — the validated destination goes to the
            # client, which performs the shell.open after explicit user
            # interaction. The backend can never produce a browser window.
            url = _bridge.resolve_launch_url(provider, strat)
            audit["after"]["bridge"] = {
                "action": "launch", "provider_key": provider["key"],
                "strategy_id": strat["strategy_id"], "method": strat["method"],
                "url": url, "open_via": _bridge.OPEN_VIA,
            }

    elif command_type == "bridge.watch.scan":
        # Stage A ImportWatcher — audited read-only enumeration. Candidates
        # live only in the response; nothing is imported until a separate
        # bridge.import command confirms each file.
        import os
        from pathlib import Path
        from app.domain import bridge as _bridge
        from app.domain.providers import registry as _preg
        provider = _bridge.get_provider(payload["provider_key"])
        cfg = _preg.get_config(session).get(provider["key"], {})
        folder, _ferr = _bridge.validate_watch_folder(cfg)
        if folder is None:
            raise CommandError(422, _ferr)
        imported_norm: set[str] = set()
        for a in session.query(Asset).filter(
                Asset.project_id == project.id,
                Asset.origin == "external"):
            sp = ((a.provenance_json or {}).get("request") or {}) \
                .get("source_path")
            if sp:
                try:
                    imported_norm.add(
                        os.path.normcase(str(Path(sp).resolve(strict=False))))
                except OSError:
                    pass
        cands, truncated = _bridge.scan_watch_folder(
            folder, _bridge.watch_eligible_exts(provider), imported_norm)
        # Audit minimization: the durable record keeps only what answers
        # "a scan happened, how much did it find?" — full paths and the
        # candidate list go to the live response only, never to disk.
        audit["after"]["bridge"] = {
            "action": "watch_scan", "provider_key": provider["key"],
            "candidate_count": len(cands), "truncated": truncated,
            "folder_name": folder.name,
        }
        bridge_response = {
            "action": "watch_scan", "provider_key": provider["key"],
            "folder": str(folder), "candidates": cands,
            "truncated": truncated,
        }

    elif command_type in ("bridge.watch.enable", "bridge.watch.disable"):
        # Persisted opt-in/out — the flag lives in provider settings and is
        # re-read by the polling service every tick. Audited here as a
        # command; the command row IS the audit record (no path stored).
        from app.domain.providers import registry as _preg
        key = payload["provider_key"]
        cur = _preg.get_config(session).get(key, {})
        settings = dict(cur.get("settings") or {})
        enable = command_type.endswith("enable")
        settings["import_behavior"] = "poll" if enable else "manual"
        _preg.set_provider_config(session, key, {"settings": settings})
        audit["after"]["bridge"] = {
            "action": "watch_enable" if enable else "watch_disable",
            "provider_key": key,
        }
        bridge_response = dict(audit["after"]["bridge"])

    elif command_type == "bridge.cli.detect":
        # C1.5 §5 — read-only executable-path discovery. The binary is
        # never executed (no subprocess, no --version); a detected path is
        # unauthenticated and unverified. Paths go to the live response
        # only — the durable audit keeps status/counts, no filesystem data.
        from app.domain import bridge as _bridge
        from app.domain.cli_detect import detect_cli
        provider = _bridge.get_provider(payload["provider_key"])
        result = detect_cli(provider)
        audit["after"]["bridge"] = {
            "action": "cli_detect", "provider_key": provider["key"],
            "status": result["status"], "platform": result["platform"],
            "found_count": len(result["candidates"]),
            "skipped_untrusted": result["skipped_untrusted"],
        }
        bridge_response = {"action": "cli_detect", **result}

    elif command_type == "asset.link":
        asset = session.get(Asset, payload["asset_id"])
        _link_asset(session, project, asset, payload.get("mode", "link"), audit)

    elif (command_type not in ("shot.generate", "shot.video")
          and (command_type.startswith(
              ("scene.", "shot.", "entity.assets.", "refset."))
              or command_type == "character.duplicate")):
        _commit_storyboard(session, project, command, command_type, payload,
                           audit, storage=storage)

    elif parts:
        _commit_entity(session, project, command, parts[0], parts[1], payload, audit)

    elif command_type in JOB_COMMANDS:
        if job_engine is None:
            raise CommandError(500, "job engine unavailable")
        request = _job_request(session, project, command_type, payload,
                               storage=storage)
        # C2.0c — provider-key routing for cloud text; all other
        # command types keep their JOB_COMMANDS mapping.
        _adapter_id = (_cloud_adapter_id(job_engine._registry,
                                         payload["provider_key"])
                       if command_type == "generate.text.cloud"
                       # C4-B — the composed request decides which
                       # simulated image adapter runs (img2img when a
                       # source bound, else cloud text-to-image).
                       else ("cloud.image.img2img.sim"
                             if command_type == "shot.generate"
                             and request.get("mode") == "img2img"
                             else JOB_COMMANDS[command_type]))
        try:
            job = job_engine.enqueue(
                session,
                project_id=project.id,
                adapter_id=_adapter_id,
                kind=_job_kind(command_type),
                request=request,
                idempotency_key=f"job:{idempotency_key}",
                command_id=command.id,
                model_key=payload.get("model_key"),
                # Generation has no mid-flight checkpoints — an interrupted
                # gen job restarts from scratch (bounded, user-initiated).
                resumable=command_type not in (
                    "generate.image", "generate.image.persistent",
                    "generate.video", "shot.video",
                    # Remote-scoped jobs reconcile, never auto-resubmit —
                    # a resubmit could duplicate (simulated) billable work.
                    "generate.text.cloud", "generate.image.cloud",
                    "generate.image.img2img", "generate.image.edit"),
            )
        except Exception as exc:
            from app.domain.jobs import OpBusyError
            # A bridge clipboard tempfile must not survive a failed enqueue —
            # it belongs to this workflow and is never a user file.
            from app.domain import bridge as _b
            if request.get("delete_source_after") and \
                    _b.is_bridge_tmp(request.get("source_path") or ""):
                from pathlib import Path as _P
                _P(request["source_path"]).unlink(missing_ok=True)
            if isinstance(exc, OpBusyError):
                raise CommandError(409, str(exc)) from exc
            raise
        audit["after"]["job_id"] = job.id
        if command_type in ("generate.text.cloud", "generate.image.cloud",
                            "generate.image.img2img", "generate.image.edit"):
            # Minimized durable record — the prompt body lives in the job
            # request (like local generation), never re-echoed to audit.
            audit["after"]["cloud"] = {
                "provider_key": payload.get("provider_key"),
                "model_id": request.get("model_id"),
                "execution_target": "cloud",
                "est_cost_usd": request.get("est_cost_usd"),
                "is_estimate": True, "simulated": True,
            }
            # C2.0b — budget gate + open reservation in THIS transaction:
            # admission, reservation insert and job enqueue are atomic.
            # Environment is derived from the trusted adapter/runtime,
            # never from client input: adapters that can state their
            # transport truth (cloud.text.rest — all-mocked transports)
            # report it; otherwise adapter_kind decides (mock → simulated).
            from app.domain import spend as _spend
            adapter = job_engine._registry.get(_adapter_id)
            env = getattr(adapter, "spend_environment", None) or (
                "simulated" if adapter is not None
                and adapter.profile.adapter_kind == "mock" else "live")
            if env == "live":
                # Independent execution boundary: even a future adapter
                # reporting a live transport must pass the complete
                # policy gate (opt-in + model grant + credential +
                # evidence freshness) before financial booking. No
                # stored state, env var, config, or payload can skip it.
                from app.domain import live_gate as _lg
                if vault is None:
                    raise CommandError(500, "credential vault unavailable")
                allowed, code = _lg.dispatch_admission(
                    session, provider_key=request["provider_key"],
                    model_id=request.get("model_id") or "",
                    max_cost_usd=payload.get("max_cost_usd"), vault=vault)
                if not allowed:
                    raise CommandError(422, {
                        "error": code,
                        "detail": "live dispatch admission refused"})
            try:
                res = _spend.admit(
                    session, job_id=job.id, command_id=command.id,
                    project_id=project.id,
                    provider_key=request["provider_key"], environment=env,
                    est_usd=request.get("est_cost_usd"),
                    max_usd=payload.get("max_cost_usd"),
                    app_state=getattr(job_engine, "_app_state", None))
            except _spend.BudgetError as exc:
                raise CommandError(422, exc.detail) from exc
            audit["after"]["cloud"]["reservation_id"] = res.id
            audit["after"]["cloud"]["reserved_micro"] = res.reserved_micro
            audit["after"]["cloud"]["environment"] = env
        if command_type == "bridge.import":
            audit["after"]["bridge"] = {
                "action": "import", "job_id": job.id,
                "provider_key": payload.get("provider_key"),
                "external_source": request.get("external_source"),
            }

    elif command_type in ("spend.budget_set", "spend.budget_remove",
                          "spend.reconcile"):
        # C2.0b — budget config and explicit reconciliation go through
        # the same preview→commit→audit path. Amounts in audit are
        # micro-USD integers; nothing sensitive is stored.
        from app.domain import spend as _spend
        target = payload.get("target", "project")
        if command_type == "spend.reconcile":
            try:
                outcome = _spend.reconcile(
                    session, project_id=project.id,
                    job_id=payload["job_id"], outcome=payload["outcome"],
                    actual_usd=payload.get("actual_usd"),
                    accept_overage=bool(payload.get("accept_overage")),
                    note=payload.get("note"))
            except _spend.BudgetError as exc:
                raise CommandError(422, exc.detail) from exc
            audit["after"]["reconcile"] = {
                "job_id": outcome["job_id"], "outcome": payload["outcome"],
                "status": outcome["status"],
                "environment": outcome.get("environment"),
            }
        else:
            if target not in ("project", "global"):
                raise CommandError(422, "target must be 'project' or 'global'")
            budget_pid = project.id if target == "project" else None
            if command_type == "spend.budget_set":
                row, old = _spend.set_budget(
                    session, project_id=budget_pid,
                    scope=payload.get("scope", ""),
                    limit_usd=payload.get("limit_usd"),
                    label=payload.get("label", ""))
                audit["before"]["budget"] = {
                    "limit_micro": old, "scope": row.scope,
                    "target": target}
                audit["after"]["budget"] = {
                    "limit_micro": row.limit_micro, "scope": row.scope,
                    "target": target}
                if old is not None and row.limit_micro < old:
                    audit["warnings"].append(
                        "Budget lowered — existing open reservations are "
                        "unchanged; new admissions refuse until "
                        "consumption drops below the new limit")
            else:
                row = _spend.remove_budget(
                    session, project_id=budget_pid,
                    scope=payload.get("scope", ""))
                if row is None:
                    raise CommandError(404, "no such budget")
                audit["before"]["budget"] = {
                    "limit_micro": row.limit_micro, "scope": row.scope,
                    "target": target}
                audit["after"]["budget"] = None
                audit["warnings"].append(
                    "Budget removed — that scope is now unconstrained")

    elif command_type in ("cloud.live.validate_arm",
                          "cloud.live.validate_run"):
        # B2a — handled by the dedicated permit branch further down;
        # checked before the startswith branch so validate_* never
        # falls into the Stage A handler.
        from app.domain import live_permit as _lp
        from app.domain.workers.live_transport import LiveDispatchRefused
        if vault is None:
            raise CommandError(500, "credential vault unavailable")
        try:
            if command_type == "cloud.live.validate_arm":
                permit = _lp.arm(
                    session, project_id=project.id,
                    credential_ref=payload["credential_ref"],
                    prompt=payload["prompt"],
                    max_output_tokens=payload["max_output_tokens"],
                    max_cost_usd=payload["max_cost_usd"],
                    command_id=command.id, vault=vault,
                    data_dir=data_dir)
                # Prompt content stays in the job request only — the
                # audit records its length, never its body.
                audit["after"]["permit"] = {
                    "permit_id": permit["permit_id"],
                    "provider_key": permit["provider_key"],
                    "model_id": permit["model_id"],
                    "credential_ref": permit["credential_ref"],
                    "max_output_tokens": permit["max_output_tokens"],
                    "max_cost_usd": permit["max_cost_usd"],
                    "prompt_len": len(permit["prompt"]),
                    "stage": "B2a",
                }
            else:  # cloud.live.validate_run
                if session_factory is None:
                    raise CommandError(500, "session factory unavailable")
                job_id = new_id()
                # Durable job row created INSIDE this transaction —
                # the command commit and the job's 'validating' record
                # are atomic. A crash after commit leaves an auditable
                # accepted-but-never-dispatched state; the dispatch
                # itself transitions validating → dispatching →
                # succeeded|failed|unknown on its own session.
                session.add(GenerationJob(
                    id=job_id, project_id=project.id,
                    adapter_id="cloud.live.validate", kind="text",
                    idempotency_key=f"job:{idempotency_key}",
                    request_json={"stage": "B2a",
                                  "provider_key": _lp.PERMIT_PROVIDER,
                                  "model_id": _lp.PERMIT_MODEL},
                    status="validating", resumable=False,
                    execution_scope="remote",
                    command_id=command.id))
                session.flush()
                post_commit = (lambda: _lp.run_validation(
                    session_factory, project_id=project.id,
                    job_id=job_id,
                    job_idempotency_key=f"job:{idempotency_key}",
                    command_id=command.id, vault=vault,
                    data_dir=data_dir))
                audit["after"]["validate_run"] = {
                    "job_id": job_id,
                    "status": "dispatch_requested",
                    "stage": "B2a",
                }
        except (_lp.PermitRefused, LiveDispatchRefused) as exc:
            raise CommandError(
                422, {"error": exc.code,
                      "detail": str(exc)[:300]}) from exc

    elif command_type.startswith("cloud.live."):
        # C2 exit gate Stage A — durable enablement state only. These
        # commands never dispatch, never touch a transport, and never
        # read secret material (vault.status() metadata only).
        from app.domain import live_gate as _lg
        if vault is None:
            raise CommandError(500, "credential vault unavailable")
        try:
            if command_type == "cloud.live.enable":
                entry = _lg.enable(
                    session, provider_key=payload["provider_key"],
                    model_id=payload["model_id"],
                    credential_ref=payload["credential_ref"], vault=vault)
            elif command_type == "cloud.live.disable":
                entry = _lg.disable(
                    session, provider_key=payload["provider_key"],
                    reason=str(payload.get("reason") or "user"))
            elif command_type == "cloud.live.disable_all":
                _lg.disable_all(
                    session, reason=str(payload.get("reason") or "kill_switch"))
                entry = {"global_disabled": True}
            else:  # cloud.live.resume
                _lg.resume_global(session)
                entry = {"global_disabled": False}
        except _lg.LiveGateRefused as exc:
            raise CommandError(
                422, {"error": exc.code, "detail": str(exc)}) from exc
        audit["after"]["live"] = {
            "action": command_type.rsplit(".", 1)[1],
            "provider_key": payload.get("provider_key"),
            "model_id": payload.get("model_id"),
            "credential_ref": payload.get("credential_ref"),
            "stage": "A",
        }

    else:
        raise CommandError(422, f"unsupported command_type '{command_type}'")

    project.revision += 1
    session.add(
        ProjectRevision(
            id=new_id(), project_id=project.id, revision=project.revision,
            command_id=command.id, summary=_summary(command_type, payload),
        )
    )
    session.add(
        CommandTransaction(
            id=new_id(), command_id=command.id, project_id=project.id,
            before_revision=before_revision, after_revision=project.revision,
            status="applied", audit_json=audit,
        )
    )
    session.add(
        AuditEvent(
            id=new_id(), project_id=project.id, event_type="command.committed",
            payload_json={"command_id": command.id, "command_type": command_type,
                          "job_id": job.id if job else None},
        )
    )
    out: dict[str, Any] = {
        "command": command, "job": job, "idempotent_replay": False, "warnings": warnings,
        "post_commit": post_commit,
    }
    if command_type == "project.duplicate":
        out["duplicate_project_id"] = audit["after"].get("duplicate_project_id")
    if command_type.startswith("bridge."):
        out["bridge"] = bridge_response or audit["after"].get("bridge")
    return out


def _job_kind(command_type: str) -> str:
    return {
        "generate.image.mock": "image",
        "generate.image": "image",
        "generate.image.persistent": "image",
        "generate.video": "video",
        "generate.text.cloud": "text",
        "generate.image.cloud": "image",
        "generate.image.img2img": "image",
        "generate.image.edit": "image",
        "shot.generate": "image",
        "shot.video": "video",
        "asset.import": "import",
        "bridge.import": "import",
        "asset.relink": "relink",
        "assets.verify": "verify",
        "backup.export": "backup",
        "backup.import": "import",
        "backup.restore": "restore",
    }[command_type]


def _cloud_adapter_id(registry, provider_key: str) -> str:
    """Route cloud text jobs by provider: documented REST contract
    impls (REST_PROVIDER_KEYS) run through cloud.text.rest; every
    other catalog provider keeps the in-memory mock contract."""
    rest = registry.get("cloud.text.rest")
    if rest is not None and getattr(rest, "supports_provider",
                                   lambda k: False)(provider_key):
        return "cloud.text.rest"
    return JOB_COMMANDS["generate.text.cloud"]


def _job_request(session: Session, project: Project, command_type: str,
                 payload: dict, storage=None) -> dict:
    req: dict[str, Any] = {"command_type": command_type, **payload}
    if command_type == "shot.generate":
        # C4-B — compose the shot's bindings into an existing image
        # command, then let that pipeline build its own request (caps,
        # refs, est, provenance). Nothing about the pipeline is
        # duplicated — the request IS the inner command's request plus
        # shot.generate provenance.
        _shot, _inner, _composed = _compose_shot_generate(
            session, project, payload)
        req = _job_request(session, project, _inner, _composed,
                           storage=storage)
        req["origin_command_type"] = "shot.generate"
        req["shot_generate"] = {
            "shot_id": _shot.id, "scene_id": _shot.scene_id,
            "reference_set_id": _composed.get("reference_set_id"),
        }
        return req
    if command_type == "shot.video":
        # C4-D — compose the shot's bindings into a real generate.video
        # request; same engine, same admission, plus shot provenance.
        _shot, _inner, _composed = _compose_shot_video(
            session, project, payload)
        req = _job_request(session, project, _inner, _composed,
                           storage=storage)
        req["origin_command_type"] = "shot.video"
        req["shot_video"] = {
            "shot_id": _shot.id, "scene_id": _shot.scene_id,
            "source_asset_id": _composed.get("image_asset_id"),
            "model_key": _composed.get("model_key"),
        }
        return req
    if command_type == "generate.image.mock":
        req = {"prompt": payload["prompt"], "seed": payload.get("seed", 0)}
        if payload.get("shot_id"):
            req["shot_id"] = payload["shot_id"]
        req["set_thumbnail"] = bool(payload.get("set_thumbnail"))
    elif command_type in ("generate.image", "generate.image.persistent"):
        from app.domain import compatibility, model_catalog
        spec = model_catalog.get(payload.get("model_key") or "")
        req = {
            "command_type": command_type,
            "model_key": payload["model_key"],
            "prompt": payload["prompt"],
            "negative_prompt": payload.get("negative_prompt") or "",
            "width": int(payload.get("width", 512)),
            "height": int(payload.get("height", 512)),
            "steps": int(payload.get("steps", 25)),
            "cfg": float(payload.get("cfg", 7.0)),
            "seed": payload.get("seed", -1),
            "sampler": payload.get("sampler"),
            "batch": int(payload.get("batch", 1)),
            "device": payload.get("device", "auto"),
            "runtime_flavor": payload.get("runtime_flavor"),
            "shot_id": payload.get("shot_id"),
            "scene_id": payload.get("scene_id"),
            "set_thumbnail": bool(payload.get("set_thumbnail")),
            "residency_mode": payload.get("residency_mode"),
            "memory_risk_acknowledged": bool(payload.get("memory_risk_acknowledged")),
            "est_vram_bytes": compatibility.estimate_vram(
                spec, int(payload.get("width", 512)), int(payload.get("height", 512)),
                int(payload.get("batch", 1)), payload.get("residency_mode")) if spec else None,
            # models without a CPU path must stay queued (honest), not burn CPU
            "allow_cpu": bool(spec.capabilities.get("cpu_fallback")) if spec else True,
        }
    elif command_type == "generate.text.cloud":
        from app.domain import bridge as _bridge
        from app.domain import cloud_cost as _cc
        provider = _bridge.get_provider(payload["provider_key"])
        model = _cc.pick_text_model(provider, payload.get("model_id"))
        strat = _cc.t1_text_strategy(provider)
        est = _cc.estimate_text_cost(model, payload["prompt"])
        req = {
            "command_type": command_type,
            "provider_key": provider["key"],
            "model_id": model["id"],
            "strategy_id": strat["strategy_id"],
            "prompt": payload["prompt"],
            "execution_target": "cloud",
            "est_cost_usd": est["est_cost_usd"],
            "est_input_tokens": est["est_input_tokens"],
            # Catalog rates for the REST adapter's usage→cost map —
            # a locally calculated basis, never provider billing.
            "cost_table": model.get("cost_table"),
            "max_output_tokens": payload.get("max_output_tokens"),
            "cost_is_estimate": True,
            "data_out": _cc.data_out_summary(provider, payload["prompt"]),
            "simulated": True,
        }
    elif command_type == "generate.image.cloud":
        from app.domain import bridge as _bridge
        from app.domain import cloud_cost as _cc
        from app.domain.workers.rest_image_providers import (
            IMAGE_REST_KEYS)
        provider = _bridge.get_provider(payload.get("provider_key"))
        if provider is None:
            raise CommandError(404, "provider not found in catalog")
        if payload.get("provider_key") not in IMAGE_REST_KEYS:
            raise CommandError(
                422, f"no image contract implemented for provider "
                     f"'{payload.get('provider_key')}'")
        prompt = (payload.get("prompt") or "").strip()
        if not prompt:
            raise CommandError(422, "prompt is required")
        model = _cc.pick_image_model(provider, payload.get("model_id"))
        strat = _cc.t1_image_strategy(provider)
        if model is None or strat is None:
            raise CommandError(
                422, "provider has no documented image strategy/model")
        est = _cc.estimate_image_cost(
            model, count=1, size=payload.get("size"),
            quality=payload.get("quality"), prompt=prompt)
        if est["est_cost_usd"] is None:
            raise CommandError(422, "pricing_unavailable")
        req = {
            "command_type": command_type,
            "provider_key": provider["key"],
            "model_id": model["id"],
            "strategy_id": strat["strategy_id"],
            "prompt": prompt,
            "negative_prompt": payload.get("negative_prompt") or "",
            "execution_target": "cloud",
            "size": payload.get("size"),
            "quality": payload.get("quality"),
            "seed": payload.get("seed"),
            "count": 1,
            "est_cost_usd": est["est_cost_usd"],
            "cost_table": model.get("cost_table"),
            "cost_is_estimate": True,
            "data_out": _cc.data_out_summary(provider, prompt),
            "simulated": True,
            # Filmmaking linkage — passthrough into request_json and the
            # asset provenance; no schema change needed.
            "scene_id": payload.get("scene_id"),
            "shot_id": payload.get("shot_id"),
            "character_id": payload.get("character_id"),
            "world_id": payload.get("world_id"),
            "location_id": payload.get("location_id"),
            "reference_asset_id": payload.get("reference_asset_id"),
        }
    elif command_type == "generate.image.img2img":
        # C3-D2 — resolve source/reference CAS payloads at commit time:
        # existence + project ownership + kind were checked at preview,
        # and every byte payload passes the image-safety decode gate
        # here again before the job row is written. The job stores CAS
        # paths + content hashes (never raw bytes); the adapter reads,
        # re-inspects, and base64s them into the provider-shaped submit
        # body at run time. Source-to-output lineage is recorded in the
        # output asset's provenance media (see _media_extra).
        from app.db.models import Asset
        from app.domain import bridge as _bridge
        from app.domain import cloud_cost as _cc
        from app.domain import image_safety
        provider = _bridge.get_provider(payload.get("provider_key"))
        if provider is None:
            raise CommandError(404, "provider not found in catalog")
        model = _cc.pick_image_model(provider, payload.get("model_id"))
        strat = _cc.t1_image_strategy(provider)
        if model is None or strat is None:
            raise CommandError(
                422, "provider has no documented image strategy/model")
        # Commit-time capability gate — a direct commit must not bypass
        # the declared/implemented contract checks preview performed.
        from app.domain.providers import catalog as _pc
        from app.domain.workers.rest_image_providers import IMAGE_SPECS
        caps = _pc.model_editing_caps(provider["key"], model["id"])
        if not caps.get("img2img"):
            raise CommandError(
                422, f"model '{model['id']}' does not declare img2img "
                     "editing support")
        _spec = IMAGE_SPECS.get(provider["key"])
        if _spec is None or _spec.edit_path is None:
            raise CommandError(
                422, "no img2img edits contract implemented for provider "
                     f"'{provider['key']}'")
        est = _cc.estimate_image_cost(
            model, count=1, size=payload.get("size"),
            quality=payload.get("quality"),
            prompt=(payload.get("prompt") or "").strip())
        if est["est_cost_usd"] is None:
            raise CommandError(422, "pricing_unavailable")

        def _resolve_image(aid: str, label: str) -> tuple:
            return _resolve_project_image(session, storage, project,
                                          aid, label)

        src, src_path = _resolve_image(
            payload.get("source_asset_id") or "", "source image")
        # Commit-time re-check of the preview gates — a direct commit
        # must not carry a combination preview would have refused.
        ref_set = None
        if payload.get("reference_set_id"):
            if payload.get("reference_asset_ids"):
                raise CommandError(
                    422, "reference_set_id and reference_asset_ids "
                         "are mutually exclusive")
            try:
                ref_set, entries, refset_weights = _refset_refs(
                    session, project, payload)
            except KeyError:
                raise CommandError(404, "reference set not found in this project") from None
            ref_ids = [e["asset_id"] for e in entries]
            # Stale-approval guard: if the caller previewed a resolved
            # set and it changed since, refuse rather than silently
            # generating against different references. Membership,
            # ordering, weights, and content hashes live in
            # expected_references; seed/revision live in expected_set.
            expected = payload.get("expected_references")
            if expected is not None and expected != entries:
                raise CommandError(
                    422, "reference set changed since preview — "
                         "re-preview the exact resolved references")
            expected_set = payload.get("expected_set")
            if expected_set is not None:
                current_set = {"id": ref_set.id, "seed": ref_set.seed,
                               "revision": ref_set.revision}
                if expected_set != current_set:
                    raise CommandError(
                        422, "reference set changed since preview — "
                             "re-preview the exact resolved references")
            weights = refset_weights
        else:
            ref_ids = list(payload.get("reference_asset_ids") or [])
            weights = payload.get("reference_weights") or []
        if len(ref_ids) > int(caps.get("max_reference_images") or 0):
            raise CommandError(
                422, "reference count exceeds the model's declared limit")
        if src.id in ref_ids or len(set(ref_ids)) != len(ref_ids):
            raise CommandError(
                422, "duplicate asset in source/references")
        if weights:
            if not caps.get("reference_weights"):
                raise CommandError(
                    422, "this model does not implement reference weights")
            if len(weights) != len(ref_ids):
                raise CommandError(
                    422, "reference_weights must match reference_asset_ids")
            for w in weights:
                _validate_reference_weight(w)
        refs, ref_paths, ref_shas = [], [], []
        for rid in ref_ids:
            r, rp = _resolve_image(rid, "reference image")
            refs.append(r.id)
            ref_paths.append(str(rp))
            ref_shas.append(r.sha256)
        req = {
            "command_type": command_type,
            "provider_key": provider["key"],
            "model_id": model["id"],
            "strategy_id": strat["strategy_id"],
            "mode": "img2img",
            "prompt": (payload.get("prompt") or "").strip(),
            "negative_prompt": payload.get("negative_prompt") or "",
            "execution_target": "cloud",
            "size": payload.get("size"),
            "quality": payload.get("quality"),
            # C4-A — the set's pinned seed is guidance used only when no
            # explicit seed was given; never an identity guarantee.
            "seed": payload.get("seed")
                if payload.get("seed") is not None
                else (ref_set.seed if ref_set else None),
            "count": 1,
            "est_cost_usd": est["est_cost_usd"],
            "cost_table": model.get("cost_table"),
            "cost_is_estimate": True,
            "data_out": _cc.data_out_summary(
                provider, (payload.get("prompt") or "").strip()),
            "simulated": True,
            "source_asset_id": src.id,
            "source_sha256": src.sha256,
            "source_path": str(src_path),
            "reference_asset_ids": refs,
            "reference_paths": ref_paths,
            "reference_sha256": ref_shas,
            "reference_weights": weights,
            # C4-A provenance: which set produced the resolved list.
            "reference_set_id": ref_set.id if ref_set else None,
            "reference_set_revision": ref_set.revision if ref_set else None,
            "scene_id": payload.get("scene_id"),
            "shot_id": payload.get("shot_id"),
            "character_id": payload.get("character_id"),
            "world_id": payload.get("world_id"),
            "location_id": payload.get("location_id"),
        }
    elif command_type == "generate.image.edit":
        # C3-D3 — commit-time re-validation: capability gates, mask
        # convention (exact dims match, editable region), and outpaint
        # bounds are all checked here against the real CAS payloads so a
        # direct commit cannot bypass preview's refusals.
        from app.domain import bridge as _bridge
        from app.domain import cloud_cost as _cc
        from app.domain import image_edit, image_safety
        from app.domain.providers import catalog as _pc
        from app.domain.workers.rest_image_providers import IMAGE_SPECS
        operation = payload.get("operation")
        if operation not in ("inpaint", "outpaint"):
            raise CommandError(422, "operation must be 'inpaint' or 'outpaint'")
        cap_key = "mask_inpaint" if operation == "inpaint" else "outpaint"
        provider = _bridge.get_provider(payload.get("provider_key"))
        if provider is None:
            raise CommandError(404, "provider not found in catalog")
        model = _cc.pick_image_model(provider, payload.get("model_id"))
        strat = _cc.t1_image_strategy(provider)
        if model is None or strat is None:
            raise CommandError(
                422, "provider has no documented image strategy/model")
        caps = _pc.model_editing_caps(provider["key"], model["id"])
        if not caps.get(cap_key):
            raise CommandError(
                422, f"model '{model['id']}' does not declare {operation} "
                     "editing support")
        _spec = IMAGE_SPECS.get(provider["key"])
        _path = (_spec.inpaint_path if operation == "inpaint"
                 else _spec.outpaint_path) if _spec else None
        if _path is None:
            raise CommandError(
                422, f"no {operation} contract implemented for provider "
                     f"'{provider['key']}'")
        est = _cc.estimate_image_cost(
            model, count=1, size=payload.get("size"),
            quality=payload.get("quality"),
            prompt=(payload.get("prompt") or "").strip())
        if est["est_cost_usd"] is None:
            raise CommandError(422, "pricing_unavailable")
        src, src_path = _resolve_project_image(
            session, storage, project,
            payload.get("source_asset_id") or "", "source image")
        src_info = image_safety.inspect_image(src_path.read_bytes())
        extra_req: dict = {}
        if operation == "inpaint":
            mask, mask_path = _resolve_project_image(
                session, storage, project,
                payload.get("mask_asset_id") or "", "mask image")
            if mask.id == src.id:
                raise CommandError(
                    422, "mask must be a distinct asset from the source")
            try:
                image_edit.inspect_edit_mask(
                    mask_path.read_bytes(),
                    src_w=src_info.width, src_h=src_info.height)
            except Exception as exc:
                raise CommandError(422, f"invalid edit mask: {exc}")
            extra_req = {"mask_asset_id": mask.id,
                         "mask_sha256": mask.sha256,
                         "mask_path": str(mask_path)}
        else:
            try:
                out_w, out_h = int(payload.get("out_width")), int(payload.get("out_height"))
            except (TypeError, ValueError):
                raise CommandError(
                    422, "outpaint requires integer out_width/out_height")
            placement = payload.get("placement") or "center"
            try:
                image_edit.validate_outpaint(
                    src_info.width, src_info.height, out_w, out_h)
                pad = image_edit.compute_pad(
                    src_info.width, src_info.height, out_w, out_h, placement)
            except Exception as exc:
                raise CommandError(422, f"invalid outpaint request: {exc}")
            extra_req = {"out_width": out_w, "out_height": out_h,
                         "placement": placement, "pad": pad}
        ref_ids = list(payload.get("reference_asset_ids") or [])
        if len(ref_ids) > int(caps.get("max_reference_images") or 0):
            raise CommandError(
                422, "reference count exceeds the model's declared limit")
        if src.id in ref_ids or len(set(ref_ids)) != len(ref_ids):
            raise CommandError(
                422, "duplicate asset in source/references")
        refs, ref_paths, ref_shas = [], [], []
        for rid in ref_ids:
            r, rp = _resolve_project_image(
                session, storage, project, rid, "reference image")
            refs.append(r.id)
            ref_paths.append(str(rp))
            ref_shas.append(r.sha256)
        req = {
            "command_type": command_type,
            "provider_key": provider["key"],
            "model_id": model["id"],
            "strategy_id": strat["strategy_id"],
            "mode": operation,
            "prompt": (payload.get("prompt") or "").strip(),
            "negative_prompt": payload.get("negative_prompt") or "",
            "execution_target": "cloud",
            "size": payload.get("size"),
            "quality": payload.get("quality"),
            "seed": payload.get("seed"),
            "count": 1,
            "est_cost_usd": est["est_cost_usd"],
            "cost_table": model.get("cost_table"),
            "cost_is_estimate": True,
            "data_out": _cc.data_out_summary(
                provider, (payload.get("prompt") or "").strip()),
            "simulated": True,
            "source_asset_id": src.id,
            "source_sha256": src.sha256,
            "source_path": str(src_path),
            "reference_asset_ids": refs,
            "reference_paths": ref_paths,
            "reference_sha256": ref_shas,
            "scene_id": payload.get("scene_id"),
            "shot_id": payload.get("shot_id"),
            "character_id": payload.get("character_id"),
            "world_id": payload.get("world_id"),
            "location_id": payload.get("location_id"),
            **extra_req,
        }
    elif command_type == "generate.video":
        from app.domain import compatibility, model_catalog
        spec = model_catalog.get(payload.get("model_key") or "")
        req = {
            "command_type": command_type,
            "model_key": payload["model_key"],
            "prompt": payload.get("prompt") or "",
            "negative_prompt": payload.get("negative_prompt") or "",
            "width": int(payload.get("width", 832)),
            "height": int(payload.get("height", 480)),
            "frames": int(payload.get("frames", 33)),
            "fps": int(payload.get("fps", 24)),
            "steps": int(payload.get("steps", 30)),
            "cfg": float(payload.get("cfg", 7.0)),
            "seed": payload.get("seed", -1),
            "sampler": payload.get("sampler"),
            "device": payload.get("device", "auto"),
            "runtime_flavor": payload.get("runtime_flavor"),
            "image_asset_id": payload.get("image_asset_id"),
            "shot_id": payload.get("shot_id"),
            "scene_id": payload.get("scene_id"),
            "residency_mode": payload.get("residency_mode"),
            "max_vram_gib": payload.get("max_vram_gib"),
            "memory_risk_acknowledged": bool(payload.get("memory_risk_acknowledged")),
            "est_vram_bytes": compatibility.estimate_vram(
                spec, int(payload.get("width", 832)), int(payload.get("height", 480)), 1,
                payload.get("residency_mode")) if spec else None,
            "allow_cpu": False,  # Wan-class video on CPU is days, not minutes
        }
        # i2v: resolve the project image asset to an on-disk path for the
        # worker via the CAS resolver (location root + blob rel_path).
        if payload.get("image_asset_id"):
            from app.db.models import Asset
            a = session.get(Asset, payload["image_asset_id"])
            if a is not None and storage is not None:
                p = storage.asset_path(session, a)
                if p is not None:
                    req["image_path"] = str(p)
    if command_type == "backup.export":
        req["kind"] = payload.get("kind", "backup")
    if command_type == "bridge.import":
        import os
        import tempfile
        from pathlib import Path
        from app.domain import bridge as _bridge
        provider = _bridge.get_provider(payload["provider_key"])
        strat, _err = _bridge.resolve_strategy(
            provider, "import", payload.get("strategy_id"))
        req = {
            "command_type": command_type,
            "source_path": (payload.get("source_path") or "").strip() or None,
            "name": payload.get("name"),
            "kind": payload.get("kind", "other"),
            "external_source": {
                "provider_key": provider["key"],
                "strategy_id": strat["strategy_id"],
                "method": strat["method"],
                "note": (payload.get("note") or "")[:200],
            },
        }
        if req["kind"] == "text" and req["source_path"]:
            # Honest plain-text gate: ingest stores bytes verbatim — there
            # is no document extraction, so binary containers must never
            # be labeled readable text. BOM-prefixed files stay decodable.
            try:
                head = Path(req["source_path"]).read_bytes()[:4096]
            except OSError:
                head = b""  # missing files are reported by job validation
            is_bin = head.startswith((b"PK\x03\x04", b"%PDF-")) or (
                b"\x00" in head
                and not head.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")))
            if is_bin:
                raise CommandError(
                    422, "not a plain-text file — export Word/PDF documents "
                         "to .txt or .md before importing")
        if payload.get("content") is not None:
            # Clipboard payload → temp file the ops adapter ingests, then
            # deletes. The frontend passes content the user explicitly
            # pasted; the backend never reads the clipboard itself.
            fd, tmp = tempfile.mkstemp(prefix=_bridge.TMP_PREFIX, suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload["content"])
            req["source_path"] = tmp
            req["delete_source_after"] = True
            if not req["name"]:
                req["name"] = f"clipboard-import-{provider['key']}.txt"
    return req


def _delete_project_files(session: Session, project_id: str) -> None:
    """Delete blob files that become unreferenced after a project delete.
    A blob is unreferenced when no remaining AssetVersion points at it."""
    from app.db.models import AssetFile, AssetVersion, StorageLocation

    referenced = {
        row[0]
        for row in session.execute(select(AssetVersion.sha256).distinct()).all()
    }
    orphans = session.execute(
        select(AssetFile).where(AssetFile.sha256.notin_(referenced))
    ).scalars().all()
    for af in orphans:
        loc = session.get(StorageLocation, af.storage_location_id)
        if loc is not None:
            try:
                from app.domain.storage import safe_join
                path = safe_join(loc.root_path, af.rel_path)
                if path.exists() and BLOB_PREFIX in path.parts:
                    path.unlink()
            except OSError:
                pass
        session.delete(af)


BLOB_PREFIX = "blobs"


def undo(session: Session, project: Project, *, command_id: str) -> CreativeCommand:
    command = session.get(CreativeCommand, command_id)
    if command is None or command.project_id != project.id:
        raise CommandError(404, "command not found in this project")
    if command.status != "committed":
        raise CommandError(409, f"command already {command.status}")
    if command.command_type not in UNDOABLE:
        raise CommandError(
            409,
            "this command cannot be undone (completed generation/jobs are not reversible; "
            "external compute cannot be refunded)",
        )

    txn = session.execute(
        select(CommandTransaction).where(CommandTransaction.command_id == command.id)
    ).scalar_one_or_none()
    audit_json = (txn.audit_json or {}) if txn else {}
    before = audit_json.get("before", {})
    after = audit_json.get("after", {})

    if command.command_type == "prompt.set_text":
        draft = session.execute(
            select(PromptDraft).where(PromptDraft.project_id == project.id)
        ).scalar_one_or_none()
        if draft is not None:
            draft.text = before.get("text", draft.text)
    elif command.command_type == "project.rename":
        project.name = before.get("name", project.name)
    elif command.command_type == "project.update_settings":
        for f in PROJECT_SETTING_FIELDS:
            if f in before:
                setattr(project, f, before[f])
        if "settings" in before:
            project.settings_json = before["settings"]
    elif command.command_type.startswith(("scene.", "shot.", "entity.assets.")):
        _undo_storyboard(session, project, command, before, after)
    else:
        parts = _entity_parts(command.command_type)
        if parts:
            etype = parts[0]
            entity_id = before.get(f"{etype}_id")
            if entity_id:
                try:
                    entity = bibles.get_entity(session, etype, entity_id, project.id)
                except KeyError:
                    entity = None
                if entity is not None:
                    if "snapshot" in before:
                        snap = before["snapshot"]
                        col = bibles.DATA_COLUMNS[etype]
                        entity.name = snap["name"]
                        entity.summary = snap["summary"]
                        setattr(entity, col, snap.get(col) or {})
                        if etype == "location":
                            entity.world_id = snap.get("world_id")
                    elif "status" in before:
                        entity.status = before["status"]
                    entity.revision += 1
                    bibles.write_revision(
                        session, entity, command_id=command.id,
                        summary="Undo",
                    )

    command.status = "undone"
    session.add(
        AuditEvent(
            id=new_id(), project_id=project.id, event_type="command.undone",
            payload_json={"command_id": command.id},
        )
    )
    return command


def _undo_storyboard(session: Session, project: Project, command: CreativeCommand,
                     before: dict, after: dict) -> None:
    """Reverse M2 mutations from their audit records."""
    from app.db.models import EntityAssetLink, Scene, SceneCharacter, Shot, ShotCharacter

    ct = command.command_type

    def get_scene() -> Scene | None:
        sid = after.get("scene_id") or before.get("scene_id") or command.target_id
        row = session.get(Scene, sid) if sid else None
        return row if row and row.project_id == project.id else None

    def get_shot() -> Shot | None:
        sid = after.get("shot_id") or before.get("shot_id") or command.target_id
        row = session.get(Shot, sid) if sid else None
        return row if row and row.project_id == project.id else None

    if ct in ("scene.update", "scene.archive", "scene.unarchive"):
        s = get_scene()
        if s is None:
            return
        if "snapshot" in before:
            snap = before["snapshot"]
            for f in sb.SCENE_FIELDS + ("status", "world_id", "location_id",
                                        "continuity_json", "position"):
                if f in snap:
                    setattr(s, f, snap[f])
            sb._restore_scene_cast(session, s, snap.get("cast") or [])
        elif "status" in before:
            s.status = before["status"]
        s.revision += 1
        s.updated_at = utcnow()
        session.flush()
        sb.write_scene_revision(session, s, command_id=command.id, summary="Undo")
        return

    if ct == "scene.move":
        for sid, pos in (before.get("scene_move", {}).get("positions") or {}).items():
            row = session.get(Scene, sid)
            if row and row.project_id == project.id:
                row.position = pos
        s = get_scene()
        if s is not None:
            s.revision += 1
            session.flush()
            sb.write_scene_revision(session, s, command_id=command.id, summary="Undo move")
        return

    if ct in ("shot.update", "shot.archive", "shot.unarchive"):
        sh = get_shot()
        if sh is None:
            return
        if "snapshot" in before:
            snap = before["snapshot"]
            for f in sb.SHOT_FIELDS + ("status", "scene_id", "location_id",
                                       "thumbnail_asset_id", "continuity_json", "position"):
                if f in snap:
                    setattr(sh, f, snap[f])
            sb._restore_shot_cast(session, sh, snap.get("cast") or [])
        elif "status" in before:
            sh.status = before["status"]
        sh.revision += 1
        sh.updated_at = utcnow()
        session.flush()
        sb.write_shot_revision(session, sh, command_id=command.id, summary="Undo")
        return

    if ct == "shot.move":
        mv = before.get("shot_move", {})
        sh = get_shot()
        if sh is not None and mv.get("scene_id"):
            # Move back to the original scene, then restore positions in both.
            sh.scene_id = mv["scene_id"]
        for key in ("positions", "old_positions", "new_positions"):
            for sid, pos in (mv.get(key) or {}).items():
                row = session.get(Shot, sid)
                if row and row.project_id == project.id:
                    row.position = pos
        if sh is not None:
            # Its own position lives in whichever map covers its scene.
            for key in ("positions", "new_positions", "old_positions"):
                if sh.id in (mv.get(key) or {}):
                    sh.position = mv[key][sh.id]
                    break
            sh.revision += 1
            sh.updated_at = utcnow()
            session.flush()
            sb.write_shot_revision(session, sh, command_id=command.id, summary="Undo move")
        return

    if ct.endswith(".cast.assign"):
        row_id = (after.get("cast_row") or {}).get("id")
        for model in (SceneCharacter, ShotCharacter):
            row = session.get(model, row_id) if row_id else None
            if row is not None and row.project_id == project.id:
                session.delete(row)
                break
        _bump_parent_revision(session, project, command, ct)
        return

    if ct.endswith(".cast.update"):
        snap = before.get("cast_row") or {}
        for model in (SceneCharacter, ShotCharacter):
            row = session.get(model, snap.get("id", ""))
            if row is not None and row.project_id == project.id:
                for f in snap:
                    if f not in ("id", "created_at", "updated_at") and hasattr(row, f):
                        setattr(row, f, snap[f])
                break
        _bump_parent_revision(session, project, command, ct)
        return

    if ct.endswith(".cast.remove"):
        snap = before.get("cast_row") or {}
        model = SceneCharacter if ct.startswith("scene.") else ShotCharacter
        row = session.get(model, snap.get("id", ""))
        if row is None and snap:
            cols = {c.name for c in model.__table__.columns} - {"id", "created_at", "updated_at"}
            session.add(model(id=new_id(), **{k: v for k, v in snap.items() if k in cols}))
            session.flush()
        _bump_parent_revision(session, project, command, ct)
        return

    if ct == "entity.assets.link":
        link = session.get(EntityAssetLink, after.get("link_id") or "")
        if link is not None and link.project_id == project.id:
            session.delete(link)
        return

    if ct == "entity.assets.unlink":
        snap = before.get("link") or {}
        if snap and session.get(EntityAssetLink, snap.get("id", "")) is None:
            session.add(EntityAssetLink(
                id=new_id(), project_id=project.id,
                entity_type=snap["entity_type"], entity_id=snap["entity_id"],
                asset_id=snap["asset_id"], role=snap["role"],
                label=snap.get("label", ""), position=snap.get("position", 0),
                weight=snap.get("weight"),
            ))
        return

    if ct == "entity.assets.update":
        snap = before.get("link") or {}
        link = session.get(EntityAssetLink, snap.get("id", ""))
        if link is not None and link.project_id == project.id:
            link.label = snap.get("label", "")
            link.weight = snap.get("weight")
            session.flush()
        return

    if ct == "entity.assets.reorder":
        for pos, lid in enumerate(before.get("order") or []):
            link = session.get(EntityAssetLink, lid)
            if link is not None and link.project_id == project.id:
                link.position = pos
        session.flush()
        return


def _bump_parent_revision(session: Session, project: Project, command: CreativeCommand,
                          command_type: str) -> None:
    """After undoing a cast change, bump the parent scene/shot revision so
    history reflects it."""
    from app.db.models import Scene, Shot
    payload = command.payload_json or {}
    if command_type.startswith("scene."):
        parent = session.get(Scene, payload.get("scene_id") or "")
        if parent is not None and parent.project_id == project.id:
            parent.revision += 1
            parent.updated_at = utcnow()
            session.flush()
            sb.write_scene_revision(session, parent, command_id=command.id, summary="Undo cast change")
    else:
        parent = session.get(Shot, payload.get("shot_id") or "")
        if parent is not None and parent.project_id == project.id:
            parent.revision += 1
            parent.updated_at = utcnow()
            session.flush()
            sb.write_shot_revision(session, parent, command_id=command.id, summary="Undo cast change")


def new_idempotency_key() -> str:
    return str(uuid.uuid4())
