import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../../api/client";
import { fmtBytes } from "./BackupsPage";

// ---------- C4-E — unified resource status ----------

interface SystemResources {
  memory: {
    total_phys_bytes: number; avail_phys_bytes: number;
    phys_load_pct: number; commit_headroom_bytes: number;
    pressure: "ok" | "warn" | "critical";
  };
  gpus: {
    gpu_count: number; free: number;
    slots: Array<{ index: number; name: string | null;
      vram_bytes: number | null; vram_free_bytes: number | null;
      busy_job_id: string | null }>;
  };
  disk: {
    work_dir: string; work_dir_free_bytes: number | null;
    storage_root: string | null; storage_root_free_bytes: number | null;
  };
  jobs: {
    queued: Array<{ id: string; kind: string; message: string | null }>;
    running: Array<{ id: string; kind: string; gpu_index: number | null;
      progress: number; message: string | null }>;
  };
  margins: {
    effective: Record<string, number>;
    user_overrides: Record<string, number>;
    basis: string;
  };
  settings: Record<string, number | null>;
}

/** Beginner-friendly resource view: measured RAM/GPU/disk, queued and
 * running jobs with wait reasons, and the admission thresholds in
 * force. Fetches on mount + on demand — no polling. */
export function ResourceSection({ setError }: { setError: (m: string | null) => void }) {
  const [res, setRes] = useState<SystemResources | null>(null);
  const [busy, setBusy] = useState(false);
  const [diskGiB, setDiskGiB] = useState("");
  const [vramGiB, setVramGiB] = useState("");
  const [saved, setSaved] = useState(false);

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setRes(await apiFetch<SystemResources>("/api/system/resources"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  }, [setError]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (res) {
      setDiskGiB(res.settings.min_disk_free_gib != null
        ? String(res.settings.min_disk_free_gib) : "");
      setVramGiB(res.settings.vram_headroom_gib != null
        ? String(res.settings.vram_headroom_gib) : "");
    }
  }, [res]);

  async function save() {
    setBusy(true); setSaved(false);
    try {
      await apiFetch("/api/settings/resources", {
        method: "PUT",
        body: JSON.stringify({
          min_disk_free_gib: diskGiB === "" ? null : Number(diskGiB),
          vram_headroom_gib: vramGiB === "" ? null : Number(vramGiB),
        }),
      });
      setSaved(true);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally { setBusy(false); }
  }

  const pressureTone =
    res?.memory.pressure === "critical" ? "text-danger"
    : res?.memory.pressure === "warn" ? "text-warn" : "text-teal";

  return (
    <div className="mt-5 rounded-lg border border-line bg-card p-5" data-testid="resource-section">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">
          System resources
        </h3>
        <button onClick={() => void load()} disabled={busy}
          className="rounded-md border border-line px-2.5 py-1 text-xs text-muted hover:text-fg disabled:opacity-40">
          Refresh
        </button>
      </div>
      {res === null ? (
        <p className="mt-3 text-xs text-faint">Loading resource status…</p>
      ) : (
        <>
          <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
            <div className="rounded border border-line p-2.5">
              <div className="text-[10px] font-semibold uppercase text-faint">Memory</div>
              <div className="mt-1 text-muted">
                {fmtBytes(res.memory.avail_phys_bytes)} free of{" "}
                {fmtBytes(res.memory.total_phys_bytes)}
              </div>
              <div className={"mt-0.5 font-medium " + pressureTone}>
                {res.memory.pressure === "ok" ? "Healthy"
                  : res.memory.pressure === "warn" ? "Getting tight" : "Critically low"}
              </div>
            </div>
            <div className="rounded border border-line p-2.5">
              <div className="text-[10px] font-semibold uppercase text-faint">Disk</div>
              <div className="mt-1 text-muted">
                {res.disk.work_dir_free_bytes != null
                  ? (fmtBytes(res.disk.work_dir_free_bytes) + " free on work volume")
                  : "work volume unprobeable"}
              </div>
              <div className="mt-0.5 text-faint">
                {res.disk.storage_root_free_bytes != null
                  ? (fmtBytes(res.disk.storage_root_free_bytes) + " free on media volume")
                  : "media volume unprobeable"}
              </div>
            </div>
          </div>

          {res.gpus.gpu_count > 0 ? (
            <ul className="mt-3 space-y-1 text-xs text-muted">
              {res.gpus.slots.map((s) => (
                <li key={s.index} className="flex justify-between rounded border border-line px-2 py-1">
                  <span>{"GPU " + s.index + ": " + (s.name ?? "?")}</span>
                  <span>
                    {fmtBytes(s.vram_free_bytes ?? s.vram_bytes ?? 0)}
                    {s.busy_job_id ? " · in use" : " · free"}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-3 text-xs text-muted">
              No GPU detected — jobs that need one wait until a GPU is available.
            </p>
          )}

          {(res.jobs.running.length > 0 || res.jobs.queued.length > 0) && (
            <div className="mt-3">
              <div className="text-[10px] font-semibold uppercase text-faint">Jobs</div>
              <ul className="mt-1 space-y-1 text-xs text-muted">
                {res.jobs.running.map((j) => (
                  <li key={j.id} className="flex justify-between rounded border border-line px-2 py-1">
                    <span>{j.kind}{j.gpu_index != null ? (" · GPU " + j.gpu_index) : ""}</span>
                    <span>{Math.round(j.progress * 100) + "%"}</span>
                  </li>
                ))}
                {res.jobs.queued.map((j) => (
                  <li key={j.id} className="rounded border border-line px-2 py-1">
                    <span className="text-faint">{"queued " + j.kind}</span>
                    {j.message && <div className="text-[11px] text-warn">{j.message}</div>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <details className="mt-4">
            <summary className="cursor-pointer text-xs text-muted">
              Advanced admission thresholds
            </summary>
            <div className="mt-2 grid grid-cols-2 gap-3">
              <label className="block text-[11px] text-muted">
                Min free disk (GiB, blank = default)
                <input value={diskGiB} onChange={(e) => setDiskGiB(e.target.value)}
                  type="number" min={0} step="0.5"
                  className="mt-0.5 w-full rounded border border-line bg-panel px-2 py-1 text-xs text-fg" />
              </label>
              <label className="block text-[11px] text-muted">
                VRAM headroom (GiB, blank = default)
                <input value={vramGiB} onChange={(e) => setVramGiB(e.target.value)}
                  type="number" min={0} step="0.5"
                  className="mt-0.5 w-full rounded border border-line bg-panel px-2 py-1 text-xs text-fg" />
              </label>
            </div>
            <p className="mt-1.5 text-[10px] text-faint">
              Thresholds are admission guard rails only — hard critical floors still apply.
            </p>
            <button onClick={() => void save()} disabled={busy}
              className="mt-2 rounded-md border border-line px-3 py-1 text-xs text-muted hover:text-fg disabled:opacity-40">
              Save thresholds{saved ? " — saved" : ""}
            </button>
          </details>
        </>
      )}
    </div>
  );
}
