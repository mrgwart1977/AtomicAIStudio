import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import { commitCommand, pickDirectory, pickFile } from "../api/commands";
import { openBridgeResult, capabilityDisplay } from "../api/bridge";
import { useApp } from "../stores/appStore";
import type {
  BridgeResult, CommitResult, Project, ProviderCapabilitiesReport,
  ProviderDetail, ProviderSummary, ProviderTestResult, WatchCandidate,
  WatchStatus,
} from "../api/types";

const CATEGORIES = [
  { id: "", label: "All" },
  { id: "text", label: "Text & Writing" },
  { id: "image", label: "Images" },
  { id: "video", label: "Video" },
  { id: "audio", label: "Audio & Voice" },
];

const STATUS_BADGE: Record<string, { label: string; cls: string }> = {
  configured: { label: "Configured", cls: "bg-teal/15 text-teal" },
  available: { label: "Ready", cls: "bg-sky/15 text-sky" },
  needs_key: { label: "Credential required", cls: "bg-amber/15 text-amber" },
  plan_required: { label: "Plan required", cls: "bg-amber/15 text-amber" },
  preview_only: { label: "Preview API", cls: "bg-amber/15 text-amber" },
  assisted: { label: "Assisted workflow", cls: "bg-sky/15 text-sky" },
  manual: { label: "Manual import", cls: "bg-line/40 text-muted" },
  external_only: { label: "External only", cls: "bg-line/40 text-muted" },
  credential_unreadable: { label: "Credential unreadable", cls: "bg-rose/15 text-rose" },
  unavailable: { label: "Unavailable", cls: "bg-line/40 text-muted" },
};

const METHOD_LABEL: Record<string, string> = {
  official_api: "Official API",
  aggregator_api: "Authorized aggregator",
  mcp_connector: "MCP connector",
  browser_authorized: "Approved browser integration",
  desktop_plugin: "Desktop CLI/plugin",
  assisted_workflow: "Assisted workflow",
  import_export: "Import / export",
  external_info: "External (informational)",
};

const TIER_LABEL: Record<string, string> = {
  T1: "Direct integration",
  T2: "Assisted website workflow",
  T3: "Manual import / export",
  T4: "External only",
};

