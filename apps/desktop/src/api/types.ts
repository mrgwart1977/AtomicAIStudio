// API contract types — mirrors backend/app/schemas/api.py.
// Regenerate a fuller client later with `npm run gen:api` (openapi-typescript).

export interface BackendInfo {
  baseUrl: string;
  token: string;
}

export interface Project {
  id: string;
  name: string;
  status: string;
  template: string;
  kind: string;
  aspect_ratio: string;
  width: number;
  height: number;
  fps_num: number;
  fps_den: number;
  style: string;
  storage_location_id: string | null;
  backup_dir: string | null;
  revision: number;
  settings_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface ProjectCard extends Project {
  counts: { characters: number; worlds: number; locations: number; assets: number };
  running_jobs: number;
  thumbnail_asset_id: string | null;
}

// ---- C1 providers ----

export interface ProviderSummary {
  key: string;
  label: string;
  category: string; // text | image | video | audio
  account: string;
  status: string;
  status_label: string;
  tier: string; // T1 | T2 | T3 | T4
  method: string;
  access_class: string; // A | B | C | D | E
  enabled: boolean;
  credential_configured: boolean;
  credential_unreadable: boolean;
  capabilities: string[];
  is_mock_only: boolean;
  status_reason: string;
}

export interface CredentialStatus {
  configured: boolean;
  fields: string[];
  set_at: string | null;
  readable: boolean;
  unreadable_reason: string | null;
}

export interface ProviderStrategy {
  method: string;
  tier: string;
  capabilities: string[];
  automation_permission: string;
  user_interaction: string;
  note: string;
  strategy_id?: string;
  /** User-driven strategies may document a launch surface. */
  connection?: { launch_url?: string | null } | null;
  last_verified?: string | null;
  tos_evidence?: { url?: string | null; checked?: string | null } | null;
}

export interface ProviderDetail extends Omit<ProviderSummary, "capabilities"> {
  auth_methods: string[];
  credential_fields: Array<{ name: string; secret: boolean; required: boolean; hint: string }>;
  base_url: string | null;
  region_selectable: boolean;
  job_model: string | null;
  pricing_model: string | null;
  strategies: ProviderStrategy[];
  models: Array<{
    id: string;
    capability: string;
    cost_table?: {
      input_per_mtok?: number;
      output_per_mtok?: number;
      currency?: string;
      checked?: string;
    } | null;
  }>;
  docs_url: string | null;
  tos_url: string | null;
  privacy_summary: string;
  notes: string;
  aliases: string[];
  // C1.5 §5 — documented local-CLI surface; null when none is documented.
  cli: {
    executables: string[];
    platforms: string[];
    install_hint_dirs: string[];
    docs_url: string;
    note: string;
  } | null;
  shared_credential_with: string[];
  settings: Record<string, unknown>;
  region: string | null;
  credential: CredentialStatus;
  capabilities: string[];
}

// ---------- C2 exit gate (Stage A) ----------

export interface LiveProviderStatus {
  enabled: boolean;
  model_id: string | null;
  credential_ref: string | null;
  granted_at: string | null;
  revoked_at: string | null;
  revoked_reason: string | null;
  credential_configured: boolean;
  dispatch_ready: boolean;
  blocked_reason: string | null;
  live_exposure_usd: number;
}

export interface LiveStatus {
  stage: string;
  global_disabled: boolean;
  global_disabled_at: string | null;
  caps_usd: Record<string, string>;
  global_exposure_usd: number;
  timeouts: Record<string, Record<string, number>>;
  providers: Record<string, LiveProviderStatus>;
}

// bridge.cli.detect live response (C1.5 §5) — unauthenticated paths only;
// the durable audit keeps status/counts, never these filesystem details.
export interface CliDetectResult {
  action: string;
  status: "detected" | "not_found" | "unsupported_platform";
  provider_key: string;
  platform: string;
  candidates: Array<{ name: string; path: string; via: string }>;
  skipped_untrusted: number;
  verified: false; // every detected path is explicitly unverified
  reason?: string;
}

export interface ProviderTestResult {
  ok: boolean;
  via: string;
  note?: string | null;
  error?: string | null;
  would_use?: string | null;
  tier?: string | null;
}

// ---- C1.5 access bridge ----

export interface CapabilityEntry {
  capability: string;
  strategy_id: string | null;
  method: string | null;
  tier: string | null;
  status: string; // documented | needs_recheck | refused_prohibited
  stale: boolean;
  verification: string; // always "catalog_documented"
  dispatch: string[]; // bridge actions runnable now, e.g. ["prepare","launch","import"]
  dispatch_blocked: string | null; // stale | t1_not_implemented | informational_only | prohibited
  required_user_action: string; // site_workflow | file_import | reverify_strategy | none_available
  alternate: {
    strategy_id: string | null;
    method: string | null;
    tier: string | null;
  } | null;
}

export interface ProviderCapabilitiesReport {
  provider_key: string;
  evidence_stamp: string;
  provider_status: string;
  enabled: boolean;
  capabilities: CapabilityEntry[];
}

export interface CommandProposal {
  command: Record<string, unknown>;
  summary: string;
  affected: Array<Record<string, unknown>>;
  warnings: string[];
  diff?: Record<string, unknown>;
  estimated_cost: { compute: string; currency_usd: number; requires_approval: boolean };
  current_revision: number;
  launches_generation: boolean;
}

export interface CommitResult {
  command_id: string;
  status: string;
  revision: number;
  idempotent_replay: boolean;
  job_id: string | null;
  duplicate_project_id?: string | null;
  deleted?: boolean;
  warnings: string[];
  bridge?: BridgeResult | null;
}

export interface WatchCandidate {
  name: string;
  path: string;
  size: number;
  mtime: number;
  kind: string;
}

/** C1.5 stage B — live polling state for one provider's watch folder.
 * Candidates are in-memory only; enabling/disabling is an audited
 * bridge.watch.enable/disable command, never a direct settings write. */
export interface WatchStatus {
  enabled: boolean;
  status: "ok" | "degraded" | "off";
  reason: string | null;
  folder_name: string | null;
  candidates: WatchCandidate[];
  last_poll: string | null;
  paused_reason: string | null;
}

export interface BridgeResult {
  action: "prepare" | "launch" | "import" | "watch_scan"
    | "watch_enable" | "watch_disable";
  provider_key: string;
  strategy_id?: string;
  method?: string;
  url?: string | null;
  open_via?: string; // "client_shell" — desktop opens it, backend never can
  prepared_text?: string;
  char_count?: number;
  paste_hint?: string;
  template_applied?: boolean;
  job_id?: string;
  external_source?: Record<string, unknown>;
  // watch_scan results — candidates are read-only listings, never auto-imported
  folder?: string;
  candidates?: WatchCandidate[];
  truncated?: boolean;
}

export interface Job {
  id: string;
  project_id: string | null; // null = app-level job (model downloads)
  adapter_id: string;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  progress: number;
  message: string;
  error: string | null;
  resumable: boolean;
  output_asset_id: string | null;
  command_id: string | null;
  gpu_index?: number | null;
  model_key?: string | null;
  result_json?: Record<string, unknown> | null;
  progress_json?: JobProgressDetail | null;
  cancel_requested?: boolean;
  attempt_no?: number;
  execution_scope?: string; // "local" | "remote"
  recovery_state?: string | null; // e.g. "needs_reconciliation"
  created_at: string;
  completed_at: string | null;
}

export interface JobProgressDetail {
  bytes_done: number;
  bytes_total: number;
  current_file: string;
  rate_bps: number;
  eta_seconds: number | null;
  elapsed_seconds: number;
  // ---- remote lifecycle fields (C2 cloud jobs; absent on local jobs) ----
  remote_intent?: {
    provider_key?: string;
    model_id?: string | null;
    lifecycle?: string; // "sync" | "async_poll" | "mock"
    simulated?: boolean;
    data_out?: string;
  } | null;
  remote_job_id?: string | null;   // provider op id — synthetic under simulation
  remote_status?: string | null;   // last observed provider status
  remote_outcome?: string | null;  // succeeded | provider_rejected | ambiguous | ...
  reconcile_required?: boolean;
  transport_simulated?: boolean;
  remote_note?: string | null;
  remote_polls?: number;
  usage_json?: Record<string, unknown> | null;
}

export interface Asset {
  id: string;
  project_id: string;
  name: string;
  kind: string;
  origin: string;
  scope: string;
  status: string;
  uri: string;
  sha256: string;
  byte_size: number;
  current_version_id: string | null;
  created_at: string;
  provenance_json?: Record<string, unknown>;
  media_json?: Record<string, unknown>;
}

export interface RuntimeInstallInfo {
  status: string;
  version?: string;
  install_dir?: string;
  exe_path?: string;
  verified?: boolean;
}

export interface RuntimeInfo {
  key: string;
  display_name: string;
  backends: string[];
  install?: RuntimeInstallInfo | null;
  installs?: Record<string, RuntimeInstallInfo>;
}

export interface AssetPage {
  items: Asset[];
  total: number;
  offset: number;
  limit: number;
}

export interface Entity {
  id: string;
  project_id: string;
  name: string;
  summary: string;
  status: string;
  revision: number;
  created_at: string;
  updated_at: string;
  world_id?: string | null;
  data: Record<string, unknown>;
}

export interface EntityRevision {
  id: string;
  entity_type: string;
  entity_id: string;
  revision: number;
  summary: string;
  snapshot_json: Record<string, unknown>;
  created_at: string;
}

// ---------- storyboard (M2) ----------

export interface CastMember {
  id: string;
  character_id: string;
  position: number;
  character: { id: string; name: string; status: string };
  role?: string | null;
  notes_json?: Record<string, unknown> | null;
  wardrobe_variant?: string | null;
  blocking?: string | null;
  expression?: string | null;
  emotion?: string | null;
  action_instructions?: string | null;
  dialogue?: string | null;
  voice_override?: string | null;
  model_ref?: string | null;
  lora_json?: unknown[] | null;
  continuity_json?: Record<string, unknown> | null;
  reference_ids_json?: unknown[] | null;
  ext_json?: Record<string, unknown> | null;
  /** "shot" = own row (per-shot override) · "scene" = inherited from scene cast */
  source?: "shot" | "scene" | null;
}

export interface Shot {
  id: string;
  project_id: string;
  scene_id: string;
  title: string;
  description: string;
  intent: string;
  prompt: string;
  shot_type: string;
  duration_sec: number;
  position: number;
  status: string;
  location_id: string | null;
  thumbnail_asset_id: string | null;
  continuity_json: Record<string, unknown>;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface StoryboardShot {
  shot: Shot;
  location_name: string;
  cast: CastMember[];
  /** Scene-cast members without an own row in this shot — inherited state. */
  inherited: CastMember[];
}

export interface Scene {
  id: string;
  project_id: string;
  title: string;
  description: string;
  intent: string;
  position: number;
  status: string;
  world_id: string | null;
  location_id: string | null;
  continuity_json: Record<string, unknown>;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface StoryboardScene {
  scene: Scene;
  location_name: string;
  cast: CastMember[];
  shots: StoryboardShot[];
}

export interface EntityAssetLink {
  id: string;
  project_id: string;
  entity_type: string;
  entity_id: string;
  asset_id: string;
  role: string;
  label: string;
  position: number;
  created_at: string;
}

export interface StorageLocation {
  id: string;
  label: string;
  root_path: string;
  kind: string;
  status: string;
  is_default: boolean;
  created_at: string;
}

export interface BackupRun {
  id: string;
  project_id: string | null;
  project_name: string;
  kind: string;
  status: string;
  package_path: string;
  package_sha256: string;
  byte_size: number;
  destination_dir: string;
  bytes_total: number;
  stats_json: Record<string, unknown>;
  job_id: string | null;
  error: string | null;
  created_at: string;
  completed_at: string | null;
}

export interface BackupDestination {
  dir: string;
  source: string;
  app_default_dir: string;
  builtin_dir: string;
  project_override: string;
  estimated_bytes: number;
  path: string;
  exists: boolean;
  writable: boolean;
  free_bytes: number;
  needed_bytes: number;
  errors: string[];
  warnings: string[];
}

export interface PackagePreview {
  project_name: string;
  format_version: number;
  created_at: string | null;
  counts: Record<string, number>;
  files: number;
  byte_size: number;
}

export interface SharedAsset {
  asset: Asset;
  source_project_name: string;
  linked_here: boolean;
}

export interface HardwareReport {
  probed_at: number;
  os: { system: string; release: string; version: string };
  cpu: { name: string; physical_cores: number | null; logical_cores: number | null };
  memory: { total_bytes: number; available_bytes: number };
  gpus: Array<{
    index: number;
    name: string | null;
    vendor: string;
    vram_bytes: number | null;
    vram_source?: string;
    vram_free_bytes?: number | null;
    vram_capped_at_4gb: boolean;
    driver_version: string | null;
    inference_capable: boolean | null;
  }>;
  disks: Array<{ mount: string; fstype: string; total_bytes: number; free_bytes: number }>;
  backends?: Record<string, { available: boolean; measured?: boolean; icd_count?: number }>;
  runtimes: {
    python: string;
    ffmpeg: { available: boolean; version: string | null; path: string | null };
    torch_installed: boolean;
    cuda_detected: boolean | null;
  };
  hardware_profile: string | null;
}

// ---------- models & generation (M3) ----------

export type CompatStatus =
  | "compatible" | "limited" | "unsupported" | "unknown"
  | "restricted" | "unavailable" | "planned";

export type SupportTier = "production" | "tested" | "experimental" | "planned";
export type Modality = "image" | "video" | "audio" | "multimodal";

export interface Compatibility {
  status: CompatStatus;
  support?: SupportTier;
  modality?: Modality;
  modes?: string[];
  reasons: string[];
  warnings: string[];
  suggested: Record<string, unknown>;
  estimate: { vram_bytes: number | null; basis: string };
}

export interface ModelInstall {
  id?: string;
  status: string; // not_installed | downloading | verifying | installed | failed | removed
  install_root?: string;
  rel_dir?: string;
  file_name?: string;
  byte_size?: number;
  verified?: boolean;
  version?: string;
  error?: string | null;
  created_at?: string | null;
}

export interface ModelCatalogEntry {
  key: string;
  display_name: string;
  family: string;
  version: string;
  adapter_id: string;
  format: string;
  source: Record<string, unknown>;
  license: { name: string; url: string; commercial_ok: boolean; notes: string };
  files: Array<{ name: string; size_bytes: number; sha256: string | null }>;
  download_bytes: number;
  requirements: {
    min_vram_bytes?: number; rec_vram_bytes?: number;
    min_ram_bytes?: number; min_disk_bytes?: number; backends?: string[];
  };
  capabilities: {
    tasks?: string[];
    negative_prompt?: boolean;
    seed?: boolean;
    sizes?: { min?: number; max?: number; step?: number; presets?: Array<{ w: number; h: number; label: string }> };
    steps?: { min?: number; max?: number; default?: number };
    cfg?: { min?: number; max?: number; default?: number };
    samplers?: string[];
    schedulers?: string[];
    batch?: { min?: number; max?: number; default?: number };
    ref_images?: boolean;
    lora?: boolean;
    precision?: string[];
    gpu_selection?: boolean;
    cpu_fallback?: boolean;
    resolutions?: string[];
    aspect_ratios?: string[];
    durations_s?: { min?: number; max?: number };
    fps?: number[];
    audio?: boolean;
    multi_shot?: boolean;
    character_consistency?: boolean;
    strengths?: string[];
  };
  runtime_key: string;
  support: SupportTier;
  modality: Modality;
  modes: string[];
  notes: string;
  install: ModelInstall | null;
  compatibility: Compatibility;
}

export interface ModelsDir {
  dir: string;
  source: string;
  builtin_dir: string;
  exists: boolean;
  writable: boolean;
  free_bytes: number | null;
  used_bytes: number;
}

export interface GpuSchedulerState {
  gpu_count: number;
  free: number;
  slots: Array<{ index: number; name: string | null; vendor: string; vram_bytes: number | null; busy_job_id: string | null }>;
  prefs: { mode?: string; preferred_gpu?: number | null };
}

export interface AdapterProfile {
  adapter_id: string;
  display_name: string;
  adapter_kind: string;
  tasks: string[];
  supports: Record<string, boolean>;
  limits: Record<string, unknown>;
  requires_cloud: boolean;
  cost_model: string;
  lane?: string;
  needs_gpu?: boolean;
  notes: string;
}

export interface WsEvent {
  event: string;
  seq: number;
  project_id: string | null;
  resource_id: string | null;
  ts: number;
  payload: Record<string, unknown>;
}

// ---------- cloud spend reporting (C2.0d; read-only views) ----------

export interface SpendScopeRow {
  scope: "session" | "day" | "month" | "lifetime";
  limit_micro: number | null;
  consumed_micro: number;
  open_micro: number;    // reserved exposure (open holds)
  charged_micro: number; // settled/recorded charges
}

export interface SpendBudgetRow {
  id: string;
  target: "project" | "global";
  scope: string;
  limit_micro: number;
  label: string;
}

export interface SpendSummary {
  environment: "live" | "simulated";
  session_id: string;
  scopes: SpendScopeRow[];
  budgets: SpendBudgetRow[];
}

export interface SpendReservation {
  id: string;
  job_id: string;
  command_id: string | null;
  project_id: string;
  provider_key: string;
  environment: "live" | "simulated";
  status: "open" | "settled" | "released";
  reserved_micro: number;
  remote_job_id: string | null;
  reconcile_count: number;
  reconcile_note: string | null;
  created_at: string | null;
}
