/**
 * C5-D — guided First Film workflow.
 *
 * A beginner-facing, step-by-step path through real ADE commands:
 * film settings → characters → scene → shots → media method →
 * generate/import → timeline → arrange/trim → export. Every action
 * commits through the Unified Creative Command system against real
 * project rows; progress persists in `project.settings_json.first_film`
 * so leaving and returning (or restarting the app) resumes where the
 * user left off, and stored ids are re-validated on load.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { apiFetch } from "../../api/client";
import { commitCommand, previewCommand } from "../../api/commands";
import type {
  Asset, Entity, ModelCatalogEntry, ProviderDetail, ProviderSummary,
  StoryboardScene, Timeline, TimelineDetail,
} from "../../api/types";
import { useApp } from "../../stores/appStore";
import {
  STEPS, friendlyError, initialState, loadState, methodOptions,
  reconcileState, stepReady,
  type FirstFilmState, type MethodOption, type StepId,
} from "./wizard";
import {
  ArrangeStep, CharactersStep, ExportStep, GenerateStep, MediaStep,
  MethodStep, ProfileStep, SceneStep, SetupStep, ShotsStep, TimelineStep,
} from "./steps";
import { pollJob } from "./jobs";

type FFAssets = { items: Asset[] };

export function FirstFilmPage() {
  const project = useApp((s) => s.currentProject());
  const jobs = useApp((s) => s.jobs);
  const setTab = useApp((s) => s.setWorkspaceTab);

  const [state, setState] = useState<FirstFilmState | null>(null);
  const [notices, setNotices] = useState<string[]>([]);
  const [board, setBoard] = useState<StoryboardScene[]>([]);
  const [characters, setCharacters] = useState<Entity[]>([]);
  const [, setTimelines] = useState<Timeline[]>([]);
  const [tlDetail, setTlDetail] = useState<TimelineDetail | null>(null);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [methods, setMethods] = useState<MethodOption[]>([]);
  const [importJobs, setImportJobs] = useState<Record<string, { jobId: string; message: string }>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<{ title: string; detail: string } | null>(null);
  const [showDetail, setShowDetail] = useState(false);

  const pid = project?.id;

  const fail = useCallback((e: unknown) => {
    setErr(friendlyError(e));
    setBusy(null);
  }, []);

  /** Latest project revision straight from the store — the commit pin. */
  const rev = useCallback(
    () => useApp.getState().currentProject()?.revision ?? 0,
    [],
  );

  const refreshAll = useCallback(async () => {
    if (!pid) return;
    const [b, chars, tls, ap] = await Promise.all([
      apiFetch<StoryboardScene[]>(`/api/projects/${pid}/storyboard`),
      apiFetch<Entity[]>(`/api/projects/${pid}/character`),
      apiFetch<Timeline[]>(`/api/projects/${pid}/timelines`),
      apiFetch<FFAssets>(`/api/projects/${pid}/assets?limit=200`),
    ]);
    setBoard(b); setCharacters(chars.filter((c) => c.status === "active"));
    setTimelines(tls); setAssets(ap.items);
    await useApp.getState().refreshProjectData();
  }, [pid]);

  // ---- load + reconcile persisted wizard state ----
  useEffect(() => {
    if (!pid || !project) return;
    let live = true;
    (async () => {
      const [b, chars, tls, ap] = await Promise.all([
        apiFetch<StoryboardScene[]>(`/api/projects/${pid}/storyboard`),
        apiFetch<Entity[]>(`/api/projects/${pid}/character`),
        apiFetch<Timeline[]>(`/api/projects/${pid}/timelines`),
        apiFetch<FFAssets>(`/api/projects/${pid}/assets?limit=200`),
      ]);
      const [provs, models] = await Promise.all([
        apiFetch<ProviderSummary[]>("/api/providers?category=image").catch(() => [] as ProviderSummary[]),
        apiFetch<ModelCatalogEntry[]>("/api/models").catch(() => [] as ModelCatalogEntry[]),
      ]);
      const details: Record<string, ProviderDetail> = {};
      await Promise.all(provs.map(async (p) => {
        try { details[p.key] = await apiFetch<ProviderDetail>(`/api/providers/${p.key}`); }
        catch { /* provider detail missing → no image options from it */ }
      }));
      if (!live) return;
      setBoard(b); setCharacters(chars.filter((c) => c.status === "active"));
      setTimelines(tls); setAssets(ap.items);
      setMethods(methodOptions(provs, details, models));

      const persisted = loadState(project.settings_json) ?? initialState(project.name);
      const shotIds = new Set(b.flatMap((sc) => sc.shots.map((sh) => sh.shot.id)));
      const { state: clean, notices } = reconcileState(persisted, {
        characterIds: new Set(chars.map((c) => c.id)),
        sceneIds: new Set(b.map((sc) => sc.scene.id)),
        shotIds, timelineIds: new Set(tls.map((t) => t.id)),
        assetIds: new Set(ap.items.map((a) => a.id)),
      });
      setNotices(notices);
      setState(clean);
    })().catch(fail);
    return () => { live = false; };
  }, [pid]); // eslint-disable-line react-hooks/exhaustive-deps

  /** Persist wizard state through the command system (project-scoped). */
  const save = useCallback(async (next: FirstFilmState) => {
    if (!pid) return;
    const s: FirstFilmState = {
      ...next,
      furthest: Math.max(next.furthest, next.step),
      updated_at: new Date().toISOString(),
    };
    setState(s);
    await commitCommand(pid, rev(), "project.update_settings",
      { settings: { first_film: s } }, "first_film");
    await useApp.getState().refreshProjectData();
  }, [pid, rev]);

  const commit = useCallback(async (ct: string, payload: Record<string, unknown>) => {
    if (!pid) throw new Error("no project");
    return commitCommand(pid, rev(), ct, payload, "first_film");
  }, [pid, rev]);

  const go = useCallback(async (step: number) => {
    if (!state) return;
    await save({ ...state, step });
  }, [state, save]);

  const refreshTimeline = useCallback(async (tid: string) => {
    if (!pid) return;
    setTlDetail(await apiFetch<TimelineDetail>(`/api/projects/${pid}/timelines/${tid}`));
  }, [pid]);

  useEffect(() => {
    if (state?.timeline_id) void refreshTimeline(state.timeline_id);
  }, [state?.timeline_id, refreshTimeline]);

  // shot → its storyboard row (prompt/title for display + generation)
  const shotsById = useMemo(() => {
    const m = new Map<string, { title: string; prompt: string; sceneId: string }>();
    for (const sc of board) for (const sh of sc.shots) {
      m.set(sh.shot.id, { title: sh.shot.title, prompt: sh.shot.prompt, sceneId: sc.scene.id });
    }
    return m;
  }, [board]);

  if (!project || !state) {
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

  const stepId = STEPS[state.step].id as StepId;
  const ready = stepReady(state, stepId);
  const lastStep = state.step === STEPS.length - 1;
  const job = state.export_job_id ? jobs[state.export_job_id] : undefined;

  return (
    <div className="mx-auto max-w-3xl p-6">
      <header className="mb-4">
        <h1 className="text-xl font-bold">Make your first film</h1>
        <p className="mt-1 text-sm text-muted">
          Step {state.step + 1} of {STEPS.length} — {STEPS[state.step].label}
          {!STEPS[state.step].required && <span className="ml-2 text-faint">(optional)</span>}
        </p>
        <div className="mt-3 flex gap-1" role="list" aria-label="Progress">
          {STEPS.map((s, i) => (
            <button
              key={s.id}
              role="listitem"
              aria-label={`Step ${i + 1}: ${s.label}${i === state.step ? " (current)" : ""}`}
              aria-current={i === state.step ? "step" : undefined}
              onClick={() => i <= state.furthest && void go(i)}
              disabled={i > state.furthest}
              title={s.label}
              className={clsx(
                "h-2 flex-1 rounded-full",
                i < state.step ? "bg-teal" : i === state.step ? "bg-action" : "bg-card-hi",
                i <= state.furthest ? "cursor-pointer" : "cursor-default opacity-60",
              )}
            />
          ))}
        </div>
      </header>

      {notices.length > 0 && (
        <div className="mb-3 rounded border border-amber/40 bg-amber/10 p-2 text-sm text-amber">
          {notices.map((n, i) => <p key={i}>{n}</p>)}
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
        {stepId === "setup" && (
          <SetupStep state={state} busy={busy === "setup"} onSave={async (film) => {
            setBusy("setup"); setErr(null);
            try {
              if (film.name !== project.name) {
                await commit("project.rename", { name: film.name });
              }
              await commit("project.update_settings", {
                aspect_ratio: film.aspect_ratio,
                fps_num: film.fps_num, fps_den: film.fps_den,
              });
              await save({ ...state, film });
            } catch (e) { fail(e); return; }
            setBusy(null); void go(state.step + 1);
          }} />
        )}
        {stepId === "characters" && (
          <CharactersStep state={state} characters={characters} busy={busy === "char"}
            onCreate={async (name, summary) => {
              setBusy("char"); setErr(null);
              try { await commit("character.create", { name, summary, data: {} }); }
              catch (e) { fail(e); return; }
              const chars = await apiFetch<Entity[]>(`/api/projects/${pid}/character`);
              setCharacters(chars.filter((x) => x.status === "active"));
              const c = chars.filter((x) => x.status === "active").find((x) => x.name === name);
              if (c) setState({ ...state, character_ids: [...state.character_ids, c.id] });
              setBusy(null);
            }}
            onToggle={(id) => setState({
              ...state,
              character_ids: state.character_ids.includes(id)
                ? state.character_ids.filter((x) => x !== id)
                : [...state.character_ids, id],
            })} />
        )}
        {stepId === "scene" && (
          <SceneStep state={state} board={board} busy={busy === "scene"}
            onSelect={(id) => setState({ ...state, scene_id: id })}
            onCreate={async (title, description) => {
              setBusy("scene"); setErr(null);
              try { await commit("scene.create", { title, description }); }
              catch (e) { fail(e); return; }
              const b = await apiFetch<StoryboardScene[]>(`/api/projects/${pid}/storyboard`);
              setBoard(b);
              const sc = b.find((x) => x.scene.title === title);
              if (sc) setState({ ...state, scene_id: sc.scene.id });
              setBusy(null);
            }} />
        )}
        {stepId === "shots" && (
          <ShotsStep state={state} shotsById={shotsById} characters={characters} busy={busy === "shot"}
            onCreate={async (title, prompt, characterIds) => {
              setBusy("shot"); setErr(null);
              try {
                await commit("shot.create", {
                  scene_id: state.scene_id, title, prompt, shot_type: "medium",
                });
                const b = await apiFetch<StoryboardScene[]>(`/api/projects/${pid}/storyboard`);
                setBoard(b);
                const sc = b.find((x) => x.scene.id === state.scene_id);
                const sh = sc?.shots.find((x) => x.shot.title === title);
                if (sh) {
                  for (const cid of characterIds) {
                    await commit("shot.cast.assign",
                      { shot_id: sh.shot.id, character_id: cid });
                  }
                  setState({ ...state, shot_ids: [...state.shot_ids, sh.shot.id] });
                }
              } catch (e) { fail(e); return; }
              setBusy(null);
            }}
            onRemove={async (shotId) => {
              setBusy("shot"); setErr(null);
              try { await commit("shot.archive", { shot_id: shotId }); }
              catch (e) { fail(e); return; }
              const { [shotId]: _drop, ...rest } = state.shot_assets;
              setState({ ...state, shot_ids: state.shot_ids.filter((x) => x !== shotId), shot_assets: rest });
              await refreshAll(); setBusy(null);
            }} />
        )}
        {stepId === "method" && (
          <MethodStep state={state} methods={methods}
            onPick={(m, detail) => setState({ ...state, method: m, method_detail: detail })} />
        )}
        {stepId === "generate" && (
          <GenerateStep state={state} shotsById={shotsById}
            pid={pid!} commitFn={commit}
            onGenerated={async (shotId, assetId) => {
              await save({ ...state, shot_assets: { ...state.shot_assets, [shotId]: assetId } });
            }}
            onSkip={() => void go(state.step + 1)}
            onError={fail} />
        )}
        {stepId === "media" && (
          <MediaStep state={state} shotsById={shotsById} assets={assets}
            busy={busy} importJobs={importJobs}
            onCancel={async (shotId) => {
              const j = importJobs[shotId];
              if (!j) return;
              await apiFetch(`/api/jobs/${j.jobId}/cancel`, { method: "POST" })
                .catch(() => {});
            }}
            onImport={async (shotId, path) => {
              setBusy(`imp-${shotId}`); setErr(null);
              try {
                const name = path.split(/[\\/]/).pop() || "clip";
                const r = await commit("asset.import",
                  { source_path: path, name, kind: "video" });
                setImportJobs((x) => ({ ...x, [shotId]: { jobId: r.job_id!, message: "Importing…" } }));
                const done = await pollJob(pid!, r.job_id!, undefined,
                  (j) => setImportJobs((x) => x[shotId]
                    ? { ...x, [shotId]: { jobId: j.id, message: j.message || "Importing…" } }
                    : x));
                const ap = await apiFetch<FFAssets>(`/api/projects/${pid}/assets?limit=200`);
                setAssets(ap.items);
                const a = ap.items.find((x) => x.id === done.output_asset_id)
                  ?? ap.items.find((x) => x.name === name);
                if (a) {
                  setState({ ...state, shot_assets: { ...state.shot_assets, [shotId]: a.id } });
                } else {
                  throw new Error("Import finished but the asset isn't visible — check the assets list.");
                }
              } catch (e) { fail(e); return; }
              finally {
                setImportJobs((x) => { const { [shotId]: _d, ...rest } = x; return rest; });
              }
              setBusy(null);
            }}
            onPickAsset={(shotId, assetId) =>
              setState({ ...state, shot_assets: { ...state.shot_assets, [shotId]: assetId } })} />
        )}
        {stepId === "timeline" && (
          <TimelineStep state={state} shotsById={shotsById} assets={assets} busy={busy === "tl"}
            onBuild={async () => {
              setBusy("tl"); setErr(null);
              try {
                let tid = state.timeline_id;
                if (!tid) {
                  await commit("timeline.create", {
                    name: state.film.name || "My film",
                    fps_num: state.film.fps_num, fps_den: state.film.fps_den,
                  });
                  const tls = await apiFetch<Timeline[]>(`/api/projects/${pid}/timelines`);
                  setTimelines(tls);
                  tid = [...tls].reverse().find((t) => t.status === "active")?.id ?? null;
                }
                if (!tid) throw new Error("timeline was not created");
                const detail = await apiFetch<TimelineDetail>(`/api/projects/${pid}/timelines/${tid}`);
                // Dedup on (shot, asset) pairs — re-running Build is
                // idempotent, but two shots may legitimately share one file.
                const have = new Set(
                  detail.clips.map((c) => `${c.shot_id}:${c.source_asset_id}`));
                for (const shotId of state.shot_ids) {
                  const aid = state.shot_assets[shotId];
                  if (!aid || have.has(`${shotId}:${aid}`)) continue;
                  await commit("timeline.clip.insert",
                    { timeline_id: tid, asset_id: aid, shot_id: shotId });
                }
                await save({ ...state, timeline_id: tid });
                await refreshTimeline(tid);
              } catch (e) { fail(e); return; }
              setBusy(null); void go(state.step + 1);
            }} />
        )}
        {stepId === "arrange" && (
          <ArrangeStep detail={tlDetail} assets={assets} busy={busy === "arr"}
            onMove={async (clipId, to) => {
              setBusy("arr"); setErr(null);
              try {
                await commit("timeline.clip.move",
                  { timeline_id: state.timeline_id, clip_id: clipId, to_index: to });
                await refreshTimeline(state.timeline_id!);
              } catch (e) { fail(e); return; }
              setBusy(null);
            }}
            onTrim={async (clipId, inF, outF) => {
              setBusy("arr"); setErr(null);
              try {
                await commit("timeline.clip.trim",
                  { timeline_id: state.timeline_id, clip_id: clipId, in_frame: inF, out_frame: outF });
                await refreshTimeline(state.timeline_id!);
              } catch (e) { fail(e); return; }
              setBusy(null);
            }} />
        )}
        {stepId === "profile" && (
          <ProfileStep state={state}
            onPick={(profile) => setState({ ...state, profile })} />
        )}
        {stepId === "export" && (
          <ExportStep state={state} job={job} busy={busy === "export"}
            onExport={async () => {
              setBusy("export"); setErr(null);
              try {
                const p = await previewCommand(pid!, "timeline.export",
                  { timeline_id: state.timeline_id, profile: state.profile });
                const rt = p.resolved_timeline;
                const r = await commit("timeline.export", {
                  timeline_id: state.timeline_id, profile: state.profile,
                  expected_timeline: rt ? { id: rt.id, revision: rt.revision } : undefined,
                  expected_edl_sha: rt?.edl_sha256,
                });
                await save({ ...state, export_job_id: r.job_id ?? null });
              } catch (e) { fail(e); return; }
              setBusy(null);
            }}
            onFinish={async () => { await save({ ...state, done: true }); }}
            onOpenTimeline={() => setTab("timeline")}
            onOpenAssets={() => setTab("assets")} />
        )}
      </div>

      <footer className="mt-4 flex items-center justify-between">
        <button
          onClick={() => void go(state.step - 1)}
          disabled={state.step === 0 || busy !== null}
          className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg disabled:opacity-40"
        >
          ← Back
        </button>
        <div className="flex items-center gap-3 text-xs text-faint">
          <button onClick={() => setTab("storyboard")} className="underline hover:text-fg">
            Open advanced storyboard editor
          </button>
          <button onClick={() => setTab("timeline")} className="underline hover:text-fg">
            Open advanced timeline
          </button>
        </div>
        {!lastStep ? (
          <button
            onClick={() => void go(state.step + 1)}
            disabled={!ready || busy !== null}
            className="rounded-md bg-action px-5 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
          >
            Continue →
          </button>
        ) : (
          <span />
        )}
      </footer>
    </div>
  );
}
