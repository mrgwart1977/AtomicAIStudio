import { useState } from "react";
import { commitCommand, pickFile } from "../../api/commands";
import { apiFetch } from "../../api/client";
import { AuthImage } from "../../components/AuthImage";
import type { Asset, SharedAsset } from "../../api/types";
import { useApp } from "../../stores/appStore";

function fmtBytes(n: number): string {
  if (!n) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log2(n) / 10));
  return `${(n / 2 ** (10 * i)).toFixed(i ? 1 : 0)} ${u[i]}`;
}

const KINDS = ["", "image", "video", "audio", "document", "model"] as const;

/** Simulated provenance (mock + C2 cloud adapters) — honest labeling,
 * never shown as genuine provider output. */
function isSimulated(a: Asset): boolean {
  const p = a.provenance_json ?? {};
  const src = String(a.media_json?.source ?? "");
  return p.provenance_confidence === "simulated"
    || p.simulated === true
    || a.media_json?.is_mock === true
    || src.includes("simulat") || src.includes("mock");
}

/** C1.5 assisted workflow — user-reported external provenance via
 * bridge.import. Distinct from simulated (no mock transport involved)
 * and never confused with verified remote API output. */
function isAssisted(a: Asset): boolean {
  const p = a.provenance_json ?? {};
  return p.provenance_confidence === "user_reported"
    || (p.external_provider != null && !isSimulated(a));
}

