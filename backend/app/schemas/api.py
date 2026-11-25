"""Typed API contracts. The generated TypeScript client is produced
from /openapi.json so these are the single source of truth."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.domain.redact import redact_detail


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
    "generate.video", "generate.text.cloud", "generate.image.cloud",
    "generate.image.img2img",
    "generate.image.edit",
    "character.create", "character.update", "character.archive",
    "character.unarchive", "character.revert",
    "world.create", "world.update", "world.archive",
    "world.unarchive", "world.revert",
    "location.create", "location.update", "location.archive",
    "location.unarchive", "location.revert",
    "asset.import", "asset.share", "asset.link", "asset.relink", "assets.verify",
    "asset.waveform",
    "backup.export", "backup.import", "backup.restore",
    "scene.create", "scene.update", "scene.archive", "scene.unarchive",
    "scene.revert", "scene.duplicate", "scene.move",
    "scene.cast.assign", "scene.cast.update", "scene.cast.remove",
    "shot.create", "shot.update", "shot.archive", "shot.unarchive",
    "shot.revert", "shot.duplicate", "shot.move",
    "shot.cast.assign", "shot.cast.update", "shot.cast.remove",
    "screenplay.create", "screenplay.update", "screenplay.archive",
    "screenplay.unarchive", "screenplay.revert", "screenplay.move",
    "screenplay_scene.create", "screenplay_scene.update",
    "screenplay_scene.delete", "screenplay_scene.move",
    "shotlist.scaffold", "shotlist.sync", "shotlist.undo",
    "shotlist.redo",
    "character.duplicate",
    "entity.assets.link", "entity.assets.unlink",
    "entity.assets.reorder", "entity.assets.update",
    "refset.create", "refset.update", "refset.delete",
    "shot.generate", "shot.video",
    "keyframe.create", "keyframe.update", "keyframe.archive",
    "keyframe.unarchive", "keyframe.revert", "keyframe.compose",
    "keyframe.generate", "keyframe.result", "keyframe.pin",
    "clip.create", "clip.update", "clip.archive", "clip.unarchive",
    "clip.revert", "clip.compose", "clip.generate", "clip.result",
    "dialogue.create", "dialogue.update", "dialogue.archive",
    "dialogue.unarchive", "dialogue.revert", "dialogue.generate",
    "dialogue.result", "dialogue.place",
    # A6 — first-cut assembly: deterministic plan → review → apply
    "assembly.plan", "assembly.apply", "assembly.review",
    "assembly.revert",
    # A7 — simulated GPU scheduling + provider-neutral cloud planning
    "sim.gpu.work", "cloud.plan",
    "bridge.prepare", "bridge.launch", "bridge.import",
    "bridge.watch.scan", "bridge.watch.enable", "bridge.watch.disable",
    "bridge.cli.detect",
    "spend.budget_set", "spend.budget_remove", "spend.reconcile",
    "cloud.live.enable", "cloud.live.disable",
    "cloud.live.disable_all", "cloud.live.resume",
    "cloud.live.validate_arm", "cloud.live.validate_run",
    "cloud.live.validate_disarm",
    "timeline.create", "timeline.update", "timeline.archive",
    "timeline.unarchive", "timeline.revert", "timeline.duplicate",
    "timeline.undo", "timeline.redo",
    "timeline.clip.insert", "timeline.clip.remove",
    "timeline.clip.move", "timeline.clip.trim",
    "timeline.track.add", "timeline.track.update",
    "timeline.track.reorder", "timeline.track.remove",
    "timeline.clip.place",
    "timeline.clip.split", "timeline.gap.close", "timeline.gap.insert",
    "timeline.clip.gain",
    "timeline.clip.fade", "timeline.clip.automation",
    "timeline.crossfade.set", "timeline.crossfade.clear",
    "timeline.track.automation",
    "timeline.export",
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
    # C3-B — structured authoritative estimate for generation commands
    # (null for commands without one). The approval surface renders this
    # figure; any client-side hint is preliminary only.
    cost_estimate: dict[str, Any] | None = None
    current_revision: int
    launches_generation: bool
    # C4-A — resolved contents of reference_set_id (ordered asset ids +
    # weights) so a UI approval can fingerprint the actual members, not
    # just the set id; None when no set was referenced.
    resolved_references: list[dict[str, Any]] | None = None
    # C4-A — {id, seed, revision} of the previewed set: a pinned-seed
    # or metadata edit changes no member fingerprint, so the approval
    # pins set identity separately via expected_set at commit.
    resolved_set: dict[str, Any] | None = None
    # C4-B — shot.generate only: the shot's generation-relevant state
    # fingerprint (prompt/source/cast/continuity/revision).
    resolved_shot: dict[str, Any] | None = None
    # C4-D — shot.video only: the resolved generation-parameter set
    # (model, dims, duration, sampling, source) pinned via
    # expected_params at commit.
    resolved_params: dict[str, Any] | None = None
    # C5-A — timeline ops only: {id, revision, clip_count} the approval
    # pins via expected_timeline at commit.
    resolved_timeline: dict[str, Any] | None = None
    # A1 — shotlist.scaffold/sync only: the full deterministic diff
    # (adds/updates/archive-only removals/manual-edit conflicts) the
    # approval surfaces; destructive entries apply only via explicit
    # approve_overwrite/approve_remove id lists at commit.
    shotlist_diff: dict[str, Any] | None = None
    # C5-C — shot.generate/shot.video only: the composed prompt
    # (sections, omissions, conflicts, budget) the approval pins via
    # expected_prompt at commit.
    resolved_prompt: dict[str, Any] | None = None
    # A3 — keyframe.* only: keyframe state fingerprint pinned via
    # expected_keyframe; resolved_draft is the keyframe.compose
    # proposal (prompt + source sections) shown before apply.
    resolved_keyframe: dict[str, Any] | None = None
    resolved_draft: dict[str, Any] | None = None
    # A4 — clip.* only: motion-clip state fingerprint pinned via
    # expected_clip (same contract as resolved_keyframe).
    resolved_clip: dict[str, Any] | None = None
    # A5 — dialogue.* only: segment state fingerprint pinned via
    # expected_dialogue; resolved_audio is the deterministic generate
    # plan (text sha, duration, sample rate, resolved voice);
    # resolved_audio_lane pins the audio track dialogue.place targets.
    resolved_dialogue: dict[str, Any] | None = None
    resolved_audio: dict[str, Any] | None = None
    resolved_audio_lane: dict[str, Any] | None = None
    # A6 — assembly.plan only: the deterministic first-cut proposal
    # (items, lanes, missing media, conflicts); assembly.apply shows
    # the apply decision walk (per-item keep/insert/skip/replace).
    resolved_assembly: dict[str, Any] | None = None
    # A7 — cloud.plan: provider-neutral capability/cost/budget/
    # credential-presence/availability preview + plan_fingerprint;
    # sim.gpu.work: scheduler admission preview (device/queue/why).
    resolved_cloud: dict[str, Any] | None = None
    resolved_gpu: dict[str, Any] | None = None


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
    reserved_vram_bytes: int | None = None
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

    @field_validator("progress_json", "result_json", mode="before")
    @classmethod
    def _redact_sensitive_detail(cls, v):
        # Signed/temporary remote URLs persist for reconcile but are
        # bearer references — never leave via job-detail responses.
        return redact_detail(v)


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
    set_id: str | None = None
    role: str
    label: str
    position: int
    weight: float | None = None
    created_at: datetime


class ReferenceSetOut(BaseModel):
    """C4-A — named character reference set + identity guidance."""
    id: str
    project_id: str
    character_id: str
    name: str
    seed: int | None = None
    notes: str
    position: int
    status: str
    revision: int
    member_count: int = 0
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
    # C4-B — generation bindings (character ref set + img2img source)
    reference_set_id: str | None = None
    source_asset_id: str | None = None
    continuity_json: dict[str, Any]
    # A2 — shot planning (camera/framing/notes); outside the A1
    # scaffold-managed set.
    planning_json: dict[str, Any] = {}
    revision: int
    created_at: datetime
    updated_at: datetime


class StoryboardShotOut(BaseModel):
    shot: ShotOut
    location_name: str
    cast: list[CastMemberOut]
    inherited: list[CastMemberOut] = []  # scene-cast members without an own row
    # A2 — shot-scoped CAS associations + missing-media flags
    media: dict[str, Any] = {}
    # A3 — compact keyframe summary {count, roles, with_result}; the
    # full list loads via /shots/{id}/keyframes.
    keyframes: dict[str, Any] = {}
    # A4 — compact per-shot motion-clip summary for card badges; the
    # full list loads via /shots/{id}/clips.
    clips: dict[str, Any] = {}
    # A5 — compact per-shot dialogue summary {count, with_result}; the
    # full list loads via /dialogue?shot_id=.
    dialogue: dict[str, Any] = {}
    # C4-C — durable per-shot generation state (job status + latest
    # produced asset); {}-valued when nothing was generated yet.
    generation: dict[str, Any] | None = None


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
    supports: dict[str, Any]
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


class ModelArtifactOut(BaseModel):
    """U-1 — universal model/adapter registry row (read-only projection).
    `install` is projected from the authoritative model_installs row,
    never stored on the artifact."""
    artifact_id: str
    display_name: str
    kind: str            # model | adapter | auxiliary
    arch: str
    family: str
    modality: str
    fmt: str
    role: str
    version: str
    source_type: str     # catalog | import | discovered | trained
    managed_by: str      # atomic | external
    uri: str | None = None
    content_sha256: str | None = None
    byte_size: int
    license: dict[str, Any]
    support_tier: str
    enabled: bool
    tags: list[str]
    meta: dict[str, Any]
    install_id: str | None = None
    install: dict[str, Any]


class ModelImportRequest(BaseModel):
    """U-2a — reference-only import of ONE user-owned file. The path is
    retained only in job.request_json for execution/crash recovery —
    JobOut never serializes it. U-2b-1 adds presentation-only fields:
    `display_name` (bounded label) and `mode`. U-2b-2a: "managed_copy"
    is accepted only behind the ATOMIC_ENABLE_MANAGED_COPY feature gate
    — the route, claim-time validation, and execution all fail closed
    with mode_not_supported when the gate is off."""
    path: str
    license: dict[str, Any] = {}
    family: str = ""
    role_hint: str = ""
    display_name: str = ""
    mode: str = "reference"


class ModelImportOut(BaseModel):
    job_id: str
    status: str


class ModelArtifactDetailOut(ModelArtifactOut):
    """Authenticated detail view — the only surface that carries
    source_uri and the full inspection report (local trust boundary).
    `managed_copy` (U-2b-2a) is present only for managed import rows —
    it reports live filesystem presence plus publication provenance
    (basename-only dest_rel, never an absolute managed path)."""
    source_uri: str | None = None
    imported_at: str | None = None
    import_report: dict[str, Any] = {}
    managed_copy: dict[str, Any] | None = None


class ManagedCopyReleaseOut(BaseModel):
    """U-2b-2a — synchronous metadata-only release result. `released`
    reports the request succeeded; `already` marks the idempotent no-op
    for an import artifact that was not currently managed. No managed
    bytes are ever deleted by this operation."""
    artifact_id: str
    released: bool
    already: bool


class ImportCapabilitiesOut(BaseModel):
    """U-2b-2b — read-only import-mode capability probe for the desktop.
    `managed_copy_enabled` mirrors the server-side feature gate; `modes`
    is the server-computed list of accepted ModelImportRequest.mode
    values ("reference" is always present). No filesystem paths, env
    vars, or configuration details are exposed — the desktop uses this
    only to decide whether the managed-copy option may be offered."""
    managed_copy_enabled: bool
    modes: list[str]


# ---------- U-3 model discovery ----------

class DiscoveryRootIn(BaseModel):
    """Approve (or re-approve) a scan root. `path` is validated to a
    canonical local directory; `max_depth` clamps to 0..8."""
    path: str
    max_depth: int | None = None


class DiscoveryRootOut(BaseModel):
    """Root-management surface — `display_path` is the verbatim path the
    user approved (same trust boundary as models_dir settings)."""
    root_id: str
    display_path: str
    enabled: bool
    max_depth: int
    added_at: datetime
    archived_at: datetime | None
    last_scan_outcome: str | None
    last_code: str | None
    last_scan_at: datetime | None
    last_entries_seen: int | None
    last_candidates: int | None
    candidate_count: int


class DiscoveryCandidateOut(BaseModel):
    """List payload — NO absolute paths by contract. `rel_path` is
    anchored to the candidate's owning (first-discovering) root;
    `root_label` is that root's basename for disambiguation."""
    candidate_id: str
    name: str
    rel_path: str
    root_id: str
    root_label: str
    fmt_guess: str
    byte_size: int
    presence: str
    linked_artifact_id: str | None
    seen_count: int
    first_seen_at: datetime
    last_seen_at: datetime


