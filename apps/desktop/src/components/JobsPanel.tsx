import { getBackendInfo, assetFileUrl } from "../api/client";
import { useEffect, useState } from "react";
import { useApp } from "../stores/appStore";

const STATUS_STYLE: Record<string, string> = {
  queued: "text-muted",
  running: "text-teal",
  succeeded: "text-teal",
  failed: "text-danger",
  cancelled: "text-amber",
};

export function JobsPanel() {
  const jobs = useApp((s) => s.jobs);
  const pid = useApp((s) => s.currentProjectId);
  const assets = useApp((s) => s.assets);
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
          return (
            <li key={j.id} className="rounded-md border border-line bg-card p-3">
              <div className="flex items-center justify-between text-xs">
                <span className="text-muted">{j.adapter_id}</span>
                <span className={STATUS_STYLE[j.status] ?? "text-muted"}>{j.status}</span>
              </div>
              <div className="mt-2 h-1.5 rounded-full bg-line">
                <div
                  className="h-full rounded-full bg-teal transition-all"
                  style={{ width: `${Math.round(j.progress * 100)}%` }}
                />
              </div>
              <p className="mt-1.5 text-xs text-muted">{j.message || j.error || "queued"}</p>
              {asset && base && pid && (
                <img
                  src={assetFileUrl(pid, asset.id, base)}
                  alt="Generated output"
                  className="mt-2 w-full rounded border border-line"
                />
              )}
            </li>
          );
        })}
      </ul>
    </aside>
  );
}
