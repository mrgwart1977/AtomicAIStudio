import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../api/client";
import { useApp } from "../stores/appStore";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { ImportModelDialog } from "./ImportModelDialog";
import { DiscoverySection } from "./DiscoverySection";
import type {
  AvailabilityClass,
  AvailabilityReason,
  CompatModelEntry,
  CompatibilityReport,
  CompatStatus,
  ImportCapabilitiesOut,
  ManagedCopyReleaseOut,
  ModelArtifact,
  ModelArtifactDetail,
  ModelCatalogEntry,
  ModelPreflight,
  ModelValidation,
  ModelsDir,
} from "../api/types";
import {
  isLoraEntry, loraCatalogState, loraRole,
  LORA_RUNTIME_NOTE, LORA_STATE_BADGE,
} from "./workspace/lora";

function gb(bytes: number | null | undefined): string {
  if (bytes == null) return "unknown";
  return `${(bytes / 1e9).toFixed(1)} GB`;
}

/** Basename for managed URIs — absolute managed paths never render in
 * ordinary library rows (U-2b-2b privacy contract). */
function baseName(p: string): string {
  const parts = p.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? p;
}

/** Visible-list-only dedup note — never a global/system claim. */
function sharedManagedCount(artifacts: ModelArtifact[], a: ModelArtifact): number {
  if (a.managed_by !== "atomic" || !a.uri) return 0;
  return artifacts.filter(
    (b) => b.uri === a.uri && b.artifact_id !== a.artifact_id).length;
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

/** C5-E — the five beginner-facing availability classes folded from
 *  classification + install state. */
const AVAIL: Record<AvailabilityClass, { label: string; cls: string }> = {
  installed_operational: { label: "Installed & ready", cls: "bg-teal/15 text-teal" },
  installed_limited: { label: "Installed — limited", cls: "bg-amber/15 text-amber" },
  installable: { label: "Can be installed", cls: "bg-sky/15 text-sky" },
  planned: { label: "Coming in a future update", cls: "bg-line/40 text-muted" },
  blocked: { label: "Not available", cls: "bg-rose/15 text-rose" },
};

/** S-C5 — the machine-readable cause behind a blocked/limited verdict,
 * rendered so "Not available" never collapses license, download, and
 * hardware gaps into one undifferentiated failure. */
const REASON: Record<AvailabilityReason, string> = {
  missing_runtime: "runtime not installed",
  missing_model_files: "model files missing",
  license_restricted: "restricted license",
  download_unavailable: "no verified download",
  hardware_incompatible: "hardware incompatible",
  hardware_limited: "limited on this hardware",
  compatibility_unknown: "compatibility unknown",
  planned: "planned for a future milestone",
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
  const artifacts = useApp((s) => s.artifacts);
  const gpuState = useApp((s) => s.gpuState);
  const jobs = useApp((s) => s.jobs);
  const refreshHw = useApp((s) => s.refreshHardware);
  const refreshModels = useApp((s) => s.refreshModels);

  const [modelsDir, setModelsDir] = useState<ModelsDir | null>(null);
  const [compat, setCompat] = useState<CompatibilityReport | null>(null);
  const [installTarget, setInstallTarget] = useState<ModelCatalogEntry | null>(null);
  const [removeTarget, setRemoveTarget] = useState<ModelCatalogEntry | null>(null);
  const [preflight, setPreflight] = useState<ModelPreflight | null>(null);
  const [checks, setChecks] = useState<Record<string, ModelValidation | "loading" | null>>({});
  const [benchTarget, setBenchTarget] = useState<CompatModelEntry | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  // U-3 — candidate-driven import prefill; ephemeral, cleared by the
  // dialog's own reset exactly like the picker/manual path.
  const [importInitialPath, setImportInitialPath] = useState("");
  const [discoveryRefresh, setDiscoveryRefresh] = useState(0);
  const [detail, setDetail] = useState<Record<string, ModelArtifactDetail | "loading" | null>>({});
  const [importCaps, setImportCaps] = useState<ImportCapabilitiesOut | null>(null);
  const [releaseTarget, setReleaseTarget] = useState<ModelArtifact | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const refreshCompat = () =>
    apiFetch<CompatibilityReport>("/api/compatibility").then(setCompat).catch(() => {});

  // U-2b-2b — capability probe: only a well-formed flag-ON response may
  // enable the managed option; loading/unavailable/unauthorized/
  // malformed all resolve to null (fail closed, no footnote).
  const fetchImportCaps = () =>
    apiFetch<ImportCapabilitiesOut>("/api/models/import-capabilities")
      .then((v) => {
        const wellFormed = typeof v?.managed_copy_enabled === "boolean"
          && Array.isArray(v?.modes)
          && v.modes.every((m) => m === "reference" || m === "managed_copy");
        setImportCaps(wellFormed ? {
          managed_copy_enabled:
            v.managed_copy_enabled === true
            && v.modes.includes("managed_copy"),
          modes: v.modes,
        } : null);
      })
      .catch(() => setImportCaps(null));

  useEffect(() => {
    void refreshHw();
    void refreshModels();
    void refreshCompat();
    void fetchImportCaps();
    void apiFetch<ModelsDir>("/api/models/dir").then(setModelsDir).catch(() => {});
  }, [refreshHw, refreshModels]);

  const compatByKey = useMemo(
    () => new Map((compat?.models ?? []).map((m) => [m.key, m])),
    [compat],
  );

  // B-3a — attachable adapters (modality "lora") live in their own
  // section: they modify a base model at generation time, so listing
  // them among generatable models would misrepresent what they do.
  const generativeModels = useMemo(() => models.filter((m) => !isLoraEntry(m)), [models]);
  const loraModels = useMemo(() => models.filter(isLoraEntry), [models]);

  // Model-download jobs are app-level (no project) — find any in-flight ones.
  const downloadJobs = useMemo(
    () => Object.values(jobs).filter((j) => j.kind === "model_download"),
    [jobs],
  );
  // Refresh catalog whenever a download job lands/updates.
  const dlDone = downloadJobs.filter((j) => j.status !== "queued" && j.status !== "running").length;
  useEffect(() => {
    if (dlDone > 0) { void refreshModels(); void refreshCompat(); }
  }, [dlDone, refreshModels]);

  const benchJobs = useMemo(
    () => Object.values(jobs).filter((j) => j.kind === "model_benchmark"),
    [jobs],
  );
  const benchDone = benchJobs.filter((j) => j.status !== "queued" && j.status !== "running").length;
  useEffect(() => {
    if (benchDone > 0) void refreshCompat();
  }, [benchDone]);

  // U-2b-1 — refresh the artifact registry whenever a model-import job
  // lands in a terminal state (registered OR reused → library reflects it).
  const importJobs = useMemo(
    () => Object.values(jobs).filter((j) => j.kind === "model_import"),
    [jobs],
  );
  const importDone = importJobs.filter(
    (j) => j.status !== "queued" && j.status !== "running").length;
  useEffect(() => {
    if (importDone > 0) void refreshModels();
  }, [importDone, refreshModels]);

  // Lazy provenance: source_uri + inspection report live ONLY on the
  // authenticated detail endpoint — fetched on explicit expand, never in bulk.
  async function toggleProvenance(a: ModelArtifact) {
    const cur = detail[a.artifact_id];
    // cur != null (not !== undefined): a collapsed/failed entry must
    // REFETCH on re-expand — managed_copy.present is a live probe and
    // stale presence must never persist behind a dead expand click.
    if (cur != null) { setDetail((d) => ({ ...d, [a.artifact_id]: null })); return; }
    setDetail((d) => ({ ...d, [a.artifact_id]: "loading" }));
    try {
      const v = await apiFetch<ModelArtifactDetail>(`/api/models/artifacts/${a.artifact_id}`);
      setDetail((d) => ({ ...d, [a.artifact_id]: v }));
    } catch { setDetail((d) => ({ ...d, [a.artifact_id]: null })); }
  }

  // U-2b-2b — metadata-only release: detaches the artifact's managed
  // association. NEVER deletes the managed file or frees disk space;
  // available regardless of the managed-copy feature gate.
  async function releaseManaged(a: ModelArtifact) {
    setBusy(true); setErr(null);
    try {
      await apiFetch<ManagedCopyReleaseOut>(
        `/api/models/artifacts/${a.artifact_id}/release-managed-copy`,
        { method: "POST" });
      // `already:true` is a successful no-op — same refresh path.
      try {
        const v = await apiFetch<ModelArtifactDetail>(
          `/api/models/artifacts/${a.artifact_id}`);
        setDetail((d) => ({ ...d, [a.artifact_id]: v }));
      } catch { setDetail((d) => ({ ...d, [a.artifact_id]: null })); }
      void refreshModels();
    } catch (e) {
      const m = e instanceof Error ? e.message : String(e);
      if (m.startsWith("404")) {
        void refreshModels();
        setErr("That artifact no longer exists — the library was refreshed.");
      } else {
        setErr("The copy couldn't be detached — try again.");
      }
    } finally {
      setBusy(false);
    }
  }

  async function openPreflight(model: ModelCatalogEntry) {
    setInstallTarget(model);
    setPreflight(null);
    try {
      setPreflight(await apiFetch<ModelPreflight>(`/api/models/${model.key}/preflight`));
    } catch { /* dialog falls back to catalog data */ }
  }

  async function toggleChecks(model: ModelCatalogEntry) {
    const cur = checks[model.key];
    if (cur) { setChecks((c) => ({ ...c, [model.key]: null })); return; }
    setChecks((c) => ({ ...c, [model.key]: "loading" }));
    try {
      const v = await apiFetch<ModelValidation>(`/api/models/${model.key}/validation`);
      setChecks((c) => ({ ...c, [model.key]: v }));
    } catch { setChecks((c) => ({ ...c, [model.key]: null })); }
  }

  async function runBenchmark(model: CompatModelEntry) {
    setBusy(true);
    setErr(null);
    try {
      await apiFetch(`/api/models/${model.key}/benchmark`,
        { method: "POST", body: JSON.stringify({ runs: 3 }) });
      setBenchTarget(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function install(model: ModelCatalogEntry) {
    setBusy(true);
    setErr(null);
    try {
      await apiFetch(`/api/models/${model.key}/install`, { method: "POST", body: "{}" });
      setInstallTarget(null);
      void refreshModels();
      void refreshCompat();
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
        {generativeModels.map((m) => {
          const c = COMPAT[m.compatibility.status];
          const ce = compatByKey.get(m.key);
          const inst = m.install;
          const job = modelJob(m.key);
          const benchJob = benchJobs.find((j) => j.model_key === m.key
            && (j.status === "queued" || j.status === "running"));
          const downloading = job && (job.status === "queued" || job.status === "running");
          const installed = inst?.status === "installed";
          const s = SUPPORT[m.support];
          const avail = ce ? AVAIL[ce.availability] : null;
          const check = checks[m.key];
          return (
            <div key={m.key} className="rounded-lg border border-line bg-card p-4">
              <div className="flex flex-wrap items-center gap-2">
                <h2 className="text-base font-semibold">{m.display_name}</h2>
                <span className="text-xs text-faint">v{m.version} · {m.family}</span>
                {m.modality !== "image" && (
                  <span className="rounded bg-sky/15 px-2 py-0.5 text-xs capitalize text-sky">{m.modality}</span>
                )}
                {avail && <span className={`rounded px-2 py-0.5 text-xs ${avail.cls}`}>{avail.label}</span>}
                {ce?.availability_reason && (
                  <span className="rounded bg-line/40 px-2 py-0.5 text-xs text-muted">
                    {REASON[ce.availability_reason] ?? ce.availability_reason}
                  </span>
                )}
                {!avail && <span className={`rounded px-2 py-0.5 text-xs ${c.cls}`}>{c.label}</span>}
                {s && <span className={`rounded px-2 py-0.5 text-xs ${s.cls}`}>{s.label}</span>}
                {inst?.status === "failed" && (
                  <span className="rounded bg-rose/15 px-2 py-0.5 text-xs text-rose">install failed</span>
                )}
              </div>
              <p className="mt-1 text-sm text-muted">{m.notes}</p>

              <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-muted md:grid-cols-4">
                <div><dt className="inline text-faint">License: </dt><dd className="inline">{m.license.name}</dd></div>
                <div><dt className="inline text-faint">Download: </dt><dd className="inline">{gb(m.download_bytes)}</dd></div>
                <div><dt className="inline text-faint">Est. VRAM: </dt><dd className="inline">{gb(ce?.estimates.vram_bytes ?? m.requirements.min_vram_bytes)} <span className="text-faint">(estimate)</span></dd></div>
                <div><dt className="inline text-faint">Est. RAM: </dt><dd className="inline">{ce ? gb(ce.estimates.ram_bytes) : "unknown"} <span className="text-faint">(estimate)</span></dd></div>
                <div><dt className="inline text-faint">Runtime: </dt><dd className="inline">{ce?.runtime.key ?? m.runtime_key}{ce && !ce.runtime.known && " (unknown)"}{ce?.runtime.planned && " (planned)"}</dd></div>
                <div><dt className="inline text-faint">Source: </dt><dd className="inline">{String(m.source.provider ?? "local")}</dd></div>
                <div className="col-span-2"><dt className="inline text-faint">Can generate: </dt><dd className="inline">{m.modes.join(", ")}</dd></div>
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
              {benchJob && (
                <p className="mt-2 text-xs text-muted">
                  Benchmark running — {benchJob.message || "measuring"} ({Math.round(benchJob.progress * 100)}%)
                </p>
              )}
              {ce?.benchmark && !benchJob && (
                <div className="mt-2 rounded border border-line p-2 text-xs text-muted">
                  <span className="text-fg">Measured:</span> median {(ce.benchmark.median_duration_ms / 1000).toFixed(1)}s
                  over {ce.benchmark.runs.length} run(s)
                  {ce.benchmark.peak_worker_rss_bytes != null && <> · peak worker RAM {gb(ce.benchmark.peak_worker_rss_bytes)}</>}
                  {ce.benchmark.gpu && <> · {ce.benchmark.gpu}</>}
                  <p className="mt-1 text-faint">{ce.benchmark.estimate_vs_measured.warning}</p>
                </div>
              )}
              {check === "loading" && <p className="mt-2 text-xs text-muted">Running read-only checks…</p>}
              {check && check !== "loading" && (
                <ul className="mt-2 space-y-0.5 rounded border border-line p-2 text-xs">
                  {check.checks.map((ch) => (
                    <li key={ch.name} className={
                      ch.ok === false
                        ? (ch.severity === "warning" ? "text-amber"
                           : ch.severity === "info" ? "text-muted" : "text-rose")
                        : ch.ok === null ? "text-faint" : "text-teal"}>
                      {ch.ok === false ? "✗" : ch.ok === null ? "·" : "✓"} {ch.name.replaceAll("_", " ")} — {ch.detail}
                    </li>
                  ))}
                </ul>
              )}

              <div className="mt-3 flex flex-wrap gap-2">
                {!installed && !downloading && m.license.commercial_ok && m.files.length > 0 && m.support !== "planned" && (
                  <button
                    onClick={() => void openPreflight(m)}
                    disabled={busy}
                    className="rounded-md bg-action px-3 py-1.5 text-xs text-ink disabled:opacity-50"
                  >
                    Install…
                  </button>
                )}
                {ce?.benchmarkable && !benchJob && (
                  <button
                    onClick={() => setBenchTarget(ce)}
                    disabled={busy}
                    className="rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:text-fg disabled:opacity-50"
                  >
                    Run benchmark…
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
                <button
                  onClick={() => void toggleChecks(m)}
                  className="rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:text-fg"
                >
                  {check ? "Hide checks" : "Compatibility checks"}
                </button>
              </div>
            </div>
          );
        })}
      </section>

      {/* --- adapters (LoRA) --- */}
      <section className="mt-8">
        <h2 className="text-lg font-semibold">Adapters (LoRA)</h2>
        <p className="mt-1 text-sm text-muted">
          Adapters tune an installed base model for a character identity or a visual
          style at generation time — they are not models you run on their own.
        </p>
        <p className="mt-1 text-xs text-faint" role="note">{LORA_RUNTIME_NOTE}</p>
        {loraModels.length === 0 ? (
          <div className="mt-3 rounded-lg border border-dashed border-line p-4 text-sm text-muted">
            No adapters are cataloged yet. When LoRA adapters are added to the
            catalog and installed, they appear here with their role, base-family
            compatibility, and verification status.
          </div>
        ) : (
          <div className="mt-3 space-y-3">
            {loraModels.map((m) => {
              const inst = m.install;
              const job = modelJob(m.key);
              const downloading = job && (job.status === "queued" || job.status === "running");
              const installed = inst?.status === "installed";
              const role = loraRole(m);
              const state = LORA_STATE_BADGE[loraCatalogState(m, null)];
              const s = SUPPORT[m.support];
              const check = checks[m.key];
              return (
                <div key={m.key} className="rounded-lg border border-line bg-card p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <h3 className="text-base font-semibold">{m.display_name}</h3>
                    <span className="text-xs text-faint">v{m.version}</span>
                    {role && (
                      <span className="rounded bg-sky/15 px-2 py-0.5 text-xs capitalize text-sky">
                        {role} adapter
                      </span>
                    )}
                    <span className="rounded bg-line/40 px-2 py-0.5 text-xs text-muted">
                      for {m.family} bases
                    </span>
                    <span className={`rounded px-2 py-0.5 text-xs ${state.cls}`}>{state.label}</span>
                    {s && <span className={`rounded px-2 py-0.5 text-xs ${s.cls}`}>{s.label}</span>}
                    {inst?.status === "failed" && (
                      <span className="rounded bg-rose/15 px-2 py-0.5 text-xs text-rose">install failed</span>
                    )}
                  </div>
                  <p className="mt-1 text-sm text-muted">{m.notes}</p>
                  <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-xs text-muted md:grid-cols-4">
                    <div><dt className="inline text-faint">License: </dt><dd className="inline">{m.license.name}</dd></div>
                    <div><dt className="inline text-faint">Download: </dt><dd className="inline">{gb(m.download_bytes)}</dd></div>
                    <div><dt className="inline text-faint">Format: </dt><dd className="inline">{m.format}</dd></div>
                    <div><dt className="inline text-faint">Source: </dt><dd className="inline">{String(m.source.provider ?? "local")}</dd></div>
                    {installed && (
                      <div className="col-span-2">
                        <dt className="inline text-faint">On disk: </dt>
                        <dd className="inline">{gb(inst?.byte_size)} · {inst?.install_root}\{inst?.rel_dir}</dd>
                      </div>
                    )}
                  </dl>
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
                  {check === "loading" && <p className="mt-2 text-xs text-muted">Running read-only checks…</p>}
                  {check && check !== "loading" && (
                    <ul className="mt-2 space-y-0.5 rounded border border-line p-2 text-xs">
                      {check.checks.map((ch) => (
                        <li key={ch.name} className={
                          ch.ok === false
                            ? (ch.severity === "warning" ? "text-amber"
                               : ch.severity === "info" ? "text-muted" : "text-rose")
                            : ch.ok === null ? "text-faint" : "text-teal"}>
                          {ch.ok === false ? "✗" : ch.ok === null ? "·" : "✓"} {ch.name.replaceAll("_", " ")} — {ch.detail}
                        </li>
                      ))}
                    </ul>
                  )}
                  <div className="mt-3 flex flex-wrap gap-2">
                    {!installed && !downloading && m.license.commercial_ok && m.files.length > 0 && m.support !== "planned" && (
                      <button
                        onClick={() => void openPreflight(m)}
                        disabled={busy}
                        className="rounded-md bg-action px-3 py-1.5 text-xs text-ink disabled:opacity-50"
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
                    <button
                      onClick={() => void toggleChecks(m)}
                      className="rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:text-fg"
                    >
                      {check ? "Hide checks" : "Compatibility checks"}
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
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

      {/* --- U-1: universal model library (read-only registry view) --- */}
      <section className="mt-6">
        <div className="flex items-center justify-between gap-3">
          <h2 className="text-lg font-semibold">Universal Model Library</h2>
          <button
            onClick={() => setImportOpen(true)}
            className="rounded-md bg-action px-3 py-1.5 text-xs font-medium text-ink hover:bg-action-hi"
          >
            Import model file…
          </button>
        </div>
        <p className="mt-1 text-sm text-muted">
          The artifact registry — every model and adapter the app knows
          about, with where it came from and who manages it. This view is
          read-only: installs still run through the catalog above.
        </p>
        {artifacts.length === 0 ? (
          <div className="mt-3 rounded-lg border border-dashed border-line p-4 text-sm text-muted">
            No artifacts registered yet. Import a model file you already
            have to add it here.
          </div>
        ) : (
          <ul className="mt-3 space-y-2">
            {artifacts.map((a) => {
              const det = detail[a.artifact_id];
              const detObj: ModelArtifactDetail | null =
                det != null && det !== "loading" ? det : null;
              const sharedN = sharedManagedCount(artifacts, a);
              return (
              <li key={a.artifact_id}
                  className="rounded-lg border border-line bg-card p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-semibold text-fg">{a.display_name}</span>
                  <span className="rounded bg-line/40 px-2 py-0.5 text-xs text-muted">{a.kind}</span>
                  <span className="rounded bg-line/40 px-2 py-0.5 text-xs text-muted">
                    {a.arch}{a.source_type === "import" && a.arch !== "unknown" ? " (detected)" : ""}
                  </span>
                  <span className="rounded bg-line/40 px-2 py-0.5 text-xs text-muted">{a.modality}</span>
                  <span className={`rounded px-2 py-0.5 text-xs ${
                    a.managed_by === "external" ? "bg-sky/15 text-sky" : "bg-teal/15 text-teal"}`}>
                    {a.managed_by === "external" ? "external"
                      : a.source_type === "import" ? "managed copy"
                      : "atomic-managed"}
                  </span>
                  {a.managed_by === "atomic" && a.source_type === "import"
                    && detObj?.managed_copy?.present === false && (
                    <span className="rounded bg-amber/15 px-2 py-0.5 text-xs text-amber"
                          title="The managed file isn't on disk — it may have been moved or the drive is unavailable. The record is intact.">
                      managed copy missing
                    </span>
                  )}
                  <span className="rounded bg-line/40 px-2 py-0.5 text-xs text-faint">{a.source_type}</span>
                  {a.source_type === "import" && (
                    <span className="rounded bg-line/40 px-2 py-0.5 text-xs text-muted"
                          title="Registered for tracking — it can't be used for generation yet">
                      library only
                    </span>
                  )}
                  {a.support_tier !== "production" && (
                    <span className={`rounded px-2 py-0.5 text-xs ${
                      a.support_tier === "experimental" ? "bg-amber/15 text-amber"
                      : a.support_tier === "tested" ? "bg-sky/15 text-sky"
                      : "bg-line/40 text-muted"}`}>
                      {a.support_tier}
                    </span>
                  )}
                  <span className={`rounded px-2 py-0.5 text-xs ${
                    a.install.status === "installed" ? "bg-teal/15 text-teal"
                    : a.install.status === "failed" ? "bg-rose/15 text-rose"
                    : a.install.status === "downloading" ? "bg-amber/15 text-amber"
                    : "bg-line/40 text-faint"}`}>
                    {a.install.status.replaceAll("_", " ")}
                  </span>
                  {!a.enabled && (
                    <span className="rounded bg-line/40 px-2 py-0.5 text-xs text-faint">disabled</span>
                  )}
                </div>
                <p className="mt-1 text-xs text-faint">
                  {a.artifact_id}
                  {a.version ? ` · v${a.version}` : ""}
                  {a.fmt ? ` · ${a.fmt}` : ""}
                  {a.role ? ` · ${a.role}${
                    a.source_type === "import"
                      ? a.role === (a.meta?.suggestions as { role?: string } | undefined)?.role
                        ? " (suggested)" : " (declared)"
                      : ""}` : ""}
                  {a.family ? ` · for ${a.family} bases${
                    a.source_type === "import" ? " (declared)" : ""}` : ""}
                  {a.content_sha256 ? ` · sha256 ${a.content_sha256.slice(0, 12)}…` : " · no checksum declared"}
                </p>
                {a.uri && (
                  <p className="mt-0.5 break-all text-xs text-faint">
                    {a.managed_by === "atomic"
                      ? `managed: ${baseName(a.uri)}` : a.uri}
                  </p>
                )}
                {a.source_type === "import" && (
                  <>
                    <button
                      onClick={() => void toggleProvenance(a)}
                      aria-expanded={detail[a.artifact_id] != null}
                      className="mt-2 rounded-md border border-line px-2 py-1 text-xs text-muted hover:text-fg"
                    >
                      {detail[a.artifact_id] != null ? "Hide provenance" : "Provenance…"}
                    </button>
                    {detail[a.artifact_id] === "loading" && (
                      <p className="mt-2 text-xs text-muted">Loading provenance…</p>
                    )}
                    {detail[a.artifact_id] != null && detail[a.artifact_id] !== "loading" && (
                      <dl className="mt-2 space-y-1 rounded border border-line p-2 text-xs">
                        <div className="flex justify-between gap-4">
                          <dt className="text-faint">Original location</dt>
                          <dd className="break-all text-right text-muted">{
                            (detail[a.artifact_id] as ModelArtifactDetail).source_uri ?? "—"}</dd>
                        </div>
                        <div className="flex justify-between gap-4">
                          <dt className="text-faint">Inspected</dt>
                          <dd className="text-muted">{
                            (detail[a.artifact_id] as ModelArtifactDetail).import_report?.hashed_at
                            ?? (detail[a.artifact_id] as ModelArtifactDetail).imported_at ?? "—"}
                            <span className="text-faint"> (point in time)</span></dd>
                        </div>
                        <div className="flex justify-between gap-4">
                          <dt className="text-faint">Detected</dt>
                          <dd className="text-muted">
                            {(detail[a.artifact_id] as ModelArtifactDetail).import_report?.fmt ?? a.fmt}
                            {` · arch ${(detail[a.artifact_id] as ModelArtifactDetail).import_report?.suggested_arch ?? "unknown"}`}
                            {(detail[a.artifact_id] as ModelArtifactDetail).import_report?.suggested_role
                              ? ` · role ${(detail[a.artifact_id] as ModelArtifactDetail).import_report?.suggested_role}`
                              : ""}
                            <span className="text-faint"> (heuristic, may be wrong)</span>
                          </dd>
                        </div>
                        <div className="flex justify-between gap-4">
                          <dt className="text-faint">Declared license</dt>
                          <dd className="text-muted">
                            {String(a.license?.name ?? "—")}
                            {a.license?.commercial_ok ? " · commercial ok" : " · no commercial use declared"}
                            <span className="text-faint"> (you declared this — unverified)</span>
                          </dd>
                        </div>
                        {detObj?.managed_copy && (
                          <>
                            <div className="flex justify-between gap-4">
                              <dt className="text-faint">Managed copy</dt>
                              <dd className="text-muted">
                                {detObj.managed_copy.present
                                  ? "present on disk" : "not present on disk"}
                                <span className="text-faint">
                                  {" "}(existence only — not a content check)
                                </span>
                              </dd>
                            </div>
                            {detObj.managed_copy.dest_rel && (
                              <div className="flex justify-between gap-4">
                                <dt className="text-faint">Managed file</dt>
                                <dd className="break-all text-right text-muted">
                                  {detObj.managed_copy.dest_rel}</dd>
                              </div>
                            )}
                            {detObj.managed_copy.provenance && (
                              <div className="flex justify-between gap-4">
                                <dt className="text-faint">Copy origin</dt>
                                <dd className="text-muted">
                                  {detObj.managed_copy.provenance === "adopted"
                                    ? "adopted existing verified content"
                                    : "copied by this import"}
                                </dd>
                              </div>
                            )}
                            {detObj.managed_copy.published_at && (
                              <div className="flex justify-between gap-4">
                                <dt className="text-faint">Copied at</dt>
                                <dd className="text-muted">
                                  {detObj.managed_copy.published_at}
                                  <span className="text-faint">
                                    {" "}(point in time — verified at copy)
                                  </span>
                                </dd>
                              </div>
                            )}
                          </>
                        )}
                        {detObj?.import_report?.release?.managed_copy_released_at && (
                          <div className="flex justify-between gap-4">
                            <dt className="text-faint">Detached</dt>
                            <dd className="text-muted">
                              {detObj.import_report.release.managed_copy_released_at}
                              <span className="text-faint">
                                {detObj.import_report.release.last_uri_rel
                                  ? ` — was ${detObj.import_report.release.last_uri_rel};`
                                  : " —"}{" "}
                                managed file left on disk
                              </span>
                            </dd>
                          </div>
                        )}
                      </dl>
                    )}
                    {detObj && sharedN > 0 && (
                      <p className="mt-1 text-xs text-faint">
                        Shares this verified managed copy with {sharedN}{" "}
                        other {sharedN === 1 ? "artifact" : "artifacts"} in
                        the loaded library list.
                      </p>
                    )}
                    {detObj?.managed_by === "atomic" && (
                      <button
                        onClick={() => setReleaseTarget(a)}
                        className="mt-2 rounded-md border border-amber/50 px-2 py-1 text-xs text-amber hover:text-fg"
                      >
                        Stop managing this copy…
                      </button>
                    )}
                  </>
                )}
              </li>
              );
            })}
          </ul>
        )}
      </section>
      <DiscoverySection
        onImportCandidate={(p) => {
          setImportInitialPath(p);
          setImportOpen(true);
        }}
        refreshKey={discoveryRefresh}
      />
      <ImportModelDialog
        open={importOpen}
        onOpenChange={(o) => {
          setImportOpen(o);
          if (!o) setImportInitialPath("");   // U-3 — stale prefill
        }}                                    //   must not leak
        initialPath={importInitialPath}
        onImported={() => { void refreshModels();
          setDiscoveryRefresh((k) => k + 1); }}
        capabilities={importCaps}
        onModeUnsupported={() => void fetchImportCaps()}
      />
      <ConfirmDialog
        open={releaseTarget !== null}
        onOpenChange={(o) => { if (!o) setReleaseTarget(null); }}
        title="Stop managing copy"
        body={"Stop managing this copy? The artifact record will retain its"
          + " original source reference. The managed file will not be"
          + " deleted, and no disk space will be freed. The original"
          + " source file will not be changed. Its current availability"
          + " is not guaranteed."}
        confirmLabel="Stop managing"
        onConfirm={() => {
          if (releaseTarget) void releaseManaged(releaseTarget);
        }}
      />

      {/* install confirmation — the approval gate, preflight-informed */}
      <ConfirmDialog
        open={installTarget !== null}
        onOpenChange={(o) => { if (!o) { setInstallTarget(null); setPreflight(null); } }}
        title={`Install ${installTarget?.display_name ?? ""}?`}
        confirmLabel="Approve download & install"
        body={
          installTarget ? (
            preflight ? (
              <div className="space-y-1.5 text-left text-sm">
                <p>Downloads <b>{gb(preflight.download_bytes)}</b> to {preflight.models_root}
                  {preflight.disk_free_bytes != null && <> — {gb(preflight.disk_free_bytes)} free
                    {!preflight.disk_sufficient && <span className="text-rose"> (not enough space)</span>}</>}.</p>
                <p>Needs ≈ {gb(preflight.est_vram_bytes)} VRAM + {gb(preflight.est_ram_bytes)} RAM
                  <span className="text-faint"> (estimates, not guarantees)</span>.</p>
                <p>Runtime: {preflight.runtime.display_name}
                  {preflight.runtime.planned
                    ? " — planned, no installable build yet"
                    : preflight.runtime.flavors.some((f) => f.operational)
                      ? " — installed & operational"
                      : " — will be installed with the model"}</p>
                <p>License: {preflight.license.name}{preflight.license.notes ? ` — ${preflight.license.notes}` : ""}
                  {!preflight.license.commercial_ok && <span className="text-rose"> (no commercial use)</span>}</p>
                {preflight.limitations.length > 0 && (
                  <ul className="text-amber">
                    {preflight.limitations.map((l, i) => <li key={i}>· {l}</li>)}
                  </ul>
                )}
                <p className="text-faint">Nothing downloads until you approve. Verified against published checksums.</p>
              </div>
            ) : "Checking disk space, memory, runtime, and license…"
          ) : ""
        }
        onConfirm={() => { if (installTarget) void install(installTarget); }}
      />
      {/* benchmark confirmation — explicit opt-in, never automatic */}
      <ConfirmDialog
        open={benchTarget !== null}
        onOpenChange={(o) => { if (!o) setBenchTarget(null); }}
        title={`Benchmark ${benchTarget?.display_name ?? ""}?`}
        confirmLabel="Run benchmark"
        body={
          benchTarget
            ? `Runs 3 minimal generations on this machine to measure real speed and memory — ` +
              `takes a few minutes and keeps the GPU/CPU busy. No downloads, no cloud calls. ` +
              `Results are estimates for THIS hardware, stored locally.`
            : ""
        }
        onConfirm={() => { if (benchTarget) void runBenchmark(benchTarget); }}
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