class DiscoveryCandidateDetailOut(DiscoveryCandidateOut):
    """Authenticated detail — the ONLY surface carrying the absolute
    candidate path (plus the owning root's display path)."""
    resolved_path: str
    mtime_ns: int
    root_display_path: str
    linked_at: datetime | None


class DiscoveryScanOut(BaseModel):
    job_id: str
    status: str


class ModelsDirOut(BaseModel):
    dir: str
    source: str  # "app setting" | "built-in default"
    builtin_dir: str
    exists: bool
    writable: bool
    free_bytes: int | None
    used_bytes: int


# ---------- U-4b model compatibility verdicts ----------

class CompatVerdictOut(BaseModel):
    """One persisted verdict for (artifact, runtime). Axes are
    independent — a missing runtime never masquerades as architecture
    incompatibility. `stale` is computed live on read (inputs
    fingerprint drift), never stored. No paths or provider data."""
    artifact_id: str
    runtime_key: str
    arch_axis: str            # supported | unsupported | unknown
    arch_confidence: str | None  # tested | documented | unverified
    runtime_axis: str         # operational | installed_unverified |
                              # not_installed | planned | unknown
    hardware_axis: str        # fits | limited | insufficient | unknown
    policy_axis: str          # eligible | restricted | blocked
    level: int                # 0..4 — V5 does not exist
    domain_levels: dict[str, int]
    overall: str              # compatible | limited | blocked |
                              # incompatible | unknown
    blocking_axes: list[str]
    confidence: str
    reasons: list[str]
    warnings: list[str]
    missing: list[str]
    hardware_limits: list[str]
    actions: list[str]
    stale: bool
    ruleset_version: str
    evaluated_at: datetime | None


