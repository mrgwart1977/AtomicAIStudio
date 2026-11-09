import { useState } from "react";
import { commitCommand, pickFile } from "../../api/commands";
import { apiFetch } from "../../api/client";
import type { BackupRun, PackagePreview } from "../../api/types";
import { ConfirmDialog } from "../../components/ConfirmDialog";
import { useApp } from "../../stores/appStore";

function fmtBytes(n: number): string {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log2(n) / 10));
  return `${(n / 2 ** (10 * i)).toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function BackupsPage() {
  const project = useApp((s) => s.currentProject());
  const backups = useApp((s) => s.backups);
  const refresh = useApp((s) => s.refreshBackups);
  const refreshProject = useApp((s) => s.refreshProjectData);
  const refreshProjects = useApp((s) => s.refreshProjects);

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [importPath, setImportPath] = useState("");
  const [preview, setPreview] = useState<PackagePreview | null>(null);
  const [restoreRun, setRestoreRun] = useState<BackupRun | null>(null);

  async function run(type: string, payload: Record<string, unknown>): Promise<boolean> {
    if (!project) return false;
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      await commitCommand(project.id, project.revision, type, payload);
      setMsg("Queued — follow progress in the Jobs panel. This page refreshes when it finishes.");
      await Promise.all([refresh(), refreshProject(), refreshProjects()]);
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
          disabled={busy}
          className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
        >
          Back up this project
        </button>
      </div>

      {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}
      {msg && <p className="mt-3 rounded border border-teal/40 bg-teal/10 p-2 text-sm text-teal">{msg}</p>}

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
                <td className="px-3 py-2 text-muted">{fmtBytes(b.byte_size)}</td>
                <td className="px-3 py-2">
                  <span className={
                    b.status === "succeeded" ? "text-teal" : b.status === "failed" ? "text-danger" : "text-amber"
                  }>
                    {b.status}
                  </span>
                  {b.error && <div className="text-xs text-danger">{b.error}</div>}
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
