/**
 * A8 — guided filmmaking wizard state.
 *
 * The wizard is a thin layer over real ADE commands: every step writes
 * ordinary project entities (settings, screenplays, scenes, shots,
 * cast, keyframes, motion clips, dialogue, assemblies, timelines,
 * exports) through preview → approve → commit. Progress itself
 * persists on the project (`settings_json.wizard`) via
 * `project.update_settings`, so it survives restart, stays
 * project-scoped, rides backups, and creates no parallel database.
 *
 * Stage *truth* is never stored: the backend `/wizard` endpoint
 * derives it from live rows on every load. Stored ids are
 * re-validated against live data — deleted rows are dropped with a
 * notice rather than trusted. `settings_json.first_film` (the C5-D
 * guided flow) seeds a v2 blob as a legacy upgrade — it is read, never
 * rewritten or deleted.
 */
export interface WizardState {
  version: 2;
  /** 0-based index into STAGES of the step the user is on. */
  step: number;
  /** Highest step index reached — drives the progress indicator. */
  furthest: number;
  /** Presentation depth — same rows underneath, never a data switch. */
  mode: "beginner" | "advanced";
  screenplay_id: string | null;
  timeline_id: string | null;
  plan_id: string | null;
  export_job_id: string | null;
  export_profile: string;
  done: boolean;
  updated_at: string;
}

export const STAGES = [
  { id: "project",        label: "Project",            required: true,
    blurb: "Your film lives in a project — this one is open now." },
  { id: "film_settings",  label: "Film settings",      required: true,
    blurb: "Pick the shape and speed of your film. Safe defaults are already chosen." },
  { id: "screenplay",     label: "Screenplay",         required: true,
    blurb: "Write or paste your story. Scenes and dialogue here become shots later." },
  { id: "scenes",         label: "Scenes & dialogue",  required: true,
    blurb: "Organize your screenplay scenes — headings, action, and who says what." },
  { id: "cast",           label: "Cast & continuity",  required: false,
    blurb: "Add characters and locations so shots know who and where they are." },
  { id: "shots",          label: "Storyboard shots",   required: true,
    blurb: "Turn the screenplay into a shot list — the camera plan for your film." },
  { id: "planning",       label: "Shot planning",      required: false,
    blurb: "Choose camera angles, movement, and lighting for each shot." },
  { id: "keyframes",      label: "Keyframes",          required: false,
    blurb: "Draft the start and end images your clips will move between." },
  { id: "motion",         label: "Motion clips",       required: false,
    blurb: "Turn keyframes into moving shots." },
  { id: "dialogue",       label: "Dialogue & audio",   required: false,
    blurb: "Prepare spoken lines and sound for the cut." },
  { id: "assembly_plan",  label: "First-cut plan",     required: false,
    blurb: "Preview how your scenes, shots, and sound line up into a first cut." },
  { id: "assembly_apply", label: "Review & apply",     required: false,
    blurb: "Check the plan, approve what looks right, then build the timeline." },
  { id: "edit",           label: "Edit film",          required: false,
    blurb: "Trim, arrange, and mix your assembled film." },
  { id: "export_config",  label: "Export settings",    required: true,
    blurb: "Pick the quality and format for the finished film." },
  { id: "export",         label: "Export & verify",    required: true,
    blurb: "Render the film file and check it." },
] as const;

export type StageId = (typeof STAGES)[number]["id"];
export const STAGE_INDEX: Record<StageId, number> = Object.fromEntries(
  STAGES.map((s, i) => [s.id, i]),
) as Record<StageId, number>;

export function initialState(): WizardState {
  return {
    version: 2, step: 0, furthest: 0, mode: "beginner",
    screenplay_id: null, timeline_id: null, plan_id: null,
    export_job_id: null, export_profile: "720p", done: false,
    updated_at: new Date().toISOString(),
  };
}

/** Read persisted wizard state off a project row. `legacy` is true
 * when only the C5-D `first_film` blob exists — the v2 blob is seeded
 * fresh (the old key is left untouched). */
export function loadState(
  settings_json: Record<string, unknown> | undefined | null,
): { state: WizardState; legacy: boolean } {
  const settings = (settings_json ?? {}) as Record<string, unknown>;
  const raw = settings["wizard"];
  if (raw && typeof raw === "object") {
    const s = raw as Partial<WizardState>;
    if (s.version === 2 && typeof s.step === "number") {
      return {
        state: { ...initialState(), ...s, version: 2 } as WizardState,
        legacy: false,
      };
    }
  }
  const seeded = initialState();
  // Map the old 11-step progress onto the nearest A8 stage so an
  // upgraded project resumes roughly where it left off — a notice
  // explains the jump rather than pretending nothing changed.
  const legacy = settings["first_film"];
  if (legacy && typeof legacy === "object") {
    const old = (legacy as { step?: number }).step ?? 0;
    const mapped = old <= 0 ? 0
      : old <= 1 ? STAGE_INDEX.cast
      : old <= 3 ? STAGE_INDEX.shots
      : old <= 6 ? STAGE_INDEX.motion
      : old <= 8 ? STAGE_INDEX.edit
      : STAGE_INDEX.export_config;
    seeded.step = Math.min(mapped, STAGES.length - 1);
    seeded.furthest = seeded.step;
    return { state: seeded, legacy: true };
  }
  return { state: seeded, legacy: false };
}