class ArtifactCompatOut(BaseModel):
    artifact_id: str
    verdicts: list[CompatVerdictOut]


class CompatEvalOut(BaseModel):
    job_id: str
    status: str


class CompatRuntimeDeclOut(BaseModel):
    runtime_key: str
    display_name: str
    caps_version: int
    planned: bool
    backends: list[str]
    validated_versions: list[str]
    families: dict[str, dict[str, Any]]


class CompatRulesetOut(BaseModel):
    """Read-only description of the active compat ruleset — which
    families and runtime claims the verdict engine knows, at what
    versions. No install state, no paths."""
    ruleset_version: str
    families_version: int
    caps_schema_version: int
    max_verification_level: int  # always 4 — V5 deferred
    axes: dict[str, list[str]]
    runtimes: list[CompatRuntimeDeclOut]


# ---------- U-5a workflow contracts + runtime candidates ----------

class WorkflowDefOut(BaseModel):
    """One declarative workflow requirement contract. `status` is a
    static audited declaration — never derived at request time."""
    workflow_key: str
    display_name: str
    required_modes: list[str]
    required_components: list[str]
    optional_components: list[str]
    min_level: int
    policy: str
    status: str          # implemented | partial | missing | held
    notes: str


class WorkflowDefsOut(BaseModel):
    workflow_defs_version: int
    defs: list[WorkflowDefOut]


