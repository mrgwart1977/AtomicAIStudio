/**
 * A6 — first-cut assembly for the selected timeline. Computes a
 * deterministic proposal from ordered scenes/shots, selected motion
 * clips, dialogue segments, and shot-linked audio — then applies it
 * non-destructively through the command system:
 *
 *   assembly.plan   → preview the full proposal (lanes, items, missing
 *                     media, conflicts) → approve → commit
 *   assembly.apply  → preview the decision walk (insert/keep/skip/
 *                     replace per item) → approve → commit; undo is a
 *                     single timeline step
 *   assembly.review → per-item or per-scene approve/reject/skip + notes
 *   assembly.revert → restore a prior plan revision
 *
 * Manual timeline edits are always preserved by default; removing them
 * requires the explicit "replace" approval. Assembly never renders or
 * evaluates media — it records edit decisions.
 */
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../api/client";
import { commitCommand, previewCommand } from "../../api/commands";
import { useApp } from "../../stores/appStore";
import type {
  AssemblyApplyPreview, AssemblyItem, AssemblyPlan,
  AssemblyProposal, EntityRevision,
} from "../../api/types";

const SEL = "rounded border border-line bg-ink px-2 py-1 text-xs";
const INP = SEL + " w-full";
const LBL = "text-[10px] uppercase tracking-wide text-faint";
const BTN = "rounded border border-line px-2 py-0.5 text-[10px] text-muted hover:text-teal disabled:opacity-40";

const KIND_LABEL: Record<string, string> = {
  shot: "Shot", dialogue: "Line", audio: "Audio"};

function flagBadges(it: {
  flags: AssemblyItem["flags"];
  stale?: boolean; source_gone?: boolean;
}) {
  const out: { text: string; cls: string }[] = [];
  if (it.flags.missing) {
    out.push({ text: `⚠ ${it.flags.missing}`, cls: "text-amber" });
  }
  if (it.flags.removed) {
    out.push({ text: "⚠ removed from cut", cls: "text-amber" });
  }
  if ((it.flags.conflicts ?? []).length > 0) {
    out.push({ text: `⚠ ${it.flags.conflicts!.length} conflict(s)`,
               cls: "text-amber" });
  }
  if (it.stale) {
    out.push({ text: "stale — source changed", cls: "text-amber" });
  }
  if (it.source_gone) {
    out.push({ text: "⚠ source gone", cls: "text-danger" });
  }
  return out;
}

function reviewChip(status: string) {
  const cls = status === "approved" ? "text-teal"
    : status === "rejected" ? "text-danger"
    : status === "skipped" ? "text-faint" : "text-muted";
  return <span className={`text-[10px] ${cls}`}>{status}</span>;
}

