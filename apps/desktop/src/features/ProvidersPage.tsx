import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "../api/client";
import type {
  ProviderDetail, ProviderSummary, ProviderTestResult,
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
    try {
      const d = await apiFetch<ProviderDetail>(`/api/providers/${key}`);
      setSelected(d);
      setSettingsDraft(d.settings || {});
      setCredValues({});
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
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
