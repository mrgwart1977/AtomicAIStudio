/**
 * C5-D — guided First Film workflow state.
 *
 * The wizard is a thin layer over real ADE commands: every step writes
 * ordinary project entities (project settings, characters, scenes,
 * shots, assets, timelines, exports) through preview → approve →
 * commit. Progress itself persists on the project (`settings_json.
 * first_film`) via `project.update_settings`, so it survives restart,
 * is project-scoped like everything else, and creates no parallel
 * database. On resume, stored entity ids are re-validated against the
 * live project — deleted or missing rows are dropped with a notice
 * rather than trusted.
 */
import type { ModelCatalogEntry, ProviderDetail, ProviderSummary } from "../../api/types";

export interface FirstFilmState {
  version: 1;
  /** 0-based index into STEPS of the step the user is on. */
  step: number;
  /** Highest step index reached — drives the progress indicator. */
  furthest: number;
  film: { name: string; aspect_ratio: string; fps_num: number; fps_den: number };
  character_ids: string[];
  scene_id: string | null;
  shot_ids: string[];
  method: MethodId | null;
  /** provider_key:model_id or model_key of the chosen generation path. */
  method_detail: string | null;
  /** shot_id → asset_id for imported or generated shot media. */
  shot_assets: Record<string, string>;
  timeline_id: string | null;
  profile: string;
  export_job_id: string | null;
  done: boolean;
  updated_at: string;
}

export type MethodId = "import" | "image" | "video";

export const STEPS = [
  { id: "setup", label: "Film settings", required: true },
  { id: "characters", label: "Characters", required: false },
  { id: "scene", label: "Scene", required: true },
  { id: "shots", label: "Shots", required: true },
  { id: "method", label: "How you'll fill shots", required: true },
  { id: "generate", label: "Preview & generate", required: false },
  { id: "media", label: "Shot media", required: true },
  { id: "timeline", label: "Build timeline", required: true },
  { id: "arrange", label: "Arrange & trim", required: false },
  { id: "profile", label: "Export settings", required: true },
  { id: "export", label: "Export", required: true },
] as const;

export type StepId = (typeof STEPS)[number]["id"];
export const STEP_INDEX: Record<StepId, number> = Object.fromEntries(
  STEPS.map((s, i) => [s.id, i]),
) as Record<StepId, number>;

export function initialState(projectName = "My First Film"): FirstFilmState {
  return {
    version: 1,
    step: 0,
    furthest: 0,
    film: { name: projectName, aspect_ratio: "16:9", fps_num: 24, fps_den: 1 },
    character_ids: [],
    scene_id: null,
    shot_ids: [],
    method: null,
    method_detail: null,
    shot_assets: {},
    timeline_id: null,
    profile: "720p",
    export_job_id: null,
    done: false,
    updated_at: new Date().toISOString(),
  };
}

/** Read persisted wizard state off a project row; null when absent or malformed. */
export function loadState(settings_json: Record<string, unknown> | undefined | null): FirstFilmState | null {
  const raw = (settings_json ?? {})["first_film"];
  if (!raw || typeof raw !== "object") return null;
  const s = raw as Partial<FirstFilmState>;
  if (s.version !== 1 || typeof s.step !== "number") return null;
  return { ...initialState(), ...s, shot_assets: s.shot_assets ?? {} } as FirstFilmState;
}

/**
 * Drop stored entity ids that no longer exist in the live project.
 * Returns the sanitized state plus human-readable notices for anything
 * removed — deleted entities are detected, never trusted.
 */
export function reconcileState(
  s: FirstFilmState,
  known: { characterIds: Set<string>; sceneIds: Set<string>; shotIds: Set<string>; timelineIds: Set<string>; assetIds: Set<string> },
): { state: FirstFilmState; notices: string[] } {
  const notices: string[] = [];
  const state = { ...s, shot_assets: { ...s.shot_assets } };
  const chars = state.character_ids.filter((id) => known.characterIds.has(id));
  if (chars.length !== state.character_ids.length) {
    notices.push("A character you picked was deleted — removed from the list.");
    state.character_ids = chars;
  }
  if (state.scene_id && !known.sceneIds.has(state.scene_id)) {
    notices.push("Your scene was deleted — pick or create a scene again.");
    state.scene_id = null;
  }
  const shots = state.shot_ids.filter((id) => known.shotIds.has(id));
  if (shots.length !== state.shot_ids.length) {
    notices.push("A shot was deleted — removed from your film.");
    state.shot_ids = shots;
  }
  for (const [shotId, assetId] of Object.entries(state.shot_assets)) {
    if (!state.shot_ids.includes(shotId) || !known.assetIds.has(assetId)) {
      delete state.shot_assets[shotId];
      notices.push("Some shot media is missing — re-import or regenerate it.");
    }
  }
  if (state.timeline_id && !known.timelineIds.has(state.timeline_id)) {
    notices.push("Your timeline was deleted — rebuild it from your shot media.");
    state.timeline_id = null;
  }
  return { state, notices };
}

