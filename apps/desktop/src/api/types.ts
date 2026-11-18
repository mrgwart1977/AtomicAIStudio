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
    /** Catalog-declared generation parameters (C3-B image UI). */
    sizes?: string[];
    qualities?: string[];
    /** True when the implemented contract actually transmits this
     * model's negative_prompt field (C3-C). */
    negative_prompt?: boolean;
    seed?: boolean;
    /** C3-D1 — catalog-declared editing capabilities. A declaration is
     * intent metadata only: img2img additionally requires the provider
     * to have an implemented edits contract (image-contracts
     * img2img_implemented list) before the mode can run. */
    editing?: {
      img2img?: boolean;
      max_reference_images?: number;
      mask_inpaint?: boolean;
      outpaint?: boolean;
      reference_weights?: boolean;
      identity_reference?: boolean;
      note?: string;
    };
    cost_table?: {
      unit?: string;                       // per_image | per_mpixel | per_mtok | per_credit
      rate?: number;
      rates?: Record<string, number>;      // quality-tiered per-image rates
      credits_per_image?: number;
      usd_per_credit?: number;
      assumed_output_tokens?: number;
      input_per_mtok?: number;
      output_per_mtok?: number;
      currency?: string;
      checked?: string;
      source?: string;
      note?: string;
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

/** GET /api/providers/image-contracts — catalog image providers with an
 * implemented generation contract. Everything else is shown disabled;
 * all contracts run on simulated transports in this build. */
export interface ImageContracts {
  implemented: string[];
  /** C3-D2 — strict subset of `implemented`: providers whose contract
   * implements an image-edits (img2img) endpoint. */
  img2img_implemented: string[];
  img2img_adapter_id: string;
  /** C3-D3 — implemented inpaint/outpaint contract subsets. */
  inpaint_implemented?: string[];
  outpaint_implemented?: string[];
  edit_adapter_id?: string;
  adapter_id: string;
  simulated: boolean;
  note: string;
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
  /** Authoritative estimate for generation commands — the figure the
   * approval surface MUST display. Null for commands without a catalog
   * estimate; client-side hints are preliminary only. */
  cost_estimate?: {
    est_cost_usd: number | null;
    unit: string | null;
    currency: string;
    is_estimate: boolean;
    simulated: boolean;
    settlement: string;
    rates_checked?: string | null;
  } | null;
  current_revision: number;
  launches_generation: boolean;
  /** C4-A — the actual ordered members of a previewed
   * reference_set_id; the approval commits this as
   * expected_references so a changed set can never slip through. */
  resolved_references?: Array<{ asset_id: string; weight: number | null; sha256: string }> | null;
  /** C4-A — {id, seed, revision} of the previewed set; committed back
   * as expected_set so pinned-seed/metadata edits invalidate too. */
  resolved_set?: { id: string; seed: number | null; revision: number } | null;
  /** C4-B — shot.generate only: the SHOT's generation-relevant state
   * (prompt/source/cast/continuity/revision). Commit pins this as
   * expected_shot — a shot edit after preview refuses the commit. */
  resolved_shot?: Record<string, unknown> | null;
  /** C4-D — shot.video only: resolved generation parameters
   * (model/dims/duration/sampling/source) pinned via expected_params. */
  resolved_params?: Record<string, unknown> | null;
  /** C5-A — timeline ops only: {id, revision, clip_count} pinned via
   * expected_timeline at commit. */
  resolved_timeline?: {
    id: string; revision: number; clip_count: number;
    edl_sha256?: string;   // C5-B - timeline.export pins the EDL bytes
  } | null;

  /** C5-C -- shot.generate/shot.video: the composed prompt record
   * (sections, omissions, conflicts, budget). The approval pins
   * prompt_sha256 as expected_prompt; context edits refuse the commit. */
  resolved_prompt?: {
    prompt_sha256: string;
    prompt: string;
    user_prompt: string | null;
    context_text: string;
    sections: Array<Record<string, unknown>>;
    omitted: string[];
    conflicts: string[];
    warnings: string[];
    budget: { limit: number; used: number; user: number };
    user_over_budget: boolean;
  } | null;
  /** A3 — keyframe.* commands: the keyframe's generation-relevant
   * state (id/revision/prompt/settings/continuity) the approval pins
   * as expected_keyframe at commit. */
  resolved_keyframe?: Record<string, unknown> | null;
  /** A4 — clip.* commands: the motion clip's generation-relevant
   * state the approval pins as expected_clip at commit. */
  resolved_clip?: Record<string, unknown> | null;
  /** A5 — dialogue.* commands: the segment's generation-relevant
   * state (id/revision/text/voice/timing) the approval pins as
   * expected_dialogue at commit. */
  resolved_dialogue?: Record<string, unknown> | null;
  /** A5 — dialogue.generate only: the deterministic mock-audio plan
   * (text, voice resolution, estimated duration, sample rate) shown
   * before anything is enqueued. */
  resolved_audio?: {
    simulated: boolean;
    text: string;
    direction?: string;
    voice?: Record<string, unknown> | null;
    sample_rate?: number;
    duration_s?: number;
    language?: string;
  } | null;
  /** A3 — keyframe.compose only: the proposed continuity-aware draft
   * (prompt + source sections) shown before anything is written. */
  resolved_draft?: {
    prompt: string;
    prompt_sha256: string;
    user_prompt: string;
    context_text: string;
    sections: Array<Record<string, unknown>>;
    omitted: string[];
    conflicts: string[];
    warnings: string[];
    budget: { limit: number; used: number; user: number };
  } | null;
  /** A6 — assembly.plan / assembly.apply previews: the deterministic
   * first-cut proposal (lanes + items) or the apply decision walk
   * (per-item insert/keep/skip/replace + stale flag) — discriminated
   * by `"lanes" in` vs `"sources_stale" in`. */
  resolved_assembly?: AssemblyProposal | AssemblyApplyPreview | null;
  /** A7 — cloud.plan previews: capability + credential presence +
   * cost + budget headroom + availability, with the fingerprint a
   * submit pins as plan_fingerprint at commit. */
  resolved_cloud?: CloudPlan | null;
  /** A7 — sim.gpu.work previews: what the scheduler would do with
   * this job right now (admitted device or queue reason). */
  resolved_gpu?: {
    would_schedule: boolean;
    device: number | "cpu" | null;
    queue_reason: string | null;
    slots: GpuSchedulerState["slots"];
    lost_devices: number[];
  } | null;
}

/** A7 — provider-neutral cloud plan (preview + approval record). */
export interface CloudPlan {
  ok: boolean;
  error?: string;
  detail?: string;
  provider_key?: string;
  provider_label?: string;
  access_class?: string;
  task?: string;
  model_id?: string | null;
  task_supported?: boolean;
  status?: string;
  status_label?: string;
  enabled?: boolean;
  credential_required?: boolean;
  credential?: { configured: boolean; fields: string[]; unreadable: boolean } | null;
  environment?: string;             // 'simulated' | 'live' | 'unrouted'
  route_command?: string | null;
  routed?: boolean;
  cost_model?: string;
  est_cost_usd?: number | null;
  max_cost_usd?: number | null;
  reserved_micro?: number;
  budgets?: Array<{ scope: string; limit_micro: number | null; consumed_micro: number; would_exceed: boolean }>;
  over_budget?: string[];
  availability?: string;            // available | disabled | blocked | incompatible | unrouted | over_budget
  unavailable_reason?: string | null;
  would_admit?: boolean;
  simulated?: boolean;
  plan_fingerprint?: string;
  note?: string;
}

/** A6 — one row of a first-cut proposal (assembly.plan preview). */
export interface AssemblyProposalItem {
  key: string;
  kind: string;                       // 'shot' | 'dialogue' | 'audio'
  title?: string;
  scene_id: string | null;
  shot_id: string | null;
  dialogue_id?: string | null;
  motion_clip_id?: string | null;
  asset_id: string | null;
  lane: string;                       // 'video' | 'dialogue' | 'music'
  lane_key?: string;                  // proposal lane key (dialogue2…)
  track_id: string | null;
  start_frames: number;
  extent_frames: number;
  review_status?: string;
  notes?: string;
  flags: {
    missing?: string | null;
    warnings?: string[];
    conflicts?: Array<{ clip_id: string; reason: string }>;
    removed?: string;
  };
  applied?: { clip_id: string | null; state: string } | null;
}

/** A6 — compute_plan output: the full deterministic proposal. */
export interface AssemblyProposal {
  timeline_id: string;
  lanes: Record<string, {
    track_id: string | null; track_name: string | null; kind: string;
    create: { kind: string; name: string } | null;
  }>;
  items: AssemblyProposalItem[];
  removed: Array<Record<string, unknown>>;
  warnings: string[];
  program_frames: number;
  counts: { items: number; missing: number; conflicts: number;
            removed: number };
}

/** A6 — preview_apply output: the apply decision walk. */
export interface AssemblyApplyPreview {
  plan_id: string;
  timeline_id: string;
  sources_stale: boolean;
  resolve_conflicts: string;
  replace_keys: string[];
  items: Array<{ key: string; kind: string; title: string;
                 action: string; reason: string }>;
  counts: { insert: number; keep: number; skip: number; replace: number };
}

/** A6 — a persisted assembly item (serializer output). */
export interface AssemblyItem {
  id: string;
  key: string;
  kind: string;
  position: number;
  scene_id: string | null;
  shot_id: string | null;
  dialogue_id: string | null;
  motion_clip_id: string | null;
  asset_id: string | null;
  track_id: string | null;
  timeline_clip_id: string | null;
  on_timeline: boolean;
  start_frames: number;
  extent_frames: number;
  review_status: string;    // pending | approved | rejected | skipped
  notes: string;
  flags: {
    missing?: string | null;
    conflicts?: Array<Record<string, unknown>>;
    removed?: string;
    warnings?: string[];
  };
  stale: boolean;           // source signature drifted since plan
  source_gone: boolean;
}

/** A6 — the persisted plan row (GET …/assemblies/{id}). */
export interface AssemblyPlan {
  id: string;
  project_id: string;
  timeline_id: string;
  name: string;
  status: string;           // draft | applied
  revision: number;
  created_at: string;
  updated_at: string;
  sources_stale?: boolean;
  proposal?: AssemblyProposal;
  items?: AssemblyItem[];
  counts?: { items: number; missing: number; conflicts: number;
             stale: number };
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

// ---------- A3: keyframes ----------

export type KeyframeRole = "start" | "end" | "reference" | "intermediate";

export interface Keyframe {
  id: string;
  shot_id: string;
  scene_id: string | null;
  role: KeyframeRole;
  position: number;
  frame_num: number | null;
  frame_den: number | null;
  prompt: string;
  negative_prompt: string;
  settings_json: {
    seed?: number | null;
    model_key?: string | null;
    steps?: number | null;
    cfg?: number | null;
    width?: number | null;
    height?: number | null;
    sampler?: string | null;
  };
  continuity_json: { refs?: Array<{ kind: string; id: string; label?: string }>; notes?: string };
  result_asset_id: string | null;
  result_asset_name?: string | null;
  result_asset_status?: string | null;
  generate_job_id: string | null;
  job_status?: string | null;
  job_progress?: number | null;
  prompt_origin: "user" | "compose";
  status: string;
  revision: number;
}

// ---------- A4: motion clips ----------

export interface MotionClip {
  id: string;
  shot_id: string;
  scene_id: string | null;
  position: number;
  start_keyframe_id: string | null;
  end_keyframe_id: string | null;
  prompt: string;
  negative_prompt: string;
  motion_json: {
    instructions?: string | null;
    intensity?: "subtle" | "moderate" | "strong" | null;
    temporal_consistency?: string | null;
  };
  camera_json: {
    movement?: string | null;
    follow_mode?: string | null;
    follow_targets?: Array<{ kind: string; id?: string; name?: string }> | null;
    follow_params?: Record<string, unknown> | null;
    framing?: string | null;
    subject_placement?: string | null;
    angle?: string | null;
  };
  duration_frames: number | null;
  fps_num: number;
  fps_den: number;
  width: number;
  height: number;
  model_key: string | null;
  seed: number | null;
  continuity_json: { refs?: Array<{ kind: string; id: string; label?: string }> };
  result_asset_id: string | null;
  result_asset_name?: string | null;
  result_asset_status?: string | null;
  generate_job_id: string | null;
  job_status?: string | null;
  job_progress?: number | null;
  prompt_origin: "user" | "compose";
  status: string;
  revision: number;
}

// ---------- A5: dialogue segments ----------

export interface DialogueSegment {
  id: string;
  sp_scene_id: string | null;
  element_id: string | null;
  scene_id: string | null;
  shot_id: string | null;
  character_id: string | null;
  position: number;
  text: string;
  /** Delivery direction (parenthetical) — 'whispered', 'urgent', … */
  direction: string;
  /** Voice overrides layered on the speaker's character profile —
   * {profile_override, style, delivery, emotion}; never a second
   * identity system. */
  voice_json: Record<string, unknown>;
  language: string;
  pronunciation_json: { hints?: Array<{ word: string; say_as: string }> };
  /** Planned rational timing — {start_frames, fps_num, fps_den,
   * timeline_id, duration_s}; the TimelineClip row is the actual
   * placement record. */
  timing_json: Record<string, unknown>;
  seed: number | null;
  model_key: string | null;
  settings_json: Record<string, unknown>;
  result_asset_id: string | null;
  result_asset_name?: string | null;
  result_asset_status?: string | null;
  generate_job_id: string | null;
  job_status?: string | null;
  job_progress?: number | null;
  status: string;
  revision: number;
  // Serializer-resolved fields (not columns):
  speaker_name?: string | null;
  /** Effective voice = character profile + segment overrides;
   * {missing: true} when the speaker has no usable voice. */
  resolved_voice?: Record<string, unknown> | null;
  /** F2 — true when the speaker/voice/element/scene the result was
   * generated against has since drifted; the audio is flagged, never
   * silently replaced. */
  result_stale?: boolean;
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
  /** C4-B — generation bindings (character ref set + img2img source) */
  reference_set_id: string | null;
  source_asset_id: string | null;
  continuity_json: Record<string, unknown>;
  /** A2 — shot planning document (camera/framing/notes); never touched
   * by screenplay sync. Shape validated by the backend. */
  planning_json?: ShotPlanning;
  revision: number;
  created_at: string;
  updated_at: string;
}

/** A2 — validated shot-planning document (planning_json). */
export interface ShotPlanning {
  camera?: {
    angle?: string;
    movement?: string;
    follow_mode?: string;
    follow_params?: Record<string, unknown>;
    follow_targets?: ({ kind: "character"; id: string }
      | { kind: "label"; name: string })[];
  };
  framing?: string;
  subject_placement?: string;
  fps?: { num: number; den: number; frames?: number };
  continuity_refs?: { kind: string; id: string }[];
  lighting?: string;
  action_notes?: string;
  notes?: string;
}

/** A2 — one shot-scoped CAS association on the storyboard card. */
export interface ShotMediaLink {
  link_id: string;
  asset_id: string;
  role: string;
  label: string;
  position: number;
  asset_name: string;
  asset_status: string;
  missing: boolean;
}

export interface StoryboardShot {
  shot: Shot;
  location_name: string;
  cast: CastMember[];
  /** Scene-cast members without an own row in this shot — inherited state. */
  inherited: CastMember[];
  /** A2 — CAS links + missing-media flags (values are link_ids, or the
   * tags "thumbnail"/"source" for direct bindings). */
  media?: { assets: ShotMediaLink[]; missing: string[] };
  /** A3 — compact keyframe summary for card badges. */
  keyframes?: { count: number; roles: string[]; with_result: number };
  /** A4 — compact motion-clip summary for card badges. */
  clips?: { count: number; with_result: number };
  /** A5 — compact dialogue-segment summary for card badges. */
  dialogue?: { count: number; with_result: number };
  /** C4-C — durable per-shot generation state (backend-authoritative). */
  generation?: {
    output_count: number;
    latest_asset_id: string | null;
    latest_asset_at: string | null;
    latest_job_id: string | null;
    latest_job_status: string | null;
    latest_job_kind: string | null;
    latest_job_at: string | null;
    video_count: number;
    latest_video_id: string | null;
    latest_video_duration_s: number | null;
  } | null;
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
  weight: number | null;
  set_id: string | null;
  created_at: string;
}

/** C4-A — named character reference set. Membership lives on the
 * entity-asset links (set_id); the set row carries the group metadata
 * plus the optional pinned-seed guidance. */
export interface ReferenceSet {
  id: string;
  project_id: string;
  character_id: string;
  name: string;
  seed: number | null;
  notes: string;
  position: number;
  status: string;
  revision: number;
  member_count: number;
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


// ---------- C5-E: consolidated compatibility ----------

export type AvailabilityClass =
  | "installed_operational" | "installed_limited"
  | "installable" | "planned" | "blocked";

/** S-C5 — machine-readable cause behind an availability class; null when
 * nothing adverse applies. Keeps "blocked" honest instead of lumping
 * license/download/runtime gaps under a hardware failure. */
export type AvailabilityReason =
  | "missing_runtime" | "missing_model_files"
  | "license_restricted" | "download_unavailable"
  | "hardware_incompatible" | "hardware_limited"
  | "compatibility_unknown" | "planned";

export interface RuntimeFlavorState {
  flavor: string;
  backend_available: boolean;
  installed: boolean;
  operational: boolean;
  error: string | null;
  benchmark: Record<string, unknown> | null;
}

export interface CompatRuntime {
  key: string;
  display_name: string;
  version: string;
  license: { name?: string };
  backends: string[];
  est_bytes: number;
  notes: string;
  flavors: RuntimeFlavorState[];
  any_operational: boolean;
  planned: boolean;
}

export interface BenchmarkResult {
  model_key: string;
  model_version: string;
  runtime: { key: string; flavor: string; selection_reason?: string };
  gpu: string | null;
  gpu_index: number | null;
  runs: { run: number; condition: "cold" | "warm"; duration_ms: number; peak_worker_rss_bytes: number | null }[];
  model_file_sha256: string | null;
  driver_version: string | null;
  measurement_limitations: string[];
  median_duration_ms: number;
  peak_worker_rss_bytes: number | null;
  peak_memory_note: string;
  params: Record<string, unknown>;
  estimate_vs_measured: { estimated_vram_bytes: number | null; warning: string };
  measured_at: string;
}

export interface CompatModelEntry extends ModelCatalogEntry {
  availability: AvailabilityClass;
  availability_reason: AvailabilityReason | null;
  runtime: { key: string; known: boolean; installed_flavors: string[];
             flavors: { flavor: string; installed: boolean; operational: boolean; backend_available: boolean }[];
             any_operational: boolean; planned: boolean };
  estimates: { vram_bytes: number | null; ram_bytes: number | null; basis: string };
  resolutions: unknown;
  fps: unknown;
  durations_s: unknown;
  benchmark: BenchmarkResult | null;
  benchmarkable: boolean;
  limitations: string[];
}

export interface CompatibilityReport {
  hardware_profile: { key: string; label: string; detail: string; gpu_count: number; max_vram_bytes: number | null };
  runtimes: CompatRuntime[];
  models: CompatModelEntry[];
}

export interface ModelPreflight {
  model_key: string;
  display_name: string;
  version: string;
  support: string;
  download_bytes: number;
  models_root: string;
  disk_free_bytes: number | null;
  disk_sufficient: boolean;
  est_vram_bytes: number | null;
  est_ram_bytes: number | null;
  estimate_basis: string;
  runtime: { key: string; display_name: string; known: boolean; planned: boolean;
             flavors: { flavor: string; installed: boolean; operational: boolean }[] };
  license: { name?: string; url?: string; commercial_ok: boolean; notes?: string };
  compatibility: { status: string; reasons: string[]; warnings: string[]; suggested: Record<string, unknown> };
  limitations: string[];
  requires_explicit_approval: boolean;
  auto_download: boolean;
  installable: boolean;
  availability_reason: AvailabilityReason | null;
}

export interface ValidationCheck {
  name: string;
  ok: boolean | null;
  detail: string;
  severity?: string;
}

export interface ModelValidation {
  model_key: string;
  read_only: boolean;
  ran_inference: boolean;
  verify_hashes: boolean;
  checks: ValidationCheck[];
  summary: { blocking: number; warning: number; info: number };
  ok: boolean;
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
  /** A7 — indexes of devices that vanished while holding
   * reservations; surfaced, never silently freed. */
  lost_devices?: number[];
  /** A7-F5 — true when the inventory came from ATOMIC_HW_JSON:
   * simulated devices are never presented as measured hardware. */
  simulated?: boolean;
  slots: Array<{
    index: number; name: string | null; vendor: string;
    vram_bytes: number | null;
    vram_free_bytes?: number | null;
    /** A7 — usable VRAM after headroom; the admission bound. */
    usable_bytes?: number;
    /** A7 — bytes reserved by running jobs on this device. */
    reserved_bytes?: number;
    free_after_reserve_bytes?: number;
    /** A7 — job_id → reserved bytes held on this device. */
    jobs?: Record<string, number>;
    /** false when the device vanished from the probe while holding
     * reservations — reported, never scheduled onto. */
    present?: boolean;
    busy_job_id: string | null
  }>;
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

// ---------- timelines (C5-A) ----------

/** N1-A — a lane on a timeline. `position` is the stack order
 * (0 = top). */
export interface TimelineTrack {
  id: string;
  project_id: string;
  timeline_id: string;
  kind: string;           // "video" | "audio" | "overlay"
  name: string;
  position: number;
  visible: boolean;
  locked: boolean;
  muted: boolean;
  /** N2-C — lane gain in decibels (0 = unity, −60…+12). */
  gain_db: number;
  /** N2-C — when ≥1 lane is soloed, non-soloed lanes are silent. */
  solo: boolean;
  /** N3-A — JSON keyframe list [{id, at_tl_frames, gain_db}] keyed in
   * ABSOLUTE timeline frames (clip-local clips use `at_frames`). */
  automation_json: string;
  /** N3-A — lane keyframe interpolation: "linear" (dB ramp) | "step". */
  automation_interp: string;
  created_at: string;
}

export interface TimelineClip {
  id: string;
  project_id: string;
  timeline_id: string;
  /** N1-A — owning lane (null only for legacy/orphaned rows). */
  track_id: string | null;
  /** N1-A — integer timeline-frame start on its lane. */
  start_frames: number;
  position: number;
  source_asset_id: string;
  source_version_id: string;
  source_sha256: string;
  in_frame: number;
  out_frame: number;
  src_frames: number;
  src_fps_num: number;
  src_fps_den: number;
  src_duration_s: number;
  /** N2-F — probed-at-insert: the pinned source carries an audio
   * stream. Video-lane clips with this set contribute embedded audio
   * to preview/export. */
  src_has_audio: boolean;
  /** N2-C — per-clip gain in decibels (0 = unity). Audio clips only. */
  gain_db: number;
  /** N2-D — fade length in SOURCE units (samples for audio). */
  fade_in_units: number;
  fade_in_curve: string;   // "linear" | "equal_power"
  fade_out_units: number;
  fade_out_curve: string;
  /** N2-D — JSON keyframe list [{id, at_frames, gain_db}]. */
  automation_json: string;
  /** N2-F — keyframe interpolation: "linear" (dB ramp) | "step". */
  automation_interp: string;
  shot_id: string | null;
  scene_id: string | null;
  created_at: string;
}

export interface TimelineCrossfade {
  id: string;
  project_id: string;
  timeline_id: string;
  /** N2-D — outgoing (earlier) and incoming (later) clip pair. */
  out_clip_id: string;
  in_clip_id: string;
  tl_start: number;
  tl_frames: number;
  curve: string;
  /** Read-time feasibility — false when the window detached. */
  satisfied: boolean;
  created_at: string;
}

export interface Timeline {
  id: string;
  project_id: string;
  name: string;
  description: string;
  fps_num: number;
  fps_den: number;
  status: string;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface TimelineDetail extends Timeline {
  clips: TimelineClip[];
  tracks: TimelineTrack[];
  crossfades: TimelineCrossfade[];
  total_duration_s: number;
  undo_label: string | null;
  redo_label: string | null;
}

export interface TimelineRevision {
  revision: number;
  summary: string;
  created_at: string;
}
