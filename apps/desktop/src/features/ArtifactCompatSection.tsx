/** U-4c — per-artifact five-axis compatibility verdicts (U-4b backend).
 *
 * Contracts:
 * - GETs are read-only; evaluation is NEVER synchronous and NEVER
 *   automatic. Both POSTs enqueue a durable `model_compat_eval` ops
 *   job — progress comes through the shared jobs store (WS), exactly
 *   like the U-3 discovery scan.
 * - The strongest positive label is "Compatible — static evaluation."
 *   Nothing here can ever claim "Ready to Run" / "Verified to Run":
 *   U-4 proves nothing about runtime execution. V5 does not exist.
 * - Axes stay visibly independent — a missing runtime is not an
 *   architecture failure, and a point-in-time free-VRAM sample is not
 *   current availability.
 * - No absolute paths, source_uri, or provider data are rendered.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "../api/client";
import { useApp } from "../stores/appStore";
import type {
  ArtifactCompatOut,
  CompatEvalOut,
  CompatRuleset,
  CompatVerdict,
  ModelArtifact,
} from "../api/types";

const badge = "rounded px-1.5 py-0.5 text-[11px] font-medium";
const btnCls =
  "rounded-md border border-line bg-card px-2.5 py-1 text-xs text-fg hover:bg-card/80 disabled:opacity-50";

const OVERALL: Record<string, { label: string; cls: string }> = {
  compatible: { label: "Compatible — static evaluation", cls: "bg-teal/15 text-teal" },
  limited: { label: "Limited — static evaluation", cls: "bg-amber/15 text-amber" },
  blocked: { label: "Blocked", cls: "bg-rose/15 text-rose" },
  incompatible: { label: "Incompatible", cls: "bg-rose/15 text-rose" },
  unknown: { label: "Unknown", cls: "bg-line/40 text-muted" },
};

const ARCH: Record<string, string> = {
  supported: "Recognized",
  unsupported: "Unsupported",
  unknown: "Unknown",
};
const RUNTIME: Record<string, string> = {
  operational: "Installed & operational",
  installed_unverified: "Installed — version not validated",
  not_installed: "Not installed",
  planned: "Planned — not installed",
  unknown: "Unknown",
};
const HARDWARE: Record<string, string> = {
  fits: "Fits",
  limited: "Limited",
  insufficient: "Insufficient",
  unknown: "Unknown",
};
const POLICY: Record<string, string> = {
  eligible: "Eligible",
  restricted: "Restricted",
  blocked: "Blocked",
};

/** Backend reason codes → readable explanations (exact keys from the
 * U-4b evaluator; unknown codes fall through humanized). */
const REASONS: Record<string, string> = {
  arch_conflict: "Stored evidence conflicts — architecture is not confirmed",
  family_unrecognized: "Architecture is not recognized from stored evidence",
  family_unsupported: "This runtime does not support this model family",
  fmt_unsupported: "This runtime does not support this file format",
  mode_unsupported: "No supported serving mode is declared for this combination",
  components_missing: "Required companion components are missing",
  runtime_path_unverified: "Support is documented but not covered by tests",
  dtype_unverified: "Precision/quantization is not verified for this runtime",
  runtime_planned: "This runtime is planned — not available yet",
  runtime_not_installed: "The runtime is not installed",
  runtime_version_unverified:
    "The installed runtime version is not in the validated set",
  no_declared_runtime: "No runtime declares support for this artifact",
  requirements_undeclared:
    "Hardware requirements are undeclared — fit cannot be determined",
  no_backend: "No usable compute backend is available for this runtime",
  vram_below_min: "GPU VRAM is below the declared minimum",
  hold_gated:
    "Policy hold — adapter/LoRA use is gated off pending milestone approval",
  license_restricted: "The declared license restricts this use",
};
const WARNINGS: Record<string, string> = {
  vram_tight: "Free VRAM was tight at evaluation time",
  ram_tight: "Available RAM was tight at evaluation time",
};
const LIMITS: Record<string, string> = {
  cpu_only: "CPU backend only — no GPU backend for this runtime",
  vram_unmeasured: "GPU VRAM could not be measured",
  vram_headroom_tight: "VRAM headroom is below the configured reserve",
  ram_below_min: "System RAM is below the declared minimum",
};

