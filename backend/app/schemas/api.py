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
    "generate.image.mock",
    "character.create", "character.update", "character.archive",
    "character.unarchive", "character.revert",
    "world.create", "world.update", "world.archive",
    "world.unarchive", "world.revert",
    "location.create", "location.update", "location.archive",
    "location.unarchive", "location.revert",
    "asset.import", "asset.share", "asset.link", "asset.relink", "assets.verify",
    "backup.export", "backup.import", "backup.restore",
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
    project_id: str
    adapter_id: str
    kind: str
    status: str
    progress: float
    message: str
    error: str | None
    resumable: bool
    output_asset_id: str | None
    command_id: str | None
    result_json: dict[str, Any] | None = None
    progress_json: dict[str, Any] | None = None
    cancel_requested: bool = False
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
    notes: str
