import { getBackendInfo, apiFetch } from "../api/client";
import { AuthImage } from "./AuthImage";
import { useEffect, useState } from "react";
import { useApp } from "../stores/appStore";
import { fmtBytes } from "../features/workspace/BackupsPage";

const STATUS_STYLE: Record<string, string> = {
  queued: "text-muted",
  running: "text-teal",
  succeeded: "text-teal",
  failed: "text-danger",
  cancelled: "text-amber",
};

function fmtEta(sec: number | null | undefined): string {
  if (sec == null) return "estimating…";
  if (sec < 60) return `~${Math.max(1, Math.round(sec))}s left`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m < 60) return `~${m}m ${s}s left`;
  return `~${Math.floor(m / 60)}h ${m % 60}m left`;
}

export function JobsPanel() {
  const jobs = useApp((s) => s.jobs);
  const pid = useApp((s) => s.currentProjectId);
  const assetsPage = useApp((s) => s.assetsPage);
  const assets = assetsPage?.items ?? [];
  const [base, setBase] = useState<{ baseUrl: string; token: string } | null>(null);

  useEffect(() => {
    void getBackendInfo().then(setBase);
  }, []);

  const list = Object.values(jobs).sort((a, b) => b.created_at.localeCompare(a.created_at));
  if (list.length === 0) return null;

  return (
    <aside className="w-72 overflow-y-auto border-l border-line bg-panel p-4" aria-label="Activity">
      <h2 className="text-sm font-semibold text-muted">Activity</h2>
      <ul className="mt-3 space-y-3">
        {list.map((j) => {
          const asset = j.output_asset_id ? assets.find((a) => a.id === j.output_asset_id) : null;
          const running = j.status === "running" || j.status === "queued";
          const d = j.progress_json;
          return (
            <li key={j.id} className="rounded-md border border-line bg-card p-3">
              {j.execution_scope === "remote" && (
                <p className="mb-2 rounded border border-amber/50 bg-amber/10 px-2 py-1 text-[10px] font-semibold uppercase tracking-wide text-amber">
                  SIMULATION — No external AI provider contacted.
                </p>
              )}
              <div className="flex items-center justify-between text-xs">
                <span className="text-muted">{j.adapter_id}</span>
                <span className={STATUS_STYLE[j.status] ?? "text-muted"}>
                  {j.recovery_state === "needs_reconciliation"
                    ? "needs reconciliation"
                    : j.cancel_requested && running ? "cancelling" : j.status}
                </span>
              </div>
              {j.execution_scope !== "remote" && (
                <div className="mt-2 h-1.5 rounded-full bg-line">
                  <div
                    className="h-full rounded-full bg-teal transition-all"
                    style={{ width: `${Math.round(j.progress * 100)}%` }}
                  />
                </div>
              )}
              {j.progress_json?.remote_outcome && (
                <p className="mt-1 text-[10px] text-faint">
                  remote outcome: {j.progress_json.remote_outcome}
                  {j.progress_json.remote_job_id ? ` · remote id ${j.progress_json.remote_job_id} (synthetic)` : ""}
                </p>
              )}
              <p className="mt-1.5 text-xs text-muted">{j.message || j.error || "queued"}</p>
              {running && d && (
                <p className="mt-1 text-xs text-faint">
                  {fmtBytes(d.bytes_done)} / {fmtBytes(d.bytes_total)}
                  {d.rate_bps ? ` · ${fmtBytes(d.rate_bps)}/s` : ""}
                  {` · ${fmtEta(d.eta_seconds)}`}
                </p>
              )}
              {running && (
                j.execution_scope === "remote"
                && j.progress_json?.remote_intent?.lifecycle === "sync" ? (
                  <p className="mt-2 text-[10px] text-faint">
                    Provider documents no cancellation — reconcile instead.
                  </p>
                ) : (
                  <button
                    onClick={() =>
                      void apiFetch(`/api/jobs/${j.id}/cancel`, { method: "POST" }).catch(() => {})
                    }
                    disabled={j.cancel_requested}
                    className="mt-2 rounded border border-line px-2 py-0.5 text-xs text-muted hover:text-danger disabled:opacity-40"
                  >
                    {j.cancel_requested ? "Cancelling…" : "Cancel"}
                  </button>
                )
              )}
              {asset && base && pid && (
                asset.kind === "image" ? (
                  <AuthImage
                    pid={pid}
                    assetId={asset.id}
                    alt="Generated output"
                    className="mt-2 w-full rounded border border-line"
                    fallback={
                      <p className="mt-2 rounded border border-line px-2 py-1 text-[10px] text-faint">
                        {asset.kind} asset · {asset.name}
                      </p>
                    }
                  />
                ) : (
                  <p className="mt-2 rounded border border-line px-2 py-1 text-[10px] text-faint">
                    {asset.kind} asset · {asset.name}
                  </p>
                )
              )}
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