class RuntimeCandidateOut(BaseModel):
    """One (runtime, adapter) candidacy — a STATIC evidence claim for
    plan review, never an execution offer. `state` is exactly one of
    candidate_compatible_static | candidate_limited |
    candidate_unknown | candidate_blocked. `verdict_stale` is a
    separate marker — it never weakens or overrides a block.
    `verdict` embeds the stored U-4 verdict when one exists."""
    runtime_key: str
    adapter_id: str | None
    binding_kind: str    # catalog_default | compatible_adapter |
                         # alternate_runtime
    state: str
    rank: int
    verdict_stale: bool
    has_verdict: bool
    reasons: list[str]
    missing: list[str]
    verdict: CompatVerdictOut | None


class RuntimeCandidatesOut(BaseModel):
    artifact_id: str
    workflow_key: str
    # Typed completeness indicator for include_stale=false: how many
    # stale-verdict candidates were omitted from `candidates`. Omitted
    # entries are never presented as fresh, and blocked candidates are
    # never omitted — so a nonzero count always means stale optimistic
    # claims were hidden, nothing else.
    stale_candidates_omitted: int
    candidates: list[RuntimeCandidateOut]


class DepFillOut(BaseModel):
    """One resolved fill for a requirement slot. `component_key` is the
    stable C1 identity (`artifact_id` for whole-artifact fills,
    `artifact_id::member_key` inside a bundle). `provenance` records
    WHERE the fill came from; `confidence` is the independent U-4
    evidence grade — never conflated. `file_integrity` is a recorded
    fact only: verified_at_install | recorded_at_import | declared |
    missing — never implied coverage."""
    component_key: str
    artifact_id: str
    role: str
    file_name: str | None
    provenance: str
    confidence: str
    file_integrity: str
    reasons: list[str]


class DepCandidateOut(BaseModel):
    artifact_id: str
    outcome: str             # eligible | ineligible | ambiguous_member
    reasons: list[str]


class DepSlotOut(BaseModel):
    """One requirement slot and its resolution outcome."""
    requirement_id: str
    role: str
    required: bool
    source: str              # workflow_def|family_spec|family_cap|
                             # cast_binding|request
    binds_character_id: str | None
    outcome: str             # resolved|ambiguous|missing|
                             # no_fill_evidence|conflicted|
                             # selector_refused|dep_limit_exceeded|
                             # needs_shot_context
    fills: list[DepFillOut]
    candidates: list[DepCandidateOut]
    reasons: list[str]
    limits: list[str]
    cycle_path: list[str]


class DependencyPlanOut(BaseModel):
    """U-5b — advisory dependency plan for one root artifact +
    workflow + runtime. Five INDEPENDENT axes aggregate into
    `advisory_state` (plan_complete|plan_incomplete|plan_blocked) —
    advisory is NEVER execution authorization."""
    artifact_id: str
    workflow_key: str
    runtime_key: str
    structural_state: str    # complete|incomplete_missing|ambiguous|
                             # conflicted|unknown
    runtime_state: str       # U-5a candidate_* state | unevaluated
    policy_state: str        # clear|held|restricted|blocked
    capacity_state: str      # within_capacity|exceeds_capacity|
                             # not_applicable|unknown
    evidence_state: str      # tested|documented|unverified|missing
    advisory_state: str
    limitations: list[str]
    reasons: list[str]
    requirements: list[DepSlotOut]
    stale_candidates_omitted: int
    limits: dict[str, int]
    limits_exceeded: list[str]
    suggestions: list[str]
    resolution_version: str  # "dep:v1:" + fingerprint
    advisory: bool = True


# ---------- U-5c — execution-plan preview ----------

class HwBackendEvidenceOut(BaseModel):
    available: bool
    measured: bool


class HwDeviceOut(BaseModel):
    """One GPU in the evidence snapshot. Every quantity is labeled by
    provenance: vram_bytes / vram_free_bytes are MEASURED (driver /
    registry / WMI), usable_* are derived estimates, reserved_* are
    scheduler bookkeeping (never physical memory), and the planning
    budget is a derived, conservative figure — not a guarantee of
    physically available VRAM."""
    index: int
    name: str | None
    vendor: str | None
    vram_bytes: int | None                 # physical total
    vram_source: str | None                # registry|nvidia-smi|wmi|wmi_capped
    vram_capped_at_4gb: bool
    vram_free_bytes: int | None            # driver-reported free
    driver_version: str | None
    usable_bytes: int | None               # derived (free | 85%·total | None)
    vram_headroom_bytes: int
    usable_after_headroom_bytes: int | None
    reserved_bytes: int                    # scheduler claims — bookkeeping
    conservative_planning_budget_bytes: int | None
    optimistic_bound_bytes: int | None
    reservation_overlap_possible: bool
    present: bool
    index_stability: str                   # "per_boot"


class HwLostDeviceOut(BaseModel):
    index: int
    reserved_bytes: int                    # bookkeeping retained as evidence