/** Drop stored ids that no longer exist — never trust stale refs. */
export function reconcileState(
  s: WizardState,
  known: {
    screenplayIds: Set<string>; timelineIds: Set<string>;
    planIds: Set<string>; jobIds: Set<string>;
  },
): { state: WizardState; notices: string[] } {
  const notices: string[] = [];
  const state = { ...s };
  if (state.screenplay_id && !known.screenplayIds.has(state.screenplay_id)) {
    notices.push("Your screenplay was removed — pick or create one again.");
    state.screenplay_id = null;
  }
  if (state.timeline_id && !known.timelineIds.has(state.timeline_id)) {
    notices.push("Your timeline was removed — re-apply the assembly or "
      + "create one again.");
    state.timeline_id = null;
  }
  if (state.plan_id && !known.planIds.has(state.plan_id)) {
    notices.push("Your first-cut plan was removed — plan again from "
      + "the current shots.");
    state.plan_id = null;
  }
  if (state.export_job_id && !known.jobIds.has(state.export_job_id)) {
    state.export_job_id = null; // job history is diagnostic only — no notice
  }
  return { state, notices };
}

// ---------- generation-state honesty ----------

export const GEN_STATE_LABEL: Record<string, string> = {
  planned: "Planned", draft: "Draft", simulated: "Simulated",
  generated: "Generated", missing: "Missing", failed: "Failed",
  cancelled: "Cancelled", stale: "Stale", approved: "Approved",
};

/** Badge class per state — simulated always visibly distinct. */
export const GEN_STATE_CLASS: Record<string, string> = {
  planned: "border-info/40 bg-info/10 text-info",
  draft: "border-line bg-panel text-muted",
  simulated: "border-amber/50 bg-amber/10 text-amber",
  generated: "border-teal/50 bg-teal/10 text-teal",
  missing: "border-danger/50 bg-danger/10 text-danger",
  failed: "border-danger/50 bg-danger/10 text-danger",
  cancelled: "border-line bg-panel text-muted",
  stale: "border-amber/50 bg-amber/10 text-amber",
  approved: "border-teal/50 bg-teal/10 text-teal",
};

// ---------- beginner-facing error translation ----------

export interface FriendlyError {
  title: string;
  detail: string; // original technical message — kept, expandable
}

const ERROR_RULES: [RegExp, string][] = [
  [/not installed|install.*first|no installed/i, "That AI model isn't downloaded yet — pick another method or import your own clips."],
  [/vram|video memory|cuda.*memory/i, "Your graphics card doesn't have enough memory for that model — try a smaller model or import clips instead."],
  [/ram|memory/i, "This computer doesn't have enough free memory right now — close other apps and try again."],
  [/disk|space|storage/i, "There's not enough free disk space — free up space and try again."],
  [/not a valid|invalid|probe.*fail|media_json|no measured duration/i, "That file couldn't be read as media — try a different MP4 or MOV file."],
  [/changed since preview|stale|prompt context changed|revision conflict|re-preview|plan_stale/i, "Something changed since you looked at this — re-check the preview and approve again."],
  [/cancelled|canceled/i, "The job was cancelled — nothing was produced. You can run it again."],
  [/archived/i, "That item is archived — restore it or pick another."],
  [/not found in this project|not found/i, "That item doesn't exist anymore — it may have been deleted."],
  [/timed? ?out|timeout/i, "That took too long and was stopped — try again."],
  [/budget|spend|over_budget/i, "That would go over the spending limit — check the budget settings."],
  [/needs_key|credential/i, "That provider needs an account key first — set it up on the Providers page."],
];

export function friendlyError(err: unknown): FriendlyError {
  const msg = err instanceof Error ? err.message : String(err);
  for (const [re, title] of ERROR_RULES) {
    if (re.test(msg)) return { title, detail: msg };
  }
  if (/fail|error/i.test(msg)) return { title: "Something went wrong — see the details below.", detail: msg };
  return { title: msg || "Unknown error", detail: msg };
}

// ---------- screenplay paste-import (A8.2 step 3) ----------

export interface ParsedScene {
  heading: string;
  elements: { kind: "action" | "dialogue" | "note"; character: string | null; text: string }[];
}

/**
 * The one supported text import format — deliberately simple and
 * documented, no fountain/FDX claims:
 *   Scene headings: lines starting INT., EXT., INT./EXT., or '#'.
 *   Dialogue:       'NAME: their line' or 'NAME (aside): their line'.
 *   Notes:          lines in [brackets].
 *   Everything else inside a scene is an action beat.
 * Unknown/malformed lines land as action text — nothing is dropped.
 */
export function parseScriptText(text: string): ParsedScene[] {
  const scenes: ParsedScene[] = [];
  let cur: ParsedScene | null = null;
  const pushScene = () => { if (cur) scenes.push(cur); };
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const isHeading = /^(#|INT\.|EXT\.|INT\.\/EXT\.|I\/E)/i.test(line);
    if (isHeading) {
      pushScene();
      cur = { heading: line.replace(/^#+\s*/, "").toUpperCase(), elements: [] };
      continue;
    }
    if (!cur) {
      cur = { heading: "", elements: [] }; // unheaded content → scene 0
    }
    const bracket = line.match(/^\[(.+)\]$/);
    if (bracket) {
      cur.elements.push({ kind: "note", character: null, text: bracket[1] });
      continue;
    }
    const dlg = line.match(/^([A-Z][A-Za-z' .]{1,40}?)\s*(?:\(([^)]*)\))?\s*:\s+(.+)$/);
    if (dlg) {
      const t = dlg[2] ? `${dlg[3]} (${dlg[2].trim()})` : dlg[3];
      cur.elements.push({ kind: "dialogue", character: dlg[1].trim(), text: t.trim() });
      continue;
    }
    cur.elements.push({ kind: "action", character: null, text: line });
  }
  pushScene();
  return scenes;
}
