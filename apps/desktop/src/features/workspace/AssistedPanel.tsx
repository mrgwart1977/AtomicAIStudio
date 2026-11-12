// C1.5 — assisted text workflows inside Cloud Studio.
//
// Visually and functionally separate from the simulated REST providers
// above: these catalog providers have NO API integration in ADE. The
// user performs all sign-in, generation, and export on the provider's
// own site; ADE prepares text locally, opens the documented HTTPS
// destination only on explicit click, and imports results as
// user-reported external assets. Nothing is submitted, scraped,
// automated, or uploaded in the background — every step is an audited
// command fired from a user gesture.
import { useEffect, useMemo, useState } from "react";
import { apiFetch } from "../../api/client";
import { commitCommand, pickFile } from "../../api/commands";
import { openBridgeResult } from "../../api/bridge";
import { useApp } from "../../stores/appStore";
import type {
  BridgeResult, CommitResult, ProviderCapabilitiesReport,
  ProviderDetail, ProviderStrategy, WatchCandidate,
} from "../../api/types";

const ASSIST_METHODS = new Set([
  "assisted_workflow", "browser_authorized", "desktop_plugin",
  "import_export"]);

/** Catalog strategies documenting a user-driven text path (T2 site
 * workflow or T3 file round-trip) — the ONLY things this panel may
 * dispatch. Prohibited and T1 records are never offered. */
export function assistStrategies(d?: ProviderDetail): ProviderStrategy[] {
  return (d?.strategies ?? []).filter((s) =>
    ASSIST_METHODS.has(s.method)
    && (s.tier === "T2" || s.tier === "T3")
    && s.automation_permission !== "prohibited"
    && (s.capabilities ?? []).some((c) => c.startsWith("text")));
}

const inputCls =
  "w-full rounded-md border border-line bg-panel px-2.5 py-1.5 text-sm text-fg";
const labelCls = "mb-1 block text-xs font-medium text-muted";
const btnCls =
  "rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-white hover:bg-accent/90 disabled:opacity-40";
const ghostBtn =
  "rounded-md border border-line px-3 py-1.5 text-sm text-muted hover:text-fg disabled:opacity-40";

type CtxSel = { etype: string; id: string; name: string; text: string };

/** Render the shareable artifact — the EXACT text the user may paste
 * into a third-party site. Shown verbatim before any copy happens. */
function composeShareText(prompt: string, ctx: CtxSel[]): string {
  if (!ctx.length) return prompt;
  const blocks = ctx.map((c) => `### ${c.etype}: ${c.name}\n${c.text}`);
  return `[Project context — review before sharing]\n\n`
    + blocks.join("\n\n") + `\n\n${prompt}`;
}

const STEPS = [
  "Prepare", "Review & copy", "Open provider",
  "Create externally", "Return & import",
];

// Stable empty refs — selectors must never allocate `?? []`/`?? {}`.
const NO_ENTITIES = { character: [], world: [], location: [] } as const;
const NO_STORYBOARD: never[] = [];