class HwSnapshotOut(BaseModel):
    """U-5c — fingerprinted point-in-time hardware + scheduler evidence.
    The fingerprint covers canonical EVIDENCE only (timestamps excluded),
    so identical evidence yields an identical snapshot_id."""
    snapshot_id: str                       # "hw:v1:" + hash
    snapshot_schema_version: int
    captured_at: float | None
    age_s: float | None
    stale: bool                            # age beyond one probe bucket
    simulated: bool
    devices: list[HwDeviceOut]
    devices_total: int
    lost_devices: list[HwLostDeviceOut]
    backends: dict[str, HwBackendEvidenceOut]
    cpu: dict[str, Any]
    memory: dict[str, Any]
    reservations_captured: bool
    unknowns: list[str]


class DeviceAlternativeOut(BaseModel):
    """One device evaluated against the single generation stage.
    `fit_class`: fits_with_headroom | fits_tight | insufficient |
    unmeasurable | estimate_unknown | insufficient_evidence |
    device_backend_mismatch | backend_evidence_unknown |
    device_not_present. `backend_evidence` grades each runtime-declared
    backend per device: measured | inferred | unknown | mismatch — a
    machine-wide Vulkan/DirectML flag is never measured per-device
    support."""
    index: int | None
    name: str | None
    vendor: str | None
    eligible: bool
    outcome: str
    fit_class: str
    backend_evidence: dict[str, str]
    est_vram_bytes: int | None
    budget_bytes: int | None
    reservation_overlap_possible: bool
    reasons: list[str]
    limitations: list[str]


class CpuLaneOut(BaseModel):
    """The CPU alternative — explicit only, never a silent fallback.
    `evidence_class` is the static CPU-capability ladder:
    cpu_unsupported | cpu_declared | cpu_adapter_contract |
    cpu_command_consistent | cpu_operational (the last requires
    verified execution evidence — never produced today, so
    cpu_lane_ok is unreachable under current contracts)."""
    permitted: bool
    explicitly_requested: bool
    outcome: str                           # cpu_lane_ok | cpu_lane_blocked |
                                           # cpu_lane_unverified |
                                           # cpu_fallback_unsupported
    evidence_class: str = "cpu_unsupported"
    est_ram_bytes: int | None
    ram_decision: str | None               # ok | warn | block | unknown
    ram_decision_reason: str = ""
    reasons: list[str]
    limitations: list[str]


class StageComponentOut(BaseModel):
    component_key: str
    artifact_id: str
    role: str
    file_name: str | None
    provenance: str
    declared_size_bytes: int | None        # manifest-declared member size


class EstimateOut(BaseModel):
    """basis: heuristic (catalog anchors) | partial (declared weights,
    unknown activation) | unknown."""
    bytes: int | None
    basis: str


class OffloadAlternativeOut(BaseModel):
    """A secondary residency-mode estimate — advisory; requires the named
    catalog capability AND the runtime's residency contract
    (`requires_capability` is null for the capability-free 'gpu' mode)."""
    residency_mode: str
    est_vram_bytes: int | None
    basis: str
    requires_capability: str | None


class ExecStageOut(BaseModel):
    """Exactly one genuine generation stage (U-5c-1). No fabricated
    pipeline steps, no parallelism, no pooled devices."""
    stage_id: str
    kind: str
    workflow_key: str
    runtime_key: str
    residency_mode: str                    # gpu default; explicit request
    residency_basis: str                   # explicit | default
    residency_notes: list[str]             # e.g. vae_expected_on_cpu
    components: list[StageComponentOut]
    lora_applications: int
    est_vram: EstimateOut
    est_ram: EstimateOut
    runtime_overhead_basis: str            # included_in_anchor | unknown
    offload_alternatives: list[OffloadAlternativeOut]


class ExecutionPreviewOut(BaseModel):
    """U-5c-1 — advisory-only execution-plan preview. `advisory` is always
    True: nothing here is execution authorization, no GPU is reserved,
    and GpuScheduler remains the sole execution-time authority."""
    advisory: bool = True
    advisory_state: str                    # preview_ready_advisory |
                                         # preview_incomplete | preview_blocked
    plan_fingerprint: str                  # "plan:v2:" + hash
    plan_result_fingerprint: str = ""      # "planresult:v1:" + hash —
                                         # material outcome identity
    artifact_id: str
    workflow_key: str
    runtime_key: str
    dependency_plan_ref: str               # U-5b resolution_version
    snapshot: HwSnapshotOut | None         # null = hardware evidence
                                         # unavailable (cold cache)
    snapshot_stale: bool
    snapshot_freshness: str = "unavailable"  # fresh | stale | unavailable —
                                           # independent of simulated
    snapshot_simulated: bool = False
    verdict_staleness: dict[str, Any] = {}   # root state + pool counts:
                                           # current|stale|unevaluable|simulated
    ram_eval: dict[str, Any] = {}            # evaluate_ram: decision,
                                             # fully_evaluated, checks
    cpu_evidence: str = "cpu_unsupported"
    stage: ExecStageOut
    device_alternatives: list[DeviceAlternativeOut]
    devices_total: int
    device_alternatives_truncated: bool
    cpu_lane: CpuLaneOut | None
    dependency_state: str
    runtime_state: str
    policy_state: str
    capacity_state: str
    hardware_state: str
    ram_state: str
    evidence_state: str
    limitations: list[str]
    reasons: list[str]
    limits_exceeded: list[str]
    limits: dict[str, int]


class GpuSettingsOut(BaseModel):
    mode: str  # auto | manual
    preferred_gpu: int | None
    slots: list[dict[str, Any]]
    free: int
    gpu_count: int


