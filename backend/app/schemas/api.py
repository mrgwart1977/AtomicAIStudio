"""Typed API contracts. The generated TypeScript client is produced
from /openapi.json so these are the single source of truth."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------- projects ----------

PROJECT_KINDS = ("movie", "short", "scene", "image")
ASPECT_RATIOS = ("16:9", "2.39:1", "1:1", "9:16", "4:3")


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    template: str = Field(default="blank", pattern="^(blank|short_film|series|music_video)$")
    kind: str = Field(default="movie", pattern="^(movie|short|scene|image)$")
    aspect_ratio: str = Field(default="16:9")
    width: int = Field(default=1920, ge=16, le=16384)
    height: int = Field(default=1080, ge=16, le=16384)
    fps_num: int = Field(default=24, ge=1, le=480000)
    fps_den: int = Field(default=1, ge=1, le=1001)
    style: str = Field(default="none", max_length=80)
    storage_location_id: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class ProjectOut(BaseModel):
    id: str
    name: str
    status: str
    template: str
    kind: str
    aspect_ratio: str
    width: int
    height: int
    fps_num: int
    fps_den: int
    style: str
    storage_location_id: str | None
    backup_dir: str | None
    revision: int
    settings_json: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class ProjectCardOut(ProjectOut):
    """Dashboard row: project + rollup counts + thumbnail pointer."""
    counts: dict[str, int] = Field(default_factory=dict)
    running_jobs: int = 0
    thumbnail_asset_id: str | None = None


class ProjectRevisionOut(BaseModel):
    id: str
    revision: int
    summary: str
    command_id: str | None
    created_at: datetime


# ---------- prompt draft ----------

class PromptDraftOut(BaseModel):
    project_id: str
    text: str
    scope: str
    updated_at: datetime


# ---------- commands ----------

CommandType = Literal[
    "prompt.set_text",
    "project.rename", "project.archive", "project.restore",
    "project.update_settings", "project.duplicate", "project.delete",
    "generate.image.mock", "generate.image", "generate.image.persistent",
    "generate.video",
    "character.create", "character.update", "character.archive",
    "character.unarchive", "character.revert",
    "world.create", "world.update", "world.archive",
    "world.unarchive", "world.revert",
    "location.create", "location.update", "location.archive",
    "location.unarchive", "location.revert",
    "asset.import", "asset.share", "asset.link", "asset.relink", "assets.verify",
    "backup.export", "backup.import", "backup.restore",
    "scene.create", "scene.update", "scene.archive", "scene.unarchive",
    "scene.revert", "scene.duplicate", "scene.move",
    "scene.cast.assign", "scene.cast.update", "scene.cast.remove",
    "shot.create", "shot.update", "shot.archive", "shot.unarchive",
    "shot.revert", "shot.duplicate", "shot.move",
    "shot.cast.assign", "shot.cast.update", "shot.cast.remove",
    "character.duplicate",
    "entity.assets.link", "entity.assets.unlink",
    "bridge.prepare", "bridge.launch", "bridge.import",
]


class CommandPreviewRequest(BaseModel):
    command_type: CommandType
    payload: dict[str, Any] = Field(default_factory=dict)
    scope: str = Field(default="this_take")
    origin: str = Field(default="prompt_bar")
    target_type: str | None = None
    target_id: str | None = None


class CommandCommitRequest(CommandPreviewRequest):
    expected_revision: int
    idempotency_key: str = Field(min_length=8, max_length=80)


class CommandProposalOut(BaseModel):
    command: dict[str, Any]
    summary: str
    affected: list[dict[str, Any]]
    warnings: list[str]
    diff: dict[str, Any] = Field(default_factory=dict)
    estimated_cost: dict[str, Any]
    current_revision: int
    launches_generation: bool


class CommandOut(BaseModel):
    id: str
    project_id: str
    origin: str
    command_type: str
    scope: str
    status: str
    payload_json: dict[str, Any]
    created_at: datetime


# ---------- jobs & assets ----------

class JobOut(BaseModel):
    id: str
    project_id: str | None  # NULL for app-level jobs (model downloads)
    adapter_id: str
    kind: str
    status: str
    progress: float
    message: str
    error: str | None
    resumable: bool
    output_asset_id: str | None
    command_id: str | None
    gpu_index: int | None = None
    model_key: str | None = None
    result_json: dict[str, Any] | None = None
    progress_json: dict[str, Any] | None = None
    cancel_requested: bool = False
    # M4.0 recovery contracts
    attempt_no: int = 1
    execution_scope: str = "local"
    recovery_state: str | None = None
    created_at: datetime
    completed_at: datetime | None


class AssetOut(BaseModel):
    id: str
    project_id: str
    name: str
    kind: str
    origin: str
    scope: str
    status: str
    uri: str
    sha256: str
    byte_size: int
    current_version_id: str | None
    created_at: datetime
    provenance_json: dict[str, Any] = {}
    media_json: dict[str, Any] = {}


class AssetPageOut(BaseModel):
    items: list[AssetOut]
    total: int
    offset: int
    limit: int


class AssetVersionOut(BaseModel):
    id: str
    asset_id: str
    version_no: int
    sha256: str
    media_json: dict[str, Any]
    created_at: datetime


class SharedAssetOut(BaseModel):
    asset: AssetOut
    source_project_name: str
    linked_here: bool = False


# ---------- bibles (versioned entities) ----------

class EntityOut(BaseModel):
    id: str
    project_id: str
    name: str
    summary: str
    status: str
    revision: int
    created_at: datetime
    updated_at: datetime
    world_id: str | None = None  # locations only
    data: dict[str, Any] = Field(default_factory=dict)  # profile/lore/details blob


class EntityRevisionOut(BaseModel):
    id: str
    entity_type: str
    entity_id: str
    revision: int
    summary: str
    snapshot_json: dict[str, Any]
    created_at: datetime


# ---------- storyboard (M2) ----------

class EntityAssetLinkOut(BaseModel):
    id: str
    project_id: str
    entity_type: str
    entity_id: str
    asset_id: str
    role: str
    label: str
    position: int
    created_at: datetime


class CastMemberOut(BaseModel):
    """One cast row + the character's display info."""
    id: str
    character_id: str
    position: int
    character: dict[str, Any]  # {id, name, status}
    # scene-cast fields
    role: str | None = None
    notes_json: dict[str, Any] | None = None
    # shot-cast fields
    wardrobe_variant: str | None = None
    blocking: str | None = None
    expression: str | None = None
    emotion: str | None = None
    action_instructions: str | None = None
    dialogue: str | None = None
    voice_override: str | None = None
    model_ref: str | None = None
    lora_json: list[Any] | None = None
    continuity_json: dict[str, Any] | None = None
    # M2.1: named schema fields + forward-compat bag
    reference_ids_json: list[Any] | None = None
    ext_json: dict[str, Any] | None = None
    # "shot" = own row (override) · "scene" = inherited from scene cast
    source: str | None = None


