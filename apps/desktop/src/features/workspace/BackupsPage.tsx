import { useCallback, useEffect, useState } from "react";
import { commitCommand, pickDirectory, pickFile } from "../../api/commands";
import { apiFetch } from "../../api/client";
import type { BackupDestination, BackupRun, Job, PackagePreview } from "../../api/types";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useApp } from "../../stores/appStore";

export function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log2(n) / 10));
  return `${(n / 2 ** (10 * i)).toFixed(i ? 1 : 0)} ${u[i]}`;
}

function fmtEta(sec: number | null | undefined): string {
  if (sec == null) return "estimating…";
  if (sec < 60) return `~${Math.max(1, Math.round(sec))}s left`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m < 60) return `~${m}m ${s}s left`;
  return `~${Math.floor(m / 60)}h ${m % 60}m left`;
}

const BACKUP_FAMILY = /^(ops\.backup\.|ops\.assets\.verify)/;

export function BackupsPage() {
  const project = useApp((s) => s.currentProject());
  const backups = useApp((s) => s.backups);
  const jobs = useApp((s) => s.jobs);
  const refresh = useApp((s) => s.refreshBackups);
  const refreshProject = useApp((s) => s.refreshProjectData);
  const refreshProjects = useApp((s) => s.refreshProjects);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [importPath, setImportPath] = useState("");
  const [preview, setPreview] = useState<PackagePreview | null>(null);
  const [restoreRun, setRestoreRun] = useState<BackupRun | null>(null);
  const [dest, setDest] = useState<BackupDestination | null>(null);

  const refreshDest = useCallback(async () => {
    if (!project) return;
    try {
      setDest(await apiFetch<BackupDestination>(`/api/projects/${project.id}/backup-destination`));
    } catch {
      setDest(null);
    }
  }, [project]);

  useEffect(() => {
    void refreshDest();
  }, [refreshDest]);

  // A running backup-family job drives the live progress card.
  const activeJob: Job | undefined = Object.values(jobs)
    .filter((j) => BACKUP_FAMILY.test(j.adapter_id) && (j.status === "running" || j.status === "queued"))
    .sort((a, b) => b.created_at.localeCompare(a.created_at))[0];

  // Refresh history + destination snapshot when the active job finishes.
  const activeDone = !activeJob;
  useEffect(() => {
    if (activeDone) {
      void refresh();
      void refreshDest();
    }
  }, [activeDone, refresh, refreshDest]);

  async function run(type: string, payload: Record<string, unknown>): Promise<boolean> {
    if (!project) return false;
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      await commitCommand(project.id, project.revision, type, payload);
      setMsg("Started — progress is shown below. You can keep working; this runs in the background.");
      await Promise.all([refreshProject(), refreshProjects()]);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function exportBackup() {
    await run("backup.export", {});
  }

  async function cancelJob(jobId: string) {
    setError(null);
    try {
      await apiFetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function changeProjectDestination() {
    if (!project) return;
    const dir = await pickDirectory("Choose where this project's backups are saved");
    if (!dir) return;
    setError(null);
    try {
      await commitCommand(project.id, project.revision, "project.update_settings", {
        backup_dir: dir,
      });
      await Promise.all([refreshProject(), refreshProjects(), refreshDest()]);
      setMsg("This project will now save backups to the new folder.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function clearProjectDestination() {
    if (!project) return;
    setError(null);
    try {
      await commitCommand(project.id, project.revision, "project.update_settings", {
        backup_dir: null,
      });
      await Promise.all([refreshProject(), refreshProjects(), refreshDest()]);
      setMsg("This project now uses the app-wide backup folder.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function previewAndImport() {
    let p = importPath.trim();
    if (!p) {
      const picked = await pickFile("Choose an .atomicpkg backup", [
        { name: "Atomic project package", extensions: ["atomicpkg"] },
      ]);
      if (!picked) return;
      p = picked;
      setImportPath(p);
    }
    setError(null);
    try {
      const prev = await apiFetch<PackagePreview>("/api/packages/preview", {
        method: "POST",
        body: JSON.stringify({ package_path: p }),
      });
      setPreview(prev);
    } catch (e) {
      setPreview(null);
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function confirmImport() {
    if (!importPath.trim()) return;
    setPreview(null);
    await run("backup.import", { package_path: importPath.trim() });
  }

  const destOk = dest && dest.errors.length === 0;

  return (
    <div className="mx-auto max-w-5xl p-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Backups &amp; portability</h2>
          <p className="mt-0.5 text-sm text-muted">
            Whole-project <code>.atomicpkg</code> archives — hash-verified, restorable on any install.
          </p>
        </div>
        <button
          onClick={() => void exportBackup()}
          disabled={busy || !destOk || !!activeJob}
          className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
        >
          Back up this project
        </button>
      </div>

      {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}
      {msg && <p className="mt-3 rounded border border-teal/40 bg-teal/10 p-2 text-sm text-teal">{msg}</p>}

      {/* destination */}
      <div className="mt-5 rounded-lg border border-line bg-card p-4">
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-muted">
              Backup destination
            </h3>
            <p className="mt-1 truncate font-mono text-sm" title={dest?.dir}>
              {dest?.dir ?? "…"}
            </p>
            <p className="mt-1 text-xs text-faint">
              {dest?.source === "project override"
                ? "Custom folder for this project only"
                : dest?.source === "app setting"
                  ? "App-wide backup folder (set in Settings)"
                  : "Built-in app-data folder"}
            </p>
          </div>
          <div className="flex shrink-0 gap-2">
            <button
              onClick={() => void changeProjectDestination()}
              className="rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:text-fg"
            >
              Choose folder for this project…
            </button>
            {dest?.project_override ? (
              <button
                onClick={() => void clearProjectDestination()}
                className="rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:text-fg"
              >
                Use app default
              </button>
            ) : null}
          </div>
        </div>
        {dest && (
          <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
            <span>
              Free space: <b className="text-fg">{fmtBytes(dest.free_bytes)}</b>
            </span>
            <span>
              Estimated backup size: <b className="text-fg">{fmtBytes(dest.estimated_bytes)}</b>
            </span>
          </div>
        )}
        {dest?.errors.map((e) => (
          <p key={e} className="mt-2 rounded border border-danger/40 bg-danger/10 p-2 text-xs text-danger">
            {e}
          </p>
        ))}
        {dest?.warnings.map((w) => (
          <p key={w} className="mt-2 rounded border border-amber/40 bg-amber/10 p-2 text-xs text-amber">
            {w}
          </p>
        ))}
      </div>

      {/* live progress */}
      {activeJob && (
        <div className="mt-4 rounded-lg border border-teal/30 bg-card p-4">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium">
              {activeJob.adapter_id === "ops.backup.export"
                ? "Backing up…"
                : activeJob.adapter_id === "ops.backup.import"
                  ? "Importing package…"
                  : "Working…"}
            </h3>
            <button
              onClick={() => void cancelJob(activeJob.id)}
              className="rounded-md border border-line px-3 py-1 text-xs text-muted hover:text-danger"
            >
              {activeJob.cancel_requested ? "Cancelling…" : "Cancel"}
            </button>
          </div>
          <div className="mt-3 h-2 rounded-full bg-line">
            <div
              className="h-full rounded-full bg-teal transition-all"
              style={{ width: `${Math.round(activeJob.progress * 100)}%` }}
            />
          </div>
          <div className="mt-2 flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted">
            <span>{Math.round(activeJob.progress * 100)}%</span>
            {activeJob.progress_json && (
              <>
                <span>
                  {fmtBytes(activeJob.progress_json.bytes_done)} / {fmtBytes(activeJob.progress_json.bytes_total)}
                </span>
                <span>{fmtBytes(activeJob.progress_json.rate_bps)}/s</span>
                <span>{fmtEta(activeJob.progress_json.eta_seconds)}</span>
                {activeJob.progress_json.current_file && (
                  <span className="truncate font-mono" title={activeJob.progress_json.current_file}>
                    file {activeJob.progress_json.current_file}
                  </span>
                )}
              </>
            )}
            <span className="text-faint">{activeJob.message}</span>
          </div>
        </div>
      )}

      {/* history */}
      <h3 className="mt-6 text-xs font-semibold uppercase tracking-wide text-muted">Backup history</h3>
      <div className="mt-2 overflow-hidden rounded-md border border-line">
        <table className="w-full text-sm">
          <thead className="bg-panel text-left text-xs text-muted">
            <tr>
              <th className="px-3 py-2">Created</th>
              <th className="px-3 py-2">Package</th>
              <th className="px-3 py-2">Size</th>
              <th className="px-3 py-2">Status</th>
              <th className="px-3 py-2" />
            </tr>
          </thead>
          <tbody>
            {backups.map((b) => (
              <tr key={b.id} className="border-t border-line">
                <td className="px-3 py-2 text-muted">{new Date(b.created_at).toLocaleString()}</td>
                <td className="max-w-[280px] truncate px-3 py-2 text-muted" title={b.package_path}>
                  {b.package_path || "—"}
                </td>
                <td className="px-3 py-2 text-muted">{b.byte_size ? fmtBytes(b.byte_size) : "—"}</td>
                <td className="px-3 py-2">
                  <span className={
                    b.status === "succeeded" ? "text-teal"
                      : b.status === "failed" ? "text-danger"
                        : "text-amber"
                  }>
                    {b.status === "failed" && (b.error ?? "").includes("interrupted")
                      ? "interrupted"
                      : b.status}
                  </span>
                  {b.error && <div className="max-w-[260px] text-xs text-danger">{b.error}</div>}
                </td>
                <td className="px-3 py-2 text-right">
                  {b.status === "succeeded" && (
                    <button
                      onClick={() => setRestoreRun(b)}
                      className="rounded border border-line px-2 py-1 text-xs text-muted hover:text-fg"
                    >
                      Restore as new project…
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {backups.length === 0 && (
              <tr>
                <td colSpan={5} className="px-3 py-6 text-center text-sm text-muted">
                  No backups yet. Create one before big changes — restores always make a new project.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {/* import */}
      <h3 className="mt-8 text-xs font-semibold uppercase tracking-wide text-muted">Import a package</h3>
      <div className="mt-2 flex gap-2">
        <input
          value={importPath}
          onChange={(e) => setImportPath(e.target.value)}
          placeholder="Path to an .atomicpkg file…"
          className="flex-1 rounded-md border border-line bg-card px-3 py-2 text-sm"
        />
        <button
          onClick={() =>
            void pickFile("Choose an .atomicpkg backup", [
              { name: "Atomic project package", extensions: ["atomicpkg"] },
            ]).then((p) => p && setImportPath(p))
          }
          className="rounded-md border border-line px-3 py-2 text-sm text-muted hover:text-fg"
        >
          Browse…
        </button>
        <button
          onClick={() => void previewAndImport()}
          disabled={busy}
          className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg disabled:opacity-40"
        >
          Inspect
        </button>
      </div>

      {preview && (
        <div className="mt-3 rounded-md border border-line bg-card p-4">
          <h4 className="font-medium">{preview.project_name}</h4>
          <p className="mt-1 text-sm text-muted">
            Format v{preview.format_version} · {preview.files} files · {fmtBytes(preview.byte_size)}
            {preview.created_at ? ` · created ${new Date(preview.created_at).toLocaleString()}` : ""}
          </p>
          <p className="mt-1 text-xs text-faint">
            {Object.entries(preview.counts)
              .map(([k, v]) => `${v} ${k}`)
              .join(" · ")}
          </p>
          <div className="mt-4 flex gap-2">
            <button
              onClick={() => void confirmImport()}
              disabled={busy}
              className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
            >
              Import as new project
            </button>
            <button
              onClick={() => setPreview(null)}
              className="rounded-md border border-line px-4 py-2 text-sm text-muted hover:text-fg"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={restoreRun !== null}
        onOpenChange={(o) => !o && setRestoreRun(null)}
        title="Restore this backup?"
        body={`A NEW project will be created from "${restoreRun?.project_name}" — your live project is never modified.`}
        confirmLabel="Restore as new project"
        onConfirm={() =>
          restoreRun && void run("backup.restore", { backup_run_id: restoreRun.id })
        }
      />
    </div>
  );
}