class GpuSettingsUpdate(BaseModel):
    mode: str | None = None
    preferred_gpu: int | None = None


class ResourceSettingsOut(BaseModel):
    """C4-E — user-configured admission thresholds (GiB; None = default)."""
    os_headroom_gib: float | None = None
    critical_commit_gib: float | None = None
    critical_phys_gib: float | None = None
    min_disk_free_gib: float | None = None
    vram_headroom_gib: float | None = None


class ResourceSettingsUpdate(BaseModel):
    os_headroom_gib: float | None = None
    critical_commit_gib: float | None = None
    critical_phys_gib: float | None = None
    min_disk_free_gib: float | None = None
    vram_headroom_gib: float | None = None


# ---------- timelines (C5-A) ----------

class TimelineClipOut(BaseModel):
    id: str
    project_id: str
    timeline_id: str
    track_id: str | None = None
    position: int
    start_frames: int = 0
    source_asset_id: str
    source_version_id: str
    source_sha256: str
    in_frame: int
    out_frame: int
    src_frames: int
    src_fps_num: int
    src_fps_den: int
    src_duration_s: float
    src_has_audio: bool = True
    gain_db: float = 0.0
    fade_in_units: int = 0
    fade_in_curve: str = "linear"
    fade_out_units: int = 0
    fade_out_curve: str = "linear"
    automation_json: str = "[]"
    automation_interp: str = "linear"
    shot_id: str | None
    scene_id: str | None
    created_at: datetime


class TimelineCrossfadeOut(BaseModel):
    id: str
    project_id: str
    timeline_id: str
    out_clip_id: str
    in_clip_id: str
    tl_start: int
    tl_frames: int
    curve: str
    satisfied: bool = True
    created_at: datetime


class TimelineTrackOut(BaseModel):
    id: str
    project_id: str
    timeline_id: str
    kind: str
    name: str
    position: int
    visible: bool
    locked: bool
    muted: bool
    gain_db: float = 0.0
    solo: bool = False
    automation_json: str = "[]"
    automation_interp: str = "linear"
    created_at: datetime


class TimelineOut(BaseModel):
    id: str
    project_id: str
    name: str
    description: str
    fps_num: int
    fps_den: int
    status: str
    revision: int
    created_at: datetime
    updated_at: datetime


class TimelineDetailOut(TimelineOut):
    clips: list[TimelineClipOut]
    tracks: list[TimelineTrackOut] = []
    crossfades: list[TimelineCrossfadeOut] = []
    total_duration_s: float
    undo_label: str | None = None
    redo_label: str | None = None


class TimelineRevisionOut(BaseModel):
    revision: int
    summary: str
    created_at: datetime


# ---------- B-3c — LoRA resolution preview (read-only) ----------


class LoraPreviewRequest(BaseModel):
    """Read-only adapter-resolution preview context.

    `loras` mirrors the generation payload's `loras` field EXACTLY
    (bare key strings or {"key", "strength"?} objects) and is validated
    by the shared B-1 resolver — not by this schema — so refusal
    semantics are identical to a real generation request. When omitted,
    the proposed set is derived from the shot's stored cast bindings.

    Context: shot_id and/or keyframe_id (a keyframe resolves its shot
    exactly like keyframe.generate). prompt/negative_prompt are optional
    — supplied text exercises the B-1 prompt-syntax guard and unlocks
    prompt/runtime hash fields."""
    model_key: str | None = None
    loras: list[Any] | None = None
    shot_id: str | None = None
    keyframe_id: str | None = None
    prompt: str | None = None
    negative_prompt: str | None = None


class LoraPreviewRequestedOut(BaseModel):
    shape: str            # "string" | "object" | "invalid"
    key: Any = None
    strength: Any = None


class LoraPreviewAdapterOut(BaseModel):
    """A resolved, backend-accepted adapter — the same fields the
    durable generation request pins (install_row_id stays internal)."""
    key: str
    version: str
    role: str             # "identity" | "style"
    strength: float
    sha256: str
    staged_name: str
    base_family: str
    base_model_key: str
    base_model_sha256: str
    bound_character_id: str | None = None


class LoraPreviewHashesOut(BaseModel):
    adapter_sha256: dict[str, str]        # resolved key → pinned sha
    base_model_sha256: str | None = None
    prompt_sha256: str | None = Field(
        default=None,
        description="sha256 of the SUPPLIED prompt text — a durable "
                    "generation request hashes the composed prompt "
                    "(context included), so values differ")
    runtime_prompt_sha256: str | None = Field(
        default=None,
        description="sha256 of supplied prompt + resolved adapter tags "
                    "— not the composed-prompt pin a durable job "
                    "carries")
    unavailable: list[str] = []           # named-not-computable + why


class LoraPreviewContextOut(BaseModel):
    shot_id: str | None = None
    keyframe_id: str | None = None
    model_key: str | None = None
    proposed_from_cast: bool = False


class LoraPreviewOut(BaseModel):
    """Resolution preview result — refusal is DATA, not an HTTP error:
    a refused selection returns 200 with accepted=false + reasons so it
    can render alongside accepted output."""
    accepted: bool
    errors: list[str] = []                # context + B-1 resolver refusals
    prompt_errors: list[str] = []         # prompt-syntax guard refusals
    requested: list[LoraPreviewRequestedOut] = []
    resolved: list[LoraPreviewAdapterOut] = []
    hashes: LoraPreviewHashesOut
    est_vram_lora_overhead_bytes: int | None = None
    context: LoraPreviewContextOut
    notes: list[str] = []


