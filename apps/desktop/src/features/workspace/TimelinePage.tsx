import { useCallback, useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../api/client";
import { commitCommand } from "../../api/commands";
import type {
  Timeline, TimelineClip, TimelineDetail, TimelineRevision,
} from "../../api/types";
import { useApp } from "../../stores/appStore";
import { ConfirmDialog } from "../../components/ConfirmDialog";

/** C5-A — timeline assembly. Ordered clip cards with trims, reorder,
 * removal, provenance, revision history, and a deterministic EDL
 * view. Assembly only — no rendering, transitions, or audio. */

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
  const [renaming, setRenaming] = useState(false);
  const [renameTo, setRenameTo] = useState("");

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
      setShowEdl(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  const active = useMemo(
    () => timelines.filter((t) => t.status === "active"), [timelines]);
  const archived = useMemo(
    () => timelines.filter((t) => t.status === "archived"), [timelines]);
  const selTl = timelines.find((t) => t.id === selected) ?? null;

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
          <p role="alert" className="mb-3 rounded border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs text-red-300">
            {error}
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

            {detail.clips.length === 0 && (
              <p className="mt-8 text-center text-sm text-muted">
                This timeline is empty. Generate a video clip on the
                Storyboard, then use “Send to timeline”.
              </p>
            )}
            <ol className="mt-4 flex flex-wrap gap-2" aria-label="Clips">
              {detail.clips.map((c, i) => (
                <ClipCard
                  key={c.id}
                  clip={c}
                  index={i}
                  count={detail.clips.length}
                  locked={selTl.status !== "active"}
                  busy={busy}
                  onMove={(to) => run("timeline.clip.move", {
                    timeline_id: selTl.id, clip_id: c.id, to_index: to })}
                  onRemove={() => setConfirm({
                    title: "Remove clip",
                    body: "Remove this clip from the timeline? The source asset is not deleted.",
                    run: () => { setConfirm(null); run("timeline.clip.remove", {
                      timeline_id: selTl.id, clip_id: c.id }); },
                  })}
                  onTrim={(inF, outF) => run("timeline.clip.trim", {
                    timeline_id: selTl.id, clip_id: c.id,
                    in_frame: inF, out_frame: outF })}
                />
              ))}
            </ol>

            {showEdl && edl && (
              <section className="mt-4" aria-label="Edit decision list">
                <h3 className="text-xs font-semibold uppercase text-faint">
                  ade-edl/1 — deterministic edit decisions
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

function ClipCard({ clip, index, count, locked, busy, onMove, onRemove, onTrim }: {
  clip: TimelineClip;
  index: number;
  count: number;
  locked: boolean;
  busy: boolean;
  onMove: (to: number) => Promise<boolean>;
  onRemove: () => void;
  onTrim: (inF: number, outF: number) => Promise<boolean>;
}) {
  const [editing, setEditing] = useState(false);
  const [inF, setInF] = useState(String(clip.in_frame));
  const [outF, setOutF] = useState(String(clip.out_frame));
  const dur = (clip.out_frame - clip.in_frame)
    * clip.src_fps_den / clip.src_fps_num;

  return (
    <li
      className="w-52 rounded border border-line bg-card p-2"
      data-testid="timeline-clip"
      aria-label={`Clip ${index + 1} of ${count}`}
    >
      <div className="flex items-center gap-1">
        <span className="text-[10px] text-faint">#{index + 1}</span>
        <span className="min-w-0 flex-1 truncate text-xs text-muted"
          title={`${clip.source_sha256.slice(0, 12)}… · ${clip.src_duration_s}s source`}>
          {clip.source_sha256.slice(0, 10)}…
        </span>
        {!locked && (
          <>
            <button disabled={busy || index === 0} onClick={() => onMove(index - 1)}
              aria-label="Move clip earlier"
              className="rounded border border-line px-1 text-[10px] text-muted hover:text-teal disabled:opacity-30">◀</button>
            <button disabled={busy || index === count - 1} onClick={() => onMove(index + 1)}
              aria-label="Move clip later"
              className="rounded border border-line px-1 text-[10px] text-muted hover:text-teal disabled:opacity-30">▶</button>
            <button disabled={busy} onClick={onRemove}
              aria-label="Remove clip"
              className="rounded border border-line px-1 text-[10px] text-muted hover:text-red-300 disabled:opacity-30">✕</button>
          </>
        )}
      </div>
      <div className="mt-1 text-[11px] text-muted">
        {fmtS(dur)} · frames {clip.in_frame}–{clip.out_frame}
        <span className="text-faint"> of {clip.src_frames}</span>
      </div>
      <div className="text-[10px] text-faint">
        {clip.src_fps_num}/{clip.src_fps_den} fps source
        {clip.shot_id ? " · from shot" : ""}
      </div>
      {!locked && !editing && (
        <button onClick={() => { setEditing(true); setInF(String(clip.in_frame)); setOutF(String(clip.out_frame)); }}
          className="mt-1.5 rounded border border-line px-1.5 py-0.5 text-[10px] text-muted hover:text-teal">
          Trim
        </button>
      )}
      {editing && (
        <div className="mt-1.5 space-y-1">
          <div className="flex gap-1 text-[10px]">
            <label className="flex-1 text-muted">In
              <input value={inF} onChange={(e) => setInF(e.target.value)}
                inputMode="numeric" aria-label="Trim in frame"
                className="mt-0.5 w-full rounded border border-line bg-panel px-1 py-0.5" />
            </label>
            <label className="flex-1 text-muted">Out
              <input value={outF} onChange={(e) => setOutF(e.target.value)}
                inputMode="numeric" aria-label="Trim out frame"
                className="mt-0.5 w-full rounded border border-line bg-panel px-1 py-0.5" />
            </label>
          </div>
          <div className="flex gap-1">
            <button
              disabled={busy}
              onClick={() => {
                const i = parseInt(inF, 10), o = parseInt(outF, 10);
                if (Number.isInteger(i) && Number.isInteger(o)) {
                  onTrim(i, o).then((ok) => { if (ok !== false) setEditing(false); });
                }
              }}
              className="rounded bg-action px-1.5 py-0.5 text-[10px] text-white disabled:opacity-40">
              Apply
            </button>
            <button onClick={() => setEditing(false)}
              className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted">
              Cancel
            </button>
          </div>
        </div>
      )}
    </li>
  );
}
