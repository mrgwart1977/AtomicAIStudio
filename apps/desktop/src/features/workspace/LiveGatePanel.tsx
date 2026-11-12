// C2 exit gate — Stage A live-connectivity safety controls.
//
// Read-mostly surface for the durable enablement state: which
// provider+model pairs hold a live opt-in, why dispatch would be
// refused, and the emergency kill switch. Stage A wires NO live
// transport — enabling only grants the opt-in flag a future real
// transport must pass; nothing here can reach a provider.
import { useEffect, useState } from "react";
import { apiFetch } from "../../api/client";
import { commitCommand } from "../../api/commands";
import type { LiveStatus, ProviderDetail } from "../../api/types";

const btn =
  "rounded-md bg-accent px-2.5 py-1 text-xs font-medium text-white hover:bg-accent/90 disabled:opacity-40";
const ghost =
  "rounded-md border border-line px-2.5 py-1 text-xs text-muted hover:text-fg disabled:opacity-40";
const danger =
  "rounded-md border border-danger/60 px-2.5 py-1 text-xs text-danger hover:bg-danger/10 disabled:opacity-40";
const selectCls =
  "w-full rounded-md border border-line bg-panel px-2 py-1 text-xs text-fg";

export function LiveGatePanel({ pid, revision, details, onCommitted }: {
  pid: string;
  revision: number;
  details: Record<string, ProviderDetail>;
  onCommitted: () => void;
}) {
  const [status, setStatus] = useState<LiveStatus | null>(null);
  const [modelByKey, setModelByKey] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const reload = () => {
    apiFetch<LiveStatus>(`/api/projects/${pid}/cloud/live-status`)
      .then(setStatus)
      .catch((e) => setError(`live-status: ${String(e)}`));
  };
  useEffect(reload, [pid]);

  const act = (ct: string, payload: Record<string, unknown>, note: string) => {
    setBusy(true); setError(null);
    void commitCommand(pid, revision, ct, payload)
      .then(() => { setNotice(note); reload(); onCommitted(); })
      .catch((e) => setError(String(e)))
      .finally(() => setBusy(false));
  };

  const keys = Object.keys(status?.providers ?? {});

  return (
    <section className="mt-4 rounded-md border border-violet/40 bg-card p-4"
      data-testid="live-gate-panel">
      <h2 className="text-sm font-semibold">Live exit gate — Stage A</h2>
      <p className="mt-2 text-xs text-muted">
        Safety gates for future real-provider validation. <b>No live dispatch
        exists in this build</b> — enabling only records the audited opt-in a
        future transport must pass. Validation caps (proposed):
        {status && ` $${status.caps_usd.per_request}/request ·
        $${status.caps_usd.per_provider}/provider · $${status.caps_usd.global} total`}.
      </p>

      {status?.global_disabled && (
        <p className="mt-2 rounded border border-danger/50 bg-danger/10 p-2 text-xs text-danger"
          data-testid="live-kill-banner">
          KILL SWITCH ACTIVE — all live dispatch refused since {status.global_disabled_at}.
        </p>
      )}

      <ul className="mt-3 space-y-1.5" data-testid="live-provider-rows">
        {keys.map((k) => {
          const p = status!.providers[k];
          const models = (details[k]?.models ?? [])
            .filter((m) => m.capability === "text" && m.cost_table);
          const sel = modelByKey[k] ?? p.model_id ?? models[0]?.id ?? "";
          return (
            <li key={k} className="rounded border border-line bg-panel p-2 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <b className="text-fg">{details[k]?.label ?? k}</b>
                {p.enabled
                  ? <span className="text-teal">enabled · {p.model_id}</span>
                  : <span className="text-faint">disabled</span>}
                <span className="ml-auto text-faint">
                  exposure ${p.live_exposure_usd}
                </span>
              </div>
              <div className="mt-1.5 flex flex-wrap items-center gap-2">
                {!p.enabled && !status!.global_disabled && (
                  <>
                    <select className={`${selectCls} w-auto`}
                      aria-label={`model for ${k}`}
                      value={sel}
                      onChange={(e) => setModelByKey((m) => ({ ...m, [k]: e.target.value }))}>
                      {models.map((m) => (
                        <option key={m.id} value={m.id}>{m.id}</option>
                      ))}
                    </select>
                    <button className={btn} disabled={busy || !sel}
                      data-testid={`live-enable-${k}`}
                      onClick={() => act("cloud.live.enable", {
                        provider_key: k, model_id: sel, credential_ref: k,
                      }, `Live opt-in granted for ${k} — flag only, no dispatch.`)}>
                      Enable live
                    </button>
                  </>
                )}
                {p.enabled && (
                  <button className={ghost} disabled={busy}
                    data-testid={`live-disable-${k}`}
                    onClick={() => act("cloud.live.disable", { provider_key: k },
                      `Live opt-in revoked for ${k}.`)}>
                    Disable
                  </button>
                )}
                {p.blocked_reason && !p.enabled && (
                  <span className="text-[11px] text-faint" data-testid={`live-blocked-${k}`}>
                    dispatch refused: {p.blocked_reason}
                  </span>
                )}
                {!p.credential_configured && (
                  <span className="text-[11px] text-faint">no credential stored</span>
                )}
              </div>
            </li>
          );
        })}
      </ul>

      <div className="mt-3 flex items-center gap-2">
        {status?.global_disabled ? (
          <button className={btn} disabled={busy} data-testid="live-resume"
            onClick={() => act("cloud.live.resume", {},
              "Kill switch lifted — providers stay disabled until re-granted.")}>
            Lift kill switch
          </button>
        ) : (
          <button className={danger} disabled={busy} data-testid="live-kill"
            onClick={() => act("cloud.live.disable_all", {},
              "Kill switch engaged — all live dispatch refused.")}>
            Disable ALL live execution
          </button>
        )}
        <span className="text-[11px] text-faint">
          Open reservations are never auto-released by these controls.
        </span>
      </div>
      {notice && <p className="mt-2 text-xs text-teal">{notice}</p>}
      {error && <p className="mt-2 text-xs text-danger">{error}</p>}
    </section>
  );
}
