/**
 * A8 — guided filmmaking wizard.
 *
 * A step-by-step path through the real A1–A7 pipeline: film settings →
 * screenplay → scenes/cast → scaffold → planning → keyframes → motion
 * clips → dialogue → first-cut assembly → edit → export. Every action
 * commits through the Unified Creative Command system against real
 * project rows; stage truth is derived server-side (`GET /wizard`);
 * progress persists in `project.settings_json.wizard` so leaving and
 * returning (or restarting, or restoring a backup) resumes where the
 * user left off. Beginner and Advanced are views over the same data —
 * switching never writes entity rows.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { apiFetch } from "../../api/client";
import { commitCommand } from "../../api/commands";
import type {
  AssemblyPlan, Entity, StoryboardScene, Timeline, WizardDerived,
} from "../../api/types";
import { useApp, type WorkspaceTab } from "../../stores/appStore";
import {
  STAGES, friendlyError, loadState, reconcileState,
  type WizardState,
} from "./wizard";
import {
  CastStage, DialogueStage, EditStage, ExportConfigStage, ExportStage,
  FilmSettingsStage, KeyframesStage, MotionStage, PlanStage,
  PlanningStage, ProjectStage, ReviewStage, ScenesStage,
  ScreenplayStage, ShotsStage, type Ctx,
} from "./steps";

interface SpListItem { id: string; title: string }
interface SpDoc {
  id: string; title: string; revision: number;
  scenes: { id: string; position: number; heading: string; action: string; notes: string;
    elements: { id: string; kind: string; character_id: string | null; text: string; parenthetical: string }[] }[];
}

export function WizardPage() {
  const project = useApp((s) => s.currentProject());
  const jobs = useApp((s) => s.jobs);
  const setTab = useApp((s) => s.setWorkspaceTab);
  const setMode = useApp((s) => s.setWorkspaceMode);
  const workspaceMode = useApp((s) => s.workspaceMode);

  const [state, setState] = useState<WizardState | null>(null);
  const [derived, setDerived] = useState<WizardDerived | null>(null);
  const [notices, setNotices] = useState<string[]>([]);
  const [board, setBoard] = useState<StoryboardScene[]>([]);
  const [characters, setCharacters] = useState<Entity[]>([]);
  const [locations, setLocations] = useState<Entity[]>([]);
  const [timelines, setTimelines] = useState<Timeline[]>([]);
  const [screenplays, setScreenplays] = useState<SpListItem[]>([]);
  const [doc, setDoc] = useState<SpDoc | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<{ title: string; detail: string } | null>(null);
  const [showDetail, setShowDetail] = useState(false);
  const [legacyNotice, setLegacyNotice] = useState(false);

  const pid = project?.id;
  const fail = useCallback((e: unknown) => {
    setErr(friendlyError(e));
    setBusy(null);
  }, []);
  const rev = useCallback(
    () => useApp.getState().currentProject()?.revision ?? 0, []);
  const openTab = useCallback((t: WorkspaceTab) => setTab(t), [setTab]);

  const refresh = useCallback(async () => {
    if (!pid) return;
    const [w, b, chars, locs, tls, sps] = await Promise.all([
      apiFetch<WizardDerived>(`/api/projects/${pid}/wizard`),
      apiFetch<StoryboardScene[]>(`/api/projects/${pid}/storyboard`),
      apiFetch<Entity[]>(`/api/projects/${pid}/character`),
      apiFetch<Entity[]>(`/api/projects/${pid}/location`),
      apiFetch<Timeline[]>(`/api/projects/${pid}/timelines`),
      apiFetch<SpListItem[]>(`/api/projects/${pid}/screenplays`),
    ]);
    setDerived(w); setBoard(b);
    setCharacters(chars.filter((x) => x.status === "active"));
    setLocations(locs.filter((x) => x.status === "active"));
    setTimelines(tls); setScreenplays(sps);
    await useApp.getState().refreshProjectData();
  }, [pid]);

  // ---- load + reconcile persisted wizard state ----
  useEffect(() => {
    if (!pid || !project) return;
    let live = true;
    (async () => {
      const w = await apiFetch<WizardDerived>(`/api/projects/${pid}/wizard`);
      const [b, chars, locs, tls, sps, plans] = await Promise.all([
        apiFetch<StoryboardScene[]>(`/api/projects/${pid}/storyboard`),
        apiFetch<Entity[]>(`/api/projects/${pid}/character`),
        apiFetch<Entity[]>(`/api/projects/${pid}/location`),
        apiFetch<Timeline[]>(`/api/projects/${pid}/timelines`),
        apiFetch<SpListItem[]>(`/api/projects/${pid}/screenplays`),
        apiFetch<AssemblyPlan[]>(`/api/projects/${pid}/assemblies`),
      ]);
      if (!live) return;
      setDerived(w); setBoard(b);
      setCharacters(chars.filter((x) => x.status === "active"));
      setLocations(locs.filter((x) => x.status === "active"));
      setTimelines(tls); setScreenplays(sps);

      const { state: loaded, legacy } = loadState(project.settings_json);
      // guided workspace mode lands in beginner presentation on first
      // open; the blob's own mode wins once the user has chosen.
      const seeded = (legacy || w.progress === null) && workspaceMode === "guided"
        ? { ...loaded, mode: "beginner" as const } : loaded;
      const { state: clean, notices } = reconcileState(seeded, {
        screenplayIds: new Set(sps.map((s) => s.id)),
        timelineIds: new Set(tls.map((t) => t.id)),
        planIds: new Set(plans.map((p) => p.id)),
        jobIds: new Set(w.jobs.map((j) => j.id)),
      });
      setNotices(notices); setState(clean); setLegacyNotice(legacy);
      if (clean.screenplay_id) {
        try {
          setDoc(await apiFetch<SpDoc>(
            `/api/projects/${pid}/screenplays/${clean.screenplay_id}`));
        } catch { /* doc fetch is best-effort — stages degrade cleanly */ }
      }
    })().catch(fail);
    return () => { live = false; };
  }, [pid]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Persist wizard progress through the command system (project-scoped). */
  const save = useCallback(async (next: WizardState) => {
    if (!pid) return;
    const s: WizardState = {
      ...next, furthest: Math.max(next.furthest, next.step),
      updated_at: new Date().toISOString(),
    };
    setState(s);
    await commitCommand(pid, rev(), "project.update_settings",
      { settings: { wizard: s } }, "wizard");
    await useApp.getState().refreshProjectData();
  }, [pid, rev]);

  const commit = useCallback(async (ct: string, payload: Record<string, unknown>) => {
    if (!pid) throw new Error("no project");
    return commitCommand(pid, rev(), ct, payload, "wizard");
  }, [pid, rev]);

  const go = useCallback(async (step: number) => {
    if (!state) return;
    try { await save({ ...state, step }); } catch (e) { fail(e); }
  }, [state, save, fail]);

  const ctx: Ctx | null = useMemo(() => {
    if (!pid || !state || !derived) return null;
    return {
      pid, state, derived, mode: state.mode, board, characters, locations,
      timelines, jobs, busy, commit, save, refresh, fail, setBusy, openTab,
    };
  }, [pid, state, derived, board, characters, locations, timelines, jobs,
      busy, commit, save, refresh, fail, openTab]);

  if (!project || !state || !derived || !ctx) {
    return (
      <div className="p-10 text-center text-muted">
        {err ? (
          <div className="mx-auto max-w-md rounded border border-danger/40 bg-danger/10 p-3 text-sm" role="alert">
            <p className="text-danger">{err.title}</p>
            <pre className="mt-1 whitespace-pre-wrap text-xs text-faint">{err.detail}</pre>
          </div>
        ) : "Loading your film…"}
      </div>
    );
  }

  const stageMeta = STAGES[state.step];
  const stageId = stageMeta.id;
  const derivedStage = derived.stages[state.step];
  const optional = !stageMeta.required;
  const lastStep = state.step === STAGES.length - 1;

  return (
    <div className="mx-auto max-w-3xl p-6">
      <header className="mb-4">
        <div className="flex items-center gap-3">
          <h1 className="text-xl font-bold">Film wizard</h1>
          <div className="ml-auto flex rounded-md border border-line text-xs" role="group" aria-label="Mode">
            {(["beginner", "advanced"] as const).map((m) => (
              <button key={m}
                className={clsx("px-3 py-1 capitalize",
                  state.mode === m ? "bg-teal/20 text-teal" : "text-muted hover:text-fg")}
                aria-pressed={state.mode === m}
                onClick={async () => {
                  if (state.mode === m) return;
                  // mode is a view flag on the same data — never a rewrite
                  await save({ ...state, mode: m });
                  setMode(m === "beginner" ? "guided" : "advanced");
                }}>
                {m}
              </button>
            ))}
          </div>
        </div>
        <p className="mt-1 text-sm text-muted">
          Step {state.step + 1} of {STAGES.length} — {stageMeta.label}
          {optional && <span className="ml-2 text-faint">(optional)</span>}
        </p>
        <div className="mt-3 flex gap-1" role="list" aria-label="Progress">
          {STAGES.map((s, i) => {
            const d = derived.stages[i];
            return (
              <button key={s.id} role="listitem"
                aria-label={`Step ${i + 1}: ${s.label}${i === state.step ? " (current)" : ""} — ${d?.status ?? "ready"}`}
                aria-current={i === state.step ? "step" : undefined}
                onClick={() => i <= state.furthest && void go(i)}
                disabled={i > state.furthest}
                title={`${s.label} — ${d?.status ?? ""}`}
                className={clsx(
                  "h-2 flex-1 rounded-full",
                  i === state.step ? "bg-action"
                    : d?.status === "done" ? "bg-teal"
                    : d?.status === "attention" ? "bg-amber"
                    : "bg-card-hi",
                  i <= state.furthest ? "cursor-pointer" : "cursor-default opacity-60",
                )} />
            );
          })}
        </div>
      </header>

      {derived.honesty.all_simulated && (
        <div className="mb-3 rounded border border-amber/40 bg-amber/10 p-2 text-xs text-amber" role="note">
          Generated content in this build is <b>simulated</b> — mock
          adapters produce labeled placeholders, not real AI output.
        </div>
      )}
      {legacyNotice && (
        <div className="mb-3 rounded border border-info/40 bg-info/10 p-2 text-sm text-info" role="note">
          Resumed from the earlier guided flow — your step was mapped to
          the closest stage. Nothing was removed.
        </div>
      )}
      {notices.length > 0 && (
        <div className="mb-3 rounded border border-amber/40 bg-amber/10 p-2 text-sm text-amber">
          {notices.map((n, i) => <p key={i}>{n}</p>)}
        </div>
      )}
      {derived.warnings.length > 0 && (
        <div className="mb-3 rounded border border-amber/40 bg-amber/10 p-2 text-sm text-amber" role="status">
          {derived.warnings.map((n, i) => <p key={i}>{n}</p>)}
        </div>
      )}
      {err && (
        <div className="mb-3 rounded border border-danger/40 bg-danger/10 p-3 text-sm" role="alert">
          <p className="text-danger">{err.title}</p>
          <button className="mt-1 text-xs text-muted underline" onClick={() => setShowDetail((v) => !v)}>
            {showDetail ? "Hide" : "Show"} technical details
          </button>
          {showDetail && <pre className="mt-1 whitespace-pre-wrap text-xs text-faint">{err.detail}</pre>}
          <button className="ml-3 mt-1 text-xs text-muted underline" onClick={() => setErr(null)}>Dismiss</button>
        </div>
      )}

      <div className="rounded-lg border border-line bg-card p-5">
        {derivedStage && derivedStage.warnings.length > 0 && (
          <div className="mb-3 rounded border border-amber/40 bg-amber/10 p-2 text-xs text-amber">
            {derivedStage.warnings.map((w, i) => <p key={i}>{w}</p>)}
          </div>
        )}
        {stageId === "project" && <ProjectStage ctx={ctx} />}
        {stageId === "film_settings" && <FilmSettingsStage ctx={ctx} />}
        {stageId === "screenplay" && (
          <ScreenplayStage ctx={ctx} screenplays={screenplays} doc={doc} setDoc={setDoc} />)}
        {stageId === "scenes" && <ScenesStage ctx={ctx} doc={doc} setDoc={setDoc} />}
        {stageId === "cast" && <CastStage ctx={ctx} />}
        {stageId === "shots" && <ShotsStage ctx={ctx} />}
        {stageId === "planning" && <PlanningStage ctx={ctx} />}
        {stageId === "keyframes" && <KeyframesStage ctx={ctx} />}
        {stageId === "motion" && <MotionStage ctx={ctx} />}
        {stageId === "dialogue" && <DialogueStage ctx={ctx} doc={doc} />}
        {stageId === "assembly_plan" && <PlanStage ctx={ctx} />}
        {stageId === "assembly_apply" && <ReviewStage ctx={ctx} />}
        {stageId === "edit" && <EditStage ctx={ctx} />}
        {stageId === "export_config" && <ExportConfigStage ctx={ctx} />}
        {stageId === "export" && <ExportStage ctx={ctx} />}
      </div>

      <footer className="mt-4 flex items-center gap-2">
        <button className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg disabled:opacity-40"
          disabled={state.step === 0}
          onClick={() => void go(state.step - 1)}>
          ← Back
        </button>
        <span className="flex-1" />
        {optional && !lastStep && (
          <button className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg"
            onClick={() => void go(state.step + 1)}>
            Skip for now →
          </button>
        )}
        {!lastStep && (
          <button className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi"
            onClick={() => void go(state.step + 1)}>
            Next →
          </button>
        )}
        {lastStep && derivedStage?.status === "done" && (
          <button className="rounded-md bg-teal px-4 py-2 text-sm font-medium text-white"
            onClick={() => void save({ ...state, done: true })}>
            Mark film complete
          </button>
        )}
      </footer>
    </div>
  );
}
