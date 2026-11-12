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

import uuid
from typing import Any

from sqlalchemy import select
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
    "generate.video",
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
    # scenes & storyboard (M2)
    "scene.create", "scene.update", "scene.archive", "scene.unarchive",
    "scene.revert", "scene.duplicate", "scene.move",
    "scene.cast.assign", "scene.cast.update", "scene.cast.remove",
    "shot.create", "shot.update", "shot.archive", "shot.unarchive",
    "shot.revert", "shot.duplicate", "shot.move",
    "shot.cast.assign", "shot.cast.update", "shot.cast.remove",
    "character.duplicate",
    "entity.assets.link", "entity.assets.unlink",
}

# Command types that enqueue paid/GPU-visible work; preview must stay free.
GENERATION_COMMANDS = {"generate.image.mock", "generate.image",
                       "generate.image.persistent", "generate.video"}

# Commands that enqueue durable jobs (visible progress, restart-safe).
JOB_COMMANDS = {
    "generate.image.mock": "mock.image",
    "generate.image": "gen.image.sdcpp",
    "generate.image.persistent": "gen.image.sdcpp-server",
    "generate.video": "gen.video.sdcpp",
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
    if command_type == "character.duplicate":
        return "Duplicate character (with its linked assets)"
    if command_type == "entity.assets.link":
        return f"Link asset to {payload.get('entity_type', 'entity')} ({payload.get('role', 'reference')})"
    if command_type == "entity.assets.unlink":
        return "Unlink asset from entity"
    parts = _entity_parts(command_type)
    if parts:
        etype, op = parts
        if op == "create":
            return f"Create {etype} '{payload.get('name', '')}'"
        if op == "revert":
            return f"Revert {etype} to revision {payload.get('to_revision')}"
        return f"{op.capitalize()} {etype}"
    return command_type


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
    if command_type in {"project.archive", "project.restore", "project.duplicate"}:
        warnings.append("Applies to the whole project")
    if command_type == "project.delete":
        if (payload.get("confirm_name") or "") != project.name:
            raise CommandError(422, "type the project name to confirm deletion")
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
    if command_type in ("shot.update", "shot.archive", "shot.unarchive",
                        "shot.revert", "shot.duplicate", "shot.move"):
        _get_shot(session, project, payload)
        if command_type == "shot.update":
            _check_storyboard_links(session, project, payload)
            _check_shot_fields(payload)
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
    if command_type == "entity.assets.unlink":
        from app.db.models import EntityAssetLink
        link = session.get(EntityAssetLink, payload.get("link_id") or "")
        if link is None or link.project_id != project.id:
            raise CommandError(404, "link not found in this project")
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


def preview(session: Session, project: Project, *, command_type: str, payload: dict,
            scope: str, origin: str, target_type: str | None, target_id: str | None) -> dict:
    if command_type not in SUPPORTED_COMMANDS:
        raise CommandError(422, f"unsupported command_type '{command_type}'")
    warnings = _validate(session, project, command_type, payload)
    return {
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
        "current_revision": project.revision,
        "launches_generation": command_type in GENERATION_COMMANDS,
    }


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
            label=link.label, position=link.position,
        ))

    session.add(
        AuditEvent(
            id=new_id(), project_id=clone.id, event_type="project.duplicated_from",
            payload_json={"source_project_id": project.id, "command_id": command.id},
        )
    )
    return clone


def _commit_storyboard(session: Session, project: Project, command: CreativeCommand,
                       command_type: str, payload: dict, audit: dict) -> None:
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
        audit["after"]["shot_id"] = sh.id
        return

    if command_type == "character.duplicate":
        c = character()
        dup = sb.duplicate_character(session, c, name=payload.get("name"), command_id=command.id)
        audit["after"]["character_id"] = dup.id
        return

    if command_type == "entity.assets.link":
        link = sb.link_entity_asset(
            session, project.id,
            entity_type=payload["entity_type"], entity_id=payload["entity_id"],
            asset_id=payload["asset_id"], role=payload.get("role", "reference"),
            label=payload.get("label", ""),
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
           job_engine=None, storage=None) -> dict:
    existing = session.execute(
        select(CreativeCommand).where(CreativeCommand.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return {"command": existing, "idempotent_replay": True}

    if project.status == "archived" and command_type != "project.restore":
        raise CommandError(409, "project is archived; restore it before making changes")
    if expected_revision != project.revision:
        raise CommandError(409, f"revision conflict: expected {expected_revision}, current {project.revision}")

    warnings = _validate(session, project, command_type, payload)
    before_revision = project.revision
    audit: dict[str, Any] = {"before": {}, "after": {}, "warnings": warnings}
    job: GenerationJob | None = None
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

    elif command_type.startswith(("scene.", "shot.", "entity.assets.")) or command_type == "character.duplicate":
        _commit_storyboard(session, project, command, command_type, payload, audit)

    elif parts:
        _commit_entity(session, project, command, parts[0], parts[1], payload, audit)

    elif command_type in JOB_COMMANDS:
        if job_engine is None:
            raise CommandError(500, "job engine unavailable")
        request = _job_request(session, project, command_type, payload)
        try:
            job = job_engine.enqueue(
                session,
                project_id=project.id,
                adapter_id=JOB_COMMANDS[command_type],
                kind=_job_kind(command_type),
                request=request,
                idempotency_key=f"job:{idempotency_key}",
                command_id=command.id,
                model_key=payload.get("model_key"),
                # Generation has no mid-flight checkpoints — an interrupted
                # gen job restarts from scratch (bounded, user-initiated).
                resumable=command_type not in (
                    "generate.image", "generate.image.persistent", "generate.video"),
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
        if command_type == "bridge.import":
            audit["after"]["bridge"] = {
                "action": "import", "job_id": job.id,
                "provider_key": payload.get("provider_key"),
                "external_source": request.get("external_source"),
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
        "asset.import": "import",
        "bridge.import": "import",
        "asset.relink": "relink",
        "assets.verify": "verify",
        "backup.export": "backup",
        "backup.import": "import",
        "backup.restore": "restore",
    }[command_type]


def _job_request(session: Session, project: Project, command_type: str, payload: dict) -> dict:
    req: dict[str, Any] = {"command_type": command_type, **payload}
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
            ))
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
