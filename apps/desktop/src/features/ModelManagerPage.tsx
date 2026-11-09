import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import { useApp } from "../stores/appStore";
import { ConfirmDialog } from "../components/ConfirmDialog";
import type { CompatStatus, ModelCatalogEntry, ModelsDir } from "../api/types";

function gb(bytes: number | null | undefined): string {
  if (bytes == null) return "unknown";
  return `${(bytes / 1e9).toFixed(1)} GB`;
}

const COMPAT: Record<CompatStatus, { label: string; cls: string }> = {
  compatible: { label: "Compatible", cls: "bg-teal/15 text-teal" },
  limited: { label: "Limited", cls: "bg-amber/15 text-amber" },
  unsupported: { label: "Unsupported", cls: "bg-rose/15 text-rose" },
  unknown: { label: "Unknown", cls: "bg-line/40 text-muted" },
  restricted: { label: "Restricted license", cls: "bg-rose/15 text-rose" },
  unavailable: { label: "Unavailable", cls: "bg-line/40 text-muted" },
  planned: { label: "Planned", cls: "bg-line/40 text-muted" },
};

const SUPPORT: Record<string, { label: string; cls: string } | null> = {
  production: null,
  tested: { label: "Tested", cls: "bg-sky/15 text-sky" },
  experimental: { label: "Experimental", cls: "bg-amber/15 text-amber" },
  planned: null, // covered by the compatibility badge
};