# ---------- U-5c-2a declarative pipeline contracts ----------


class PipelineConstraintOut(BaseModel):
    kind: str
    value: Any = None


class PipelineParamOut(BaseModel):
    param_key: str
    ptype: str
    required: bool
    has_default: bool = False
    default: Any = None
    description: str = ""
    constraints: list[PipelineConstraintOut] = []


class PipelineBindingOut(BaseModel):
    request_key: str
    source: str                    # external | edge | constant
    external_key: str | None = None
    edge_id: str | None = None
    constant: Any = None
    constraints: list[PipelineConstraintOut] = []


class PipelinePortOut(BaseModel):
    port_key: str
    direction: str                 # in | out
    asset_kind: str
    required: bool
    cardinality: int
    command_arg: str | None = None
    output_contract: str | None = None


class PipelineNodeOut(BaseModel):
    """One executable whole-job node. The five derived axes are
    independent static evidence — `operational_readiness` is always
    the literal "not_evaluated"; nothing here asserts a runnable
    pipeline."""
    node_id: str
    node_kind: str
    command_key: str
    adapter_id: str | None
    workflow_key: str | None
    runtime_keys: list[str]
    runtime_declared: dict[str, str]       # declared | planned | unknown
    required_components: list[str]
    input_ports: list[PipelinePortOut]
    output_ports: list[PipelinePortOut]
    param_bindings: list[PipelineBindingOut]
    policy_notes: list[str]
    command_contract: str                  # exists | unknown
    adapter_class: str                     # local | mock | unknown
    execution_class: str                   # implemented | experimental |
                                           # simulated | held | missing
    policy_state: str                      # held | restricted |
                                           # notes_only | none_declared
    operational_readiness: str             # always "not_evaluated"


class PipelineEdgeOut(BaseModel):
    edge_id: str
    src_node: str
    src_port: str
    dst_node: str
    dst_port: str
    dst_command_arg: str
    asset_role: str
    ownership: str
    provenance: str


class PipelineFindingOut(BaseModel):
    code: str
    subject: str
    severity: str
    detail: str


class PipelineValidationOut(BaseModel):
    structural: str                        # valid | invalid — never
                                           # operational readiness
    findings: list[PipelineFindingOut]


class PipelineSummaryOut(BaseModel):
    pipeline_key: str
    display_name: str
    category: str
    status: str
    node_count: int
    edge_count: int
    structural: str                        # invalid defs are surfaced,
                                           # never silently valid
    definition_fingerprint: str


class PipelinePlanSelection(BaseModel):
    """U-5c-2b — typed artifact selection. `catalog_key` resolves through
    the model catalog; `artifact_id` binds one exact registry row.
    Strict: no coercion, no unknown fields, no bool-as-int."""
    model_config = {"extra": "forbid", "strict": True}
    kind: str                                # catalog_key | artifact_id
    value: str


class PipelinePlanNodePrefs(BaseModel):
    """Per-node placement controls. `device_constraint` is a HARD
    constraint ('auto'|'cpu'|<index>); `device_preference` is soft and
    feeds ranking only. `lifetime_assumption` selects the conditional
    release scenario."""
    model_config = {"extra": "forbid", "strict": True}
    device_constraint: str | int | None = None
    device_preference: str | int | None = None
    residency_mode: str | None = None
    wave_policy: str | None = None          # declared intent only —
                                           # v1 honors "fixed_level"


class PipelinePlanPreviewIn(BaseModel):
    """Request body — strictly bounded before any expensive work."""
    model_config = {"extra": "forbid", "strict": True}
    params: dict[str, str | int | float | bool] = {}
    selections: dict[str, PipelinePlanSelection] = {}
    nodes: dict[str, PipelinePlanNodePrefs] = {}
    lifetime_assumption: str | None = None   # release_assumed |
                                           # retained_model | unknown
    include_stale: bool = True


class PipelinePlanFindingOut(BaseModel):
    """One aggregate/definition finding — emitted code, subject and
    detail only."""
    model_config = {"extra": "forbid"}
    code: str
    subject: str
    detail: str


class PipelinePlanDeferredOut(BaseModel):
    """One deferred execution input — a typed binding to a future
    node output; never an asset identity."""
    model_config = {"extra": "forbid"}
    node_id: str
    request_key: str
    edge_id: str | None
    state: str                                # deferred_future_output
    asset_kind: str | None
    ownership: str | None
    provenance: str | None


class PipelinePlanNodeOut(BaseModel):
    """Per-node planning evidence row."""
    model_config = {"extra": "forbid"}
    node_id: str
    artifact_id: str | None
    model_key_effective: str | None
    bindability: str                          # bindable|non_bindable|
                                            # unknown|unresolved
    bindability_reason: str
    integrity_class: str                      # verified|recorded|
                                            # missing|declared
    dep_plan_ref: str | None
    runtime_state: str
    est_vram_bytes: int | None
    est_ram_bytes: int | None
    # plan_preview.evaluate_device() rows — one per snapshot device
    # plus at most one explicit hard index. Construction-bounded by
    # len(devices)+1; 64 is a defensive serialization cap (the 8-device
    # planner limit governs the search domain, not reporting).
    device_alternatives: list[dict] = Field(max_length=64)
    # plan_preview.cpu_lane() verdict dict when a CPU lane is evaluated.
    cpu_lane: dict | None
    residency_error: str | None
    workflow_key: str | None