export function ProvidersPage() {
  const [providers, setProviders] = useState<ProviderSummary[]>([]);
  const [category, setCategory] = useState("");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<ProviderDetail | null>(null);
  const [defaults, setDefaults] = useState<Record<string, string>>({});
  const [credValues, setCredValues] = useState<Record<string, string>>({});
  const [settingsDraft, setSettingsDraft] = useState<Record<string, unknown>>({});
  const [testResult, setTestResult] = useState<ProviderTestResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // C1.5 bridge — capability report + assisted workflow state
  const currentProjectId = useApp((s) => s.currentProjectId);
  const projects = useApp((s) => s.projects);
  const project = projects.find((p) => p.id === currentProjectId);
  const [caps, setCaps] = useState<ProviderCapabilitiesReport | null>(null);
  const [bridgePrompt, setBridgePrompt] = useState("");
  const [prepared, setPrepared] = useState<BridgeResult | null>(null);
  const [launchRes, setLaunchRes] = useState<CommitResult | null>(null);
  const [bridgeNote, setBridgeNote] = useState<string | null>(null);
  // ImportWatcher — manual candidate scan + optional background polling
  const [watchCandidates, setWatchCandidates] = useState<WatchCandidate[] | null>(null);
  const [watchTruncated, setWatchTruncated] = useState(false);
  const [watchStatus, setWatchStatus] = useState<WatchStatus | null>(null);

  const refresh = useCallback(async () => {
    try {
      const qs = new URLSearchParams();
      if (category) qs.set("category", category);
      if (search) qs.set("search", search);
      setProviders(await apiFetch<ProviderSummary[]>(`/api/providers?${qs}`));
      setDefaults((await apiFetch<{ defaults: Record<string, string> }>("/api/providers/defaults")).defaults);
    } catch {
      setProviders([]);
    }
  }, [category, search]);

  useEffect(() => { void refresh(); }, [refresh]);

  async function openDetail(key: string) {
    setErr(null);
    setTestResult(null);
    setCaps(null);
    setPrepared(null);
    setLaunchRes(null);
    setBridgeNote(null);
    setWatchCandidates(null);
    setWatchStatus(null);
    setBridgePrompt("");
    try {
      const [d, c] = await Promise.all([
        apiFetch<ProviderDetail>(`/api/providers/${key}`),
        apiFetch<ProviderCapabilitiesReport>(`/api/providers/${key}/capabilities`)
          .catch(() => null),
      ]);
      setSelected(d);
      setCaps(c);
      setSettingsDraft(d.settings || {});
      setCredValues({});
      void refreshWatch(key);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }

  // Stage B — polling state comes from the app's watch service (read-only).
  // Candidates the service surfaces flow into the same list as a manual
  // scan; importing is still a per-file audited command.
  async function refreshWatch(key?: string) {
    const k = key ?? selected?.key;
    if (!k) return;
    try {
      const qs = currentProjectId ? `?project_id=${currentProjectId}` : "";
      const st = await apiFetch<WatchStatus>(`/api/providers/${k}/watch${qs}`);
      setWatchStatus(st);
      if (st.enabled && st.candidates.length) {
        setWatchCandidates(st.candidates);
        setWatchTruncated(false);
      }
    } catch {
      // Transient backend unavailability — keep the last known status.
      // The 12s refresh tick retries slowly rather than storming, and
      // the next successful poll re-syncs state.
    }
  }

  // Stage B — light status refresh while auto-check is enabled and this
  // page is mounted. Read-only GET only: it NEVER schedules a scan (the
  // service owns the poll loop; this just mirrors its state). One
  // request in flight at most; stops on unmount or disable.
  useEffect(() => {
    if (!selected?.key || !watchStatus?.enabled) return;
    let cancelled = false, inFlight = false;
    const t = setInterval(() => {
      if (cancelled || inFlight) return;
      inFlight = true;
      void refreshWatch().finally(() => { inFlight = false; });
    }, 12000);
    return () => { cancelled = true; clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.key, watchStatus?.enabled]);

  // Every bridge mutation is an audited project command — a fresh revision is
  // fetched per call so stale store state can't cause a 409.
  async function bridgeCommit(commandType: string, payload: Record<string, unknown>): Promise<CommitResult> {
    if (!currentProjectId) throw new Error("Open a project to use bridge actions");
    const proj = await apiFetch<Project>(`/api/projects/${currentProjectId}`);
    return commitCommand(currentProjectId, proj.revision, commandType, payload);
  }

  async function saveConfig(patch: Record<string, unknown>) {
    if (!selected) return;
    setBusy(true); setErr(null);
    try {
      const d = await apiFetch<ProviderDetail>(`/api/providers/${selected.key}/config`, {
        method: "PUT", body: JSON.stringify(patch),
      });
      setSelected(d);
      void refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function saveCredential() {
    if (!selected) return;
    const fields = Object.fromEntries(
      Object.entries(credValues).filter(([, v]) => v.trim() !== ""));
    if (!Object.keys(fields).length) return;
    setBusy(true); setErr(null);
    try {
      const cred = await apiFetch<ProviderDetail["credential"]>(
        `/api/providers/${selected.key}/credential`,
        { method: "PUT", body: JSON.stringify({ fields }) });
      setSelected({ ...selected, credential: cred });
      setCredValues({});
      void refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function removeCredential() {
    if (!selected) return;
    setBusy(true); setErr(null);
    try {
      const cred = await apiFetch<ProviderDetail["credential"]>(
        `/api/providers/${selected.key}/credential`, { method: "DELETE" });
      setSelected({ ...selected, credential: cred });
      void refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function runTest() {
    if (!selected) return;
    setBusy(true); setTestResult(null);
    try {
      setTestResult(await apiFetch<ProviderTestResult>(
        `/api/providers/${selected.key}/test`, { method: "POST", body: "{}" }));
    } catch (e) {
      setTestResult({ ok: false, via: "mock", error: String(e) });
    } finally {
      setBusy(false);
    }
  }

  // ---- assisted workflow handlers (all run inside click handlers only) ----

  async function onPrepareCopy() {
    if (!selected || !bridgePrompt.trim()) return;
    setBusy(true); setErr(null); setBridgeNote(null);
    try {
      const res = await bridgeCommit("bridge.prepare", {
        provider_key: selected.key, prompt: bridgePrompt,
      });
      const b = res.bridge ?? null;
      setPrepared(b);
      if (b?.prepared_text) {
        // Clipboard write only on this explicit click — ADE never reads or
        // writes the clipboard outside a user gesture.
        await navigator.clipboard.writeText(b.prepared_text);
        setBridgeNote("Prepared prompt copied — paste it into the provider's site yourself.");
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onGetSiteLink() {
    if (!selected) return;
    setBusy(true); setErr(null); setBridgeNote(null); setLaunchRes(null);
    try {
      // bridge.launch validates + audits the destination; the response only
      // carries it — nothing opens yet. The URL is shown for explicit
      // confirmation in the next step.
      setLaunchRes(await bridgeCommit("bridge.launch", { provider_key: selected.key }));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onOpenSite() {
    if (!launchRes) return;
    setBusy(true); setErr(null);
    try {
      // THE browser-open call — only reachable from this confirm button, on a
      // URL that passed backend catalog validation + the https://* capability
      // scope + openBridgeResult's own checks.
      const url = await openBridgeResult(launchRes);
      if (url) setBridgeNote(`Opened ${url} — work on the provider's site, then import the result below.`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onImportFile() {
    if (!selected) return;
    setErr(null); setBridgeNote(null);
    const path = await pickFile(`Import ${selected.label} output`);
    if (!path) { setBridgeNote("No file selected (native picker requires the desktop app)."); return; }
    setBusy(true);
    try {
      const res = await bridgeCommit("bridge.import", {
        provider_key: selected.key, source_path: path,
      });
      setBridgeNote(`Import queued (job ${res.job_id ?? res.bridge?.job_id ?? "?"}) — provenance recorded as user-reported external.`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onImportClipboard() {
    if (!selected) return;
    setErr(null); setBridgeNote(null);
    // Clipboard READ only inside this explicit click — the spec forbids
    // automatic clipboard access.
    const content = await navigator.clipboard.readText();
    if (!content.trim()) { setBridgeNote("Clipboard is empty — nothing to import."); return; }
    setBusy(true);
    try {
      const res = await bridgeCommit("bridge.import", {
        provider_key: selected.key, content,
      });
      setBridgeNote(`Clipboard import queued (job ${res.job_id ?? res.bridge?.job_id ?? "?"}) — provenance recorded as user-reported external.`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  // ---- ImportWatcher Stage A — designate + explicit scan + per-file import

  async function onDesignateFolder() {
    if (!selected) return;
    setErr(null); setBridgeNote(null);
    const dir = await pickDirectory(`Designate ${selected.label} import folder`);
    if (!dir) return;
    // Folder designation goes through the existing provider config path —
    // every scan itself is an audited project command.
    await saveConfig({ settings: { ...settingsDraft, import_dir: dir } });
    setBridgeNote(`Import folder set: ${dir}`);
  }

  async function onCheckFolder() {
    if (!selected) return;
    setBusy(true); setErr(null); setBridgeNote(null); setWatchCandidates(null);
    try {
      const res = await bridgeCommit("bridge.watch.scan",
        { provider_key: selected.key });
      const b = res.bridge;
      setWatchCandidates(b?.candidates ?? []);
      setWatchTruncated(Boolean(b?.truncated));
      if (!b?.candidates?.length) setBridgeNote("No new eligible files found.");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onToggleWatch() {
    if (!selected) return;
    const cmd = watchStatus?.enabled
      ? "bridge.watch.disable" : "bridge.watch.enable";
    setBusy(true); setErr(null); setBridgeNote(null);
    try {
      await bridgeCommit(cmd, { provider_key: selected.key });
      const d = await apiFetch<ProviderDetail>(`/api/providers/${selected.key}`);
      setSelected(d);
      setSettingsDraft(d.settings || {});
      await refreshWatch();
      setBridgeNote(watchStatus?.enabled
        ? "Auto-check stopped — the folder will not be scanned again."
        : "Auto-check on — the folder is polled about once a minute while the app runs. Nothing is imported automatically.");
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function onImportCandidate(c: WatchCandidate) {
    if (!selected) return;
    setBusy(true); setErr(null); setBridgeNote(null);
    try {
      const res = await bridgeCommit("bridge.import", {
        provider_key: selected.key, source_path: c.path,
        kind: c.kind, name: c.name, from_watch: true,
        note: "watch folder candidate — origin user-reported, unverified",
      });
      setWatchCandidates((prev) => prev?.filter((x) => x.path !== c.path) ?? null);
      void refreshWatch();
      setBridgeNote(`Imported ${c.name} (job ${res.job_id ?? res.bridge?.job_id ?? "?"}) — provenance: user-reported external.`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function setTaskDefault(task: string, key: string) {
    setBusy(true);
    try {
      const r = await apiFetch<{ defaults: Record<string, string> }>(
        "/api/providers/defaults",
        { method: "PUT", body: JSON.stringify({ task, provider_key: key || null }) });
      setDefaults(r.defaults);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const badge = (s: ProviderSummary) => {
    const b = STATUS_BADGE[s.status] ?? STATUS_BADGE.unavailable;
    return <span className={`rounded px-1.5 py-0.5 text-[10px] ${b.cls}`}>{b.label}</span>;
  };

  return (
    <div className="flex h-full">
      {/* ------- catalog list ------- */}
      <div className="flex w-[380px] shrink-0 flex-col border-r border-line bg-panel">
        <div className="border-b border-line p-3">
          <h1 className="mb-2 text-sm font-semibold text-fg">AI Providers</h1>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search providers…"
            className="mb-2 w-full rounded-md border border-line bg-ink px-2.5 py-1.5 text-sm text-fg placeholder:text-faint focus:border-action focus:outline-none"
          />
          <div className="flex gap-1">
            {CATEGORIES.map((c) => (
              <button
                key={c.id}
                onClick={() => setCategory(c.id)}
                className={`rounded-md px-2 py-1 text-[11px] ${
                  category === c.id ? "bg-action/30 text-teal" : "text-muted hover:bg-card"
                }`}
              >
                {c.label}
              </button>
            ))}
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-2">
          {providers.map((p) => (
            <button
              key={p.key}
              onClick={() => openDetail(p.key)}
              className={`mb-1 flex w-full items-center justify-between rounded-md px-2.5 py-2 text-left ${
                selected?.key === p.key ? "bg-action/20" : "hover:bg-card"
              }`}
            >
              <div className="min-w-0">
                <div className="truncate text-sm text-fg">{p.label}</div>
                <div className="text-[10px] text-faint">
                  {TIER_LABEL[p.tier] ?? p.tier} · {p.access_class}-class
                  {p.enabled ? " · on" : ""}
                </div>
              </div>
              {badge(p)}
            </button>
          ))}
          {!providers.length && (
            <div className="p-4 text-center text-xs text-faint">No providers match.</div>
          )}
        </div>
        {/* defaults strip */}
        <div className="border-t border-line p-2 text-[11px] text-faint">
          {(["text", "image", "video", "audio"] as const).map((task) => (
            <div key={task} className="mb-1 flex items-center justify-between gap-2">
              <span className="capitalize">{task} default</span>
              <select
                value={defaults[task] ?? ""}
                disabled={busy}
                onChange={(e) => setTaskDefault(task, e.target.value)}
                className="max-w-[200px] rounded border border-line bg-ink px-1.5 py-0.5 text-fg"
              >
                <option value="">—</option>
                {providers.filter((p) => p.category === task && p.tier === "T1")
                  .map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
              </select>
            </div>
          ))}
        </div>
      </div>

      {/* ------- detail ------- */}
      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {err && (
          <div className="mb-3 rounded-md border border-rose/40 bg-rose/10 px-3 py-2 text-sm text-rose">{err}</div>
        )}
        {!selected ? (
          <div className="flex h-full items-center justify-center text-sm text-faint">
            Select a provider to see access strategies, settings, and credentials.
          </div>
        ) : (
          <div className="mx-auto max-w-2xl">
            <div className="mb-3 flex items-start justify-between">
              <div>
                <h2 className="text-lg font-semibold text-fg">{selected.label}</h2>
                <div className="mt-0.5 text-xs text-faint">
                  {selected.category} · {selected.access_class}-class · account: {selected.account}
                  {selected.shared_credential_with.length > 0 && (
                    <> · shares credential with {selected.shared_credential_with.join(", ")}</>
                  )}
                </div>
              </div>
              {badge(selected)}
            </div>

            {selected.status_reason && (
              <div className="mb-3 rounded-md border border-amber/30 bg-amber/10 px-3 py-2 text-xs text-amber">
                {selected.status_reason}
              </div>
            )}
            {selected.notes && <p className="mb-3 text-xs text-muted">{selected.notes}</p>}
            {selected.privacy_summary && (
              <p className="mb-3 text-xs text-muted">Privacy: {selected.privacy_summary}</p>
            )}

            {/* access strategies */}
            <section className="mb-4 rounded-md border border-line bg-panel p-3">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-faint">
                Access strategies
              </h3>
              {selected.strategies.map((s, i) => (
                <div key={i} className="mb-1.5 flex items-center justify-between text-sm">
                  <span className="text-fg">{METHOD_LABEL[s.method] ?? s.method}</span>
                  <span className="text-[11px] text-faint">
                    {TIER_LABEL[s.tier] ?? s.tier}
                    {s.capabilities.length ? ` · ${s.capabilities.join(", ")}` : ""}
                    {s.note ? ` — ${s.note}` : ""}
                  </span>
                </div>
              ))}
              {(selected.docs_url || selected.tos_url) && (
                <div className="mt-2 flex gap-3 text-[11px]">
                  {selected.docs_url && (
                    <a className="text-sky hover:underline" href={selected.docs_url} target="_blank" rel="noreferrer">Docs</a>
                  )}
                  {selected.tos_url && (
                    <a className="text-sky hover:underline" href={selected.tos_url} target="_blank" rel="noreferrer">Terms</a>
                  )}
                </div>
              )}
            </section>

            {/* C1.5 capability report — honest per-feature status */}
            {caps && caps.capabilities.length > 0 && (
              <section className="mb-4 rounded-md border border-line bg-panel p-3">
                <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-faint">
                  Capability report
                </h3>
                <p className="mb-2 text-[10px] leading-snug text-faint">
                  {caps.evidence_stamp} — a documented strategy + valid evidence URL is
                  not proof a method is authorized, works, or was ever tested.
                </p>
                {caps.capabilities.some((c) => c.stale || c.status === "needs_recheck") && (
                  <div className="mb-2 rounded-md border border-amber/30 bg-amber/10 px-3 py-2 text-xs text-amber">
                    Verification is stale — re-check required before any bridge dispatch.
                  </div>
                )}
                {caps.capabilities.map((c) => {
                  const d = capabilityDisplay(c);
                  const badgeCls = d.statusClass === "warn"
                    ? "bg-amber/15 text-amber"
                    : d.statusClass === "muted"
                      ? "bg-line/40 text-muted"
                      : "bg-sky/15 text-sky";
                  return (
                    <div key={c.capability} className="mb-2 border-t border-line/40 pt-2 text-sm first:border-0 first:pt-0">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium text-fg">{c.capability}</span>
                        <span className={`rounded px-1.5 py-0.5 text-[10px] ${badgeCls}`}>
                          {d.statusLabel}
                        </span>
                      </div>
                      <div className="mt-0.5 text-[11px] text-faint">
                        {c.tier ? (TIER_LABEL[c.tier] ?? c.tier) : "No usable strategy"}
                        {c.method ? ` · ${METHOD_LABEL[c.method] ?? c.method}` : ""}
                        {d.actionLabel ? ` — ${d.actionLabel}` : ""}
                      </div>
                      {c.required_user_action === "reverify_strategy" && (
                        <div className="mt-0.5 text-[11px] text-amber">
                          Required action: re-verify the strategy evidence (older than 180 days).
                        </div>
                      )}
                      {c.alternate && (
                        <div className="mt-0.5 text-[11px] text-faint">
                          Alternate path: {TIER_LABEL[c.alternate.tier ?? ""] ?? c.alternate.tier}
                          {" · "}{METHOD_LABEL[c.alternate.method ?? ""] ?? c.alternate.method}
                        </div>
                      )}
                    </div>
                  );
                })}
              </section>
            )}

            {/* C1.5 assisted workflow — canonical T2/T3 flow */}
            {caps && caps.capabilities.some((c) => c.dispatch.length > 0) && (
              <section className="mb-4 rounded-md border border-line bg-panel p-3">
                <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-faint">
                  Assisted workflow
                </h3>
                <p className="mb-2 text-[10px] leading-snug text-faint">
                  You do the work on the provider's site — ADE prepares the prompt,
                  opens the site on your click, and imports your result. Nothing is
                  submitted, automated, or read without your action.
                </p>
                {!project ? (
                  <p className="text-xs text-amber">
                    Open a project to use assisted workflows — every step is an audited
                    project command.
                  </p>
                ) : (
                  <>
                    {caps.capabilities.some((c) => c.dispatch.includes("prepare")) && (
                      <div className="mb-3">
                        <textarea
                          value={bridgePrompt}
                          onChange={(e) => setBridgePrompt(e.target.value)}
                          placeholder={`Prompt to take to ${selected.label}…`}
                          rows={3}
                          className="mb-1.5 w-full rounded-md border border-line bg-ink px-2.5 py-1.5 text-sm text-fg placeholder:text-faint focus:border-action focus:outline-none"
                        />
                        <button
                          onClick={onPrepareCopy}
                          disabled={busy || !bridgePrompt.trim()}
                          className="rounded-md bg-action px-3 py-1.5 text-xs text-ink hover:bg-action/80 disabled:opacity-40"
                        >
                          Prepare &amp; copy prompt
                        </button>
                        {prepared?.prepared_text && (
                          <div className="mt-2 rounded-md border border-line bg-ink p-2">
                            <pre className="whitespace-pre-wrap text-[11px] text-fg">{prepared.prepared_text}</pre>
                            <p className="mt-1 text-[10px] text-faint">{prepared.paste_hint}</p>
                          </div>
                        )}
                      </div>
                    )}
                    {caps.capabilities.some((c) => c.dispatch.includes("launch")) && (
                      <div className="mb-3">
                        {!launchRes ? (
                          <button
                            onClick={onGetSiteLink}
                            disabled={busy}
                            className="rounded-md border border-action/50 px-3 py-1.5 text-xs text-teal hover:bg-action/20 disabled:opacity-40"
                          >
                            Get provider site link
                          </button>
                        ) : launchRes.bridge?.url ? (
                          <div className="rounded-md border border-line bg-ink p-2">
                            <div className="mb-1.5 break-all text-[11px] text-fg">
                              Destination: <span className="text-sky">{launchRes.bridge.url}</span>
                            </div>
                            <button
                              onClick={onOpenSite}
                              disabled={busy}
                              className="rounded-md bg-action px-3 py-1.5 text-xs text-ink hover:bg-action/80 disabled:opacity-40"
                            >
                              Open this site in my browser
                            </button>
                            <p className="mt-1 text-[10px] text-faint">
                              ADE never automates, logs in, fills, or clicks anything on the site.
                            </p>
                          </div>
                        ) : null}
                      </div>
                    )}
                    {caps.capabilities.some((c) => c.dispatch.includes("import")) && (
                      <>
                        <div className="mb-1 flex gap-2">
                          <button
                            onClick={onImportFile}
                            disabled={busy}
                            className="rounded-md border border-action/50 px-3 py-1.5 text-xs text-teal hover:bg-action/20 disabled:opacity-40"
                          >
                            Import file…
                          </button>
                          <button
                            onClick={onImportClipboard}
                            disabled={busy}
                            className="rounded-md border border-line px-3 py-1.5 text-xs text-muted hover:bg-card disabled:opacity-40"
                          >
                            Import clipboard text
                          </button>
                        </div>
                        {/* ImportWatcher Stage A — designated folder + manual check */}
                        <div className="mt-2 border-t border-line/40 pt-2">
                          <div className="mb-1 flex items-center justify-between">
                            <span className="text-[11px] font-medium text-muted">Import folder</span>
                            {selected.settings?.import_dir ? (
                              <div className="flex gap-2">
                                <button
                                  onClick={onCheckFolder}
                                  disabled={busy}
                                  className="rounded-md border border-action/50 px-3 py-1 text-xs text-teal hover:bg-action/20 disabled:opacity-40"
                                >
                                  Check folder
                                </button>
                                <button
                                  onClick={onToggleWatch}
                                  disabled={busy}
                                  className={`rounded-md border px-2 py-1 text-[10px] disabled:opacity-40 ${
                                    watchStatus?.enabled
                                      ? "border-amber/60 text-amber hover:bg-amber/10"
                                      : "border-line text-muted hover:bg-card"}`}
                                >
                                  {watchStatus?.enabled ? "Stop auto-check" : "Auto-check"}
                                </button>
                                <button
                                  onClick={onDesignateFolder}
                                  disabled={busy}
                                  className="rounded-md border border-line px-2 py-1 text-[10px] text-muted hover:bg-card disabled:opacity-40"
                                >
                                  Change
                                </button>
                              </div>
                            ) : (
                              <button
                                onClick={onDesignateFolder}
                                disabled={busy}
                                className="rounded-md border border-line px-3 py-1 text-xs text-muted hover:bg-card disabled:opacity-40"
                              >
                                Designate folder…
                              </button>
                            )}
                          </div>
                          {watchStatus?.enabled && (
                            <p className={`mb-1 text-[10px] ${watchStatus.status === "degraded" ? "text-amber" : "text-faint"}`}>
                              {watchStatus.status === "degraded"
                                ? `Auto-check paused — ${watchStatus.reason ?? "folder unavailable"}`
                                : watchStatus.paused_reason
                                  ? `Auto-check idle — ${watchStatus.paused_reason}`
                                  : `Auto-checking about once a minute${watchStatus.last_poll ? ` — last check ${new Date(watchStatus.last_poll).toLocaleTimeString()}` : " — first check pending"}`}
                            </p>
                          )}
                          {selected.settings?.import_dir ? (
                            <p className="mb-1 truncate text-[10px] text-faint" title={String(selected.settings.import_dir)}>
                              {String(selected.settings.import_dir)}
                            </p>
                          ) : (
                            <p className="mb-1 text-[10px] text-faint">
                              Pick a folder where you save this provider's outputs. Nothing is
                              watched or scanned until you click "Check folder" or turn on
                              auto-check (off by default — still never auto-imports).
                            </p>
                          )}
                          {watchCandidates && (
                            <div className="rounded-md border border-line bg-ink p-2">
                              {watchCandidates.length === 0 ? (
                                <p className="text-[11px] text-faint">No new eligible files.</p>
                              ) : (
                                <>
                                  {watchCandidates.map((c) => (
                                    <div key={c.path} className="mb-1 flex items-center justify-between gap-2 text-[11px]">
                                      <span className="min-w-0 truncate text-fg" title={c.path}>
                                        {c.name}
                                        <span className="ml-1.5 text-faint">
                                          {c.kind} · {(c.size / 1024).toFixed(0)} KB
                                        </span>
                                      </span>
                                      <button
                                        onClick={() => onImportCandidate(c)}
                                        disabled={busy}
                                        className="shrink-0 rounded border border-action/50 px-2 py-0.5 text-[10px] text-teal hover:bg-action/20 disabled:opacity-40"
                                      >
                                        Import
                                      </button>
                                    </div>
                                  ))}
                                  <p className="mt-1 text-[10px] text-faint">
                                    Found in your folder — ADE does not verify these came from{" "}
                                    {selected.label}. Each import needs your click.
                                  </p>
                                  {watchTruncated && (
                                    <p className="text-[10px] text-amber">
                                      Showing the newest 200 candidates — folder contains more.
                                    </p>
                                  )}
                                </>
                              )}
                            </div>
                          )}
                        </div>
                      </>
                    )}
                    {bridgeNote && (
                      <p className="mt-2 rounded-md bg-teal/10 px-2 py-1.5 text-[11px] text-teal">{bridgeNote}</p>
                    )}
                  </>
                )}
              </section>
            )}

            {/* credentials — write-only */}
            {selected.credential_fields.length > 0 && (
              <section className="mb-4 rounded-md border border-line bg-panel p-3">
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-faint">
                  Credentials (stored encrypted, never shown)
                </h3>
                {selected.credential.configured ? (
                  <div className="mb-2 flex items-center justify-between text-xs">
                    <span className={selected.credential.readable ? "text-teal" : "text-rose"}>
                      {selected.credential.readable
                        ? `Stored (${selected.credential.fields.join(", ")}) since ${selected.credential.set_at?.slice(0, 10) ?? "—"}`
                        : `Unreadable — ${selected.credential.unreadable_reason ?? "re-enter"}`}
                    </span>
                    <button onClick={removeCredential} disabled={busy}
                      className="rounded border border-rose/40 px-2 py-0.5 text-rose hover:bg-rose/10 disabled:opacity-40">
                      Remove
                    </button>
                  </div>
                ) : (
                  <p className="mb-2 text-[11px] text-faint">No credential stored.</p>
                )}
                {selected.credential_fields.map((f) => (
                  <label key={f.name} className="mb-2 block text-xs">
                    <span className="mb-0.5 block text-muted">{f.name}{f.required ? " *" : ""}</span>
                    <input
                      type={f.secret ? "password" : "text"}
                      autoComplete="off"
                      placeholder={f.hint}
                      value={credValues[f.name] ?? ""}
                      onChange={(e) => setCredValues({ ...credValues, [f.name]: e.target.value })}
                      className="w-full rounded-md border border-line bg-ink px-2.5 py-1.5 text-sm text-fg placeholder:text-faint focus:border-action focus:outline-none"
                    />
                  </label>
                ))}
                <button onClick={saveCredential} disabled={busy || !Object.values(credValues).some((v) => v.trim())}
                  className="rounded-md bg-action px-3 py-1.5 text-sm text-ink hover:bg-action/80 disabled:opacity-40">
                  Save credential
                </button>
              </section>
            )}

            {/* settings */}
            <section className="mb-4 rounded-md border border-line bg-panel p-3">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-faint">Settings</h3>
              <div className="mb-3 flex items-center justify-between">
                <span className="text-sm text-fg">Enabled</span>
                <button
                  onClick={() => saveConfig({ enabled: !selected.enabled })}
                  disabled={busy}
                  className={`h-5 w-9 rounded-full transition-colors ${selected.enabled ? "bg-teal" : "bg-line"}`}
                  aria-pressed={selected.enabled}
                >
                  <span className={`block h-4 w-4 translate-x-0.5 rounded-full bg-ink transition-transform ${selected.enabled ? "translate-x-[18px]" : ""}`} />
                </button>
              </div>
              {selected.models.length > 0 && (
                <label className="mb-3 block text-xs">
                  <span className="mb-0.5 block text-muted">Default model</span>
                  <select
                    value={String(settingsDraft.model ?? "")}
                    disabled={busy}
                    onChange={(e) => {
                      const v = e.target.value;
                      setSettingsDraft({ ...settingsDraft, model: v });
                      void saveConfig({ settings: { ...settingsDraft, model: v } });
                    }}
                    className="w-full rounded-md border border-line bg-ink px-2.5 py-1.5 text-sm text-fg"
                  >
                    <option value="">—</option>
                    {selected.models.map((m) => (
                      <option key={m.id} value={m.id}>{m.id} · {m.capability}</option>
                    ))}
                  </select>
                </label>
              )}
              {selected.region_selectable && (
                <label className="mb-3 block text-xs">
                  <span className="mb-0.5 block text-muted">Region</span>
                  <input
                    value={String(settingsDraft.region ?? selected.region ?? "")}
                    placeholder="e.g. singapore"
                    onChange={(e) => setSettingsDraft({ ...settingsDraft, region: e.target.value })}
                    onBlur={() => saveConfig({ settings: settingsDraft })}
                    className="w-full rounded-md border border-line bg-ink px-2.5 py-1.5 text-sm text-fg placeholder:text-faint"
                  />
                </label>
              )}
              <div className="grid grid-cols-3 gap-2">
                {(["rate_limit_rpm", "timeout_s", "max_retries"] as const).map((k) => (
                  <label key={k} className="block text-xs">
                    <span className="mb-0.5 block text-muted">{k.replace(/_/g, " ")}</span>
                    <input
                      type="number"
                      value={settingsDraft[k] === undefined ? "" : Number(settingsDraft[k])}
                      onChange={(e) => setSettingsDraft({
                        ...settingsDraft,
                        [k]: e.target.value === "" ? undefined : Number(e.target.value),
                      })}
                      onBlur={() => saveConfig({ settings: settingsDraft })}
                      className="w-full rounded-md border border-line bg-ink px-2 py-1.5 text-sm text-fg"
                    />
                  </label>
                ))}
              </div>
            </section>

            {/* mock test */}
            <section className="rounded-md border border-line bg-panel p-3">
              <div className="mb-2 flex items-center justify-between">
                <h3 className="text-xs font-semibold uppercase tracking-wide text-faint">
                  Connection test
                </h3>
                <button onClick={runTest} disabled={busy}
                  className="rounded-md border border-action/50 px-3 py-1 text-xs text-teal hover:bg-action/20 disabled:opacity-40">
                  Run mock test
                </button>
              </div>
              {testResult && (
                <div className={`rounded-md px-3 py-2 text-xs ${testResult.ok ? "bg-teal/10 text-teal" : "bg-rose/10 text-rose"}`}>
                  {testResult.ok ? `Mock OK — ${testResult.note}` : `Failed: ${testResult.error}`}
                  <div className="mt-1 text-[10px] opacity-70">
                    This is a mock check of stored config only — no live provider contact was made.
                  </div>
                </div>
              )}
            </section>
          </div>
        )}
      </div>
    </div>
  );
}
