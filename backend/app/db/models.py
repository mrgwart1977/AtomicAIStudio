"""Milestone-0 relational model.

Project scoping is the invariant: every project-owned row carries
project_id with a real foreign key. Contract tables
(approved_shot_manifests, render_cache_entries) exist so later
milestones extend them instead of bolting on competing stores.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (DDL, JSON, Boolean, CheckConstraint, DateTime, Float,
                        ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, event, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | archived
    template: Mapped[str] = mapped_column(String(40), default="blank")
    # M1 format/production fields (columns, queryable for dashboard filters)
    kind: Mapped[str] = mapped_column(String(40), default="movie")  # movie|short|scene|image
    aspect_ratio: Mapped[str] = mapped_column(String(12), default="16:9")
    width: Mapped[int] = mapped_column(Integer, default=1920)
    height: Mapped[int] = mapped_column(Integer, default=1080)
    fps_num: Mapped[int] = mapped_column(Integer, default=24)
    fps_den: Mapped[int] = mapped_column(Integer, default=1)
    style: Mapped[str] = mapped_column(String(80), default="none")
    storage_location_id: Mapped[str | None] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="SET NULL"), nullable=True
    )
    backup_dir: Mapped[str | None] = mapped_column(Text, nullable=True)  # M1.1 project override
    settings_json: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ProjectRevision(Base):
    """Immutable per-commit revision record; project.revision mirrors latest."""

    __tablename__ = "project_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    command_id: Mapped[str | None] = mapped_column(ForeignKey("creative_commands.id"), nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PromptDraft(Base):
    """One editable prompt draft per project (survives mode switches/restarts)."""

    __tablename__ = "prompt_drafts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), unique=True, index=True
    )
    text: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[str] = mapped_column(String(40), default="this_take")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class CreativeCommand(Base):
    __tablename__ = "creative_commands"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    origin: Mapped[str] = mapped_column(String(40), default="prompt_bar")  # prompt_bar|api|assistant|...
    command_type: Mapped[str] = mapped_column(String(80))
    scope: Mapped[str] = mapped_column(String(40), default="this_take")
    target_type: Mapped[str | None] = mapped_column(String(60), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    expected_revision: Mapped[int] = mapped_column(Integer, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="committed")  # committed|undone|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CommandTransaction(Base):
    __tablename__ = "command_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    command_id: Mapped[str] = mapped_column(ForeignKey("creative_commands.id"), index=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    before_revision: Mapped[int] = mapped_column(Integer)
    after_revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="applied")
    # Before/after values needed for undo plus a human-readable diff summary.
    audit_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GenerationJob(Base):
    """Durable job record. Status is the source of truth for the engine and UI."""

    __tablename__ = "generation_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    # NULL for app-level jobs (model downloads/runtime installs) — those are
    # not project-scoped and broadcast WS events with project_id=null.
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True)
    adapter_id: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(40), default="image")
    request_json: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(String(400), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    resumable: Mapped[bool] = mapped_column(Boolean, default=True)
    output_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    command_id: Mapped[str | None] = mapped_column(ForeignKey("creative_commands.id"), nullable=True)
    # M3: GPU the job was scheduled onto (None = CPU/not GPU-bound).
    gpu_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # M3: catalog key of the model this job used/downloaded (provenance).
    model_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    result_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # non-asset op outputs
    progress_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # bytes/ETA detail
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # M4.0 recovery contracts: attempt counter (retries are distinct
    # attempts), local vs future remote execution, reconciliation flag set
    # when a restart finds the job mid-flight with unknown outcome.
    attempt_no: Mapped[int] = mapped_column(Integer, default=1)
    execution_scope: Mapped[str] = mapped_column(String(16), default="local")
    recovery_state: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StorageLocation(Base):
    """A named filesystem root that can hold media blobs (CAS) and other
    project files. Locations are app-level (not project-scoped); projects
    choose one as their default ingest target. root_path may point at a
    drive that is currently offline — status reflects the last probe."""

    __tablename__ = "storage_locations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    label: Mapped[str] = mapped_column(String(120))
    root_path: Mapped[str] = mapped_column(Text)  # absolute; location-relative rel_path lives on files
    kind: Mapped[str] = mapped_column(String(20), default="local")  # local | removable
    status: Mapped[str] = mapped_column(String(20), default="online")  # online | offline | missing
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssetFile(Base):
    """Content-addressed file blob registry. One row per unique blob per
    storage location; bytes are immutable once status='ready'. Multiple
    assets/versions (and duplicated projects) can reference the same blob —
    that is what makes duplication and backup dedupe cheap."""

    __tablename__ = "asset_files"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    storage_location_id: Mapped[str] = mapped_column(
        ForeignKey("storage_locations.id", ondelete="RESTRICT"), index=True
    )
    rel_path: Mapped[str] = mapped_column(Text)  # relative to location root, e.g. blobs/ab/<sha>
    byte_size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="ready", index=True)  # pending | ready | missing
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Asset(Base):
    """File-backed project asset. Bytes live on disk, never in SQLite.
    `uri`/`sha256` remain as a convenience mirror of the current version
    (set on write); the CAS records below are authoritative."""

    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(240), default="")
    kind: Mapped[str] = mapped_column(String(40), default="image", index=True)  # image|video|audio|other
    origin: Mapped[str] = mapped_column(String(40), default="generated")  # generated|imported|shared
    scope: Mapped[str] = mapped_column(String(20), default="project", index=True)  # project | shared
    status: Mapped[str] = mapped_column(String(20), default="ready", index=True)  # ready | missing | deleted
    uri: Mapped[str] = mapped_column(Text, default="")  # location-relative path of current version
    sha256: Mapped[str] = mapped_column(String(64), default="")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    current_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provenance_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AssetVersion(Base):
    """Immutable version of an asset's bytes + media metadata."""

    __tablename__ = "asset_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(ForeignKey("asset_files.sha256", ondelete="RESTRICT"), index=True)
    media_json: Mapped[dict] = mapped_column(JSON, default=dict)
    command_id: Mapped[str | None] = mapped_column(ForeignKey("creative_commands.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("asset_id", "version_no", name="uq_asset_version"),)


class SharedAssetLink(Base):
    """Explicit, approved sharing of an asset into another project.
    mode='link' references the origin asset's blob; mode='copy' points at a
    new asset row in the consuming project. Created only via the
    asset.share/asset.link commands — this is the sanctioned cross-project
    channel; nothing else may read another project's rows."""

    __tablename__ = "shared_asset_links"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    source_project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    mode: Mapped[str] = mapped_column(String(10), default="link")  # link | copy
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("project_id", "asset_id", name="uq_shared_link"),)


class Character(Base):
    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)  # appearance|personality|backstory|voice|notes
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | archived
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class World(Base):
    __tablename__ = "worlds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    lore_json: Mapped[dict] = mapped_column(JSON, default=dict)  # tone|era|rules|aesthetic|notes
    status: Mapped[str] = mapped_column(String(20), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Location(Base):
    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    world_id: Mapped[str | None] = mapped_column(ForeignKey("worlds.id", ondelete="SET NULL"), nullable=True)
    name: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    details_json: Mapped[dict] = mapped_column(JSON, default=dict)  # geography|mood|visual_refs|notes
    status: Mapped[str] = mapped_column(String(20), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EntityRevision(Base):
    """Immutable snapshot of a versioned entity at one revision. Written on
    every committed mutation; revert creates a NEW revision copying an old
    snapshot — history is never rewritten."""

    __tablename__ = "entity_revisions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(30), index=True)  # character | world | location
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[str] = mapped_column(Text, default="")
    command_id: Mapped[str | None] = mapped_column(ForeignKey("creative_commands.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("entity_type", "entity_id", "revision", name="uq_entity_rev"),)


class Scene(Base):
    """Ordered scene within a project. position is a dense int per project;
    reorder renumbers. Version history rides entity_revisions (entity_type
    'scene'); snapshots also embed the scene cast so revert restores it."""

    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    intent: Mapped[str] = mapped_column(Text, default="")  # cinematic intent
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | archived
    world_id: Mapped[str | None] = mapped_column(ForeignKey("worlds.id", ondelete="SET NULL"), nullable=True)
    location_id: Mapped[str | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    continuity_json: Mapped[dict] = mapped_column(JSON, default=dict)  # structured continuity metadata
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SceneCharacter(Base):
    """Scene-level cast membership — which characters the scene involves."""

    __tablename__ = "scene_characters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    role: Mapped[str] = mapped_column(String(120), default="")  # lead|supporting|background|...
    notes_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("scene_id", "character_id", name="uq_scene_character"),)


class Shot(Base):
    """One storyboard shot inside a scene. position is dense per scene.
    prompt stays user-editable at all times — generation reads it later;
    editing it never requires regeneration."""

    __tablename__ = "shots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    scene_id: Mapped[str] = mapped_column(ForeignKey("scenes.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(240), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    intent: Mapped[str] = mapped_column(Text, default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    shot_type: Mapped[str] = mapped_column(String(30), default="medium")
    duration_sec: Mapped[float] = mapped_column(Float, default=0.0)
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    location_id: Mapped[str | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"), nullable=True)
    thumbnail_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    # C4-B — generation bindings: the character reference set to apply
    # (membership stays on entity_assets; SET NULL on set archive/drop)
    # and the source image for img2img shot generation.
    reference_set_id: Mapped[str | None] = mapped_column(ForeignKey("reference_sets.id", ondelete="SET NULL"), nullable=True)
    source_asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id", ondelete="SET NULL"), nullable=True)
    continuity_json: Mapped[dict] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class ShotCharacter(Base):
    """One character's per-shot assignment — every field is independent
    of the character bible and of other cast rows (identity/appearance
    come from the linked character; everything else is shot-local)."""

    __tablename__ = "shot_characters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    shot_id: Mapped[str] = mapped_column(ForeignKey("shots.id", ondelete="CASCADE"), index=True)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    wardrobe_variant: Mapped[str] = mapped_column(String(200), default="")
    blocking: Mapped[str] = mapped_column(Text, default="")          # position & movement
    expression: Mapped[str] = mapped_column(String(200), default="") # facial expression
    emotion: Mapped[str] = mapped_column(String(120), default="")    # emotional state
    action_instructions: Mapped[str] = mapped_column(Text, default="")
    dialogue: Mapped[str] = mapped_column(Text, default="")
    voice_override: Mapped[str] = mapped_column(String(200), default="")
    model_ref: Mapped[str] = mapped_column(String(200), default="")  # model association override
    lora_json: Mapped[list] = mapped_column(JSON, default=list)      # LoRA associations
    continuity_json: Mapped[dict] = mapped_column(JSON, default=dict)
    # Identity/reference asset IDs used for this shot's look (M2.1 schema v1).
    reference_ids_json: Mapped[list] = mapped_column(JSON, default=list)
    # Forward-compatible extension bag — schema fields we don't model yet and
    # legacy keys from older writers survive here instead of being dropped.
    ext_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("shot_id", "character_id", name="uq_shot_character"),)


class ReferenceSet(Base):
    """C4-A — named grouping of a character's entity-asset reference
    links plus per-set identity guidance. Membership lives on
    entity_assets.set_id (links keep role/weight/ordering); this row
    only carries set identity: name, optional pinned seed, notes.

    A pinned seed is GENERATION GUIDANCE, not an identity guarantee —
    it is recorded in job provenance and fed to workflows that accept
    a seed parameter, and the UI must say so."""

    __tablename__ = "reference_sets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    character_id: Mapped[str] = mapped_column(ForeignKey("characters.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class EntityAssetLink(Base):
    """Explicit link between a bible entity and a project asset —
    character reference images, identity plates, wardrobe refs, world
    style frames. This is the sanctioned entity↔asset channel; role
    says what the asset is FOR."""

    __tablename__ = "entity_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    entity_type: Mapped[str] = mapped_column(String(30), index=True)  # character | world | location
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    # C4-A — optional membership in a named ReferenceSet. SET NULL keeps
    # the link as an ungrouped reference when its set is deleted.
    set_id: Mapped[str | None] = mapped_column(
        ForeignKey("reference_sets.id", ondelete="SET NULL"), nullable=True)
    role: Mapped[str] = mapped_column(String(30), default="reference")  # reference|identity|wardrobe|voice|style
    label: Mapped[str] = mapped_column(String(200), default="")
    position: Mapped[int] = mapped_column(Integer, default=0)
    # C3-D1 — optional guidance strength (0.0–2.0; NULL = provider
    # default). Honored only by models declaring editing.reference_weights.
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        UniqueConstraint("entity_type", "entity_id", "asset_id", "role", name="uq_entity_asset"),
    )


class BackupRun(Base):
    """One backup/export/restore/import operation against a project."""

    __tablename__ = "backup_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_name: Mapped[str] = mapped_column(String(200), default="")  # survives project deletion
    kind: Mapped[str] = mapped_column(String(20))  # backup | export | import | restore
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)  # running | succeeded | failed | cancelled
    package_path: Mapped[str] = mapped_column(Text, default="")  # absolute path of .atomicpkg
    package_sha256: Mapped[str] = mapped_column(String(64), default="")
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    destination_dir: Mapped[str] = mapped_column(Text, default="")  # dir the package was written to
    bytes_total: Mapped[int] = mapped_column(Integer, default=0)  # expected payload size
    stats_json: Mapped[dict] = mapped_column(JSON, default=dict)  # entity/file counts, verified hashes
    job_id: Mapped[str | None] = mapped_column(ForeignKey("generation_jobs.id", ondelete="SET NULL"), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApprovedShotManifest(Base):
    """Contract table (Phase 0 schema). Sealed manifests arrive with shot approvals."""

    __tablename__ = "approved_shot_manifests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    digest: Mapped[str] = mapped_column(String(64), index=True)
    manifest_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RenderCacheEntry(Base):
    """Contract table (Phase 0 schema). Content-addressed cache lands with generation."""

    __tablename__ = "render_cache_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    cache_key: Mapped[str] = mapped_column(String(128), index=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("project_id", "cache_key", name="uq_cache_project_key"),)


class ModelInstall(Base):
    """An installed model on local storage. App-level (not project-scoped):
    models live under the configured models root, separate from project
    assets, and may be shared by every project. Status lifecycle:
    downloading → verifying → installed | failed; removal deletes the row's
    files and marks status='removed' (row kept for audit)."""

    __tablename__ = "model_installs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    model_key: Mapped[str] = mapped_column(String(80), index=True)  # catalog key, e.g. "sd15.fp16"
    adapter_id: Mapped[str] = mapped_column(String(80))             # inference adapter that serves it
    version: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(20), default="downloading", index=True)
    install_root: Mapped[str] = mapped_column(Text, default="")     # absolute models root used at install time
    rel_dir: Mapped[str] = mapped_column(Text, default="")          # model_key-relative dir under root
    file_name: Mapped[str] = mapped_column(String(240), default="") # primary weight file
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")     # verified checksum when available
    verified: Mapped[bool] = mapped_column(Boolean, default=False)  # passed integrity check
    license: Mapped[str] = mapped_column(String(120), default="")
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)     # source url, etag, extra files
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RuntimeInstall(Base):
    """An installed inference runtime binary (e.g. stable-diffusion.cpp).
    Kept separate from models: runtimes are small executables, models are
    large weights; they download and version independently."""

    __tablename__ = "runtime_installs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    runtime_key: Mapped[str] = mapped_column(String(80), index=True)  # e.g. "sdcpp"
    version: Mapped[str] = mapped_column(String(60), default="")
    status: Mapped[str] = mapped_column(String(20), default="downloading", index=True)
    install_dir: Mapped[str] = mapped_column(Text, default="")        # absolute dir containing binaries
    exe_path: Mapped[str] = mapped_column(Text, default="")           # main executable
    byte_size: Mapped[int] = mapped_column(Integer, default=0)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AppSetting(Base):
    """Durable app-level key/value settings (backup default dir, etc.).
    Not project-scoped — survives restarts and migrations."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AuditEvent(Base):
    """Append-only project-scoped audit log for command/job lifecycle."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    payload_json: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------- C2.0b spend ledger ----------
# Financial rows are the DOCUMENTED EXCEPTION to the project_id-FK rule:
# spend history must survive `project.delete` (which cascades
# generation_jobs) and remain attributable. project_id / job_id /
# command_id / remote_job_id are therefore plain snapshot values —
# isolation is still enforced at the query layer (values are present and
# indexed). Internal link reservation_id stays a real FK because
# reservations are never deleted.
#
# Monetary authority is INTEGER micro-USD (1 USD = 1_000_000 µUSD).
# No floating-point arithmetic anywhere in admission or consumption.

class SpendBudget(Base):
    """A budget cap for one scope. project_id NULL = global (all cloud
    spend); set = per-project. Scope semantics live in domain/spend.py —
    day/month are UTC calendar periods, session is the app-run billing
    session stored on ledger rows. Edits are forward-only and never
    mutate open reservations."""

    __tablename__ = "spend_budgets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    scope: Mapped[str] = mapped_column(String(12))          # session|day|month|lifetime
    limit_micro: Mapped[int] = mapped_column(Integer)       # CHECK >= 0
    label: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (
        CheckConstraint("limit_micro >= 0", name="ck_budget_limit_nonneg"),
        CheckConstraint("scope IN ('session','day','month','lifetime')",
                        name="ck_budget_scope"),
    )


class SpendReservation(Base):
    """One budget hold per remote job. open → settled | released (terminal,
    trigger-locked). Ambiguous remote outcomes stay open — worst-case
    exposure is held until explicit reconciliation."""

    __tablename__ = "spend_reservations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(String(36), unique=True, index=True)   # snapshot
    command_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    provider_key: Mapped[str] = mapped_column(String(60))
    environment: Mapped[str] = mapped_column(String(12), default="live")       # live|simulated
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open|settled|released
    reserved_micro: Mapped[int] = mapped_column(Integer)    # CHECK >= 0 — ceiling of est, capped by max
    remote_job_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    day_key: Mapped[str] = mapped_column(String(10), index=True)   # YYYY-MM-DD UTC
    month_key: Mapped[str] = mapped_column(String(7), index=True)  # YYYY-MM UTC
    reconcile_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reconcile_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        CheckConstraint("reserved_micro >= 0", name="ck_res_reserved_nonneg"),
        CheckConstraint("status IN ('open','settled','released')", name="ck_res_status"),
        CheckConstraint("environment IN ('live','simulated')", name="ck_res_env"),
        CheckConstraint("reconcile_count >= 0", name="ck_res_reconcile_nonneg"),
    )


class SpendCharge(Base):
    """Append-only settled/actual charges. Corrections are NEW rows with
    source='adjustment' (may be negative — refunds); everything else is
    nonnegative. usage_json carries counters only — never prompts or
    secrets. UPDATE/DELETE are trigger-aborted."""

    __tablename__ = "spend_charges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    reservation_id: Mapped[str] = mapped_column(
        ForeignKey("spend_reservations.id"), index=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    provider_key: Mapped[str] = mapped_column(String(60))
    environment: Mapped[str] = mapped_column(String(12), default="live")
    source: Mapped[str] = mapped_column(String(20))         # provider_reported|estimate_at_settle|reconciled|simulated|adjustment
    micro: Mapped[int] = mapped_column(Integer)             # >=0 unless source='adjustment'
    overage_micro: Mapped[int] = mapped_column(Integer, default=0)  # charged above reserved
    usage_json: Mapped[dict] = mapped_column(JSON, default=dict)
    remote_job_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    session_id: Mapped[str] = mapped_column(String(36), index=True)
    day_key: Mapped[str] = mapped_column(String(10), index=True)
    month_key: Mapped[str] = mapped_column(String(7), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (
        CheckConstraint("environment IN ('live','simulated')", name="ck_chg_env"),
        CheckConstraint("source IN ('provider_reported','estimate_at_settle',"
                        "'reconciled','simulated','adjustment')", name="ck_chg_source"),
        # Refunds/credits are negative ONLY via explicit adjustment rows.
        CheckConstraint("micro >= 0 OR source = 'adjustment'",
                        name="ck_chg_micro_sign"),
        CheckConstraint("overage_micro >= 0", name="ck_chg_overage_nonneg"),
    )


# Consumption indexes — sums are always (environment, <period key>)
# filtered; keep them composite so every scope query is covered.
Index("ix_res_scope", SpendReservation.environment, SpendReservation.status,
      SpendReservation.project_id)
Index("ix_res_day", SpendReservation.environment, SpendReservation.status,
      SpendReservation.day_key)
Index("ix_res_month", SpendReservation.environment, SpendReservation.status,
      SpendReservation.month_key)
Index("ix_res_session", SpendReservation.environment, SpendReservation.status,
      SpendReservation.session_id)
Index("ix_chg_project", SpendCharge.environment, SpendCharge.project_id)
Index("ix_chg_day", SpendCharge.environment, SpendCharge.day_key)
Index("ix_chg_month", SpendCharge.environment, SpendCharge.month_key)
Index("ix_chg_session", SpendCharge.environment, SpendCharge.session_id)
# One spend budget per scope per target — NULL project_id (global) needs
# a separate partial unique since SQL NULLs never compare equal.
Index("uq_spend_budget_global", SpendBudget.scope, unique=True,
      sqlite_where=SpendBudget.project_id.is_(None))
Index("uq_spend_budget_project", SpendBudget.project_id, SpendBudget.scope,
      unique=True, sqlite_where=SpendBudget.project_id.isnot(None))
# Exactly ONE primary settlement charge per reservation, regardless of
# source — a simulated engine settle and a manual 'reconciled' settle
# can never both exist. Adjustment/refund rows are unrestricted.
Index("uq_chg_res_primary", SpendCharge.reservation_id, unique=True,
      sqlite_where=text("spend_charges.source <> 'adjustment'"))

# Hard DB-level guards (also emitted by migration 0008 for existing DBs):
# terminal reservation states never transition; charges are append-only.
for _tbl, _ddl in (
    (SpendReservation.__table__,
     "CREATE TRIGGER trg_spend_res_terminal BEFORE UPDATE ON spend_reservations"
     " WHEN OLD.status <> 'open' AND NEW.status <> OLD.status"
     " BEGIN SELECT RAISE(ABORT, 'settled/released reservations are terminal'); END"),
    (SpendCharge.__table__,
     "CREATE TRIGGER trg_spend_chg_no_update BEFORE UPDATE ON spend_charges"
     " BEGIN SELECT RAISE(ABORT, 'spend charges are append-only'); END"),
    (SpendCharge.__table__,
     "CREATE TRIGGER trg_spend_chg_no_delete BEFORE DELETE ON spend_charges"
     " BEGIN SELECT RAISE(ABORT, 'spend charges are append-only'); END"),
):
    event.listen(_tbl, "after_create", DDL(_ddl))


project_children = relationship  # silence unused-import linters for future relationships
