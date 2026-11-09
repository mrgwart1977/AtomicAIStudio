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
}

export interface Job {
  id: string;
  project_id: string;
  adapter_id: string;
  kind: string;
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  progress: number;
  message: string;
  error: string | null;
  resumable: boolean;
  output_asset_id: string | null;
  command_id: string | null;
  result_json?: Record<string, unknown> | null;
  progress_json?: JobProgressDetail | null;
  cancel_requested?: boolean;
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
    name: string | null;
    vendor: string;
    vram_bytes: number | null;
    vram_capped_at_4gb: boolean;
    driver_version: string | null;
    inference_capable: boolean | null;
  }>;
  disks: Array<{ mount: string; fstype: string; total_bytes: number; free_bytes: number }>;
  runtimes: {
    python: string;
    ffmpeg: { available: boolean; version: string | null; path: string | null };
    torch_installed: boolean;
    cuda_detected: boolean | null;
  };
  hardware_profile: string | null;
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
