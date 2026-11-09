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
  revision: number;
  settings_json: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface CommandProposal {
  command: Record<string, unknown>;
  summary: string;
  affected: Array<Record<string, unknown>>;
  warnings: string[];
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
  created_at: string;
  completed_at: string | null;
}

export interface Asset {
  id: string;
  project_id: string;
  kind: string;
  origin: string;
  uri: string;
  sha256: string;
  created_at: string;
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
