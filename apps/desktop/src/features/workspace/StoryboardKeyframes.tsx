/**
 * A3 — keyframe editing for a shot: role-based still-image beats with
 * user-owned prompts, a continuity-aware draft proposal, mock
 * generation through the durable job system, and result pinning.
 * All mutations are Unified Creative Commands — generation is preview
 * → approve → commit with the backend's pins, never a direct call.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "../../api/client";
import { commitCommand, previewCommand } from "../../api/commands";
import { useApp } from "../../stores/appStore";
import type {
  Job, Keyframe, KeyframeRole, ModelCatalogEntry,
} from "../../api/types";

const SEL = "w-full rounded border border-line bg-ink px-2 py-1 text-xs";
const INP = SEL;
const LBL = "text-[10px] uppercase tracking-wide text-faint";
const BTN = "rounded border border-line px-2 py-0.5 text-[10px] text-muted hover:text-teal disabled:opacity-40";

const ROLE_LABEL: Record<KeyframeRole, string> = {
  start: "Start", end: "End", reference: "Reference",
  intermediate: "Intermediate",
};

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

export function KeyframesSection({ projectId, shotId, run, onChanged }: {
  projectId: string;
  shotId: string;
  /** StoryboardPage's plain-command runner (create/update/archive/pin). */
  run: (t: string, p: Record<string, unknown>) => Promise<boolean>;
  /** Called after any mutation so the board refreshes badges. */
  onChanged?: () => void;
}) {
  const [items, setItems] = useState<Keyframe[]>([]);
  const [models, setModels] = useState<ModelCatalogEntry[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [preview, setPreview] = useState<{
    kind: "compose" | "generate"; kfId: string;
    prompt: string; warnings: string[];
    expected_keyframe?: Record<string, unknown>;
    expected_prompt?: string;
  } | null>(null);
  const jobs = useApp((s) => s.jobs);

  const load = useCallback(async () => {
    try {
      const rows = await apiFetch<Keyframe[]>(
        `/api/projects/${projectId}/shots/${shotId}/keyframes`);
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
  }, []);
  // When a keyframe job newly settles, reload once — the result asset
  // lands via the job's apply_command server-side.
  const settledRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    let newly = 0;
    for (const kf of items) {
      const j = jobFor(jobs, kf.generate_job_id);
      if (j && ["succeeded", "failed", "cancelled"].includes(j.status)
          && !settledRef.current.has(j.id)) {
        settledRef.current.add(j.id);
        newly += 1;
      }
    }
    if (newly > 0) void load();
  }, [jobs, items, load]);

  const imageModels = useMemo(
    () => models.filter((m) => m.modality === "image"),
    [models]);

  const dget = (kf: Keyframe, k: string, fallback: string) =>
    draft[kf.id + ":" + k] ?? fallback;
  const dset = (kf: Keyframe, k: string, v: string) =>
    setDraft((d) => ({ ...d, [kf.id + ":" + k]: v }));
  const dirty = (kf: Keyframe) =>
    dget(kf, "prompt", kf.prompt) !== kf.prompt
    || dget(kf, "negative", kf.negative_prompt) !== kf.negative_prompt
    || dget(kf, "seed", String(kf.settings_json?.seed ?? "")) !== String(kf.settings_json?.seed ?? "")
    || dget(kf, "model", kf.settings_json?.model_key ?? "") !== (kf.settings_json?.model_key ?? "");

  async function act(t: string, p: Record<string, unknown>) {
    setErr(null);
    if (!(await run(t, p))) {
      setErr("Keyframe change failed — see the board message.");
      return;
    }
    await load();
    onChanged?.();
  }

  async function add(role: KeyframeRole) {
    await act("keyframe.create", { shot_id: shotId, role });
  }

  async function save(kf: Keyframe) {
    const payload: Record<string, unknown> = {
      keyframe_id: kf.id,
      prompt: dget(kf, "prompt", kf.prompt),
      negative_prompt: dget(kf, "negative", kf.negative_prompt),
      settings: {
        seed: (() => {
          const v = dget(kf, "seed", String(kf.settings_json?.seed ?? ""));
          if (!v.trim()) return null;
          const n = parseInt(v, 10);
          return Number.isNaN(n) ? null : n;
        })(),
        model_key: dget(kf, "model", kf.settings_json?.model_key ?? "") || null,
      },
    };
    await act("keyframe.update", payload);
  }

  async function doCompose(kf: Keyframe) {
    setErr(null);
    try {
      const prop = await previewCommand(
        projectId, "keyframe.compose", { keyframe_id: kf.id });
      const d = prop.resolved_draft;
      if (!d) { setErr("No draft available for this keyframe."); return; }
      setPreview({
        kind: "compose", kfId: kf.id, prompt: d.prompt,
        warnings: [...(prop.warnings ?? []), ...(d.warnings ?? [])],
        expected_keyframe: prop.resolved_keyframe ?? undefined,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  async function doGenerate(kf: Keyframe) {
    setErr(null);
    if (dirty(kf)) await save(kf);
    try {
      const prop = await previewCommand(
        projectId, "keyframe.generate", { keyframe_id: kf.id });
      const rp = prop.resolved_prompt;
      setPreview({
        kind: "generate", kfId: kf.id,
        prompt: rp?.prompt ?? "(unresolved prompt)",
        warnings: prop.warnings ?? [],
        expected_keyframe: prop.resolved_keyframe ?? undefined,
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
        await commitCommand(projectId, rev, "keyframe.compose",
          { keyframe_id: pv.kfId,
            ...(pv.expected_keyframe
                ? { expected_keyframe: pv.expected_keyframe } : {}) },
          "storyboard");
      } else {
        await commitCommand(projectId, rev, "keyframe.generate",
          { keyframe_id: pv.kfId,
            ...(pv.expected_keyframe
                ? { expected_keyframe: pv.expected_keyframe } : {}),
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

  return (
    <section className="mt-4 rounded-md border border-line bg-ink/40 p-3">
      <div className="flex items-center justify-between">
        <h4 className="text-xs font-semibold text-fg">Keyframes</h4>
        <div className="flex gap-1.5">
          {(["start", "end", "reference", "intermediate"] as const).map((r) => (
            <button key={r} type="button" className={BTN}
              aria-label={`Add ${ROLE_LABEL[r]} keyframe`}
              onClick={() => void add(r)}>
              + {ROLE_LABEL[r]}
            </button>
          ))}
        </div>
      </div>
      <p className="mt-0.5 text-[10px] text-muted">
        Still-image beats for this shot. Drafts compose continuity context;
        generation is mock — labeled, deterministic, no inference.
      </p>
      {err && <p className="mt-1 text-[11px] text-danger" role="alert">{err}</p>}

      {preview && (
        <div className="mt-2 rounded border border-teal/40 bg-panel p-2" role="dialog"
          aria-label={preview.kind === "compose" ? "Prompt draft proposal" : "Generation preview"}>
          <p className="text-[10px] uppercase tracking-wide text-teal">
            {preview.kind === "compose" ? "Proposed draft" : "Composed prompt — mock generation"}
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
        {items.map((kf) => {
          const job = jobFor(jobs, kf.generate_job_id);
          const running = job && ["queued", "running"].includes(job.status);
          return (
            <li key={kf.id}
              className="rounded border border-line bg-panel px-2 py-1.5 text-[11px]">
              <div className="flex items-center gap-2">
                <button type="button"
                  onClick={() => setExpanded(expanded === kf.id ? null : kf.id)}
                  className="font-medium text-fg hover:text-teal"
                  aria-expanded={expanded === kf.id}>
                  {expanded === kf.id ? "▾" : "▸"} {ROLE_LABEL[kf.role]}
                </button>
                {kf.frame_num !== null && kf.frame_den !== null && (
                  <span className="text-faint">f{kf.frame_num}/{kf.frame_den}</span>
                )}
                {kf.prompt_origin === "compose" && (
                  <span className="text-[10px] text-teal">drafted</span>)}
                {running && (
                  <span className="text-[10px] text-amber">
                    generating… {Math.round((job?.progress ?? 0) * 100)}%
                  </span>
                )}
                {job && job.status === "failed" && !running && (
                  <span className="text-[10px] text-danger">failed — retry</span>)}
                {job && job.status === "cancelled" && !running && (
                  <span className="text-[10px] text-faint">cancelled</span>)}
                {kf.result_asset_name && (
                  <span className={`truncate text-[10px] ${
                    kf.result_asset_status === "ready" ? "text-teal" : "text-amber"}`}>
                    {kf.result_asset_status === "ready" ? "● " : "⚠ "}
                    {kf.result_asset_name}
                  </span>
                )}
                <span className="ml-auto flex gap-1">
                  <button type="button" className={BTN}
                    aria-label={`Draft prompt for ${ROLE_LABEL[kf.role]} keyframe`}
                    onClick={() => void doCompose(kf)}>Draft</button>
                  <button type="button" className={BTN}
                    aria-label={`Generate ${ROLE_LABEL[kf.role]} keyframe`}
                    disabled={!!running}
                    onClick={() => void doGenerate(kf)}>
                    Generate</button>
                  {kf.result_asset_id && (
                    <button type="button" className={BTN}
                      aria-label={`Pin ${ROLE_LABEL[kf.role]} result`}
                      onClick={() => void act("keyframe.pin", { keyframe_id: kf.id })}>
                      Pin</button>)}
                  {kf.status === "active" ? (
                    <button type="button" className={BTN}
                      aria-label={`Archive ${ROLE_LABEL[kf.role]} keyframe`}
                      onClick={() => void act("keyframe.archive", { keyframe_id: kf.id })}>
                      Archive</button>
                  ) : (
                    <button type="button" className={BTN}
                      aria-label={`Restore ${ROLE_LABEL[kf.role]} keyframe`}
                      onClick={() => void act("keyframe.unarchive", { keyframe_id: kf.id })}>
                      Restore</button>
                  )}
                </span>
              </div>
              {expanded === kf.id && (
                <div className="mt-2 space-y-1.5 border-t border-line/50 pt-2">
                  <label className="block"><span className={LBL}>Prompt</span>
                    <textarea className={INP + " h-14 resize-y"}
                      aria-label="Keyframe prompt"
                      value={dget(kf, "prompt", kf.prompt)}
                      maxLength={4000}
                      onChange={(e) => dset(kf, "prompt", e.target.value)} />
                  </label>
                  <div className="grid grid-cols-3 gap-2">
                    <label className="block"><span className={LBL}>Negative</span>
                      <input className={INP}
                        value={dget(kf, "negative", kf.negative_prompt)}
                        maxLength={4000}
                        onChange={(e) => dset(kf, "negative", e.target.value)} /></label>
                    <label className="block"><span className={LBL}>Seed</span>
                      <input className={INP} inputMode="numeric"
                        value={dget(kf, "seed", String(kf.settings_json?.seed ?? ""))}
                        onChange={(e) => dset(kf, "seed", e.target.value)} /></label>
                    <label className="block"><span className={LBL}>Model</span>
                      <select className={SEL}
                        value={dget(kf, "model", kf.settings_json?.model_key ?? "")}
                        onChange={(e) => dset(kf, "model", e.target.value)}>
                        <option value="">mock (default)</option>
                        {imageModels.map((m) => {
                          const c = COMPAT_BADGE[m.compatibility.status]
                            ?? COMPAT_BADGE.unknown;
                          return (
                            <option key={m.key} value={m.key}>
                              {m.display_name} — {c.label}
                            </option>);
                        })}
                      </select></label>
                  </div>
                  {(() => {
                    const mk = dget(kf, "model", kf.settings_json?.model_key ?? "");
                    const m = imageModels.find((x) => x.key === mk);
                    if (!m) return null;
                    const c = COMPAT_BADGE[m.compatibility.status] ?? COMPAT_BADGE.unknown;
                    return (
                      <p className={`text-[10px] ${c.cls}`}>
                        {m.display_name}: {c.label}
                        {m.compatibility.reasons.length > 0 &&
                          ` — ${m.compatibility.reasons[0]}`}
                        {m.install?.status !== "installed" &&
                          " · not installed (mock output regardless)"}
                      </p>
                    );
                  })()}
                  <div className="flex items-center gap-2">
                    <button type="button" disabled={!dirty(kf)}
                      onClick={() => void save(kf)}
                      className="rounded bg-teal px-2 py-0.5 text-[10px] font-medium text-ink disabled:opacity-40">
                      Save prompt
                    </button>
                    {dirty(kf) && (
                      <span className="text-[10px] text-amber">unsaved edits</span>)}
                  </div>
                </div>
              )}
            </li>
          );
        })}
        {items.length === 0 && (
          <li className="text-[11px] text-faint">
            No keyframes yet — add a Start or End frame to plan this shot's beats.
          </li>
        )}
      </ul>
    </section>
  );
}
