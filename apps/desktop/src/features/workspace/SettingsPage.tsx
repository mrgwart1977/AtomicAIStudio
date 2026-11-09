import { useCallback, useEffect, useState } from "react";
import { commitCommand, pickDirectory } from "../../api/commands";
import { apiFetch } from "../../api/client";
import type { BackupDestination, StorageLocation } from "../../api/types";
import { useApp } from "../../stores/appStore";
import { fmtBytes } from "./BackupsPage";

const FORMATS = [
  { label: "16:9 — 1920×1080", ratio: "16:9", w: 1920, h: 1080 },
  { label: "2.39:1 — 2048×858", ratio: "2.39:1", w: 2048, h: 858 },
  { label: "9:16 — 1080×1920", ratio: "9:16", w: 1080, h: 1920 },
  { label: "1:1 — 1080×1080", ratio: "1:1", w: 1080, h: 1080 },
  { label: "4:3 — 1440×1080", ratio: "4:3", w: 1440, h: 1080 },
] as const;

export function SettingsPage() {
  const project = useApp((s) => s.currentProject());
  const locations = useApp((s) => s.locations);
  const refreshLocations = useApp((s) => s.refreshLocations);
  const refreshProject = useApp((s) => s.refreshProjectData);
  const refreshProjects = useApp((s) => s.refreshProjects);

  const [fmt, setFmt] = useState(0);
  const [fps, setFps] = useState("24");
  const [style, setStyle] = useState("none");
  const [locId, setLocId] = useState("");
  const [newLocLabel, setNewLocLabel] = useState("");
  const [newLocPath, setNewLocPath] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [backupDest, setBackupDest] = useState<BackupDestination | null>(null);

  const refreshBackupDest = useCallback(async () => {
    try {
      setBackupDest(await apiFetch<BackupDestination>("/api/settings/backup-dir"));
    } catch {
      setBackupDest(null);
    }
  }, []);

  useEffect(() => {
    void refreshLocations();
    void refreshBackupDest();
  }, [refreshLocations, refreshBackupDest]);

  useEffect(() => {
    if (!project) return;
    const i = FORMATS.findIndex(
      (f) => f.ratio === project.aspect_ratio && f.w === project.width && f.h === project.height,
    );
    setFmt(i >= 0 ? i : 0);
    const rate = project.fps_num / project.fps_den;
    setFps(Math.abs(rate - 23.976) < 0.01 ? "23.976" : String(Math.round(rate)));
    setStyle(project.style || "none");
    setLocId(project.storage_location_id ?? "");
  }, [project]);

  if (!project) return null;

  async function save() {
    if (!project) return;
    setBusy(true);
    setError(null);
    setMsg(null);
    const f = FORMATS[fmt];
    const [num, den] = fps === "23.976" ? [24000, 1001] : [parseInt(fps, 10), 1];
    try {
      await commitCommand(project.id, project.revision, "project.update_settings", {
        kind: project.kind,
        aspect_ratio: f.ratio,
        width: f.w,
        height: f.h,
        fps_num: num,
        fps_den: den,
        style,
        storage_location_id: locId || null,
      });
      setMsg("Settings saved.");
      await Promise.all([refreshProject(), refreshProjects()]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function setAppBackupDir() {
    const dir = await pickDirectory("Choose the default backup folder for all projects");
    if (!dir) return;
    setError(null);
    setMsg(null);
    try {
      await apiFetch("/api/settings/backup-dir", {
        method: "PUT",
        body: JSON.stringify({ dir }),
      });
      await refreshBackupDest();
      setMsg("Default backup folder updated for all projects.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function resetAppBackupDir() {
    setError(null);
    setMsg(null);
    try {
      await apiFetch("/api/settings/backup-dir", {
        method: "PUT",
        body: JSON.stringify({ dir: null }),
      });
      await refreshBackupDest();
      setMsg("Backup folder reset to the built-in location.");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  async function addLocation() {
    setError(null);
    try {
      await apiFetch<StorageLocation>("/api/storage/locations", {
        method: "POST",
        body: JSON.stringify({ label: newLocLabel.trim(), root_path: newLocPath.trim() }),
      });
      setNewLocLabel("");
      setNewLocPath("");
      await refreshLocations();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div className="mx-auto max-w-3xl p-6">
      <h2 className="text-xl font-semibold">Project settings</h2>

      <div className="mt-5 rounded-lg border border-line bg-card p-5">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">Picture format</h3>
        <div className="mt-3 grid grid-cols-2 gap-4">
          <label className="block text-sm text-muted">
            Frame size
            <select value={fmt} onChange={(e) => setFmt(parseInt(e.target.value, 10))}
              className="mt-1 w-full rounded-md border border-line bg-panel px-3 py-2 text-sm text-fg">
              {FORMATS.map((f, i) => (
                <option key={f.label} value={i}>{f.label}</option>
              ))}
            </select>
          </label>
          <label className="block text-sm text-muted">
            Frame rate
            <select value={fps} onChange={(e) => setFps(e.target.value)}
              className="mt-1 w-full rounded-md border border-line bg-panel px-3 py-2 text-sm text-fg">
              {["24", "25", "30", "23.976", "48", "60"].map((f) => (
                <option key={f} value={f}>{f} fps</option>
              ))}
            </select>
          </label>
        </div>
        <label className="mt-4 block text-sm text-muted">
          Cinematic style
          <select value={style} onChange={(e) => setStyle(e.target.value)}
            className="mt-1 w-full rounded-md border border-line bg-panel px-3 py-2 text-sm text-fg">
            {["none", "cinematic", "documentary", "anime", "noir", "dreamlike"].map((s) => (
              <option key={s} value={s}>{s === "none" ? "No preset" : s[0].toUpperCase() + s.slice(1)}</option>
            ))}
          </select>
        </label>
      </div>

      <div className="mt-5 rounded-lg border border-line bg-card p-5">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">Storage</h3>
        <p className="mt-1 text-xs text-faint">
          Media files live on disk — SQLite only keeps references, so projects stay fast at any size.
        </p>
        <label className="mt-3 block text-sm text-muted">
          This project's storage location
          <select value={locId} onChange={(e) => setLocId(e.target.value)}
            className="mt-1 w-full rounded-md border border-line bg-panel px-3 py-2 text-sm text-fg">
            <option value="">Default location</option>
            {locations.map((l) => (
              <option key={l.id} value={l.id}>
                {l.label} — {l.root_path}{l.is_default ? " (default)" : ""}
              </option>
            ))}
          </select>
        </label>

        <h4 className="mt-5 text-xs font-semibold uppercase tracking-wide text-muted">Add a storage location</h4>
        <div className="mt-2 grid grid-cols-[200px_1fr_auto] gap-2">
          <input
            value={newLocLabel}
            onChange={(e) => setNewLocLabel(e.target.value)}
            placeholder="Label (e.g. External drive)"
            className="rounded-md border border-line bg-panel px-3 py-2 text-sm"
          />
          <input
            value={newLocPath}
            onChange={(e) => setNewLocPath(e.target.value)}
            placeholder="Root path (e.g. D:\\AtomicMedia)"
            className="rounded-md border border-line bg-panel px-3 py-2 text-sm"
          />
          <button
            onClick={() => void addLocation()}
            disabled={!newLocLabel.trim() || !newLocPath.trim()}
            className="rounded-md border border-line px-3 py-2 text-sm text-muted hover:text-fg disabled:opacity-40"
          >
            Add
          </button>
        </div>
      </div>

      <div className="mt-5 rounded-lg border border-line bg-card p-5">
        <h3 className="text-sm font-semibold uppercase tracking-wide text-muted">Backups</h3>
        <p className="mt-1 text-xs text-faint">
          Where <code>.atomicpkg</code> project backups are saved by default. Individual projects can
          override this on their Backups page — they never silently fall back.
        </p>
        <div className="mt-3 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="truncate font-mono text-sm" title={backupDest?.dir}>
              {backupDest?.dir ?? "…"}
            </p>
            <p className="mt-1 text-xs text-faint">
              {backupDest?.source === "app setting"
                ? "Custom app-wide folder"
                : "Built-in app-data folder"}
              {backupDest ? ` · ${fmtBytes(backupDest.free_bytes)} free` : ""}
            </p>
            {backupDest?.errors.map((e) => (
              <p key={e} className="mt-2 rounded border border-danger/40 bg-danger/10 p-2 text-xs text-danger">
                {e}
              </p>
            ))}
            {backupDest?.warnings.map((w) => (
              <p key={w} className="mt-2 rounded border border-amber/40 bg-amber/10 p-2 text-xs text-amber">
                {w}
              </p>
            ))}
          </div>
          <div className="flex shrink-0 gap-2">
            <button
              onClick={() => void setAppBackupDir()}
              className="rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:text-fg"
            >
              Choose folder…
            </button>
            {backupDest?.source === "app setting" && (
              <button
                onClick={() => void resetAppBackupDir()}
                className="rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:text-fg"
              >
                Reset to default
              </button>
            )}
          </div>
        </div>
      </div>

      {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}
      {msg && <p className="mt-3 rounded border border-teal/40 bg-teal/10 p-2 text-sm text-teal">{msg}</p>}
      <div className="mt-5 flex justify-end">
        <button
          onClick={() => void save()}
          disabled={busy}
          className="rounded-md bg-action px-5 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
        >
          {busy ? "Saving…" : "Save settings"}
        </button>
      </div>
    </div>
  );
}
