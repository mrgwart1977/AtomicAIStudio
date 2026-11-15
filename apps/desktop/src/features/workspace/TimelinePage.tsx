import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../api/client";
import { commitCommand, previewCommand } from "../../api/commands";
import type {
  CommandProposal, Timeline, TimelineClip, TimelineDetail,
  TimelineRevision,
} from "../../api/types";
import { useApp } from "../../stores/appStore";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { TimelineEditor } from "./TimelineEditor";
import { exportMode } from "./timelineMath";

/** C5-A + N1-B — timeline assembly. Multitrack lane editor (ruler,
 * playhead, drag placement), trim/remove inspector, provenance,
 * revision history, deterministic EDL view, single-video export.
 * Assembly only — no rendering, transitions, or audio mixing. */

function fmtS(s: number): string {
  return `${Math.round(s * 100) / 100}s`;
}

export function TimelinePage() {
  const project = useApp((s) => s.currentProject());
  const refreshProject = useApp((s) => s.refreshProjectData);
  const [timelines, setTimelines] = useState<Timeline[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<TimelineDetail | null>(null);
  const [revisions, setRevisions] = useState<TimelineRevision[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [confirm, setConfirm] =
    useState<{ title: string; body: string; run: () => void } | null>(null);
  const [showHistory, setShowHistory] = useState(false);
  const [showEdl, setShowEdl] = useState(false);
  const [edl, setEdl] = useState<string | null>(null);
  const [edlVersion, setEdlVersion] = useState<1 | 2>(1);
  const [renaming, setRenaming] = useState(false);
  const [renameTo, setRenameTo] = useState("");
  const [showExport, setShowExport] = useState(false);
  const [exportProfile, setExportProfile] = useState("source");
  const [exportPending, setExportPending] =
    useState<CommandProposal | null>(null);
  const [exportJobId, setExportJobId] = useState<string | null>(null);

  const refreshList = useCallback(async () => {
    if (!project) return;
    const tls = await apiFetch<Timeline[]>(
      `/api/projects/${project.id}/timelines`);
    setTimelines(tls);
  }, [project]);

  const refreshDetail = useCallback(async () => {
    if (!project || !selected) { setDetail(null); return; }
    const d = await apiFetch<TimelineDetail>(
      `/api/projects/${project.id}/timelines/${selected}`);
    setDetail(d);
    const revs = await apiFetch<TimelineRevision[]>(
      `/api/projects/${project.id}/timelines/${selected}/revisions`);
    setRevisions(revs);
  }, [project, selected]);

  // Project switching must not carry a timeline selection across —
  // the old id doesn't exist in the new project (and state like open
  // panels/confirms are stale with it).
  useEffect(() => {
    setSelected(null); setDetail(null); setRevisions([]);
    setShowEdl(false); setEdl(null); setShowHistory(false);
    setShowExport(false); setExportPending(null); setExportJobId(null);
    setError(null); setConfirm(null);
  }, [project?.id]);

  useEffect(() => {
    setLoading(true);
    refreshList()
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
      .finally(() => setLoading(false));
  }, [refreshList]);

  useEffect(() => {
    refreshDetail()
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [refreshDetail]);

  async function run(commandType: string, payload: Record<string, unknown>) {
    if (!project) return false;
    setError(null); setBusy(true);
    try {
      const rev = useApp.getState().currentProject()?.revision
        ?? project.revision;
      await commitCommand(project.id, rev, commandType, payload, "timeline");
      await Promise.all([refreshList(), refreshDetail(), refreshProject()]);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally { setBusy(false); }
  }

  async function createTimeline() {
    const name = newName.trim();
    if (!name) return;
    if (await run("timeline.create", { name })) {
      setCreating(false); setNewName("");
    }
  }

  async function toggleEdl() {
    if (showEdl) { setShowEdl(false); return; }
    if (!project || !selected) return;
    try {
      const e = await apiFetch<Record<string, unknown>>(
        `/api/projects/${project.id}/timelines/${selected}/edl`);
      setEdl(JSON.stringify(e, null, 2));
      setEdlVersion(1);
      setShowEdl(true);
    } catch {
      // v1 only represents a single sequential video lane — a multitrack
      // or gapped layout is honest data, so fall back to ade-edl/2.
      try {
        const e2 = await apiFetch<Record<string, unknown>>(
          `/api/projects/${project.id}/timelines/${selected}/edl?version=2`);
        setEdl(JSON.stringify(e2, null, 2));
        setEdlVersion(2);
        setShowEdl(true);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
  }

  async function previewExport() {
    if (!project || !selTl) return;
    setError(null); setBusy(true); setExportJobId(null);
    try {
      const p = await previewCommand(project.id, "timeline.export",
        { timeline_id: selTl.id, profile: exportProfile });
      setExportPending(p);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setExportPending(null);
    } finally { setBusy(false); }
  }

  async function runExport() {
    if (!project || !selTl || !exportPending) return;
    setError(null); setBusy(true);
    try {
      const rev = useApp.getState().currentProject()?.revision
        ?? project.revision;
      const rt = exportPending.resolved_timeline ?? undefined;
      const r = await commitCommand(project.id, rev, "timeline.export", {
        timeline_id: selTl.id,
        profile: exportProfile,
        expected_timeline: rt && { id: rt.id, revision: rt.revision },
        expected_edl_sha: rt?.edl_sha256,
      }, "timeline");
      setExportJobId(r.job_id);
      setExportPending(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setExportPending(null);
    } finally { setBusy(false); }
  }

  const active = useMemo(
    () => timelines.filter((t) => t.status === "active"), [timelines]);
  const archived = useMemo(
    () => timelines.filter((t) => t.status === "archived"), [timelines]);
  const selTl = timelines.find((t) => t.id === selected) ?? null;
  const exportModeInfo = useMemo(
    () => (detail ? exportMode(detail)
                  : { mode: "blocked" as const, lanes: 0,
                      reason: "no timeline" }),
    [detail]);
  // A 422 about revisions/stale approvals deserves a one-click refresh.
  const staleError = !!error && /stale|revision|expected|re-preview/i.test(error);

  if (loading) {
    return <p className="p-6 text-sm text-muted">Loading timelines…</p>;
  }

  return (
    <div className="flex h-full" data-testid="timeline-page">
      {/* timeline list */}
      <aside className="w-60 shrink-0 border-r border-line p-3">
        <div className="flex items-center justify-between">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-faint">
            Timelines
          </h2>
          <button
            onClick={() => setCreating(true)}
            className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-teal"
          >+ New</button>
        </div>
        {creating && (
          <div className="mt-2 rounded border border-line bg-card p-2">
            <input
              autoFocus
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") createTimeline();
                if (e.key === "Escape") setCreating(false);
              }}
              placeholder="Timeline name"
              aria-label="Timeline name"
              className="w-full rounded border border-line bg-panel px-2 py-1 text-xs"
            />
            <div className="mt-1.5 flex gap-1">
              <button onClick={createTimeline} disabled={busy || !newName.trim()}
                className="rounded bg-action px-2 py-0.5 text-[11px] text-white disabled:opacity-40">Create</button>
              <button onClick={() => setCreating(false)}
                className="rounded border border-line px-2 py-0.5 text-[11px] text-muted">Cancel</button>
            </div>
          </div>
        )}
        {timelines.length === 0 && !creating && (
          <p className="mt-3 text-xs text-muted">
            No timelines yet. Create one, then send generated clips here
            from the Storyboard.
          </p>
        )}
        <ul className="mt-2 space-y-0.5" aria-label="Timelines">
          {active.map((t) => (
            <li key={t.id}>
              <button
                onClick={() => { setSelected(t.id); setShowEdl(false); }}
                aria-current={selected === t.id ? "true" : undefined}
                className={`w-full rounded px-2 py-1.5 text-left text-sm ${
                  selected === t.id
                    ? "bg-card text-teal"
                    : "text-muted hover:bg-panel hover:text-fg"}`}
              >
                {t.name}
                <span className="ml-1 text-[10px] text-faint">r{t.revision}</span>
              </button>
            </li>
          ))}
        </ul>
        {archived.length > 0 && (
          <>
            <h3 className="mt-3 text-[10px] font-semibold uppercase text-faint">
              Archived
            </h3>
            <ul className="mt-1 space-y-0.5">
              {archived.map((t) => (
                <li key={t.id}>
                  <button
                    onClick={() => { setSelected(t.id); setShowEdl(false); }}
                    className="w-full rounded px-2 py-1 text-left text-xs text-faint hover:bg-panel"
                  >{t.name}</button>
                </li>
              ))}
            </ul>
          </>
        )}
      </aside>

      {/* selected timeline */}
      <main className="min-w-0 flex-1 overflow-y-auto p-4">
        {error && (
          <p role="alert" className="mb-3 flex items-start gap-2 rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
            <span className="min-w-0 flex-1">{error}</span>
            {staleError && (
              <button
                onClick={() => {
                  setError(null);
                  refreshDetail().catch(() => {});
                }}
                className="shrink-0 rounded border border-red-400/50 px-2 py-0.5 text-[10px] text-red-200 hover:text-white"
              >Refresh timeline</button>
            )}
          </p>
        )}
        {!selTl && (
          <div className="mt-16 text-center text-sm text-muted">
            <p>Select a timeline, or create one to start assembling your cut.</p>
          </div>
        )}
        {selTl && detail && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              {renaming ? (
                <span className="flex items-center gap-1">
                  <input
                    autoFocus
                    value={renameTo}
                    onChange={(e) => setRenameTo(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" && renameTo.trim()) {
                        run("timeline.update", {
                          timeline_id: selTl.id, name: renameTo.trim(),
                        }).then((ok) => ok && setRenaming(false));
                      }
                      if (e.key === "Escape") setRenaming(false);
                    }}
                    aria-label="Rename timeline"
                    className="rounded border border-line bg-panel px-2 py-1 text-sm"
                  />
                  <button
                    onClick={() => renameTo.trim() && run("timeline.update", {
                      timeline_id: selTl.id, name: renameTo.trim(),
                    }).then((ok) => ok && setRenaming(false))}
                    className="rounded bg-action px-2 py-1 text-xs text-white"
                  >Save</button>
                </span>
              ) : (
                <h2 className="text-base font-semibold">{selTl.name}</h2>
              )}
              <span className="text-xs text-faint">
                {detail.clips.length} clip{detail.clips.length === 1 ? "" : "s"}
                {" · "}{fmtS(detail.total_duration_s)} total
                {" · "}{selTl.fps_num}/{selTl.fps_den} fps
                {selTl.status === "archived" ? " · archived" : ""}
              </span>
              {detail.clips.length > 0 && (
                <span
                  title={exportModeInfo.mode === "blocked"
                    ? exportModeInfo.reason ?? undefined
                    : exportModeInfo.mode === "sequential"
                      ? "This cut is a single sequential video lane — it exports via the fast path."
                      : `Multitrack composite — ${exportModeInfo.lanes} visible lane(s) render over black; hidden and muted lanes contribute nothing.`}
                  className={`rounded border px-1.5 py-0.5 text-[10px] ${
                    exportModeInfo.mode === "blocked"
                      ? "border-line text-faint"
                      : "border-teal/50 text-teal"}`}
                  data-testid="export-capability"
                >
                  {exportModeInfo.mode === "sequential"
                    ? "Exports as one video"
                    : exportModeInfo.mode === "composite"
                      ? `Exports ${exportModeInfo.lanes} lanes — composite`
                      : "Nothing visible to export"}
                </span>
              )}
              <div className="ml-auto flex gap-1">
                <button onClick={() => { setRenaming(true); setRenameTo(selTl.name); }}
                  className="rounded border border-line px-2 py-1 text-[11px] text-muted hover:text-fg">Rename</button>
                <button onClick={() => setConfirm({
                  title: "Duplicate timeline",
                  body: `Copy "${selTl.name}" with all its clips?`,
                  run: () => { setConfirm(null); run("timeline.duplicate", { timeline_id: selTl.id }); },
                })}
                  className="rounded border border-line px-2 py-1 text-[11px] text-muted hover:text-fg">Duplicate</button>
                <button onClick={toggleEdl}
                  className="rounded border border-line px-2 py-1 text-[11px] text-muted hover:text-fg">
                  {showEdl ? "Hide EDL" : "Edit decision list"}
                </button>
                {selTl.status === "active" && detail.clips.length > 0 && (
                  <button onClick={() => { setShowExport((v) => !v); setExportPending(null); }}
                    className="rounded border border-line px-2 py-1 text-[11px] text-muted hover:text-fg">
                    Export…
                  </button>
                )}
                <button onClick={() => setShowHistory((v) => !v)}
                  className="rounded border border-line px-2 py-1 text-[11px] text-muted hover:text-fg">
                  {showHistory ? "Hide history" : "History"}
                </button>
                <button onClick={() => setConfirm(selTl.status === "active" ? {
                  title: "Archive timeline",
                  body: `"${selTl.name}" will be hidden from pickers. Clips and history are kept.`,
                  run: () => { setConfirm(null); run("timeline.archive", { timeline_id: selTl.id }); },
                } : {
                  title: "Restore timeline",
                  body: `Bring "${selTl.name}" back to active?`,
                  run: () => { setConfirm(null); run("timeline.unarchive", { timeline_id: selTl.id }); },
                })}
                  className="rounded border border-line px-2 py-1 text-[11px] text-muted hover:text-fg">
                  {selTl.status === "active" ? "Archive" : "Restore"}
                </button>
              </div>
            </div>

            {showExport && (
              <div className="mt-3 rounded border border-line bg-card p-3"
                data-testid="export-panel">
                <p className="text-xs text-muted">
                  {exportModeInfo.mode === "composite"
                    ? `Render this cut to a single MP4 — ${exportModeInfo.lanes} visible lane(s) composite over black, timeline gaps render black, muted lanes contribute no audio.`
                    : "Render this cut to a single MP4 video file."}
                </p>
                {exportModeInfo.mode === "blocked" && (
                  <p className="mt-1.5 rounded border border-line bg-panel px-2 py-1 text-[11px] text-muted"
                    data-testid="export-blocked">
                    This layout can't export: {exportModeInfo.reason}
                  </p>
                )}
                <div className="mt-2 flex flex-wrap items-center gap-2">
                  <label className="text-[11px] text-muted">Profile
                    <select value={exportProfile}
                      onChange={(e) => { setExportProfile(e.target.value); setExportPending(null); }}
                      aria-label="Export profile"
                      className="ml-1 rounded border border-line bg-panel px-2 py-1 text-xs">
                      <option value="source">Source quality</option>
                      <option value="1080p">1080p H.264 MP4</option>
                      <option value="720p">720p H.264 MP4</option>
                    </select>
                  </label>
                  <button disabled={busy || exportModeInfo.mode === "blocked"}
                    onClick={() => void previewExport()}
                    className="rounded border border-line px-2 py-1 text-[11px] text-muted hover:text-teal disabled:opacity-40">
                    Preview export
                  </button>
                </div>
                {exportPending && (
                  <div className="mt-2 rounded border border-teal/40 bg-teal/5 p-2 text-[11px] text-muted"
                    data-testid="export-preview">
                    <p>
                      Export {exportPending.resolved_timeline?.clip_count ?? 0} clip(s)
                      as {exportProfile} · EDL pinned
                      {" "}{exportPending.resolved_timeline?.edl_sha256?.slice(0, 10)}…
                    </p>
                    <p className="mt-0.5 text-faint">
                      Any timeline edit after this point cancels the export request.
                    </p>
                    <button disabled={busy} onClick={() => void runExport()}
                      className="mt-1.5 rounded bg-action px-2.5 py-1 text-[11px] text-white disabled:opacity-40">
                      Start export
                    </button>
                  </div>
                )}
                {exportJobId && (
                  <p className="mt-2 text-[11px] text-teal" data-testid="export-started">
                    Export queued — watch progress in the Jobs panel.
                  </p>
                )}
              </div>
            )}
            <TimelineEditor
              detail={detail}
              active={selTl.status === "active"}
              busy={busy}
              onRun={run}
              onRemoveClip={(c: TimelineClip, ripple: boolean) => setConfirm({
                title: "Remove clip",
                body: ripple
                  ? "Remove this clip? Later clips on the same lane shift left to close the gap. The source asset is not deleted."
                  : "Remove this clip from the timeline? A gap is left behind. The source asset is not deleted.",
                run: () => { setConfirm(null); run("timeline.clip.remove", {
                  timeline_id: selTl.id, clip_id: c.id,
                  ...(ripple ? { ripple_scope: "track" } : {}),
                  expected_timeline: detail
                    ? { id: detail.id, revision: detail.revision }
                    : undefined }); },
              })}
            />

            {showEdl && edl && (
              <section className="mt-4" aria-label="Edit decision list">
                <h3 className="text-xs font-semibold uppercase text-faint">
                  ade-edl/{edlVersion} — deterministic edit decisions
                  {edlVersion === 2 ? " (multitrack layout)" : ""}
                </h3>
                <pre className="mt-1 max-h-72 overflow-auto rounded border border-line bg-card p-3 text-[11px] text-muted">
                  {edl}
                </pre>
              </section>
            )}
            {showHistory && (
              <section className="mt-4" aria-label="Revision history">
                <h3 className="text-xs font-semibold uppercase text-faint">
                  Revision history
                </h3>
                <ul className="mt-1 space-y-1">
                  {revisions.map((r) => (
                    <li key={r.revision}
                      className="flex items-center gap-2 rounded border border-line bg-card px-2 py-1 text-xs">
                      <span className="text-faint">r{r.revision}</span>
                      <span className="min-w-0 flex-1 truncate text-muted">{r.summary}</span>
                      {r.revision !== selTl.revision && (
                        <button
                          onClick={() => setConfirm({
                            title: "Revert timeline",
                            body: `Revert "${selTl.name}" to revision ${r.revision}? Current state is kept as a new revision.`,
                            run: () => { setConfirm(null); run("timeline.revert", {
                              timeline_id: selTl.id, to_revision: r.revision }); },
                          })}
                          className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted hover:text-teal">
                          Revert to this
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              </section>
            )}
          </>
        )}
      </main>
      <ConfirmDialog
        open={confirm !== null}
        title={confirm?.title ?? ""}
        body={confirm?.body ?? ""}
        onConfirm={() => confirm?.run()}
        onOpenChange={() => setConfirm(null)}
      />
    </div>
  );
}