class PipelinePlanAssignOut(BaseModel):
    """One node assignment inside a retained candidate."""
    model_config = {"extra": "forbid"}
    artifact_id: str | None
    runtime_key: str | None
    adapter_id: str | None
    device: int | None                        # -1 = cpu lane
    residency: str | None


class PipelinePlanCandidateOut(BaseModel):
    """One retained candidate arrangement (top-8)."""
    model_config = {"extra": "forbid"}
    # Keyed by node_id / device index — bounded by max_nodes (32) and
    # the device-alternatives reporting cap (64).
    assignment: dict[str, PipelinePlanAssignOut] = Field(max_length=32)
    # Wave groups of node ids — ≤32 groups of ≤32 nodes (max_nodes).
    wave: list[Annotated[list[str], Field(max_length=32)]] = Field(
        max_length=32)
    lifetime: str
    state: str                                # feasible|feasible_conditional|
                                            # infeasible_*|blocked
    device_fits: dict[str, str] = Field(max_length=64)
    device_budgets: dict[str, int | None] = Field(max_length=64)
    host_peak_bytes: int | None
    # resources.evaluate_ram() verdict — decision/checks/snapshot keys
    # owned by the shared helper, not this schema.
    ram_eval: dict
    node_states: dict[str, str] = Field(max_length=32)
    # Per-candidate reason strings ≤ ~3×max_nodes; limitation strings
    # are a fixed dedup'd vocabulary — bounds are defensive caps.
    reasons: list[str] = Field(max_length=128)
    limitations: list[str] = Field(max_length=32)


class PipelinePlanAxesOut(BaseModel):
    model_config = {"extra": "forbid"}
    structure: str
    resource_evidence: str
    execution_input_availability: str


class PipelinePlanOmittedOut(BaseModel):
    """Ceiling-vs-omission flags — True means work was omitted, not
    merely that the ceiling was reached."""
    model_config = {"extra": "forbid"}
    partial: bool
    completed: bool
    waves: bool
    groups: bool
    variants: bool
    findings: int


class PipelinePlanCountersOut(BaseModel):
    model_config = {"extra": "forbid"}
    partial_expansions: int
    completed_assignments: int
    group_proposals: int
    waves_attempted: int
    waves_materialized: int
    waves_evaluated: int
    explored_candidates: int
    retained: int
    omitted: PipelinePlanOmittedOut
    findings_omitted: int


class PipelinePlanPreviewOut(BaseModel):
    """U-5c-2b — advisory-only multi-node pipeline plan preview.
    `advisory` is always True; `operational_readiness` is always
    'not_evaluated'. Nothing here reserves, enqueues or executes."""
    advisory: bool = True
    operational_readiness: str = "not_evaluated"
    pipeline_key: str
    pipeline_valid: str                      # valid | invalid
    advisory_state: str                      # invalid | plan_blocked |
                                           # plan_incomplete | ready_advisory
    block_reason: str | None
    axes: PipelinePlanAxesOut
    # Bounds mirror planner LIMITS — enforced again at serialization
    # (max_nodes 32, max_retained_candidates 8, max_findings 64).
    nodes: list[PipelinePlanNodeOut] = Field(max_length=32)
    # Edge-source deferred inputs ≤ max_nodes × pipeline_defs
    # max_param_bindings_per_node (32×32).
    deferred_bindings: list[PipelinePlanDeferredOut] = Field(
        max_length=1024)
    candidates: list[PipelinePlanCandidateOut] = Field(max_length=8)
    counters: PipelinePlanCountersOut
    # Distinct TRUNC_* classes emitted — 7 exist today.
    truncations: list[str] = Field(max_length=8)
    search_exhaustive: bool
    limits: dict[str, int] = Field(max_length=16)
    snapshot_freshness: str
    snapshot_simulated: bool
    snapshot_id: str | None
    plan_fingerprint: str                    # "pipeplan:v1:" + hash
    plan_result_fingerprint: str             # "piperesult:v1:" + hash
    findings: list[PipelinePlanFindingOut] = Field(max_length=64)
    # Dedup'd vocabularies across ≤8 retained candidates — defensive
    # caps well above the per-candidate ceilings.
    limitations: list[str] = Field(max_length=256)
    reasons: list[str] = Field(max_length=1024)


class PipelineListOut(BaseModel):
    pipelines: list[PipelineSummaryOut]


class PipelineDescribeOut(BaseModel):
    """Static describe surface for one declared pipeline. Advisory
    metadata only — no execution claim, no operational evaluation,
    no project or machine state."""
    pipeline_key: str
    display_name: str
    description: str
    category: str
    status: str
    schema_version: int
    definition_version: int
    required_context: list[str]
    policy_constraints: list[str]
    limitations: list[str]
    external_params: list[PipelineParamOut]
    nodes: list[PipelineNodeOut]
    edges: list[PipelineEdgeOut]
    validation: PipelineValidationOut
    concurrency_pairs: list[list[str]]     # potential only — never
                                           # guaranteed execution
    static_assessment: str
    operational_readiness: str             # always "not_evaluated"
    definition_fingerprint: str
    contract_evidence_fingerprint: str