/** True when the step's required work exists in wizard state. */
export function stepReady(s: FirstFilmState, step: StepId): boolean {
  switch (step) {
    case "setup": return s.film.name.trim().length > 0;
    case "characters": return true; // optional
    case "scene": return s.scene_id !== null;
    case "shots": return s.shot_ids.length > 0;
    case "method": return s.method !== null;
    case "generate": return s.method === "import" || s.shot_ids.every((id) => s.shot_assets[id]);
    case "media": return s.shot_ids.length > 0 && s.shot_ids.every((id) => s.shot_assets[id]);
    case "timeline": return s.timeline_id !== null;
    case "arrange": return true; // optional polish
    case "profile": return true;   // a profile is always preselected
    case "export": return s.done;
  }
}

// ---------- generation-method capability detection ----------

export interface MethodOption {
  id: MethodId;
  detail: string;            // provider_key|model_id for image, model_key for video
  label: string;
  status: "ready" | "simulated" | "planned" | "unavailable" | "uninstalled";
  note: string;
}

/**
 * What can actually fill a shot with media right now, classified the
 * way the review requires: installed local models, simulated
 * generation, planned models, unavailable models, imported media.
 * Nothing unavailable is offered as operational.
 */
export function methodOptions(
  providers: ProviderSummary[],
  providerDetails: Record<string, ProviderDetail>,
  models: ModelCatalogEntry[],
): MethodOption[] {
  const out: MethodOption[] = [
    {
      id: "import", detail: "", label: "Import my own video clips",
      status: "ready",
      note: "Use MP4/MOV files already on this computer — no AI model needed.",
    },
  ];
  for (const p of providers) {
    const d = providerDetails[p.key];
    for (const m of d?.models ?? []) {
      if (m.capability !== "image") continue;
      out.push({
        id: "image",
        detail: `${p.key}|${m.id}`,
        label: `${p.label} — ${m.id}`,
        status: "simulated",
        // No live provider transport exists yet; cloud image commands
        // run through the simulated (mock) lifecycle.
        note: "Simulated generation — runs locally through the mock provider lifecycle; nothing is sent anywhere and nothing is billed.",
      });
    }
  }
  for (const m of models) {
    if (m.modality !== "video") continue;
    if (m.support === "planned" || m.compatibility.status === "planned") {
      out.push({
        id: "video", detail: m.key, label: `${m.display_name} (video)`,
        status: "planned",
        note: "Planned model — not operational yet; shown for visibility only.",
      });
      continue;
    }
    if (m.install?.status === "installed") {
      out.push({
        id: "video", detail: m.key, label: `${m.display_name} (video)`,
        status: "ready",
        note: "Installed on this computer — generates real video locally.",
      });
    } else {
      out.push({
        id: "video", detail: m.key, label: `${m.display_name} (video)`,
        status: "uninstalled",
        note: `Not installed — needs a model download first (${Math.round((m.download_bytes ?? 0) / 1e9)} GB). No automatic downloads happen from here.`,
      });
    }
  }
  return out;
}

// ---------- beginner-facing error translation ----------

export interface FriendlyError {
  title: string;
  detail: string; // the original technical message — kept, expandable
}

const ERROR_RULES: [RegExp, string][] = [
  [/not installed|install.*first|no installed/i, "That AI model isn't downloaded yet — pick another method or import your own clips."],
  [/vram|video memory|cuda.*memory/i, "Your graphics card doesn't have enough memory for that model — try a smaller model or import clips instead."],
  [/ram|memory/i, "This computer doesn't have enough free memory right now — close other apps and try again."],
  [/disk|space|storage/i, "There's not enough free disk space — free up space and try again."],
  [/not a valid|invalid|probe.*fail|media_json|no measured duration/i, "That file couldn't be read as media — try a different MP4 or MOV file."],
  [/changed since preview|stale|prompt context changed|revision conflict|re-preview/i, "Something changed since you looked at this — re-check the preview and approve again."],
  [/cancelled|canceled/i, "The job was cancelled — nothing was produced. You can run it again."],
  [/archived/i, "That item is archived — restore it or pick another."],
  [/not found in this project|not found/i, "That item doesn't exist anymore — it may have been deleted."],
  [/timed? ?out|timeout/i, "That took too long and was stopped — try again."],
];

export function friendlyError(err: unknown): FriendlyError {
  const msg = err instanceof Error ? err.message : String(err);
  for (const [re, title] of ERROR_RULES) {
    if (re.test(msg)) return { title, detail: msg };
  }
  if (/fail|error/i.test(msg)) return { title: "Something went wrong — see the details below.", detail: msg };
  return { title: msg || "Unknown error", detail: msg };
}