function explain(code: string, table: Record<string, string>): string {
  return table[code] ?? code.replaceAll("_", " ");
}

function runtimeName(v: CompatVerdict, ruleset: CompatRuleset | null): string {
  return ruleset?.runtimes.find((r) => r.runtime_key === v.runtime_key)
    ?.display_name ?? v.runtime_key;
}

function validatedVersions(v: CompatVerdict, ruleset: CompatRuleset | null): string[] {
  return ruleset?.runtimes.find((r) => r.runtime_key === v.runtime_key)
    ?.validated_versions ?? [];
}

function isTerminal(status: string | undefined): boolean {
  return status === "succeeded" || status === "failed" || status === "cancelled";
}

/** Per-artifact compatibility panel. Mounted read-only; the only write
 * path is the explicit Re-evaluate button → durable ops job. */
export function ArtifactCompatSection({ artifactId }: { artifactId: string }) {
  const jobs = useApp((s) => s.jobs);
  const [data, setData] = useState<ArtifactCompatOut | "loading" | "notfound" | "error">("loading");
  const [ruleset, setRuleset] = useState<CompatRuleset | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [evalJobId, setEvalJobId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const evalJob = evalJobId ? jobs[evalJobId] : undefined;
  const evaluating = !!evalJob
    && (evalJob.status === "queued" || evalJob.status === "running");

  // Any compat-eval job reaching a terminal state may have written
  // verdicts for this artifact (including a bulk evaluate-all). Count
  // transitions only — never refetch on unrelated job updates.
  const terminalCount = useMemo(
    () => Object.values(jobs).filter(
      (j) => j.kind === "model_compat_eval" && isTerminal(j.status)).length,
    [jobs],
  );
  const seenTerminal = useRef<number | null>(null);

  async function load() {
    try {
      const [c, r] = await Promise.all([
        apiFetch<ArtifactCompatOut>(`/api/models/artifacts/${artifactId}/compat`),
        apiFetch<CompatRuleset>("/api/compat/ruleset").catch(() => null),
      ]);
      // Fail closed: a malformed/unavailable payload is an error state,
      // never a crash or an empty-verdicts false positive.
      if (!c || !Array.isArray(c.verdicts)) { setData("error"); return; }
      setData(c); setRuleset(r); setErr(null);
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setData(m.startsWith("404") ? "notfound" : "error");
      setErr(m);
    }
  }

  useEffect(() => { void load(); }, [artifactId]);

  useEffect(() => {
    if (seenTerminal.current === null) {
      seenTerminal.current = terminalCount;   // baseline — don't refetch
      return;                                 //   for pre-existing jobs
    }
    if (terminalCount > seenTerminal.current) {
      seenTerminal.current = terminalCount;
      void load();
    }
  }, [terminalCount]);

  async function reevaluate() {
    setBusy(true); setErr(null);
    try {
      const out = await apiFetch<CompatEvalOut>(
        `/api/models/artifacts/${artifactId}/compat/evaluate`,
        { method: "POST" });
      // Identical requests return the SAME job handle — the store row
      // is the single source of truth for queued/running/terminal state.
      setEvalJobId(out.job_id);
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setErr(m.startsWith("409")
        ? "A different compatibility evaluation is already in progress."
        : m);
    } finally {
      setBusy(false);
    }
  }

  const verdicts = data !== "loading" && data !== "notfound" && data !== "error"
    ? data.verdicts : [];

  return (
    <div className="mt-2 rounded border border-line p-2 text-xs"
         aria-label="Compatibility evaluation">
      <div className="flex items-center justify-between gap-2">
        <span className="font-medium text-fg">Compatibility</span>
        <span className="text-faint">static evaluation — advisory only</span>
      </div>

      {data === "loading" && (
        <p className="mt-1 text-muted">Loading compatibility…</p>
      )}
      {data === "notfound" && (
        <p className="mt-1 text-muted">Artifact not found.</p>
      )}
      {data === "error" && (
        <p className="mt-1 text-rose" role="alert">
          Compatibility could not be loaded.</p>
      )}
      {err && data !== "error" && (
        <p className="mt-1 text-rose" role="alert">{err}</p>
      )}

      {verdicts.length === 0 && data !== "loading" && data !== "notfound"
        && data !== "error" && (
        <p className="mt-1 text-muted">
          No compatibility verdict yet — run an evaluation to assess this
          artifact against the declared runtimes.</p>
      )}

      <ul className="mt-1 space-y-2">
        {verdicts.map((v) => {
          const ov = OVERALL[v.overall] ?? OVERALL.unknown;
          const versions = validatedVersions(v, ruleset);
          const domains = Object.entries(v.domain_levels ?? {});
          return (
            <li key={v.runtime_key}
                className="rounded border border-line/60 p-2">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-fg">
                  {runtimeName(v, ruleset)}</span>
                <span className={`${badge} ${ov.cls}`}>{ov.label}</span>
                {v.stale && (
                  <span className={`${badge} bg-amber/15 text-amber`}>
                    Evaluation out of date</span>
                )}
              </div>
              {v.evaluated_at && (
                <p className="mt-0.5 text-faint">
                  Evaluated {v.evaluated_at}
                  {" — free-memory figures below are measurements from that"}
                  {" moment, not current availability."}
                </p>
              )}
              <dl className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 sm:grid-cols-3">
                <div><dt className="inline text-faint">Architecture: </dt>
                  <dd className="inline text-muted">
                    {ARCH[v.arch_axis] ?? v.arch_axis}
                    {v.arch_confidence ? ` (${v.arch_confidence})` : ""}</dd></div>
                <div><dt className="inline text-faint">Runtime: </dt>
                  <dd className="inline text-muted">
                    {RUNTIME[v.runtime_axis] ?? v.runtime_axis}</dd></div>
                <div><dt className="inline text-faint">Hardware: </dt>
                  <dd className="inline text-muted">
                    {HARDWARE[v.hardware_axis] ?? v.hardware_axis}</dd></div>
                <div><dt className="inline text-faint">Policy: </dt>
                  <dd className="inline text-muted">
                    {POLICY[v.policy_axis] ?? v.policy_axis}</dd></div>
                <div><dt className="inline text-faint">Evidence: </dt>
                  <dd className="inline text-muted">
                    {`V${v.level}`}
                    {v.confidence && v.confidence !== "none"
                      ? ` · ${v.confidence}` : ""}</dd></div>
                {versions.length > 0 && (
                  <div><dt className="inline text-faint">Validated versions: </dt>
                    <dd className="inline text-muted">{versions.join(", ")}</dd></div>
                )}
              </dl>
              {domains.length > 0 && (
                <p className="mt-0.5 text-faint">
                  Evidence domains: {domains
                    .map(([d, l]) => `${d.replaceAll("_", " ")} V${l}`)
                    .join(" · ")}
                </p>
              )}
              {v.reasons.length > 0 && (
                <ul className="mt-1 list-disc pl-4 text-muted">
                  {v.reasons.map((r) => (
                    <li key={r}>{explain(r, REASONS)}</li>))}
                </ul>
              )}
              {v.missing.length > 0 && (
                <p className="mt-0.5 text-amber">
                  Missing: {v.missing.map((m) => m.replaceAll("_", " "))
                    .join(", ")}</p>
              )}
              {v.hardware_limits.length > 0 && (
                <p className="mt-0.5 text-amber">
                  Hardware: {v.hardware_limits
                    .map((l) => explain(l, LIMITS)).join("; ")}</p>
              )}
              {v.warnings.length > 0 && (
                <p className="mt-0.5 text-amber">
                  {v.warnings.map((w) => explain(w, WARNINGS)).join("; ")}
                  {" (measured at evaluation time)"}</p>
              )}
              {v.actions.length > 0 && (
                <p className="mt-0.5 text-muted">
                  Suggested: {v.actions.map((a) =>
                    a.startsWith("install_runtime:")
                      ? `install the ${a.slice("install_runtime:".length)} runtime`
                      : a.replaceAll("_", " ")).join("; ")}</p>
              )}
            </li>
          );
        })}
      </ul>

      <div className="mt-2 flex items-center gap-2">
        <button onClick={() => void reevaluate()}
                disabled={busy || evaluating}
                className={btnCls}>
          {evaluating ? "Evaluating…" : "Re-evaluate"}
        </button>
        {evalJob && evaluating && (
          <span className="text-muted" role="status">
            {evalJob.status === "queued"
              ? "Evaluation queued"
              : "Evaluation running"}</span>
        )}
        {evalJob?.status === "succeeded" && (
          <span className="text-teal" role="status">Evaluation complete</span>)}
        {evalJob?.status === "failed" && (
          <span className="text-rose" role="status">
            Evaluation failed — previous verdicts are unchanged</span>)}
        {evalJob?.status === "cancelled" && (
          <span className="text-muted" role="status">
            Evaluation cancelled — previous verdicts are unchanged</span>)}
      </div>
    </div>
  );
}

/** Evaluate-all action for the Model Manager section menu. Enqueues a
 * bulk `model_compat_eval` job; per-artifact sections observe its
 * terminal transition through the same jobs store and refresh
 * themselves. */
export function CompatEvalAllButton() {
  const jobs = useApp((s) => s.jobs);
  const [jobId, setJobId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const job = jobId ? jobs[jobId] : undefined;
  const running = !!job
    && (job.status === "queued" || job.status === "running");

  async function evaluateAll() {
    setBusy(true); setErr(null);
    try {
      const out = await apiFetch<CompatEvalOut>(
        "/api/models/compat/evaluate-all", { method: "POST" });
      setJobId(out.job_id);
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      setErr(m.startsWith("409")
        ? "A different compatibility evaluation is already in progress."
        : m);
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex items-center gap-2">
      <button onClick={() => void evaluateAll()}
              disabled={busy || running}
              className="rounded-md border border-line px-3 py-1.5 text-xs font-medium text-muted hover:text-fg disabled:opacity-50">
        {running ? "Evaluating…" : "Evaluate compatibility"}
      </button>
      {job && running && (
        <span className="text-xs text-muted" role="status">
          {job.status === "queued" ? "queued" : "running"}</span>
      )}
      {job?.status === "succeeded" && (
        <span className="text-xs text-teal" role="status">done</span>)}
      {job?.status === "failed" && (
        <span className="text-xs text-rose" role="status">failed</span>)}
      {job?.status === "cancelled" && (
        <span className="text-xs text-muted" role="status">cancelled</span>)}
      {err && <span className="text-xs text-rose" role="alert">{err}</span>}
    </span>
  );
}

/** U-4c — the page-level Compatibility section: an artifact selector,
 * the selected artifact's five-axis panel, and the bulk evaluate-all
 * action. Kept OUT of the Universal Model Library's `<section>` — that
 * surface's button inventory is a frozen contract (catalog rows carry
 * zero action buttons). */
export function CompatSection({ artifacts }: { artifacts: ModelArtifact[] }) {
  const [sel, setSel] = useState("");
  const cur = artifacts.find((a) => a.artifact_id === sel) ?? artifacts[0];

  useEffect(() => {
    if (sel && !artifacts.some((a) => a.artifact_id === sel)) setSel("");
  }, [artifacts, sel]);

  return (
    <section className="mt-6" aria-label="Compatibility">
      <div className="flex items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Compatibility</h2>
        <CompatEvalAllButton />
      </div>
      <p className="mt-1 text-sm text-muted">
        Five-axis static evaluation of each artifact against the declared
        runtimes — advisory only. It never runs, downloads, or verifies a
        model; it reads the stored verdicts produced by an explicit
        evaluation.
      </p>
      {artifacts.length === 0 ? (
        <div className="mt-3 rounded-lg border border-dashed border-line p-4 text-sm text-muted">
          No artifacts to evaluate.</div>
      ) : (
        <div className="mt-3 rounded-lg border border-line bg-card p-3">
          <label className="flex items-center gap-2 text-xs text-muted">
            Artifact
            <select
              aria-label="Artifact to evaluate"
              className="rounded border border-line bg-card px-2 py-1 text-fg"
              value={cur?.artifact_id ?? ""}
              onChange={(e) => setSel(e.target.value)}>
              {artifacts.map((a) => (
                <option key={a.artifact_id} value={a.artifact_id}>
                  {`${a.display_name} — ${a.kind}`}</option>
              ))}
            </select>
          </label>
          {cur && (
            <ArtifactCompatSection
              key={cur.artifact_id}
              artifactId={cur.artifact_id} />
          )}
        </div>
      )}
    </section>
  );
}
