// C2.0d — cloud text generation UI. Everything reachable from this
// page is SIMULATED: provider adapters run on mocked transports and
// the spend ledger records synthetic entries only. The UI must never
// present any of it as a live provider connection or a real charge.
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../api/client";
import { commitCommand, previewCommand } from "../../api/commands";
import type {
  CommandProposal, Job, ProviderDetail, ProviderSummary,
  SpendReservation, SpendSummary,
} from "../../api/types";
import { useApp } from "../../stores/appStore";
import { AssistedPanel } from "./AssistedPanel";

const SIM_BANNER = "SIMULATION — No external AI provider contacted.";
const SCOPES = ["session", "day", "month", "lifetime"] as const;

const inputCls =
  "w-full rounded-md border border-line bg-card px-3 py-2 text-sm text-fg placeholder:text-faint";
const labelCls = "mb-1 block text-xs font-medium text-muted";
const btnCls =
  "rounded-md bg-action px-3 py-1.5 text-sm text-white hover:bg-action/80 disabled:opacity-40";

/** µUSD → display dollars. Financial values are always labeled by the
 * surrounding copy — this only formats magnitude. */
export function fmtMicro(micro: number | null | undefined): string {
  if (micro == null) return "—";
  const usd = micro / 1e6;
  return usd === 0 ? "$0" : usd < 0.0001 ? "<$0.0001" : `$${usd.toFixed(4)}`;
}

/** Mirrors backend cloud_cost.t1_text_strategy: A-class provider with a
 * documented T1 official_api strategy covering text.* — nothing else is
 * offered, regardless of credentials or enablement. */
function t1Eligible(d: ProviderDetail): boolean {
  return d.access_class === "A" && (d.strategies || []).some(
    (s) => s.method === "official_api" && s.tier === "T1"
      && (s.capabilities || []).some((c) => (c || "").startsWith("text")),
  );
}

/** Mirrors backend estimate_text_cost — heuristic tokens × catalog
 * rates. Labeled an estimate everywhere it renders. */
function estimateCost(prompt: string, model: ProviderDetail["models"][number] | undefined) {
  if (!model) return null;
  const inRate = model.cost_table?.input_per_mtok;
  const outRate = model.cost_table?.output_per_mtok;
  if (inRate == null || outRate == null) return null;
  const estIn = Math.max(1, Math.floor((prompt || "").length / 4));
  const usd = (estIn * inRate + 512 * outRate) / 1e6;
  return { usd, estIn, estOut: 512 };
}

function isRemoteJob(j: Job): boolean {
  return j.execution_scope === "remote" || !!j.progress_json?.remote_intent;
}

function jobOutcome(j: Job): string | null {
  return j.progress_json?.remote_outcome ?? null;
}

function needsReconcile(j: Job): boolean {
  return j.recovery_state === "needs_reconciliation"
    || j.progress_json?.reconcile_required === true
    || jobOutcome(j) === "ambiguous";
}

function SimBadge({ className = "" }: { className?: string }) {
  return (
    <p role="note" className={`rounded border border-amber/50 bg-amber/10 px-2.5 py-1.5 text-xs font-semibold uppercase tracking-wide text-amber ${className}`}>
      {SIM_BANNER}
    </p>
  );
}

/** Preview → approve → commit panel shared by every mutation on this
 * page (generation, budgets, reconciliation). */
interface Pending {
  title: string;
  commandType: string;
  payload: Record<string, unknown>;
  proposal: CommandProposal;
  after: () => void;
}

