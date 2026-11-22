/** U-3 — Discover models: user-approved roots, bounded scanning, and
 * unverified candidates that route into the EXISTING U-2 import
 * dialog on explicit per-candidate approval.
 *
 * Contracts:
 * - Candidate list rows carry NO absolute paths — rel_path is anchored
 *   to the owning root; the absolute path is fetched lazily from the
 *   authenticated detail endpoint only when the user expands a row or
 *   clicks Import.
 * - Scanning is a durable ops.model.discover job (jobs store + WS);
 *   cancellation never marks candidates absent.
 * - Candidates are unverified: fmt_guess is a sniff, presence=absent
 *   means "not found in the last complete scan" — never "corrupted"
 *   or "deleted by the app".
 */
import { useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import { pickDirectory } from "../api/commands";
import { useApp } from "../stores/appStore";
import type {
  DiscoveryCandidate,
  DiscoveryCandidateDetail,
  DiscoveryRoot,
  DiscoveryScanOut,
  Job,
} from "../api/types";

const badge = "rounded px-1.5 py-0.5 text-[11px] font-medium";
const btnCls =
  "rounded-md border border-line bg-card px-2.5 py-1 text-xs text-fg hover:bg-card/80";

function fmtBytes(n: number): string {
  return n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB`
    : `${(n / 1e6).toFixed(1)} MB`;
}

export function DiscoverySection({
  onImportCandidate,
  refreshKey = 0,
}: {
  /** Open the existing import dialog prefilled with this absolute path
   * (obtained from the authenticated detail endpoint). */
  onImportCandidate: (path: string) => void;
  /** Bumped when the library refreshes — re-fetches candidates so
   * `linked_artifact_id` badges update after an import. */
  refreshKey?: number;
}) {
  const jobs = useApp((s) => s.jobs);
  const [roots, setRoots] = useState<DiscoveryRoot[] | null>(null);
  const [cands, setCands] = useState<DiscoveryCandidate[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [rootErr, setRootErr] = useState<string | null>(null);
  const [newRoot, setNewRoot] = useState("");
  const [scanJobId, setScanJobId] = useState<string | null>(null);
  const [detail, setDetail] = useState<
    Record<string, DiscoveryCandidateDetail | "loading" | null>>({});
  const [busy, setBusy] = useState(false);

  const scanJob: Job | undefined = scanJobId ? jobs[scanJobId] : undefined;
  const scanning = !!scanJob
    ? scanJob.status === "queued" || scanJob.status === "running"
    : false;

  const refreshAll = async () => {
    try {
      const [r, c] = await Promise.all([
        apiFetch<DiscoveryRoot[]>("/api/models/discovery/roots"),
        apiFetch<DiscoveryCandidate[]>("/api/models/discovery/candidates"),
      ]);
      setRoots(r); setCands(c); setErr(null);
    } catch {
      setErr("discovery endpoints unavailable");
    }
  };

  useEffect(() => { void refreshAll(); }, [refreshKey]);

  // Refresh roots + candidates when our scan job lands terminal — the
  // card itself stays visible (result persists until dismissed or the
  // next scan replaces it).
  useEffect(() => {
    if (scanJob && !scanning) void refreshAll();
  }, [scanJob?.status]);

  async function addRoot(path: string) {
    if (!path.trim()) return;
    setRootErr(null);
    try {
      await apiFetch("/api/models/discovery/roots", {
        method: "POST",
        body: JSON.stringify({ path: path.trim() }),
      });
      setNewRoot("");
      void refreshAll();
    } catch (e) {
      setRootErr(e instanceof Error ? e.message : "could not add root");
    }
  }

  async function archiveRoot(r: DiscoveryRoot) {
    setBusy(true);
    try {
      await apiFetch(`/api/models/discovery/roots/${r.root_id}/archive`, {
        method: "POST",
      });
      void refreshAll();
    } finally {
      setBusy(false);
    }
  }

  async function startScan() {
    try {
      const out = await apiFetch<DiscoveryScanOut>(
        "/api/models/discovery/scan", { method: "POST" });
      setScanJobId(out.job_id);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : "could not start scan");
    }
  }

  async function cancelScan() {
    if (!scanJobId) return;
    await apiFetch(`/api/jobs/${scanJobId}/cancel`, { method: "POST" })
      .catch(() => {});
  }

  async function importCandidate(c: DiscoveryCandidate) {
    try {
      const d = await apiFetch<DiscoveryCandidateDetail>(
        `/api/models/discovery/candidates/${c.candidate_id}`);
      onImportCandidate(d.resolved_path);
    } catch {
      setErr("could not load the candidate path — scan may be stale");
    }
  }

  function toggleDetail(c: DiscoveryCandidate) {
    const cur = detail[c.candidate_id];
    if (cur != null) {
      setDetail((d) => ({ ...d, [c.candidate_id]: null }));
      return;
    }
    setDetail((d) => ({ ...d, [c.candidate_id]: "loading" }));
    apiFetch<DiscoveryCandidateDetail>(
      `/api/models/discovery/candidates/${c.candidate_id}`)
      .then((v) => setDetail((d) => ({ ...d, [c.candidate_id]: v })))
      .catch(() => setDetail((d) => ({ ...d, [c.candidate_id]: null })));
  }

  const scanDetail = (scanJob?.progress_json ?? {}) as {
    roots_done?: number; roots_total?: number;
    candidates_found?: number; stage?: string;
  };
  // U-3 — reconcile outcome from the durable job result; "failed" is a
  // distinct partial-success state, never rendered as clean success.
  const scanResult = (scanJob?.result_json ?? {}) as {
    absent_reconcile?: string; absent_marked?: number;
  };

  return (
    <section className="mt-6" aria-label="Discover models">
      <div className="flex items-center gap-3">
        <h2 className="text-base font-semibold text-fg">Discover models</h2>
        <button className={btnCls} onClick={() => void startScan()}
                disabled={scanning}>
          {scanning ? "Scanning…" : "Scan now"}
        </button>
        {scanning && (
          <button className={btnCls} onClick={() => void cancelScan()}>
            Cancel scan
          </button>
        )}
      </div>
      <p className="mt-1 text-xs text-muted">
        The app reads folder names and file signatures only — nothing is
        copied, changed, or imported until you approve a file yourself.
      </p>
      {err && <p role="alert" className="mt-2 text-xs text-rose">{err}</p>}

      {/* scan progress — counts only, never paths. Terminal result
       * persists until dismissed or the next scan replaces it. */}
      {scanJob && (
        <div role="status"
             className="mt-3 flex items-center gap-3 rounded-md border border-line bg-card p-3 text-xs text-muted">
          <span className="flex-1">
          {scanning ? (
            <>Scanning approved folders — root{" "}
              {scanDetail.roots_done ?? "…"} of{" "}
              {scanDetail.roots_total ?? "…"}, found{" "}
              {scanDetail.candidates_found ?? 0} candidate(s)</>
          ) : scanJob.status === "cancelled" ? (
            <>Scan cancelled — partial results are kept; nothing was
              marked missing.</>
          ) : scanJob.status === "succeeded" ? (
            scanResult.absent_reconcile === "failed" ? (
              <span className="text-amber">
                Scan complete, but absence verification could not
                finish — no files were newly marked missing by that
                scan.
              </span>
            ) : (
              <>Scan complete{scanResult.absent_marked
                ? ` — ${scanResult.absent_marked} file(s) absent since`
                  + " the last complete scan"
                : ""}.</>
            )
          ) : (
            <>Scan failed: {scanJob.error ?? "unknown"}</>
          )}
          </span>
          {!scanning && (
            <button className={btnCls}
                    onClick={() => setScanJobId(null)}>
              Dismiss
            </button>
          )}
        </div>
      )}

      {/* approved roots */}
      <div className="mt-4">
        <h3 className="text-sm font-medium text-fg">Approved folders</h3>
        <div className="mt-2 flex gap-2">
          <input
            aria-label="Folder path to scan"
            className="w-full max-w-md rounded-md border border-line bg-card px-3 py-1.5 text-xs text-fg"
            placeholder="Add a folder to scan, e.g. D:\Models"
            value={newRoot}
            onChange={(e) => setNewRoot(e.target.value)}
          />
          <button className={btnCls} onClick={() => void addRoot(newRoot)}>
            Add folder
          </button>
          <button className={btnCls}
                  onClick={() => void pickDirectory("Choose a folder to scan")
                    .then((p) => { if (p) setNewRoot(p); })}>
            Browse…
          </button>
        </div>
        {rootErr && <p role="alert" className="mt-1 text-xs text-rose">{rootErr}</p>}
        {roots && roots.length > 0 && (
          <ul className="mt-2 space-y-1">
            {roots.map((r) => (
              <li key={r.root_id}
                  className="flex items-center gap-3 rounded-md border border-line/60 px-3 py-1.5 text-xs">
                <span className="text-fg">{r.display_path}</span>
                <span className="text-muted">depth {r.max_depth}</span>
                {r.last_scan_outcome && (
                  <span className={r.last_scan_outcome === "complete"
                    ? `${badge} bg-teal/15 text-teal`
                    : `${badge} bg-amber/15 text-amber`}>
                    {r.last_scan_outcome}
                    {r.last_code ? ` (${r.last_code})` : ""}
                  </span>
                )}
                {r.archived_at && (
                  <span className={`${badge} bg-faint/20 text-muted`}>archived</span>
                )}
                <span className="text-muted">{r.candidate_count} files</span>
                {!r.archived_at && (
                  <button className="ml-auto text-muted hover:text-rose"
                          disabled={busy}
                          aria-label={`Stop scanning ${r.display_path}`}
                          onClick={() => void archiveRoot(r)}>
                    Remove
                  </button>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* candidates */}
      <div className="mt-4">
        <h3 className="text-sm font-medium text-fg">Candidates</h3>
        {cands && cands.length === 0 && (
          <p className="mt-2 text-xs text-muted">
            No candidates yet — approve a folder and run a scan.
          </p>
        )}
        {cands && cands.length > 0 && (
          <ul className="mt-2 space-y-1">
            {cands.map((c) => (
              <li key={c.candidate_id}
                  className="rounded-md border border-line/60 px-3 py-1.5 text-xs">
                <div className="flex items-center gap-3">
                  <span className="text-fg">{c.name}</span>
                  <span className={`${badge} bg-faint/20 text-muted`}>{c.fmt_guess}</span>
                  <span className="text-muted">{fmtBytes(c.byte_size)}</span>
                  {c.linked_artifact_id ? (
                    <span className={`${badge} bg-teal/15 text-teal`}>imported</span>
                  ) : c.presence === "absent" ? (
                    <span className={`${badge} bg-amber/15 text-amber`}>
                      not found in last scan
                    </span>
                  ) : c.seen_count <= 1 ? (
                    <span className={`${badge} bg-sky/15 text-sky`}>new</span>
                  ) : (
                    <span className={`${badge} bg-faint/20 text-muted`}>seen</span>
                  )}
                  <span className="text-muted">{c.rel_path} · {c.root_label}</span>
                  <button className="ml-auto text-muted hover:text-fg"
                          aria-expanded={detail[c.candidate_id] != null
                            && detail[c.candidate_id] !== "loading"}
                          onClick={() => toggleDetail(c)}>
                    Details
                  </button>
                  {!c.linked_artifact_id && (
                    <button className="text-muted hover:text-fg"
                            onClick={() => void importCandidate(c)}>
                      Import…
                    </button>
                  )}
                </div>
                {detail[c.candidate_id] === "loading" && (
                  <p className="mt-1 text-muted">Loading…</p>
                )}
                {detail[c.candidate_id] != null
                  && detail[c.candidate_id] !== "loading" && (
                  <dl className="mt-2 grid grid-cols-[8rem_1fr] gap-y-1 border-t border-line/40 pt-2 text-xs">
                    <dt className="text-muted">Full path</dt>
                    <dd className="text-fg break-all">
                      {(detail[c.candidate_id] as DiscoveryCandidateDetail)
                        .resolved_path}
                    </dd>
                    <dt className="text-muted">In folder</dt>
                    <dd className="text-fg break-all">
                      {(detail[c.candidate_id] as DiscoveryCandidateDetail)
                        .root_display_path}
                    </dd>
                    <dt className="text-muted">Seen</dt>
                    <dd className="text-fg">
                      {c.seen_count} scan(s) — first{" "}
                      {new Date(c.first_seen_at).toLocaleString()}
                    </dd>
                    {c.presence === "absent" && (
                      <>
                        <dt className="text-muted">Status</dt>
                        <dd className="text-amber">
                          Not found in the last complete scan — the app
                          did not remove it.
                        </dd>
                      </>
                    )}
                  </dl>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