export function AssetsPage() {
  const project = useApp((s) => s.currentProject());
  const page = useApp((s) => s.assetsPage);
  const kindFilter = useApp((s) => s.assetKindFilter);
  const setKindFilter = useApp((s) => s.setAssetKindFilter);
  const refreshAssets = useApp((s) => s.refreshAssets);
  const refreshProject = useApp((s) => s.refreshProjectData);

  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [importPath, setImportPath] = useState("");
  const [shared, setShared] = useState<SharedAsset[] | null>(null);
  const [missingOnly, setMissingOnly] = useState(false);
  const [detailId, setDetailId] = useState<string | null>(null);

  const items = (page?.items ?? []).filter((a) => !missingOnly || a.status === "missing");

  async function run(type: string, payload: Record<string, unknown>): Promise<boolean> {
    if (!project) return false;
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      const r = await commitCommand(project.id, project.revision, type, payload);
      setMsg(r.job_id ? "Queued — follow progress in the Jobs panel." : "Done.");
      await Promise.all([refreshAssets(), refreshProject()]);
      return true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function importAsset() {
    let p = importPath.trim();
    if (!p) {
      const picked = await pickFile("Import media file");
      if (picked) setImportPath(picked);
      p = picked ?? p;
      if (!p) return;
    }
    if (await run("asset.import", { source_path: p })) setImportPath("");
  }

  async function share(a: Asset) {
    await run("asset.share", { asset_id: a.id });
  }

  async function link(a: Asset, mode: "link" | "copy") {
    await run("asset.link", { asset_id: a.id, mode });
  }

  async function relink(a: Asset) {
    const picked = await pickFile(`Locate the file for "${a.name}"`);
    const p = picked ?? window.prompt(`Full path to the file for "${a.name}"`) ?? "";
    if (p.trim()) await run("asset.relink", { asset_id: a.id, source_path: p.trim() });
  }

  return (
    <div className="mx-auto max-w-6xl p-6">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-xl font-semibold">Assets</h2>
          <p className="mt-0.5 text-sm text-muted">
            {page ? `${page.total} file${page.total === 1 ? "" : "s"} in this project — media lives on disk, verified by content hash.` : "Loading…"}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => void run("assets.verify", {})}
            disabled={busy}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-fg disabled:opacity-40"
          >
            Verify all
          </button>
          <button
            onClick={() => {
              if (shared !== null) setShared(null);
              else if (project)
                void apiFetch<SharedAsset[]>(`/api/projects/${project.id}/shared-assets`).then(setShared);
            }}
            className="rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-fg"
          >
            Shared library
          </button>
        </div>
      </div>

      {/* import bar */}
      <div className="mt-4 flex gap-2">
        <input
          value={importPath}
          onChange={(e) => setImportPath(e.target.value)}
          placeholder="Path to a file to import (or use Browse)…"
          className="flex-1 rounded-md border border-line bg-card px-3 py-2 text-sm"
        />
        <button
          onClick={() => void pickFile("Import media file").then((p) => p && setImportPath(p))}
          className="rounded-md border border-line px-3 py-2 text-sm text-muted hover:text-fg"
        >
          Browse…
        </button>
        <button
          onClick={() => void importAsset()}
          disabled={busy}
          className="rounded-md bg-action px-4 py-2 text-sm font-medium text-white hover:bg-action-hi disabled:opacity-40"
        >
          Import
        </button>
      </div>

      <div className="mt-4 flex items-center gap-3">
        <select
          value={kindFilter}
          onChange={(e) => setKindFilter(e.target.value)}
          className="rounded-md border border-line bg-card px-2 py-1.5 text-sm"
        >
          {KINDS.map((k) => (
            <option key={k} value={k}>{k || "All kinds"}</option>
          ))}
        </select>
        <label className="flex items-center gap-1.5 text-xs text-muted">
          <input type="checkbox" checked={missingOnly} onChange={(e) => setMissingOnly(e.target.checked)} />
          Missing files only
        </label>
      </div>

      {error && <p className="mt-3 rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">{error}</p>}
      {msg && <p className="mt-3 rounded border border-teal/40 bg-teal/10 p-2 text-sm text-teal">{msg}</p>}

      <div className="mt-5 grid grid-cols-4 gap-4">
        {items.map((a) => (
          <div key={a.id} className="overflow-hidden rounded-md border border-line bg-card">
            <div className="flex h-24 items-center justify-center bg-panel text-faint">
              {a.kind === "image" && a.status !== "missing" ? (
                <AuthImage
                  pid={a.project_id}
                  assetId={a.id}
                  alt=""
                  className="h-full w-full object-cover"
                  fallback={<span className="text-xs uppercase tracking-widest">{a.kind}</span>}
                />
              ) : (
                <span className="text-xs uppercase tracking-widest">{a.kind}</span>
              )}
            </div>
            <div className="p-2.5">
              <div className="flex items-center justify-between gap-1">
                <span className="truncate text-xs font-medium" title={a.name}>{a.name}</span>
                {a.status === "missing" && <span className="text-[10px] uppercase text-danger">missing</span>}
                {a.scope === "shared" && <span className="text-[10px] uppercase text-violet">shared</span>}
                {isSimulated(a) && <span className="text-[10px] uppercase text-amber">simulated</span>}
                {isAssisted(a) && <span className="text-[10px] uppercase text-sky">assisted</span>}
              </div>
              <div className="mt-0.5 text-[10px] text-faint">
                {a.origin} · {fmtBytes(a.byte_size)}
              </div>
              <div className="mt-2 flex gap-1">
                {a.status === "missing" ? (
                  <button onClick={() => void relink(a)} className="rounded border border-amber/60 px-2 py-0.5 text-[10px] text-amber hover:bg-amber/10">
                    Relink…
                  </button>
                ) : (
                  <button onClick={() => void share(a)} className="rounded border border-line px-2 py-0.5 text-[10px] text-muted hover:text-fg">
                    Share
                  </button>
                )}
                <button onClick={() => setDetailId(detailId === a.id ? null : a.id)}
                  className="rounded border border-line px-2 py-0.5 text-[10px] text-muted hover:text-fg">
                  Details
                </button>
              </div>
              {detailId === a.id && (
                <div className="mt-2 rounded border border-line bg-panel p-2" data-testid={`asset-detail-${a.id}`}>
                  {isSimulated(a) && (
                    <p className="mb-1.5 rounded border border-amber/50 bg-amber/10 px-1.5 py-1 text-[10px] font-semibold uppercase tracking-wide text-amber">
                      SIMULATION — No external AI provider contacted.
                    </p>
                  )}
                  <dl className="space-y-0.5 text-[10px]">
                    <div className="flex justify-between"><dt className="text-faint">provenance</dt><dd className="text-muted">{String(a.provenance_json?.provenance_confidence ?? a.origin)}</dd></div>
                    {a.provenance_json?.remote != null && (
                      <div className="flex justify-between"><dt className="text-faint">remote id</dt><dd className="text-muted">{String((a.provenance_json.remote as Record<string, unknown>)?.remote_job_id ?? "—")} (synthetic)</dd></div>
                    )}
                    {isAssisted(a) && (
                      <div className="flex justify-between"><dt className="text-faint">external source</dt><dd className="text-muted">{String(a.provenance_json?.external_provider ?? "—")} · {String(a.provenance_json?.external_method ?? "import")} (user-reported)</dd></div>
                    )}
                    <div className="flex justify-between"><dt className="text-faint">adapter</dt><dd className="text-muted">{String(a.provenance_json?.adapter_id ?? "—")}</dd></div>
                    <div className="flex justify-between"><dt className="text-faint">sha256</dt><dd className="truncate pl-2 text-muted" title={a.sha256}>{a.sha256.slice(0, 16)}…</dd></div>
                  </dl>
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
      {items.length === 0 && (
        <p className="mt-8 text-center text-sm text-muted">
          {missingOnly ? "No missing files." : "No assets yet — import a file or generate one."}
        </p>
      )}

      {/* shared library drawer */}
      {shared !== null && (
        <div className="fixed inset-y-0 right-0 z-20 flex w-96 flex-col border-l border-line bg-panel shadow-2xl">
          <div className="flex items-center justify-between border-b border-line p-4">
            <h3 className="font-semibold">Shared library</h3>
            <button onClick={() => setShared(null)} className="text-muted hover:text-fg">✕</button>
          </div>
          <div className="flex-1 overflow-y-auto p-4">
            {shared.length === 0 && (
              <p className="text-sm text-muted">
                Nothing shared yet. Load the library, or mark an asset "Share" to publish it here
                for your other projects.
              </p>
            )}
            {shared.map((s) => (
              <div key={s.asset.id} className="mb-3 rounded-md border border-line bg-card p-3">
                <div className="text-sm font-medium">{s.asset.name}</div>
                <div className="mt-0.5 text-xs text-muted">
                  from “{s.source_project_name}” · {s.asset.kind} · {fmtBytes(s.asset.byte_size)}
                </div>
                {!s.linked_here && (
                  <div className="mt-2 flex gap-2">
                    <button
                      onClick={() => void link(s.asset, "link")}
                      className="rounded border border-line px-2 py-1 text-xs text-muted hover:text-fg"
                    >
                      Link (reference)
                    </button>
                    <button
                      onClick={() => void link(s.asset, "copy")}
                      className="rounded border border-line px-2 py-1 text-xs text-muted hover:text-fg"
                    >
                      Copy into project
                    </button>
                  </div>
                )}
                {s.linked_here && <div className="mt-2 text-[10px] uppercase text-teal">already in this project</div>}
              </div>
            ))}
          </div>
          {project && shared.length > 0 && (
            <div className="border-t border-line p-4">
              <button
                onClick={() => {
                  void apiFetch<SharedAsset[]>(`/api/projects/${project.id}/shared-assets`).then(setShared);
                }}
                className="w-full rounded-md bg-action px-3 py-2 text-sm text-white"
              >
                Refresh
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