export function AssistedPanel({ pid, revision, providers, details,
                                onCommitted }: {
  pid: string;
  revision: number;
  providers: { key: string; label: string }[];
  details: Record<string, ProviderDetail>;
  onCommitted: () => void;
}) {
  void providers; // eligibility is driven by the fetched details map
  const refreshAssets = useApp((s) => s.refreshAssets);
  const entities = useApp((s) => s.entities ?? NO_ENTITIES);
  const storyboard = useApp((s) => s.storyboard ?? NO_STORYBOARD);

  const [assistKey, setAssistKey] = useState("");
  const [caps, setCaps] = useState<ProviderCapabilitiesReport | null>(null);
  const [prompt, setPrompt] = useState("");
  const [ctxSel, setCtxSel] = useState<string[]>([]);
  const [prepared, setPrepared] = useState<BridgeResult | null>(null);
  const [copied, setCopied] = useState(false);
  const [launchUrl, setLaunchUrl] = useState<string | null>(null);
  const [paste, setPaste] = useState("");
  const [imported, setImported] = useState(false);
  const [watchCands, setWatchCands] = useState<WatchCandidate[] | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const detail = assistKey ? details[assistKey] : undefined;
  const strats = assistStrategies(detail);
  const hasT2 = strats.some((s) => s.tier === "T2");
  const launchCandidate = (strats.find(
    (s) => s.tier === "T2"
      && (s.connection?.launch_url ?? ""))?.connection?.launch_url)
    ?? detail?.docs_url ?? null;

  const canPrepare = !!caps && caps.capabilities.some(
    (c) => c.dispatch.includes("prepare"));
  const canLaunch = !!caps && caps.capabilities.some(
    (c) => c.dispatch.includes("launch"));
  const canImport = !!caps && caps.capabilities.some(
    (c) => c.dispatch.includes("import"));
  const stale = !!caps && caps.capabilities.some(
    (c) => c.stale || c.status === "needs_recheck");

  const importDir = String(
    (detail?.settings as Record<string, unknown> | undefined)?.import_dir ?? "");

  // Context pickers — entities + scenes already in the store.
  const ctxOptions = useMemo<CtxSel[]>(() => {
    const out: CtxSel[] = [];
    for (const etype of ["character", "world", "location"] as const) {
      for (const e of entities[etype] ?? []) {
        if (e.status === "archived") continue;
        out.push({ etype, id: e.id, name: e.name,
                   text: e.summary || "(no summary)" });
      }
    }
    for (const s of storyboard ?? []) {
      out.push({ etype: "scene", id: s.scene.id, name: s.scene.title,
                 text: [s.scene.description,
                        s.scene.intent ? `Intent: ${s.scene.intent}` : ""]
                   .filter(Boolean).join("\n") || "(no description)" });
    }
    return out;
  }, [entities, storyboard]);

  const shareText = useMemo(
    () => composeShareText(
      prompt, ctxOptions.filter((c) => ctxSel.includes(`${c.etype}:${c.id}`))),
    [prompt, ctxSel, ctxOptions]);

  // Capability report for the selected provider — drives step
  // availability + honest status labels.
  useEffect(() => {
    setCaps(null); setPrepared(null); setLaunchUrl(null);
    setCopied(false); setWatchCands(null); setImported(false);
    setError(null); setNotice(null);
    if (!assistKey) return;
    apiFetch<ProviderCapabilitiesReport>(`/api/providers/${assistKey}/capabilities`)
      .then(setCaps)
      .catch((e) => setError(`capabilities: ${String(e)}`));
  }, [assistKey]);

  const eligible = Object.keys(details).filter((k) => assistStrategies(details[k]).length > 0);

  async function bridgeCommit(commandType: string,
                              payload: Record<string, unknown>): Promise<CommitResult> {
    setBusy(true); setError(null);
    try {
      const r = await commitCommand(pid, revision, commandType, payload);
      return r;
    } catch (e) {
      setError(`${commandType} failed: ${String(e)}`);
      throw e;
    } finally {
      setBusy(false);
    }
  }

  const onPrepare = () => {
    if (!shareText.trim()) return;
    setPrepared(null); setCopied(false);
    void bridgeCommit("bridge.prepare", {
      provider_key: assistKey, prompt: shareText,
    }).then((r) => {
      if (r.bridge?.prepared_text) setPrepared(r.bridge);
      setNotice("Prepared text ready — review it below; nothing has left this device.");
    }).catch(() => {});
  };

  const onCopy = async () => {
    if (!prepared?.prepared_text) return;
    await navigator.clipboard.writeText(prepared.prepared_text);
    setCopied(true);
    setNotice("Copied — paste it into the provider's site yourself. ADE sends nothing.");
  };

  const onLaunch = () => {
    void bridgeCommit("bridge.launch", { provider_key: assistKey })
      .then(async (r) => {
        const url = await openBridgeResult(r);
        setLaunchUrl(url);
        setNotice(url
          ? `Opened ${url} — sign in and work there; then return and import the result.`
          : "Launch validated but this shell cannot open it — copy the URL from the audit log.");
      }).catch(() => {});
  };

  const onImportContent = () => {
    if (!paste.trim()) return;
    void bridgeCommit("bridge.import", {
      provider_key: assistKey, content: paste, kind: "text",
    }).then((r) => {
      setPaste("");
      setImported(true);
      setNotice(`Import queued (job ${r.job_id?.slice(0, 8) ?? "?"}) — provenance: user-reported external.`);
      onCommitted(); void refreshAssets();
    }).catch(() => {});
  };

  const onImportFile = () => {
    void pickFile("Import provider output", [
      { name: "Text", extensions: ["txt", "md", "rtf", "docx", "pdf", "srt", "vtt", "json"] },
    ]).then((path) => {
      if (!path) return;
      void bridgeCommit("bridge.import", {
        provider_key: assistKey, source_path: path, kind: "text",
      }).then((r) => {
        setImported(true);
        setNotice(`File import queued (job ${r.job_id?.slice(0, 8) ?? "?"}) — provenance: user-reported external.`);
        onCommitted(); void refreshAssets();
      }).catch(() => {});
    });
  };

  const onWatchScan = () => {
    setWatchCands(null);
    void bridgeCommit("bridge.watch.scan", { provider_key: assistKey })
      .then((r) => {
        const c = (r.bridge as { candidates?: WatchCandidate[] } | null)?.candidates ?? [];
        setWatchCands(c);
        setNotice(c.length
          ? `${c.length} file(s) found — import each explicitly.`
          : "No new eligible files in the designated folder.");
      }).catch(() => {});
  };

  const onImportCandidate = (c: WatchCandidate) => {
    void bridgeCommit("bridge.import", {
      provider_key: assistKey, source_path: c.path, from_watch: true,
      kind: "text", name: c.name,
    }).then((r) => {
      setWatchCands((cur) => (cur ?? []).filter((x) => x.path !== c.path));
      setImported(true);
      setNotice(`Imported ${c.name} (job ${r.job_id?.slice(0, 8) ?? "?"}) — user-reported external.`);
      onCommitted(); void refreshAssets();
    }).catch(() => {});
  };

  // Import-only (T3) providers have no prepare/copy/launch steps — the
  // rail only renders for a documented site workflow (hasT2).
  const stage = !shareText.trim() || !prepared ? 0
    : !copied ? 1
    : !launchUrl && canLaunch ? 2
    : imported ? 4 : 3;

  return (
    <section className="mt-4 rounded-md border border-sky/40 bg-card p-4"
      data-testid="assisted-panel">
      <h2 className="text-sm font-semibold">Assisted text — no API integration</h2>
      <p className="mt-2 text-xs text-muted">
        These providers have <b>no API integration</b> in ADE. You do the work on the
        provider's own site — ADE prepares your text, opens the documented site only
        when you click, and imports the result as a <b>user-reported external</b> asset.
        Third-party services may require a separate account, subscription, or fees.
      </p>

      <div className="mt-3 grid grid-cols-2 gap-3">
        <div>
          <label className={labelCls} htmlFor="assist-provider">Assisted provider</label>
          <select id="assist-provider" className={inputCls} value={assistKey}
            onChange={(e) => setAssistKey(e.target.value)}>
            <option value="">Choose provider…</option>
            {Object.values(details)
              .filter((d) => assistStrategies(d).length > 0)
              .map((d) => (
                <option key={d.key} value={d.key}>{d.label} (assisted)</option>
              ))}
          </select>
          {providers.length > 0 && eligible.length === 0 && (
            <p className="mt-1 text-xs text-faint">
              No providers with a documented assisted text path in the catalog.
            </p>
          )}
        </div>
        {detail && (
          <div className="rounded border border-line bg-panel p-2.5 text-[11px] text-muted"
            data-testid="assist-provider-facts">
            <p><b className="text-fg">{detail.label}</b> — assisted workflow only; ADE
              never calls their API.</p>
            {detail.privacy_summary && <p className="mt-0.5">{detail.privacy_summary}</p>}
            {detail.status_reason && <p className="mt-0.5">{detail.status_reason}</p>}
            {detail.tos_url && (
              <a className="mt-0.5 inline-block text-sky underline" href={detail.tos_url}
                target="_blank" rel="noreferrer">Provider terms</a>
            )}
          </div>
        )}
      </div>

      {assistKey && (
        <>
          {stale && (
            <p className="mt-2 rounded border border-amber/40 bg-amber/10 p-2 text-xs text-amber"
              data-testid="assist-stale">
              Verification evidence is stale — dispatch is refused until re-check.
            </p>
          )}
          {caps && !hasT2 && (
            <p className="mt-2 rounded border border-line bg-panel p-2 text-xs text-muted"
              data-testid="assist-import-only">
              No documented site workflow for this provider — the manual
              paste/export path below is the only supported return.
            </p>
          )}

          {/* step rail — only where a site workflow exists */}
          {hasT2 && (
          <ol className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-[10px] uppercase tracking-wide"
            data-testid="assist-steps">
            {STEPS.map((s, i) => (
              <li key={s} className={
                i < stage ? "text-teal" : i === stage ? "text-amber" : "text-faint"}>
                {i + 1}. {s}
              </li>
            ))}
          </ol>
          )}

          {/* 1. prepare */}
          <div className="mt-3">
            <label className={labelCls} htmlFor="assist-prompt">Your text / instructions</label>
            <textarea id="assist-prompt" className={`${inputCls} min-h-20`} value={prompt}
              onChange={(e) => { setPrompt(e.target.value); setPrepared(null); }}
              placeholder="Describe what you want the provider's site to produce…" />
            {ctxOptions.length > 0 && (
              <fieldset className="mt-2 rounded border border-line bg-panel p-2">
                <legend className="px-1 text-[10px] uppercase tracking-wide text-faint">
                  Optional project context — shared only if you paste it yourself
                </legend>
                <div className="flex flex-wrap gap-x-4 gap-y-1">
                  {ctxOptions.map((c) => {
                    const k = `${c.etype}:${c.id}`;
                    return (
                      <label key={k} className="flex items-center gap-1 text-xs text-muted">
                        <input type="checkbox" checked={ctxSel.includes(k)}
                          onChange={(e) => setCtxSel((cur) =>
                            e.target.checked ? [...cur, k] : cur.filter((x) => x !== k))} />
                        {c.etype}: {c.name}
                      </label>
                    );
                  })}
                </div>
              </fieldset>
            )}
            <div className="mt-2" data-testid="assist-share-preview">
              <p className={labelCls}>Exact text prepared for sharing
                ({shareText.length} chars — nothing else leaves this device)</p>
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded border border-line bg-panel p-2 text-xs text-muted">
                {shareText || "(empty)"}
              </pre>
            </div>
            {canPrepare ? (
              <button className={`${btnCls} mt-2`} disabled={busy || !shareText.trim() || stale}
                onClick={onPrepare} data-testid="assist-prepare">
                Prepare text
              </button>
            ) : (
              <p className="mt-2 text-xs text-faint">
                Copy the text above yourself — this provider documents no assisted
                prompt path, only file import.
              </p>
            )}
          </div>

          {/* 2–4. review / copy / open / create externally */}
          {prepared && (
            <div className="mt-3 rounded border border-line bg-panel p-3" data-testid="assist-review">
              <p className="text-xs font-medium text-muted">
                Review — this exact text goes to {detail?.label} only when YOU paste it there:
              </p>
              <pre className="mt-1.5 max-h-40 overflow-auto whitespace-pre-wrap rounded border border-line bg-card p-2 text-xs text-fg"
                data-testid="assist-prepared-text">{prepared.prepared_text}</pre>
              <div className="mt-2 flex flex-wrap gap-2">
                <button className={ghostBtn} onClick={() => void onCopy()} data-testid="assist-copy">
                  {copied ? "Copied ✓" : "Copy prepared text"}
                </button>
                {canLaunch && (
                  <button className={btnCls} onClick={onLaunch} data-testid="assist-open">
                    Open {detail?.label} site
                  </button>
                )}
                {!canLaunch && launchCandidate && (
                  <p className="self-center text-xs text-muted">
                    Open manually: <code className="text-fg">{launchCandidate}</code>
                  </p>
                )}
              </div>
              <p className="mt-2 text-[11px] text-faint">
                Sign in and work on their site — ADE does not log in, fill, click,
                or read anything there. When you have a result, bring it back below.
              </p>
            </div>
          )}

          {/* 5. return & import */}
          {canImport && (
            <div className="mt-3 rounded border border-line bg-panel p-3" data-testid="assist-return">
              <p className="text-xs font-medium text-muted">Return &amp; import</p>
              <textarea className={`${inputCls} mt-2 min-h-16`} value={paste}
                onChange={(e) => setPaste(e.target.value)}
                placeholder={`Paste the text ${detail?.label ?? "the provider"} produced…`}
                data-testid="assist-paste" />
              <div className="mt-2 flex flex-wrap gap-2">
                <button className={btnCls} disabled={busy || !paste.trim()}
                  onClick={onImportContent} data-testid="assist-import-paste">
                  Import pasted text
                </button>
                <button className={ghostBtn} disabled={busy} onClick={onImportFile}
                  data-testid="assist-import-file">
                  Import a file…
                </button>
                {importDir ? (
                  <button className={ghostBtn} disabled={busy} onClick={onWatchScan}
                    data-testid="assist-watch-scan">
                    Check import folder
                  </button>
                ) : (
                  <span className="self-center text-[11px] text-faint">
                    Designate an import folder in Providers settings to scan files.
                  </span>
                )}
              </div>
              {watchCands !== null && (
                <ul className="mt-2 space-y-1" data-testid="assist-watch-candidates">
                  {watchCands.length === 0 && (
                    <li className="text-xs text-faint">No new files found.</li>)}
                  {watchCands.map((c) => (
                    <li key={c.path} className="flex items-center justify-between gap-2 text-xs">
                      <span className="truncate text-muted">{c.name}</span>
                      <button className={ghostBtn} disabled={busy}
                        onClick={() => onImportCandidate(c)}>
                        Import
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <p className="mt-2 text-[11px] text-faint">
                Imports are recorded as <b>user-reported external</b> provenance —
                never simulated API output, never verified remote.
              </p>
            </div>
          )}
        </>
      )}
      {notice && <p className="mt-2 text-xs text-teal">{notice}</p>}
      {error && <p className="mt-2 text-xs text-danger">{error}</p>}
    </section>
  );
}