export function CloudPage() {
  const pid = useApp((s) => s.currentProjectId);
  const project = useApp((s) => s.currentProject());
  const jobs = useApp((s) => s.jobs);

  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [details, setDetails] = useState<Record<string, ProviderDetail>>({});
  const [simSummary, setSimSummary] = useState<SpendSummary | null>(null);
  const [liveSummary, setLiveSummary] = useState<SpendSummary | null>(null);
  const [reservations, setReservations] = useState<SpendReservation[]>([]);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  // generate form
  const [providerKey, setProviderKey] = useState("");
  const [modelId, setModelId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [maxCost, setMaxCost] = useState("");
  const [cloudTarget, setCloudTarget] = useState(false); // explicit opt-in
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const reload = () => {
    if (!pid) return;
    setLoadErr(null);
    apiFetch<ProviderSummary[]>("/api/providers?category=text")
      .then(async (list) => {
        setProviders(list);
        const ds = await Promise.all(
          list.map((p) =>
            apiFetch<ProviderDetail>(`/api/providers/${p.key}`)
              .then((d) => [p.key, d] as const).catch(() => null)),
        );
        setDetails(Object.fromEntries(ds.filter((d): d is NonNullable<typeof d> => !!d)));
      })
      .catch((e) => setLoadErr(`providers: ${String(e)}`));
    apiFetch<SpendSummary>(`/api/projects/${pid}/spend/summary?environment=simulated`)
      .then(setSimSummary).catch((e) => setLoadErr(`simulated spend: ${String(e)}`));
    apiFetch<SpendSummary>(`/api/projects/${pid}/spend/summary?environment=live`)
      .then(setLiveSummary).catch((e) => setLoadErr(`live spend: ${String(e)}`));
    apiFetch<SpendReservation[]>(`/api/projects/${pid}/spend/reservations`)
      .then(setReservations).catch((e) => setLoadErr(`reservations: ${String(e)}`));
  };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(reload, [pid]);

  const eligible = useMemo(
    () => providers.filter((p) => details[p.key] && t1Eligible(details[p.key])),
    [providers, details],
  );
  const detail = providerKey ? details[providerKey] : undefined;
  const models = useMemo(
    () => (detail?.models || []).filter((m) => (m.capability || "").startsWith("text")),
    [detail],
  );
  const model = models.find((m) => m.id === modelId);
  const est = useMemo(() => estimateCost(prompt, model), [prompt, model]);
  const maxCostNum = parseFloat(maxCost);

  const remoteJobs = useMemo(
    () => Object.values(jobs).filter(isRemoteJob)
      .sort((a, b) => b.created_at.localeCompare(a.created_at)),
    [jobs],
  );
  const reconcileJobs = useMemo(() => remoteJobs.filter(needsReconcile), [remoteJobs]);
  // Open holds whose job row is unavailable (outside the visible job
  // window or otherwise missing). They stay visible — never silently
  // released, hidden, or auto-reconciled — and still count into the
  // Reserved totals above.
  const orphans = useMemo(
    () => reservations.filter((r) => r.status === "open"
      && !reconcileJobs.some((j) => j.id === r.job_id)),
    [reservations, reconcileJobs],
  );

  /** Stage a mutation: preview must succeed before the commit control
   * appears — approval is never skipped. */
  const preview = (title: string, commandType: string,
                   payload: Record<string, unknown>, after: () => void) => {
    if (!pid) return;
    setBusy(true); setError(null); setNotice(null);
    previewCommand(pid, commandType, payload)
      .then((proposal) => setPending({ title, commandType, payload, proposal, after }))
      .catch((e) => setError(`Preview failed: ${String(e)}`))
      .finally(() => setBusy(false));
  };

  const commit = () => {
    if (!pid || !project || !pending) return;
    setBusy(true); setError(null);
    commitCommand(pid, project.revision, pending.commandType, pending.payload)
      .then((r) => {
        setNotice(`Committed ${pending.commandType}${r.job_id ? ` — job ${r.job_id.slice(0, 8)}` : ""}`);
        setPending(null); pending.after(); reload();
      })
      .catch((e) => setError(`Commit failed: ${String(e)}`))
      .finally(() => setBusy(false));
  };

  const genPayload = () => ({
    provider_key: providerKey,
    model_id: modelId || undefined,
    prompt,
    execution_target: "cloud",
    max_cost_usd: maxCostNum,
  });
  const genReady = !!providerKey && !!prompt.trim() && cloudTarget
    && Number.isFinite(maxCostNum) && maxCostNum >= 0;

  if (!pid || !project) {
    return <div className="p-10 text-center text-sm text-muted">Open a project to use Cloud Studio.</div>;
  }

  return (
    <div className="mx-auto max-w-5xl p-6" data-testid="cloud-page">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">Cloud Studio — text generation</h1>
      </div>
      <SimBadge className="mt-3" />
      <p className="mt-2 text-xs text-muted">
        All providers below are <b>simulated API integrations</b> — the documented request shape,
        lifecycle, and spend accounting are exercised against mocked transports only.
      </p>

      {loadErr && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{loadErr}</p>}
      {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}
      {notice && <p className="mt-3 rounded border border-teal/40 bg-teal/10 p-2 text-sm text-teal">{notice}</p>}

      {/* ---------- approval panel (any command) ---------- */}
      {pending && (
        <section className="mt-4 rounded-md border border-amber/60 bg-card p-4" data-testid="command-approval">
          <h2 className="text-sm font-semibold">Review — {pending.title}</h2>
          <SimBadge className="mt-2" />
          {pending.commandType === "generate.text.cloud" && (
            <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 rounded border border-line bg-panel p-2.5 text-xs" data-testid="gen-approval-detail">
              <div className="flex justify-between"><dt className="text-faint">Provider</dt><dd className="text-fg">{String(pending.payload.provider_key)}</dd></div>
              <div className="flex justify-between"><dt className="text-faint">Model</dt><dd className="text-fg">{String(pending.payload.model_id ?? "provider default")}</dd></div>
              <div className="flex justify-between"><dt className="text-faint">Simulation status</dt><dd className="text-amber">simulated — mocked transport</dd></div>
              <div className="flex justify-between"><dt className="text-faint">Max estimated cost</dt><dd className="text-fg">${String(pending.payload.max_cost_usd)}</dd></div>
              <div className="flex justify-between"><dt className="text-faint">Execution target</dt><dd className="text-fg">execution_target=&quot;{String(pending.payload.execution_target)}&quot;</dd></div>
              <div className="flex justify-between"><dt className="text-faint">Data out</dt><dd className="text-fg">prompt text (~{String(pending.payload.prompt ?? "").length} chars) — nothing else sent</dd></div>
            </dl>
          )}
          <p className="mt-2 text-sm text-fg">{pending.proposal.summary}</p>
          {pending.proposal.warnings.length > 0 && (
            <ul className="mt-2 list-inside list-disc text-xs text-amber">
              {pending.proposal.warnings.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          )}
          {pending.proposal.estimated_cost && (
            <p className="mt-2 text-xs text-muted">
              Estimated cost: <b>${pending.proposal.estimated_cost.currency_usd}</b>
              {" "}(estimate only — not a charge)
            </p>
          )}
          <div className="mt-3 flex gap-2">
            <button onClick={commit} disabled={busy} className={btnCls}>
              Approve &amp; commit
            </button>
            <button onClick={() => setPending(null)} disabled={busy}
              className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-fg">
              Cancel
            </button>
          </div>
        </section>
      )}

      {/* ---------- generate ---------- */}
      <section className="mt-4 rounded-md border border-line bg-card p-4">
        <h2 className="text-sm font-semibold">Generate text (simulated cloud)</h2>
        <div className="mt-3 grid grid-cols-2 gap-3">
          <div>
            <label className={labelCls} htmlFor="cloud-provider">Provider</label>
            <select id="cloud-provider" className={inputCls} value={providerKey}
              onChange={(e) => { setProviderKey(e.target.value); setModelId(""); }}>
              <option value="">Choose provider…</option>
              {eligible.map((p) => (
                <option key={p.key} value={p.key}>{p.label} (simulated API)</option>
              ))}
            </select>
            {eligible.length === 0 && providers.length > 0 && (
              <p className="mt-1 text-xs text-faint">No T1-eligible text providers in the catalog.</p>
            )}
          </div>
          <div>
            <label className={labelCls} htmlFor="cloud-model">Model</label>
            <select id="cloud-model" className={inputCls} value={modelId}
              onChange={(e) => setModelId(e.target.value)} disabled={!providerKey}>
              <option value="">Default model</option>
              {models.map((m) => <option key={m.id} value={m.id}>{m.id}</option>)}
            </select>
          </div>
        </div>
        <div className="mt-3">
          <label className={labelCls} htmlFor="cloud-prompt">Prompt</label>
          <textarea id="cloud-prompt" className={`${inputCls} min-h-24`} value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="Write the scene beat…" />
        </div>
        <div className="mt-3 grid grid-cols-2 gap-3">
          <div>
            <label className={labelCls} htmlFor="cloud-maxcost">Max estimated cost (USD) — required cap</label>
            <input id="cloud-maxcost" className={inputCls} type="number" min="0" step="0.01"
              value={maxCost} onChange={(e) => setMaxCost(e.target.value)} placeholder="0.05" />
          </div>
          <div className="flex flex-col justify-end">
            <p className="text-xs text-muted" data-testid="cost-estimate">
              Estimated cost: <b>{est ? `$${est.usd.toFixed(6)}` : "unknown (no catalog rates)"}</b>
              {est && <span className="text-faint"> — ~{est.estIn} in / {est.estOut} out tokens, heuristic estimate</span>}
            </p>
          </div>
        </div>
        <label className="mt-3 flex items-start gap-2 text-sm text-fg">
          <input type="checkbox" className="mt-0.5" checked={cloudTarget}
            onChange={(e) => setCloudTarget(e.target.checked)} data-testid="cloud-target" />
          <span>
            Run this prompt against the selected <b>cloud provider</b>
            (execution_target=&quot;cloud&quot;) — simulated transport; the provider is never contacted.
          </span>
        </label>
        <p className="mt-2 rounded border border-line bg-panel p-2 text-xs text-muted" data-testid="data-out">
          Data leaving this device (if this were a live call):{" "}
          <b>prompt text (~{prompt.length} chars){detail ? ` → ${detail.label}` : ""}</b>.
          No project files, bibles, or credentials are sent.
        </p>
        <div className="mt-3">
          <button className={btnCls} disabled={!genReady || busy}
            onClick={() => preview("Generate cloud text", "generate.text.cloud", genPayload(), () => {})}>
            Preview request
          </button>
        </div>
      </section>

      {/* ---------- remote job lifecycle ---------- */}
      <section className="mt-4 rounded-md border border-line bg-card p-4">
        <h2 className="text-sm font-semibold">Remote jobs</h2>
        {remoteJobs.length === 0 && (
          <p className="mt-2 text-xs text-muted">No cloud generations yet.</p>
        )}
        <ul className="mt-3 space-y-3">
          {remoteJobs.map((j) => <RemoteJobCard key={j.id} job={j} />)}
        </ul>
      </section>

      {/* ---------- C1.5 assisted text workflows ---------- */}
      <AssistedPanel pid={pid} revision={project.revision}
        providers={providers} details={details} onCommitted={reload} />

      {/* ---------- budgets ---------- */}
      <section className="mt-4 rounded-md border border-line bg-card p-4">
        <h2 className="text-sm font-semibold">Spend &amp; budgets</h2>
        <div className="mt-3 grid grid-cols-2 gap-4">
          <SpendView title="Simulated accounting" synthetic summary={simSummary} />
          <SpendView title="Live accounting" summary={liveSummary}
            emptyNote="No live provider connectivity exists in this build — this view is informational only." />
        </div>
        <BudgetManager
          budgets={simSummary?.budgets ?? []}
          onPreview={(title, ctype, payload, after) => preview(title, ctype, payload, after)}
          busy={busy}
        />
        {orphans.length > 0 && (
          <p className="mt-3 rounded border border-amber/40 bg-amber/10 p-2 text-xs text-amber" data-testid="orphan-spend-note">
            {orphans.length} open hold{orphans.length > 1 ? "s" : ""} totaling{" "}
            {fmtMicro(orphans.reduce((a, r) => a + r.reserved_micro, 0))} (synthetic)
            {" "}whose job row is unavailable — included in Reserved above;
            see the Reconciliation queue. These holds are never released automatically.
          </p>
        )}
      </section>

      {/* ---------- reconciliation ---------- */}
      <section className="mt-4 rounded-md border border-line bg-card p-4">
        <h2 className="text-sm font-semibold">Reconciliation queue</h2>
        <p className="mt-1 text-xs text-muted">
          Jobs whose remote outcome is ambiguous or was interrupted hold a spend
          reservation until explicitly reconciled. All actions below are
          <b> synthetic</b> — no provider billing is confirmed or denied.
        </p>
        {reconcileJobs.length === 0 && (
          <p className="mt-2 text-xs text-muted">Nothing awaiting reconciliation.</p>
        )}
        <ul className="mt-3 space-y-2">
          {reconcileJobs.map((j) => (
            <ReconcileRow key={j.id} job={j}
              reservation={reservations.find((r) => r.job_id === j.id)}
              onPreview={preview} busy={busy} />
          ))}
        </ul>
        {orphans.map((r) => (
          <div key={r.id}
            className="mt-2 rounded border border-amber/50 bg-amber/5 p-3 text-xs"
            data-testid={`orphan-${r.id}`}>
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
              <span className="text-fg"><b>{r.provider_key}</b></span>
              <span className="text-muted">job {r.job_id.slice(0, 8)}</span>
              <span className="text-muted">
                held exposure: <b>{fmtMicro(r.reserved_micro)}</b>
                {r.environment === "simulated" ? " (synthetic)" : ""}
              </span>
              <span className="text-amber">hold open{ r.reconcile_count ? ` · reconciled ×${r.reconcile_count}` : ""}</span>
            </div>
            <p className="mt-1.5 text-faint">
              The associated job is unavailable — this hold cannot be inspected
              here and <b>requires manual recovery</b>. It is not released or
              reconciled automatically; recovery uses the existing
              <code> spend.reconcile</code> command with the full job id
              <code> {r.job_id}</code>.
            </p>
          </div>
        ))}
      </section>
    </div>
  );
}

// ---------- remote job card ----------

function RemoteJobCard({ job }: { job: Job }) {
  const d = job.progress_json;
  const intent = d?.remote_intent;
  const lifecycle = intent?.lifecycle ?? "mock";
  const outcome = jobOutcome(job);
  const [cancelMsg, setCancelMsg] = useState<string | null>(null);
  const running = job.status === "running" || job.status === "queued";
  // Cancellation is offered only where the provider documents it —
  // sync lifecycles (Anthropic Messages) have none; interruption is
  // handled by reconciliation instead.
  const cancellable = running && !job.cancel_requested && lifecycle !== "sync";

  const cancel = () => {
    apiFetch<{ status?: string; note?: string }>(`/api/jobs/${job.id}/cancel`, { method: "POST" })
      .then((r) => setCancelMsg(r.note || r.status || "cancel requested"))
      .catch((e) => setCancelMsg(`Cancel failed: ${String(e)}`));
  };

  const steps: { label: string; state: "done" | "active" | "off" | "bad" }[] = [
    { label: "Intent recorded", state: intent ? "done" : "off" },
    { label: `Submitted${d?.remote_job_id
          ? " (synthetic remote id)"
          : d?.remote_status ? " (no remote id — sync contract)" : ""}`,
      state: d?.remote_job_id || d?.remote_status ? "done"
          : running ? "active" : "off" },
    { label: "Provider polling",
      state: lifecycle === "async_poll" || lifecycle === "mock"
        ? (d?.remote_polls || running ? (job.status === "running" ? "active" : "done") : "off")
        : "off" },
    { label: needsReconcile(job) ? "Needs reconciliation"
        : job.status === "succeeded" ? "Completed"
        : job.status === "cancelled" ? "Cancelled"
        : job.status === "failed" ? "Failed" : "Outcome pending",
      state: needsReconcile(job) ? "bad"
        : job.status === "succeeded" ? "done"
        : job.status === "cancelled" ? "done"
        : job.status === "failed" ? "bad" : "off" },
  ];

  return (
    <li className="rounded-md border border-line bg-panel p-3" data-testid={`remote-job-${job.id}`}>
      <SimBadge />
      <div className="mt-2 flex items-center justify-between text-xs">
        <span className="text-muted">
          {intent?.provider_key ?? job.adapter_id}
          {intent?.model_id ? ` · ${intent.model_id}` : ""}
          {intent?.lifecycle ? ` · ${intent.lifecycle}` : ""}
        </span>
        <span className="text-faint">{job.id.slice(0, 8)}</span>
      </div>
      <ol className="mt-2 space-y-1">
        {steps.map((s, i) => (
          <li key={i} className="flex items-center gap-2 text-xs">
            <span className={
              s.state === "done" ? "text-teal"
              : s.state === "active" ? "text-amber"
              : s.state === "bad" ? "text-danger"
              : "text-faint"
            }>
              {s.state === "done" ? "●" : s.state === "active" ? "◐" : s.state === "bad" ? "▲" : "○"}
            </span>
            <span className={s.state === "off" ? "text-faint" : "text-fg"}>{s.label}</span>
          </li>
        ))}
      </ol>
      {outcome && (
        <p className="mt-2 text-xs text-muted">
          remote outcome: <b className={outcome === "ambiguous" ? "text-amber" : "text-fg"}>{outcome}</b>
          {d?.remote_status ? ` · provider status: ${d.remote_status}` : ""}
          {d?.remote_job_id ? ` · remote id: ${d.remote_job_id} (synthetic)` : ""}
        </p>
      )}
      {d?.usage_json && (
        <p className="mt-1 text-[10px] text-faint">
          usage (provider-reported, simulated): {JSON.stringify(d.usage_json)}
        </p>
      )}
      {job.error && <p className="mt-1 text-xs text-danger">{job.error}</p>}
      <div className="mt-2 flex items-center gap-2">
        {running && (
          cancellable ? (
            <button onClick={cancel}
              className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-danger">
              {job.cancel_requested ? "Cancelling…" : "Cancel"}
            </button>
          ) : (
            <span className="text-[10px] text-faint" data-testid="no-cancel">
              Provider documents no single-message cancellation — reconcile instead.
            </span>
          )
        )}
        {cancelMsg && <span className="text-[10px] text-amber">{cancelMsg}</span>}
      </div>
    </li>
  );
}

// ---------- spend views ----------

function SpendView({ title, summary, synthetic, emptyNote }:
  { title: string; summary: SpendSummary | null; synthetic?: boolean; emptyNote?: string }) {
  return (
    <div className="rounded-md border border-line bg-panel p-3">
      <div className="flex items-center justify-between">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">{title}</h3>
        {synthetic && <span className="rounded bg-amber/15 px-1.5 py-0.5 text-[10px] font-semibold uppercase text-amber">synthetic</span>}
      </div>
      {emptyNote && <p className="mt-1 text-[10px] text-faint">{emptyNote}</p>}
      {!summary ? (
        <p className="mt-2 text-xs text-faint">loading…</p>
      ) : (
        <table className="mt-2 w-full text-xs">
          <thead>
            <tr className="text-left text-faint">
              <th className="pb-1 pr-2 font-medium">Scope</th>
              <th className="pb-1 pr-2 font-medium">Limit</th>
              <th className="pb-1 pr-2 font-medium">Reserved</th>
              <th className="pb-1 pr-2 font-medium">Charges</th>
              <th className="pb-1 font-medium">Available</th>
            </tr>
          </thead>
          <tbody>
            {SCOPES.map((sc) => {
              const row = summary.scopes.find((r) => r.scope === sc);
              const avail = row?.limit_micro != null && row
                ? row.limit_micro - row.consumed_micro : null;
              return (
                <tr key={sc} className="border-t border-line/60">
                  <td className="py-1 pr-2 text-fg">{sc}</td>
                  <td className="py-1 pr-2 text-muted">{row?.limit_micro != null ? fmtMicro(row.limit_micro) : "no cap"}</td>
                  <td className="py-1 pr-2 text-muted">{fmtMicro(row?.open_micro)}</td>
                  <td className="py-1 pr-2 text-muted">{fmtMicro(row?.charged_micro)}</td>
                  <td className="py-1 text-muted">
                    {avail == null ? "—" : fmtMicro(avail)}
                    {avail != null && avail < 0 ? " (over)" : ""}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      <p className="mt-2 text-[10px] text-faint">
        Reserved = open spend holds · Charges = settled entries
        {synthetic ? " · all values synthetic" : ""}
      </p>
    </div>
  );
}

function BudgetManager({ budgets, onPreview, busy }:
  { budgets: SpendSummary["budgets"];
    onPreview: (t: string, c: string, p: Record<string, unknown>, a: () => void) => void;
    busy: boolean }) {
  const [target, setTarget] = useState<"project" | "global">("project");
  const [scope, setScope] = useState("day");
  const [limit, setLimit] = useState("");
  const [label, setLabel] = useState("");
  const limitNum = parseFloat(limit);

  const group = (t: "project" | "global") =>
    budgets.filter((b) => b.target === t)
      .sort((a, b) => SCOPES.indexOf(a.scope as typeof SCOPES[number]) - SCOPES.indexOf(b.scope as typeof SCOPES[number]));

  const row = (b: SpendSummary["budgets"][number]) => (
    <li key={b.id} className="flex items-center justify-between rounded border border-line bg-card px-2 py-1 text-xs">
      <span>
        <b className="text-fg">{b.scope}</b> — {fmtMicro(b.limit_micro)}
        {b.label ? ` · ${b.label}` : ""}
        <span className="text-faint"> (simulated-ledger cap)</span>
      </span>
      <button className="text-muted hover:text-danger" disabled={busy}
        onClick={() => onPreview(`Remove ${b.target} ${b.scope} budget`, "spend.budget_remove",
          { scope: b.scope, target: b.target }, () => {})}>
        Remove
      </button>
    </li>
  );

  return (
    <div className="mt-4 grid grid-cols-2 gap-4">
      <div>
        <h4 className="text-xs font-semibold text-muted">Project budgets</h4>
        <ul className="mt-1.5 space-y-1">
          {group("project").map(row)}
          {group("project").length === 0 && <li className="text-xs text-faint">No project caps.</li>}
        </ul>
        <h4 className="mt-3 text-xs font-semibold text-muted">Global budgets (all cloud spend)</h4>
        <ul className="mt-1.5 space-y-1">
          {group("global").map(row)}
          {group("global").length === 0 && <li className="text-xs text-faint">No global caps.</li>}
        </ul>
      </div>
      <div className="rounded-md border border-line bg-panel p-3">
        <h4 className="text-xs font-semibold text-muted">Set a budget cap</h4>
        <div className="mt-2 grid grid-cols-2 gap-2">
          <div>
            <label className={labelCls} htmlFor="budget-target">Applies to</label>
            <select id="budget-target" className={inputCls} value={target}
              onChange={(e) => setTarget(e.target.value as "project" | "global")}>
              <option value="project">This project</option>
              <option value="global">Global (all projects)</option>
            </select>
          </div>
          <div>
            <label className={labelCls} htmlFor="budget-scope">Scope</label>
            <select id="budget-scope" className={inputCls} value={scope} onChange={(e) => setScope(e.target.value)}>
              {SCOPES.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
          </div>
        </div>
        <div className="mt-2">
          <label className={labelCls} htmlFor="budget-limit">Limit (USD)</label>
          <input id="budget-limit" className={inputCls} type="number" min="0" step="0.01" value={limit}
            onChange={(e) => setLimit(e.target.value)} placeholder="5.00" />
        </div>
        <div className="mt-2">
          <label className={labelCls} htmlFor="budget-label">Label (optional)</label>
          <input id="budget-label" className={inputCls} value={label} onChange={(e) => setLabel(e.target.value)} />
        </div>
        <button className={`${btnCls} mt-3`} disabled={busy || !Number.isFinite(limitNum) || limitNum < 0}
          onClick={() => onPreview(`Set ${target} ${scope} budget`, "spend.budget_set",
            { scope, limit_usd: limitNum, target, label: label || undefined }, () => {})}>
          Preview budget change
        </button>
      </div>
    </div>
  );
}

// ---------- reconciliation ----------

function ReconcileRow({ job, reservation, onPreview, busy }:
  { job: Job; reservation?: SpendReservation;
    onPreview: (t: string, c: string, p: Record<string, unknown>, a: () => void) => void;
    busy: boolean }) {
  const [mode, setMode] = useState<null | "confirmed_charge" | "confirmed_no_charge" | "still_unknown">(null);
  const [actualUsd, setActualUsd] = useState("");
  const [acceptOverage, setAcceptOverage] = useState(false);
  const [note, setNote] = useState("");
  const d = job.progress_json;
  const env = reservation?.environment ?? "simulated";
  // Live billing confirmation is out of scope for C2.0d — simulated
  // rows only; a live row (shouldn't exist) renders read-only.
  // Terminal reservations offer no actions: the money question is
  // settled and further reconcile calls only replay idempotently.
  const resolved = reservation != null && reservation.status !== "open";
  const actionable = env === "simulated" && !resolved;
  const actualNum = parseFloat(actualUsd);

  const go = (outcome: "confirmed_charge" | "confirmed_no_charge" | "still_unknown") => {
    const payload: Record<string, unknown> = { job_id: job.id, outcome };
    if (outcome === "confirmed_charge") {
      payload.actual_usd = actualNum;
      if (acceptOverage) payload.accept_overage = true;
    }
    if (note.trim()) payload.note = note.trim();
    onPreview(`Reconcile job ${job.id.slice(0, 8)}`, "spend.reconcile", payload, () => {});
  };

  return (
    <li className="rounded-md border border-line bg-panel p-3" data-testid={`reconcile-${job.id}`}>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs">
        <span className="text-fg"><b>{d?.remote_intent?.provider_key ?? reservation?.provider_key ?? "?"}</b></span>
        <span className="text-muted">job {job.id.slice(0, 8)}</span>
        <span className="text-muted">outcome: <b className="text-amber">{jobOutcome(job) ?? job.recovery_state ?? "unknown"}</b></span>
        <span className="text-muted">
          reserved: <b>{reservation ? fmtMicro(reservation.reserved_micro) : "—"}</b>
          {env === "simulated" ? " (synthetic)" : ""}
        </span>
        <span className="text-muted">
          status: {reservation ? reservation.status : "open (no row)"}
          {reservation?.reconcile_count ? ` · reconciled ×${reservation.reconcile_count}` : ""}
        </span>
      </div>
      {env !== "simulated" && (
        <p className="mt-2 text-[10px] text-faint">
          Live-environment row — reconciliation disabled in this build (no live billing workflows).
        </p>
      )}
      {env === "simulated" && resolved && (
        <p className="mt-2 text-[10px] text-faint">
          Reservation {reservation!.status} — resolved; further reconcile
          attempts would replay idempotently (no new action needed).
        </p>
      )}
      {actionable && (
        <div className="mt-2">
          {!mode ? (
            <div className="flex gap-2">
              <button className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-fg" disabled={busy}
                onClick={() => setMode("confirmed_charge")}>Confirm charge…</button>
              <button className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-fg" disabled={busy}
                onClick={() => go("confirmed_no_charge")}>Confirmed no charge</button>
              <button className="rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-fg" disabled={busy}
                onClick={() => go("still_unknown")}>Still unknown (keep hold)</button>
            </div>
          ) : (
            <div className="rounded border border-line bg-card p-2">
              <p className="text-xs text-muted">
                Record a <b>synthetic</b> confirmed charge for this simulated reservation.
              </p>
              <div className="mt-2 grid grid-cols-3 gap-2">
                <div>
                  <label className={labelCls} htmlFor={`rec-amt-${job.id}`}>Actual amount (USD)</label>
                  <input id={`rec-amt-${job.id}`} className={inputCls} type="number" min="0" step="0.0001" value={actualUsd}
                    onChange={(e) => setActualUsd(e.target.value)} />
                </div>
                <label className="flex items-end gap-1.5 pb-2 text-xs text-muted">
                  <input type="checkbox" checked={acceptOverage} onChange={(e) => setAcceptOverage(e.target.checked)} />
                  Accept overage above reserved
                </label>
                <div>
                  <label className={labelCls} htmlFor={`rec-note-${job.id}`}>Note</label>
                  <input id={`rec-note-${job.id}`} className={inputCls} value={note} onChange={(e) => setNote(e.target.value)} />
                </div>
              </div>
              <div className="mt-2 flex gap-2">
                <button className={btnCls} disabled={busy || !Number.isFinite(actualNum) || actualNum < 0}
                  onClick={() => go("confirmed_charge")}>
                  Preview reconcile
                </button>
                <button className="rounded-md border border-line px-3 py-1.5 text-sm text-muted"
                  onClick={() => setMode(null)}>Back</button>
              </div>
            </div>
          )}
        </div>
      )}
    </li>
  );
}