export function AssemblyPanel({ projectId, timeline, onChanged }: {
  projectId: string;
  timeline: { id: string; name: string };
  onChanged?: () => void;
}) {
  const [plan, setPlan] = useState<AssemblyPlan | null>(null);
  const [noPlan, setNoPlan] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [planPreview, setPlanPreview] =
    useState<AssemblyProposal | null>(null);
  const [applyPreview, setApplyPreview] =
    useState<AssemblyApplyPreview | null>(null);
  const [replaceApproved, setReplaceApproved] = useState(false);
  const [noteFor, setNoteFor] = useState<Record<string, string>>({});
  const [versions, setVersions] = useState<EntityRevision[] | null>(null);
  const [detail, setDetail] = useState(false);

  const load = useCallback(async () => {
    try {
      const plans = await apiFetch<AssemblyPlan[]>(
        `/api/projects/${projectId}/assemblies?timeline_id=${timeline.id}`);
      if (plans.length === 0) { setPlan(null); setNoPlan(true); return; }
      setNoPlan(false);
      setPlan(await apiFetch<AssemblyPlan>(
        `/api/projects/${projectId}/assemblies/${plans[0].id}`));
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, [projectId, timeline.id]);

  useEffect(() => { void load(); }, [load]);

  async function commit(commandType: string,
                        payload: Record<string, unknown>) {
    const rev = useApp.getState().currentProject()?.revision;
    if (rev === undefined) {
      setErr("Project revision unavailable.");
      return false;
    }
    setErr(null); setBusy(true);
    try {
      await commitCommand(projectId, rev, commandType, payload,
                          "timeline");
      await load();
      onChanged?.();
      return true;
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      return false;
    } finally { setBusy(false); }
  }

  async function previewPlan() {
    setErr(null); setBusy(true); setApplyPreview(null);
    try {
      const p = await previewCommand(projectId, "assembly.plan",
        { timeline_id: timeline.id });
      const ra = p.resolved_assembly;
      setPlanPreview(ra && "lanes" in ra ? ra : null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  }

  async function commitPlan() {
    setPlanPreview(null);
    await commit("assembly.plan", { timeline_id: timeline.id });
  }

  async function previewApply(replace: boolean) {
    setErr(null); setBusy(true); setPlanPreview(null);
    try {
      const p = await previewCommand(projectId, "assembly.apply", {
        timeline_id: timeline.id,
        resolve_conflicts: replace ? "replace" : "skip"});
      const ra = p.resolved_assembly;
      setApplyPreview(ra && "sources_stale" in ra ? ra : null);
      setReplaceApproved(replace);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  }

  async function commitApply() {
    const rc = replaceApproved ? "replace" : "skip";
    setApplyPreview(null);
    await commit("assembly.apply",
                 { timeline_id: timeline.id, resolve_conflicts: rc });
  }

  async function review(it: AssemblyItem, status: string) {
    const note = (noteFor[it.id] ?? "").trim();
    await commit("assembly.review", {
      plan_id: plan!.id, item_ids: [it.id], status,
      ...(note ? { note } : {})});
  }

  async function reviewScene(sceneId: string, status: string) {
    await commit("assembly.review", {
      plan_id: plan!.id, scene_id: sceneId, status });
  }

  const items = plan?.items ?? [];
  const sceneIds = [...new Set(items.map((i) => i.scene_id)
    .filter((s): s is string => !!s))];
  const counts = plan?.counts;
  const pendingCount = items.filter(
    (i) => i.review_status === "pending" && !i.flags.removed).length;

  return (
    <section className="mt-3 rounded-md border border-line bg-ink/40 p-3"
      aria-label="First-cut assembly" data-testid="assembly-panel">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold text-fg">First cut</h3>
        <div className="flex items-center gap-1">
          {plan && (
            <button type="button" className={BTN}
              onClick={() => setDetail((v) => !v)}
              aria-expanded={detail}>
              {detail ? "Hide details" : "Details"}
            </button>)}
          {plan && (
            <button type="button" className={BTN}
              disabled={busy}
              onClick={() => {
                if (versions !== null) { setVersions(null); return; }
                void apiFetch<EntityRevision[]>(
                  `/api/projects/${projectId}/assemblies/${plan.id}/versions`)
                  .then(setVersions).catch(() => setVersions([]));
              }}>
              {versions ? "Hide history" : "History"}
            </button>)}
          <button type="button"
            className="rounded bg-action px-2 py-0.5 text-[10px] text-white disabled:opacity-40"
            disabled={busy}
            onClick={() => void previewPlan()}>
            {noPlan ? "Build first cut" : "Re-plan"}
          </button>
        </div>
      </div>
      <p className="mt-0.5 text-[10px] text-muted">
        Assembles scenes, shots, dialogue and linked audio into edit
        decisions — deterministic, non-destructive. Your manual timeline
        edits are preserved; nothing renders here.
      </p>
      {err && (
        <p className="mt-1 text-[11px] text-danger" role="alert">{err}</p>)}

      {/* ---------- plan preview ---------- */}
      {planPreview && (
        <div className="mt-2 rounded border border-teal/40 bg-panel p-2"
          role="dialog" aria-label="First-cut plan preview">
          <p className="text-[10px] uppercase tracking-wide text-teal">
            Proposed first cut — nothing changes until you approve
          </p>
          <p className="mt-1 text-[11px] text-fg">
            {planPreview.counts.items} item(s)
            {planPreview.counts.missing > 0 &&
              ` · ${planPreview.counts.missing} missing media`}
            {planPreview.counts.conflicts > 0 &&
              ` · ${planPreview.counts.conflicts} conflict(s)`}
            {planPreview.counts.removed > 0 &&
              ` · ${planPreview.counts.removed} removed`}
          </p>
          <p className="mt-0.5 text-[10px] text-faint">
            Lanes:{" "}
            {Object.entries(planPreview.lanes).map(([k, l]) => (
              <span key={k} className="mr-2">
                {l.track_name ?? l.create?.name ?? k}
                {l.create && <span className="text-teal"> (new)</span>}
              </span>))}
          </p>
          {planPreview.warnings.length > 0 && (
            <ul className="mt-1 max-h-24 space-y-0.5 overflow-y-auto">
              {planPreview.warnings.map((w, i) => (
                <li key={i} className="text-[10px] text-amber">⚠ {w}</li>
              ))}
            </ul>)}
          <ul className="mt-1 max-h-40 space-y-0.5 overflow-y-auto">
            {planPreview.items.map((it) => (
              <li key={it.key}
                className="flex items-center gap-2 text-[11px]">
                <span className="w-10 shrink-0 text-faint">
                  {KIND_LABEL[it.kind] ?? it.kind}</span>
                <span className="min-w-0 flex-1 truncate text-fg">
                  {it.title ?? it.key}</span>
                <span className="text-[10px] text-faint">
                  @{it.start_frames}f · {it.extent_frames}f
                  {it.lane_key ? ` · ${it.lane_key}` : ""}</span>
                {it.flags.missing && (
                  <span className="text-[10px] text-amber">
                    ⚠ {it.flags.missing}</span>)}
                {(it.flags.conflicts ?? []).length > 0 && (
                  <span className="text-[10px] text-amber">⚠ conflict</span>)}
                {it.applied?.state === "edited" && (
                  <span className="text-[10px] text-amber">
                    manually edited — kept</span>)}
              </li>
            ))}
          </ul>
          <div className="mt-2 flex gap-2">
            <button type="button" onClick={() => void commitPlan()}
              className="rounded bg-teal px-2 py-1 text-[11px] font-medium text-ink">
              Approve plan
            </button>
            <button type="button" onClick={() => setPlanPreview(null)}
              className="rounded border border-line px-2 py-1 text-[11px] text-muted">
              Cancel
            </button>
          </div>
        </div>)}

      {/* ---------- apply preview ---------- */}
      {applyPreview && (
        <div className="mt-2 rounded border border-teal/40 bg-panel p-2"
          role="dialog" aria-label="Apply preview">
          <p className="text-[10px] uppercase tracking-wide text-teal">
            Apply preview — per-item decisions
          </p>
          {applyPreview.sources_stale ? (
            <p className="mt-1 text-[11px] text-amber" role="status">
              ⚠ Sources changed since this plan was computed — commit
              will refuse. Re-plan first.
            </p>
          ) : (
            <p className="mt-1 text-[11px] text-fg">
              {applyPreview.counts.insert} insert
              {" · "}{applyPreview.counts.keep} keep
              {" · "}{applyPreview.counts.skip} skip
              {applyPreview.counts.replace > 0 &&
                ` · ${applyPreview.counts.replace} replaced manual clip(s)`}
            </p>)}
          <ul className="mt-1 max-h-40 space-y-0.5 overflow-y-auto">
            {applyPreview.items.map((it) => (
              <li key={it.key}
                className="flex items-center gap-2 text-[11px]">
                <span className={`w-12 shrink-0 text-[10px] ${
                  it.action === "insert" ? "text-teal"
                  : it.action === "keep" ? "text-faint"
                  : it.action === "replace" ? "text-amber"
                  : "text-muted"}`}>{it.action}</span>
                <span className="w-10 shrink-0 text-faint">
                  {KIND_LABEL[it.kind] ?? it.kind}</span>
                <span className="min-w-0 flex-1 truncate text-fg">
                  {it.title}</span>
                <span className="text-[10px] text-faint">{it.reason}</span>
              </li>
            ))}
          </ul>
          <div className="mt-2 flex gap-2">
            <button type="button" onClick={() => void commitApply()}
              disabled={applyPreview.sources_stale}
              className="rounded bg-teal px-2 py-1 text-[11px] font-medium text-ink disabled:opacity-40">
              Apply to timeline
            </button>
            <button type="button" onClick={() => setApplyPreview(null)}
              className="rounded border border-line px-2 py-1 text-[11px] text-muted">
              Cancel
            </button>
          </div>
        </div>)}

      {/* ---------- current plan ---------- */}
      {plan && (
        <div className="mt-2">
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            <span className="font-medium text-fg">{plan.name}</span>
            <span className="text-faint">r{plan.revision} · {plan.status}</span>
            {plan.sources_stale && (
              <span className="text-amber" role="status">
                ⚠ sources changed — re-plan for a fresh proposal</span>)}
            {counts && (
              <span className="text-faint">
                {counts.items} items
                {counts.missing > 0 && ` · ${counts.missing} missing`}
                {counts.conflicts > 0 && ` · ${counts.conflicts} conflicts`}
                {counts.stale > 0 && ` · ${counts.stale} stale`}
                {pendingCount > 0 && ` · ${pendingCount} to review`}
              </span>)}
            <span className="ml-auto flex gap-1">
              <button type="button"
                className="rounded bg-action px-2 py-0.5 text-[10px] text-white disabled:opacity-40"
                disabled={busy || plan.sources_stale === true}
                title={plan.sources_stale
                  ? "Sources changed — re-plan before applying"
                  : "Preview the apply decisions"}
                onClick={() => void previewApply(false)}>
                Apply…
              </button>
              {(counts?.conflicts ?? 0) > 0 && (
                <button type="button" className={BTN}
                  disabled={busy}
                  title="Preview apply with manual-clip replacement approved"
                  onClick={() => void previewApply(true)}>
                  Apply replacing manual clips…
                </button>)}
            </span>
          </div>

          {detail && sceneIds.length > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className={LBL}>Scene review</span>
              {sceneIds.map((sid, i) => (
                <span key={sid} className="flex items-center gap-1">
                  <span className="text-[10px] text-faint">
                    Scene {i + 1}</span>
                  <button type="button" className={BTN}
                    aria-label={`Approve scene ${i + 1}`}
                    disabled={busy}
                    onClick={() => void reviewScene(sid, "approved")}>
                    Approve</button>
                  <button type="button" className={BTN}
                    aria-label={`Reject scene ${i + 1}`}
                    disabled={busy}
                    onClick={() => void reviewScene(sid, "rejected")}>
                    Reject</button>
                </span>))}
            </div>)}

          {detail && (
            <ul className="mt-2 space-y-1" aria-label="Assembly items">
              {items.map((it) => {
                const badges = flagBadges(it);
                const gone = it.review_status !== "pending";
                return (
                  <li key={it.id}
                    className="rounded border border-line bg-panel px-2 py-1 text-[11px]">
                    <div className="flex items-center gap-2">
                      <span className="w-10 shrink-0 text-faint">
                        {KIND_LABEL[it.kind] ?? it.kind}</span>
                      <span className="min-w-0 flex-1 truncate text-fg">
                        {it.key.split(":").slice(1).join(":").slice(0, 8)
                          || it.key}</span>
                      <span className="text-[10px] text-faint">
                        @{it.start_frames}f · {it.extent_frames}f</span>
                      {it.on_timeline && (
                        <span className="text-[10px] text-teal">
                          on timeline</span>)}
                      {badges.map((b, i) => (
                        <span key={i} className={`text-[10px] ${b.cls}`}
                          role="status">{b.text}</span>))}
                      {reviewChip(it.review_status)}
                      <span className="ml-auto flex gap-1">
                        {!gone && !it.flags.removed && (
                          <>
                            <button type="button" className={BTN}
                              aria-label={`Approve item`}
                              disabled={busy}
                              onClick={() => void review(it, "approved")}>
                              Approve</button>
                            <button type="button" className={BTN}
                              aria-label={`Reject item`}
                              disabled={busy}
                              onClick={() => void review(it, "rejected")}>
                              Reject</button>
                            <button type="button" className={BTN}
                              aria-label={`Skip item`}
                              disabled={busy}
                              onClick={() => void review(it, "skipped")}>
                              Skip</button>
                          </>)}
                        {gone && (
                          <button type="button" className={BTN}
                            aria-label="Reset item to pending"
                            disabled={busy}
                            onClick={() => void review(it, "pending")}>
                            Reset</button>)}
                      </span>
                    </div>
                    <div className="mt-1 flex items-center gap-1">
                      <input className={INP + " max-w-64"}
                        aria-label="Review note"
                        placeholder="Revision note (optional)"
                        value={noteFor[it.id] ?? it.notes}
                        onChange={(e) => setNoteFor((s) => ({
                          ...s, [it.id]: e.target.value}))} />
                    </div>
                  </li>);
              })}
            </ul>)}

          {versions && (
            <ul className="mt-2 space-y-1" aria-label="Plan history">
              {versions.length === 0 && (
                <li className="text-[10px] text-faint">No revisions.</li>)}
              {versions.map((v) => (
                <li key={v.revision}
                  className="flex items-center gap-2 rounded border border-line bg-panel px-2 py-1 text-[11px]">
                  <span className="text-faint">r{v.revision}</span>
                  <span className="min-w-0 flex-1 truncate text-muted">
                    {v.summary}</span>
                  {v.revision !== plan.revision && (
                    <button type="button" className={BTN}
                      disabled={busy}
                      onClick={() => void commit("assembly.revert", {
                        plan_id: plan.id, to_revision: v.revision })}>
                      Revert</button>)}
                </li>
              ))}
            </ul>)}
        </div>)}

      {!plan && !noPlan && (
        <p className="mt-2 text-[10px] text-faint">Loading plan…</p>)}
    </section>
  );
}
