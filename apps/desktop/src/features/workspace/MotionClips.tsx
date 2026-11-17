/**
 * A4 — motion clips for a shot: keyframe→video beats with user-owned
 * prompts + motion/camera settings, a continuity-aware draft proposal,
 * deterministic mock video generation through the durable job system,
 * and placement of the produced clip on the existing timeline.
 * All mutations are Unified Creative Commands — generation is preview
 * → approve → commit with the backend's pins, never a direct call.
 * The mock adapter is labeled explicitly — no inference is implied.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "../../api/client";
import { commitCommand, previewCommand } from "../../api/commands";
import { useApp } from "../../stores/appStore";
import type {
  Job, Keyframe, ModelCatalogEntry, MotionClip, Timeline,
} from "../../api/types";

const SEL = "w-full rounded border border-line bg-ink px-2 py-1 text-xs";
const INP = SEL;
const LBL = "text-[10px] uppercase tracking-wide text-faint";
const BTN = "rounded border border-line px-2 py-0.5 text-[10px] text-muted hover:text-teal disabled:opacity-40";

const MOVEMENTS = [
  "", "static", "pan", "tilt", "dolly_in", "dolly_out", "truck_left",
  "truck_right", "crane_up", "crane_down", "handheld", "zoom",
];
const FOLLOW_MODES = [
  "", "follow_behind", "follow_front", "follow_side", "follow_shoulder",
  "orbit_follow", "drone_follow", "vehicle_chase", "lead_and_follow",
  "group_follow", "target_switch",
];
const FRAMINGS = [
  "", "extreme_close_up", "close_up", "medium_close_up", "medium",
  "medium_wide", "wide", "extreme_wide",
];
const INTENSITIES = ["", "subtle", "moderate", "strong"];

const COMPAT_BADGE: Record<string, { cls: string; label: string }> = {
  compatible: { cls: "text-teal", label: "compatible" },
  limited: { cls: "text-amber", label: "limited" },
  restricted: { cls: "text-amber", label: "restricted" },
  unsupported: { cls: "text-danger", label: "unsupported" },
  unavailable: { cls: "text-faint", label: "unavailable" },
  planned: { cls: "text-faint", label: "planned" },
  unknown: { cls: "text-faint", label: "unknown" },
};

function jobFor(jobs: Record<string, Job>, jobId: string | null): Job | null {
  return jobId ? jobs[jobId] ?? null : null;
}

export function MotionClipsSection({ projectId, shotId, run, onChanged }: {
  projectId: string;
  shotId: string;
  run: (t: string, p: Record<string, unknown>) => Promise<boolean>;
  onChanged?: () => void;
}) {
  const [items, setItems] = useState<MotionClip[]>([]);
  const [kfs, setKfs] = useState<Keyframe[]>([]);
  const [models, setModels] = useState<ModelCatalogEntry[]>([]);
  const [timelines, setTimelines] = useState<Timeline[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [placeTl, setPlaceTl] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<{
    kind: "compose" | "generate"; clipId: string;
    prompt: string; warnings: string[];
    expected_clip?: Record<string, unknown>;
    expected_prompt?: string;
  } | null>(null);
  const jobs = useApp((s) => s.jobs);

  const load = useCallback(async () => {
    try {
      const rows = await apiFetch<MotionClip[]>(
        `/api/projects/${projectId}/shots/${shotId}/clips`);
      setItems(rows);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [projectId, shotId]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    void apiFetch<ModelCatalogEntry[]>("/api/models")
      .then(setModels).catch(() => setModels([]));
    void apiFetch<Keyframe[]>(
      `/api/projects/${projectId}/shots/${shotId}/keyframes`)
      .then(setKfs).catch(() => setKfs([]));
    void apiFetch<Timeline[]>(`/api/projects/${projectId}/timelines`)
      .then(setTimelines).catch(() => setTimelines([]));
  }, [projectId, shotId]);
  // When a clip job newly settles, reload once — the result asset
  // lands via the job's apply_command server-side.
  const settledRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    let newly = 0;
    for (const c of items) {
      const j = jobFor(jobs, c.generate_job_id);
      if (j && ["succeeded", "failed", "cancelled"].includes(j.status)
          && !settledRef.current.has(j.id)) {
        settledRef.current.add(j.id);
        newly += 1;
      }
    }
    if (newly > 0) void load();
  }, [jobs, items, load]);

  // Video models only — an image-only model can never produce motion.
  const videoModels = useMemo(
    () => models.filter((m) => m.modality === "video"
      || (m.compatibility?.modes ?? []).some(
        (x) => x.includes("video"))),
    [models]);

  const dget = (c: MotionClip, k: string, fallback: string) =>
    draft[c.id + ":" + k] ?? fallback;
  const dset = (c: MotionClip, k: string, v: string) =>
    setDraft((d) => ({ ...d, [c.id + ":" + k]: v }));
  const dirty = (c: MotionClip) =>
    dget(c, "prompt", c.prompt) !== c.prompt
    || dget(c, "negative", c.negative_prompt) !== c.negative_prompt
    || dget(c, "instructions", c.motion_json?.instructions ?? "")
      !== (c.motion_json?.instructions ?? "")
    || dget(c, "intensity", c.motion_json?.intensity ?? "")
      !== (c.motion_json?.intensity ?? "")
    || dget(c, "temporal", c.motion_json?.temporal_consistency ?? "")
      !== (c.motion_json?.temporal_consistency ?? "")
    || dget(c, "movement", c.camera_json?.movement ?? "")
      !== (c.camera_json?.movement ?? "")
    || dget(c, "follow", c.camera_json?.follow_mode ?? "")
      !== (c.camera_json?.follow_mode ?? "")
    || dget(c, "framing", c.camera_json?.framing ?? "")
      !== (c.camera_json?.framing ?? "")
    || dget(c, "dur", String(c.duration_frames ?? ""))
      !== String(c.duration_frames ?? "")
    || dget(c, "seed", String(c.seed ?? "")) !== String(c.seed ?? "");

  async function act(t: string, p: Record<string, unknown>) {
    setErr(null);
    if (!(await run(t, p))) {
      setErr("Motion clip change failed — see the board message.");
      return;
    }
    await load();
    onChanged?.();
  }

  async function add() {
    await act("clip.create", { shot_id: shotId, duration_frames: 48 });
  }

  async function save(c: MotionClip) {
    const dur = dget(c, "dur", String(c.duration_frames ?? ""));
    const seed = dget(c, "seed", String(c.seed ?? ""));
    const startKf = dget(c, "start_kf", c.start_keyframe_id ?? "");
    const endKf = dget(c, "end_kf", c.end_keyframe_id ?? "");
    const payload: Record<string, unknown> = {
      clip_id: c.id,
      prompt: dget(c, "prompt", c.prompt),
      negative_prompt: dget(c, "negative", c.negative_prompt),
      duration_frames: dur.trim() ? parseInt(dur, 10) : null,
      seed: seed.trim() ? parseInt(seed, 10) : null,
      start_keyframe_id: startKf || null,
      end_keyframe_id: endKf || null,
      motion: {
        instructions: dget(c, "instructions",
          c.motion_json?.instructions ?? "") || null,
        intensity: dget(c, "intensity",
          c.motion_json?.intensity ?? "") || null,
        temporal_consistency: dget(c, "temporal",
          c.motion_json?.temporal_consistency ?? "") || null,
      },
      camera: {
        movement: dget(c, "movement", c.camera_json?.movement ?? "") || null,
        follow_mode: dget(c, "follow", c.camera_json?.follow_mode ?? "") || null,
        framing: dget(c, "framing", c.camera_json?.framing ?? "") || null,
      },
      model_key: dget(c, "model", c.model_key ?? "") || null,
    };
    if (dur.trim() && Number.isNaN(parseInt(dur, 10))) {
      setErr("Duration must be a whole number of frames.");
      return;
    }
    await act("clip.update", payload);
  }

  async function doCompose(c: MotionClip) {
    setErr(null);
    try {
      const prop = await previewCommand(
        projectId, "clip.compose", { clip_id: c.id });
      const d = prop.resolved_draft;
      if (!d) { setErr("No draft available for this clip."); return; }
      setPreview({
        kind: "compose", clipId: c.id, prompt: d.prompt,
        warnings: [...(prop.warnings ?? []), ...(d.warnings ?? [])],
        expected_clip: prop.resolved_clip ?? undefined,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  async function doGenerate(c: MotionClip) {
    setErr(null);
    if (dirty(c)) await save(c);
    try {
      const prop = await previewCommand(
        projectId, "clip.generate", { clip_id: c.id });
      const rp = prop.resolved_prompt;
      setPreview({
        kind: "generate", clipId: c.id,
        prompt: rp?.prompt ?? "(unresolved prompt)",
        warnings: prop.warnings ?? [],
        expected_clip: prop.resolved_clip ?? undefined,
        expected_prompt: rp?.prompt_sha256,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  async function approve() {
    if (!preview) return;
    const pv = preview;
    setPreview(null);
    const rev = useApp.getState().currentProject()?.revision;
    if (rev === undefined) { setErr("Project revision unavailable."); return; }
    try {
      if (pv.kind === "compose") {
        await commitCommand(projectId, rev, "clip.compose",
          { clip_id: pv.clipId,
            ...(pv.expected_clip
                ? { expected_clip: pv.expected_clip } : {}) },
          "storyboard");
      } else {
        await commitCommand(projectId, rev, "clip.generate",
          { clip_id: pv.clipId,
            ...(pv.expected_clip
                ? { expected_clip: pv.expected_clip } : {}),
            ...(pv.expected_prompt
                ? { expected_prompt: pv.expected_prompt } : {}) },
          "storyboard");
      }
      await load();
      onChanged?.();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  async function place(c: MotionClip) {
    const tlId = placeTl[c.id] ?? timelines[0]?.id;
    if (!tlId || !c.result_asset_id) return;
    // clip.insert with no start_frames appends at the track's end —
    // the timeline's own rational fps conversion sets the extent.
    await act("timeline.clip.insert", {
      timeline_id: tlId,
      asset_id: c.result_asset_id,
      shot_id: c.shot_id,
    });
  }

  return (
    <section className="mt-4 rounded-md border border-line bg-ink/40 p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold text-fg">Motion clips</h4>
        <button type="button" className={BTN}
          aria-label="Add motion clip"
          onClick={() => void add()}>
          + Motion clip
        </button>
      </div>
      <p className="mt-0.5 text-[10px] text-muted">
        Keyframe→video beats for this shot. Generation is deterministic
        mock video — labeled, no inference.
      </p>
      {err && <p className="mt-1 text-[11px] text-danger" role="alert">{err}</p>}

      {preview && (
        <div className="mt-2 rounded border border-teal/40 bg-panel p-2" role="dialog"
          aria-label={preview.kind === "compose" ? "Motion draft proposal" : "Motion generation preview"}>
          <p className="text-[10px] uppercase tracking-wide text-teal">
            {preview.kind === "compose"
              ? "Proposed motion draft"
              : "Composed prompt — mock video generation"}
          </p>
          <p className="mt-1 max-h-24 overflow-y-auto whitespace-pre-wrap text-[11px] text-fg">
            {preview.prompt}
          </p>
          {preview.warnings.length > 0 && (
            <ul className="mt-1 space-y-0.5">
              {preview.warnings.map((w, i) => (
                <li key={i} className="text-[10px] text-amber">⚠ {w}</li>
              ))}
            </ul>
          )}
          <div className="mt-2 flex gap-2">
            <button type="button" onClick={() => void approve()}
              className="rounded bg-teal px-2 py-1 text-[11px] font-medium text-ink">
              {preview.kind === "compose" ? "Apply draft" : "Approve & generate"}
            </button>
            <button type="button" onClick={() => setPreview(null)}
              className="rounded border border-line px-2 py-1 text-[11px] text-muted">
              Cancel
            </button>
          </div>
        </div>
      )}

      <ul className="mt-2 space-y-1.5">
        {items.map((c) => {
          const job = jobFor(jobs, c.generate_job_id);
          const running = job && ["queued", "running"].includes(job.status);
          const kfName = (id: string | null) =>
            kfs.find((k) => k.id === id)?.role ?? null;
          return (
            <li key={c.id}
              className="rounded border border-line bg-panel px-2 py-1.5 text-[11px]">
              <div className="flex items-center gap-2">
                <button type="button"
                  onClick={() => setExpanded(expanded === c.id ? null : c.id)}
                  className="font-medium text-fg hover:text-teal"
                  aria-expanded={expanded === c.id}>
                  {expanded === c.id ? "▾" : "▸"} Clip {c.position + 1}
                </button>
                {c.duration_frames !== null && (
                  <span className="text-faint">
                    {c.duration_frames}f @{c.fps_num}/{c.fps_den}fps
                  </span>
                )}
                {kfName(c.start_keyframe_id) && (
                  <span className="text-[10px] text-faint">
                    {kfName(c.start_keyframe_id)}
                    {c.end_keyframe_id ? `→${kfName(c.end_keyframe_id)}` : ""}
                  </span>
                )}
                {c.camera_json?.follow_mode && (
                  <span className="text-[10px] text-teal">
                    {c.camera_json.follow_mode}
                  </span>
                )}
                {running && (
                  <span className="text-[10px] text-amber">
                    generating… {Math.round((job?.progress ?? 0) * 100)}%
                  </span>
                )}
                {job && job.status === "failed" && !running && (
                  <span className="text-[10px] text-danger">failed — retry</span>)}
                {job && job.status === "cancelled" && !running && (
                  <span className="text-[10px] text-faint">cancelled</span>)}
                {c.result_asset_name && (
                  <span className={`truncate text-[10px] ${
                    c.result_asset_status === "ready" ? "text-teal" : "text-amber"}`}>
                    {c.result_asset_status === "ready" ? "● " : "⚠ "}
                    {c.result_asset_name} (mock)
                  </span>
                )}
                <span className="ml-auto flex gap-1">
                  <button type="button" className={BTN}
                    aria-label="Draft motion prompt"
                    onClick={() => void doCompose(c)}>Draft</button>
                  <button type="button" className={BTN}
                    aria-label="Generate motion clip"
                    disabled={!!running}
                    onClick={() => void doGenerate(c)}>
                    Generate</button>
                  {running && (
                    <button type="button" className={BTN}
                      aria-label="Cancel motion generation"
                      disabled={job?.cancel_requested}
                      onClick={() => void apiFetch(
                        `/api/jobs/${job.id}/cancel`,
                        { method: "POST" }).catch(() => {})}>
                      {job.cancel_requested ? "Cancelling…" : "Cancel"}
                    </button>)}
                  {c.result_asset_id && timelines.length > 0 && (
                    <>
                      <select className="max-w-24 rounded border border-line bg-ink px-1 py-0.5 text-[10px]"
                        aria-label="Timeline for placement"
                        value={placeTl[c.id] ?? timelines[0].id}
                        onChange={(e) => setPlaceTl(
                          (p) => ({ ...p, [c.id]: e.target.value }))}>
                        {timelines.map((t) => (
                          <option key={t.id} value={t.id}>{t.name}</option>))}
                      </select>
                      <button type="button" className={BTN}
                        aria-label="Place clip on timeline"
                        onClick={() => void place(c)}>
                        Place</button>
                    </>
                  )}
                  {c.status === "active" ? (
                    <button type="button" className={BTN}
                      aria-label="Archive motion clip"
                      onClick={() => void act("clip.archive", { clip_id: c.id })}>
                      Archive</button>
                  ) : (
                    <button type="button" className={BTN}
                      aria-label="Restore motion clip"
                      onClick={() => void act("clip.unarchive", { clip_id: c.id })}>
                      Restore</button>
                  )}
                </span>
              </div>
              {expanded === c.id && (
                <div className="mt-2 space-y-1.5 border-t border-line/50 pt-2">
                  <label className="block"><span className={LBL}>Prompt</span>
                    <textarea className={INP + " h-14 resize-y"}
                      aria-label="Motion prompt"
                      value={dget(c, "prompt", c.prompt)}
                      maxLength={4000}
                      onChange={(e) => dset(c, "prompt", e.target.value)} />
                  </label>
                  <div className="grid grid-cols-2 gap-2">
                    <label className="block"><span className={LBL}>Start keyframe</span>
                      <select className={SEL}
                        value={dget(c, "start_kf", c.start_keyframe_id ?? "")}
                        onChange={(e) => dset(c, "start_kf", e.target.value)}>
                        <option value="">none</option>
                        {kfs.filter((k) => k.status === "active").map((k) => (
                          <option key={k.id} value={k.id}>{k.role}</option>))}
                      </select></label>
                    <label className="block"><span className={LBL}>End keyframe</span>
                      <select className={SEL}
                        value={dget(c, "end_kf", c.end_keyframe_id ?? "")}
                        onChange={(e) => dset(c, "end_kf", e.target.value)}>
                        <option value="">none</option>
                        {kfs.filter((k) => k.status === "active").map((k) => (
                          <option key={k.id} value={k.id}>{k.role}</option>))}
                      </select></label>
                  </div>
                  <div className="grid grid-cols-3 gap-2">
                    <label className="block"><span className={LBL}>Movement</span>
                      <select className={SEL}
                        value={dget(c, "movement", c.camera_json?.movement ?? "")}
                        onChange={(e) => dset(c, "movement", e.target.value)}>
                        {MOVEMENTS.map((m) => (
                          <option key={m} value={m}>{m || "—"}</option>))}
                      </select></label>
                    <label className="block"><span className={LBL}>Follow mode</span>
                      <select className={SEL}
                        value={dget(c, "follow", c.camera_json?.follow_mode ?? "")}
                        onChange={(e) => dset(c, "follow", e.target.value)}>
                        {FOLLOW_MODES.map((m) => (
                          <option key={m} value={m}>{m || "—"}</option>))}
                      </select></label>
                    <label className="block"><span className={LBL}>Framing</span>
                      <select className={SEL}
                        value={dget(c, "framing", c.camera_json?.framing ?? "")}
                        onChange={(e) => dset(c, "framing", e.target.value)}>
                        {FRAMINGS.map((m) => (
                          <option key={m} value={m}>{m || "—"}</option>))}
                      </select></label>
                  </div>
                  <div className="grid grid-cols-3 gap-2">
                    <label className="block"><span className={LBL}>Intensity</span>
                      <select className={SEL}
                        value={dget(c, "intensity", c.motion_json?.intensity ?? "")}
                        onChange={(e) => dset(c, "intensity", e.target.value)}>
                        {INTENSITIES.map((m) => (
                          <option key={m} value={m}>{m || "—"}</option>))}
                      </select></label>
                    <label className="block"><span className={LBL}>Duration (frames)</span>
                      <input className={INP} inputMode="numeric"
                        value={dget(c, "dur", String(c.duration_frames ?? ""))}
                        onChange={(e) => dset(c, "dur", e.target.value)} /></label>
                    <label className="block"><span className={LBL}>Seed</span>
                      <input className={INP} inputMode="numeric"
                        value={dget(c, "seed", String(c.seed ?? ""))}
                        onChange={(e) => dset(c, "seed", e.target.value)} /></label>
                  </div>
                  <label className="block"><span className={LBL}>Motion instructions</span>
                    <input className={INP}
                      value={dget(c, "instructions", c.motion_json?.instructions ?? "")}
                      maxLength={2000}
                      onChange={(e) => dset(c, "instructions", e.target.value)} /></label>
                  <label className="block"><span className={LBL}>Temporal consistency</span>
                    <input className={INP}
                      value={dget(c, "temporal", c.motion_json?.temporal_consistency ?? "")}
                      maxLength={2000}
                      onChange={(e) => dset(c, "temporal", e.target.value)} /></label>
                  <div className="grid grid-cols-2 gap-2">
                    <label className="block"><span className={LBL}>Negative</span>
                      <input className={INP}
                        value={dget(c, "negative", c.negative_prompt)}
                        maxLength={4000}
                        onChange={(e) => dset(c, "negative", e.target.value)} /></label>
                    <label className="block"><span className={LBL}>Model</span>
                      <select className={SEL}
                        value={dget(c, "model", c.model_key ?? "")}
                        onChange={(e) => dset(c, "model", e.target.value)}>
                        <option value="">mock.video (default)</option>
                        {videoModels.map((m) => {
                          const cm = COMPAT_BADGE[m.compatibility.status]
                            ?? COMPAT_BADGE.unknown;
                          return (
                            <option key={m.key} value={m.key}>
                              {m.display_name} — {cm.label}
                            </option>);
                        })}
                      </select></label>
                  </div>
                  {(() => {
                    const mk = dget(c, "model", c.model_key ?? "");
                    const m = videoModels.find((x) => x.key === mk);
                    if (!m) return null;
                    const cm = COMPAT_BADGE[m.compatibility.status] ?? COMPAT_BADGE.unknown;
                    return (
                      <p className={`text-[10px] ${cm.cls}`}>
                        {m.display_name}: {cm.label}
                        {m.compatibility.reasons.length > 0 &&
                          ` — ${m.compatibility.reasons[0]}`}
                        {m.install?.status !== "installed" &&
                          " · not installed (mock output regardless)"}
                      </p>
                    );
                  })()}
                  <div className="flex items-center gap-2">
                    <button type="button" disabled={!dirty(c)}
                      onClick={() => void save(c)}
                      className="rounded bg-teal px-2 py-0.5 text-[10px] font-medium text-ink disabled:opacity-40">
                      Save settings
                    </button>
                    {dirty(c) && (
                      <span className="text-[10px] text-amber">unsaved edits</span>)}
                  </div>
                </div>
              )}
            </li>
          );
        })}
        {items.length === 0 && (
          <li className="text-[11px] text-faint">
            No motion clips yet — add one to plan this shot's video beat.
          </li>
        )}
      </ul>
    </section>
  );
}