class ShotCastState(BaseModel):
    """Wire form of per-character shot state (schema v1) — the `state`
    object inside shot.cast.* command payloads. Unknown keys are preserved
    forward-compatibly in ext_json; legacy aliases are normalised
    (`action`→action_instructions, `voice`→voice_override, `lora_refs`→loras,
    `continuity_notes`→continuity.notes)."""
    model_config = {"extra": "allow"}
    schema_version: int = 1
    wardrobe_variant: str | None = None
    blocking: str | None = None
    expression: str | None = None
    emotion: str | None = None
    action_instructions: str | None = None
    dialogue: str | None = None
    voice_override: str | None = None
    model_ref: str | None = None
    loras: list[str] | None = None
    continuity: dict[str, Any] | None = None
    reference_asset_ids: list[str] | None = None
    position: int | None = None


class ShotOut(BaseModel):
    id: str
    project_id: str
    scene_id: str
    title: str
    description: str
    intent: str
    prompt: str
    shot_type: str
    duration_sec: float
    position: int
    status: str
    location_id: str | None
    thumbnail_asset_id: str | None
    continuity_json: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime


class StoryboardShotOut(BaseModel):
    shot: ShotOut
    location_name: str
    cast: list[CastMemberOut]
    inherited: list[CastMemberOut] = []  # scene-cast members without an own row


class SceneOut(BaseModel):
    id: str
    project_id: str
    title: str
    description: str
    intent: str
    position: int
    status: str
    world_id: str | None
    location_id: str | None
    continuity_json: dict[str, Any]
    revision: int
    created_at: datetime
    updated_at: datetime


class StoryboardSceneOut(BaseModel):
    scene: SceneOut
    location_name: str
    cast: list[CastMemberOut]
    shots: list[StoryboardShotOut]