export function ModelManagerPage() {
  const hw = useApp((s) => s.hardware);
  const adapters = useApp((s) => s.adapters);
  const models = useApp((s) => s.models);
  const gpuState = useApp((s) => s.gpuState);
  const jobs = useApp((s) => s.jobs);
  const refreshHw = useApp((s) => s.refreshHardware);
  const refreshModels = useApp((s) => s.refreshModels);

  const [modelsDir, setModelsDir] = useState<ModelsDir | null>(null);
  const [installTarget, setInstallTarget] = useState<ModelCatalogEntry | null>(null);
  const [removeTarget, setRemoveTarget] = useState<ModelCatalogEntry | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    void refreshHw();
    void refreshModels();
    void apiFetch<ModelsDir>("/api/models/dir").then(setModelsDir).catch(() => {});
  }, [refreshHw, refreshModels]);

  // Model-download jobs are app-level (no project) — find any in-flight ones.
  const downloadJobs = useMemo(
    () => Object.values(jobs).filter((j) => j.kind === "model_download"),
    [jobs],
  );
  // Refresh catalog whenever a download job lands/updates.
  const dlDone = downloadJobs.filter((j) => j.status !== "queued" && j.status !== "running").length;
  useEffect(() => {
    if (dlDone > 0) void refreshModels();
  }, [dlDone, refreshModels]);

  async function install(model: ModelCatalogEntry) {
    setBusy(true);
    setErr(null);
    try {
      await apiFetch(`/api/models/${model.key}/install`, { method: "POST", body: "{}" });
      setInstallTarget(null);
      void refreshModels();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function remove(model: ModelCatalogEntry) {
    setBusy(true);
    setErr(null);
    try {
      await apiFetch(`/api/models/${model.key}/remove`, { method: "POST", body: "{}" });
      setRemoveTarget(null);
      void refreshModels();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function changeDir() {
    const dir = prompt("Models folder (absolute path). Leave empty to reset to the default location.");
    if (dir === null) return;
    try {
      const d = await apiFetch<ModelsDir>("/api/settings/models-dir", {
        method: "PUT",
        body: JSON.stringify({ dir: dir.trim() || null }),
      });
      setModelsDir(d);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  function modelJob(key: string) {
    return downloadJobs.find((j) => j.model_key === key);
  }

  return (
    <div className="mx-auto max-w-5xl p-8">
      <h1 className="text-2xl font-bold">Model Manager</h1>
      <p className="mt-1 text-muted">
        Every model is installed only after you explicitly approve it. Licenses and hardware
        compatibility are checked and explained.
      </p>
      {err && <p className="mt-3 rounded bg-rose/15 px-3 py-2 text-sm text-rose">{err}</p>}

      {/* --- model catalog --- */}
      <section className="mt-6 space-y-3">
        {models.map((m) => {
          const c = COMPAT[m.compatibility.status];
          const inst = m.install;
          const job = modelJob(m.key);
          const downloading = job && (job.status === "queued" || job.status === "running");
          const installed = inst?.status === "installed";
          const s = SUPPORT[m.support];
          return (
            <div key={m.key} className="rounded-lg border border-line bg-card p-4">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-base font-semibold">{m.display_name}</h2>
                <span className="text-xs text-faint">v{m.version} · {m.family}</span>
                {m.modality !== "image" && (
                  <span className="rounded bg-sky/15 px-2 py-0.5 text-xs capitalize text-sky">{m.modality}</span>
                )}
                <span className={`rounded px-2 py-0.5 text-xs ${c.cls}`}>{c.label}</span>
                {s && <span className={`rounded px-2 py-0.5 text-xs ${s.cls}`}>{s.label}</span>}
                {installed && (
                  <span className="rounded bg-teal/15 px-2 py-0.5 text-xs text-teal">
                    Installed{inst?.verified ? " · verified" : ""}
                  </span>
                )}
                {inst?.status === "failed" && (
                  <span className="rounded bg-rose/15 px-2 py-0.5 text-xs text-rose">install failed</span>
                )}
              </div>
              <p className="mt-1 text-sm text-muted">{m.notes}</p>

              <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-muted md:grid-cols-4">
                <div><dt className="inline text-faint">License: </dt><dd className="inline">{m.license.name}</dd></div>
                <div><dt className="inline text-faint">Download: </dt><dd className="inline">{gb(m.download_bytes)}</dd></div>
                <div><dt className="inline text-faint">Min VRAM: </dt><dd className="inline">{gb(m.requirements.min_vram_bytes)}</dd></div>
                <div><dt className="inline text-faint">Source: </dt><dd className="inline">{String(m.source.provider ?? "local")}</dd></div>
                <div className="col-span-2"><dt className="inline text-faint">Modes: </dt><dd className="inline">{m.modes.join(", ")}</dd></div>
                {m.capabilities.audio && (
                  <div className="col-span-2"><dt className="inline text-faint">Audio: </dt><dd className="inline">synchronized audio+video</dd></div>
                )}
                {installed && (
                  <div className="col-span-2">
                    <dt className="inline text-faint">On disk: </dt>
                    <dd className="inline">{gb(inst?.byte_size)} · {inst?.install_root}\{inst?.rel_dir}</dd>
                  </div>
                )}
              </dl>

              {(m.compatibility.reasons.length > 0 || m.compatibility.warnings.length > 0) && (
                <ul className="mt-2 space-y-0.5 text-xs">
                  {m.compatibility.reasons.map((r, i) => (
                    <li key={`r${i}`} className={m.compatibility.status === "planned" ? "text-muted" : "text-rose"}>· {r}</li>
                  ))}
                  {m.compatibility.warnings.map((w, i) => (
                    <li key={`w${i}`} className="text-amber">· {w}</li>
                  ))}
                </ul>
              )}

              {downloading && job && (
                <div className="mt-3">
                  <div className="h-1.5 w-full overflow-hidden rounded bg-line/40">
                    <div className="h-full bg-teal transition-all" style={{ width: `${Math.round(job.progress * 100)}%` }} />
                  </div>
                  <p className="mt-1 text-xs text-muted">
                    {job.message || "Downloading…"} — {Math.round(job.progress * 100)}%
                  </p>
                </div>
              )}

              <div className="mt-3 flex gap-2">
                {!installed && !downloading && m.license.commercial_ok && m.files.length > 0 && m.support !== "planned" && (
                  <button
                    onClick={() => setInstallTarget(m)}
                    disabled={busy}
                    className="rounded-md bg-action px-3 py-1.5 text-xs text-white disabled:opacity-50"
                  >
                    Install…
                  </button>
                )}
                {installed && (
                  <button
                    onClick={() => setRemoveTarget(m)}
                    disabled={busy}
                    className="rounded-md border border-rose/40 px-3 py-1.5 text-xs text-rose disabled:opacity-50"
                  >
                    Remove
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </section>

      {/* --- storage --- */}
      <section className="mt-6 rounded-lg border border-line bg-card p-4">
        <h2 className="font-semibold">Model storage</h2>
        {modelsDir ? (
          <dl className="mt-2 space-y-1 text-sm text-muted">
            <div className="flex justify-between gap-4">
              <dt>Location</dt>
              <dd className="truncate text-fg" title={modelsDir.dir}>{modelsDir.dir} <span className="text-faint">({modelsDir.source})</span></dd>
            </div>
            <div className="flex justify-between"><dt>Used</dt><dd className="text-fg">{gb(modelsDir.used_bytes)}</dd></div>
            <div className="flex justify-between"><dt>Free</dt><dd className="text-fg">{modelsDir.free_bytes != null ? gb(modelsDir.free_bytes) : "unknown"}</dd></div>
          </dl>
        ) : (
          <p className="mt-2 text-sm text-muted">Loading…</p>
        )}
        <button onClick={changeDir} className="mt-3 rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:text-fg">
          Change folder…
        </button>
        <p className="mt-2 text-xs text-faint">Existing installs stay where they are; new downloads go to the folder shown.</p>
      </section>

      {/* --- GPUs --- */}
      {gpuState && gpuState.slots.length > 0 && (
        <section className="mt-6 rounded-lg border border-line bg-card p-4">
          <h2 className="font-semibold">GPU slots</h2>
          <ul className="mt-2 space-y-1 text-sm text-muted">
            {gpuState.slots.map((s) => (
              <li key={s.index} className="flex justify-between rounded border border-line px-3 py-1.5">
                <span className="text-fg">GPU {s.index}: {s.name ?? "unknown"}</span>
                <span>
                  {gb(s.vram_bytes)} · {s.busy_job_id ? "busy" : "free"}
                </span>
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-faint">Mode: {gpuState.prefs.mode ?? "auto"}{gpuState.prefs.preferred_gpu != null ? ` · prefers GPU ${gpuState.prefs.preferred_gpu}` : ""} — change it in project Settings → GPU.</p>
        </section>
      )}

      {/* --- hardware --- */}
      {!hw ? (
        <p className="mt-8 text-muted">Probing hardware…</p>
      ) : (
        <div className="mt-6 grid grid-cols-2 gap-5">
          <section className="rounded-lg border border-line bg-card p-4">
            <h2 className="font-semibold">System</h2>
            <dl className="mt-2 space-y-1 text-sm text-muted">
              <div className="flex justify-between"><dt>OS</dt><dd className="text-fg">{hw.os.system} {hw.os.release}</dd></div>
              <div className="flex justify-between"><dt>CPU</dt><dd className="text-right text-fg">{hw.cpu.name} ({hw.cpu.logical_cores ?? "?"} threads)</dd></div>
              <div className="flex justify-between"><dt>Memory</dt><dd className="text-fg">{gb(hw.memory.total_bytes)}</dd></div>
              <div className="flex justify-between"><dt>Profile</dt><dd className="text-fg">{hw.hardware_profile ?? "unknown"}</dd></div>
            </dl>
          </section>
          <section className="rounded-lg border border-line bg-card p-4">
            <h2 className="font-semibold">GPUs & inference backends</h2>
            {hw.gpus.length === 0 ? (
              <p className="mt-2 text-sm text-muted">No GPU reported. CPU-only inference may still run, slowly.</p>
            ) : (
              <ul className="mt-2 space-y-2 text-sm text-muted">
                {hw.gpus.map((g) => (
                  <li key={g.index} className="rounded border border-line p-2">
                    <div className="text-fg">#{g.index} {g.name ?? "Unknown adapter"}</div>
                    <div>
                      {g.vendor} · VRAM {gb(g.vram_bytes)}
                      {g.vram_free_bytes != null && ` (${gb(g.vram_free_bytes)} free)`}
                      {g.vram_capped_at_4gb && " (reported value capped)"} · driver {g.driver_version ?? "unknown"}
                    </div>
                  </li>
                ))}
              </ul>
            )}
            {hw.backends && (
              <p className="mt-2 text-xs text-faint">
                {Object.entries(hw.backends).map(([k, v]) => `${k.toUpperCase()}: ${v.available ? "yes" : "no"}`).join(" · ")}
              </p>
            )}
          </section>
          <section className="rounded-lg border border-line bg-card p-4">
            <h2 className="font-semibold">Storage</h2>
            <ul className="mt-2 space-y-1 text-sm text-muted">
              {hw.disks.map((d) => (
                <li key={d.mount} className="flex justify-between">
                  <span>{d.mount}</span>
                  <span className="text-fg">{gb(d.free_bytes)} free of {gb(d.total_bytes)}</span>
                </li>
              ))}
            </ul>
          </section>
          <section className="rounded-lg border border-line bg-card p-4">
            <h2 className="font-semibold">Runtimes</h2>
            <dl className="mt-2 space-y-1 text-sm text-muted">
              <div className="flex justify-between"><dt>Python</dt><dd className="text-fg">{hw.runtimes.python}</dd></div>
              <div className="flex justify-between"><dt>FFmpeg</dt><dd className="text-fg">{hw.runtimes.ffmpeg.available ? (hw.runtimes.ffmpeg.version ?? "installed") : "not found on PATH"}</dd></div>
              <div className="flex justify-between"><dt>PyTorch</dt><dd className="text-fg">{hw.runtimes.torch_installed ? "installed" : "not installed"}</dd></div>
            </dl>
          </section>
        </div>
      )}

      <section className="mt-6 rounded-lg border border-line bg-card p-4">
        <h2 className="font-semibold">Registered provider adapters</h2>
        <ul className="mt-2 space-y-2 text-sm">
          {adapters.map((a) => (
            <li key={a.adapter_id} className="rounded border border-line p-3">
              <div className="flex items-center justify-between">
                <span className="text-fg">{a.display_name}</span>
                <span className="rounded bg-amber/15 px-2 py-0.5 text-xs uppercase text-amber">{a.adapter_kind}</span>
              </div>
              <p className="mt-1 text-muted">{a.notes}</p>
              <p className="mt-0.5 text-xs text-faint">
                tasks: {a.tasks.join(", ")} · lane: {a.lane ?? "ops"} · gpu: {a.needs_gpu ? "required" : "no"} · cloud: {a.requires_cloud ? "yes" : "no"} · cost: {a.cost_model}
              </p>
            </li>
          ))}
        </ul>
      </section>

      {/* install confirmation — the approval gate */}
      <ConfirmDialog
        open={installTarget !== null}
        onOpenChange={(o) => { if (!o) setInstallTarget(null); }}
        title={`Install ${installTarget?.display_name ?? ""}?`}
        confirmLabel="Install model"
        body={
          installTarget
            ? `Downloads ${gb(installTarget.download_bytes)} to ${modelsDir?.dir ?? "the models folder"} — ` +
              `verified against published checksums. License: ${installTarget.license.name} ` +
              `(${installTarget.license.notes}). Requires ≥ ${gb(installTarget.requirements.min_vram_bytes)} ` +
              `VRAM. ${installTarget.compatibility.warnings.join(" ")}`
            : ""
        }
        onConfirm={() => { if (installTarget) void install(installTarget); }}
      />
      <ConfirmDialog
        open={removeTarget !== null}
        onOpenChange={(o) => { if (!o) setRemoveTarget(null); }}
        title={`Remove ${removeTarget?.display_name ?? ""}?`}
        confirmLabel="Remove files"
        body={
          removeTarget
            ? `Deletes ${gb(removeTarget.install?.byte_size)} of model files from ` +
              `${removeTarget.install?.install_root ?? ""}\\${removeTarget.install?.rel_dir ?? ""}. ` +
              `Project assets are never touched. You can reinstall later.`
            : ""
        }
        onConfirm={() => { if (removeTarget) void remove(removeTarget); }}
      />
    </div>
  );
}