# ---------- storage ----------

class StorageLocationOut(BaseModel):
    id: str
    label: str
    root_path: str
    kind: str
    status: str
    is_default: bool
    created_at: datetime


class StorageLocationCreate(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    root_path: str = Field(min_length=1)
    kind: str = Field(default="local", pattern="^(local|removable)$")


class StorageLocationUpdate(BaseModel):
    label: str | None = None
    root_path: str | None = None  # set to relink a moved drive


# ---------- backups ----------

class BackupRunOut(BaseModel):
    id: str
    project_id: str | None
    project_name: str
    kind: str
    status: str
    package_path: str
    package_sha256: str
    byte_size: int
    destination_dir: str = ""
    bytes_total: int = 0
    stats_json: dict[str, Any]
    job_id: str | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None


class PackagePreviewOut(BaseModel):
    project_name: str
    format_version: int
    created_at: str | None
    counts: dict[str, int]
    files: int
    byte_size: int


class PackageImportRequest(BaseModel):
    package_path: str = Field(min_length=1)


# ---------- backup destinations (M1.1) ----------

class BackupDestinationOut(BaseModel):
    """Effective destination for one project (or app default)."""
    dir: str
    source: str  # "project override" | "app setting" | "built-in default"
    app_default_dir: str
    builtin_dir: str
    project_override: str
    estimated_bytes: int
    path: str
    exists: bool
    writable: bool
    free_bytes: int
    needed_bytes: int
    errors: list[str]
    warnings: list[str]


class BackupDestinationValidate(BaseModel):
    path: str = Field(min_length=1)
    project_id: str | None = None


class AppBackupDirUpdate(BaseModel):
    # Absolute dir, or null to reset to the built-in location.
    dir: str | None = None


# ---------- adapters / hardware ----------

class AdapterProfileOut(BaseModel):
    adapter_id: str
    display_name: str
    adapter_kind: str
    tasks: list[str]
    supports: dict[str, bool]
    limits: dict[str, Any]
    requires_cloud: bool
    cost_model: str
    lane: str = "ops"
    needs_gpu: bool = False


# ---------- models & runtimes (M3) ----------

class ModelFileOut(BaseModel):
    name: str
    size_bytes: int
    sha256: str | None = None


class ModelInstallOut(BaseModel):
    id: str
    status: str
    install_root: str
    rel_dir: str
    file_name: str
    byte_size: int
    verified: bool
    version: str
    error: str | None = None
    created_at: str | None = None


class CompatibilityOut(BaseModel):
    status: str  # compatible | limited | unsupported | unknown | restricted | unavailable | planned
    support: str | None = None  # production | tested | experimental | planned
    modality: str | None = None  # image | video | audio | multimodal
    modes: list[str] = []
    reasons: list[str]
    warnings: list[str]
    suggested: dict[str, Any]
    estimate: dict[str, Any]


class ModelCatalogEntryOut(BaseModel):
    key: str
    display_name: str
    family: str
    version: str
    adapter_id: str
    format: str
    source: dict[str, Any]
    license: dict[str, Any]
    files: list[ModelFileOut]
    download_bytes: int
    requirements: dict[str, Any]
    capabilities: dict[str, Any]
    runtime_key: str
    support: str
    modality: str
    modes: list[str]
    notes: str
    install: dict[str, Any] | None = None
    compatibility: CompatibilityOut | None = None


class ModelInstallRequest(BaseModel):
    # Which runtime backend flavor to install (auto-chosen when omitted).
    runtime_flavor: str | None = None


class RuntimeOut(BaseModel):
    key: str
    display_name: str
    version: str
    license: dict[str, Any]
    backends: list[str]
    est_bytes: int
    notes: str
    install: dict[str, Any] | None = None
    installs: dict[str, Any] = {}  # per-flavor rows, keyed e.g. "sdcpp-vulkan"


class ModelsDirOut(BaseModel):
    dir: str
    source: str  # "app setting" | "built-in default"
    builtin_dir: str
    exists: bool
    writable: bool
    free_bytes: int | None
    used_bytes: int


class GpuSettingsOut(BaseModel):
    mode: str  # auto | manual
    preferred_gpu: int | None
    slots: list[dict[str, Any]]
    free: int
    gpu_count: int


class GpuSettingsUpdate(BaseModel):
    mode: str | None = None
    preferred_gpu: int | None = None
