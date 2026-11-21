// B-3c — backend-authoritative LoRA resolution preview (read-only).
// Renders what the B-1 gate would accept or refuse for a local shot/
// keyframe generation request: requested entries, resolved adapters,
// explicit refusal reasons, and the request-level pins. Everything is
// advisory — nothing here queues, writes, stages, or executes, and a
// real generation re-validates against the then-current state.
import { useEffect, useRef, useState } from "react";
import { loraPreview } from "../../api/client";
import type { LoraPreview, LoraPreviewAdapter } from "../../api/types";
import { LORA_RUNTIME_NOTE } from "./lora";

const sha12 = (sha: string | null | undefined) =>
  sha ? `${sha.slice(0, 12)}…` : "—";

function AdapterRow({ a }: { a: LoraPreviewAdapter }) {
  return (
    <li className="rounded border border-line bg-panel px-2 py-1.5 text-xs">
      <div className="flex items-center gap-2">
        <span className="font-medium text-fg">{a.key}</span>
        <span className={`rounded px-1.5 py-0.5 text-[10px] ${
          a.role === "identity"
            ? "bg-teal/15 text-teal" : "bg-violet/15 text-violet"}`}>
          {a.role}
        </span>
        <span className="ml-auto text-faint">×{a.strength}</span>
      </div>
      <p className="mt-0.5 text-[10px] text-faint">
        stages as <code>{a.staged_name}</code>
        {a.bound_character_id
          ? <> · bound to {a.bound_character_id}</>
          : null}
        {" · sha "}<code>{sha12(a.sha256)}</code>
      </p>
    </li>
  );
}

export function LoraResolutionPreview({
  pid, shotId, keyframeId, modelKey, prompt, negativePrompt, loras,
}: {
  pid: string;
  shotId?: string;
  keyframeId?: string;
  modelKey?: string;
  prompt?: string;
  negativePrompt?: string;
  loras?: unknown[];
}) {
  const [result, setResult] = useState<LoraPreview | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Stale-proofing, two layers:
  //  • ctxRef mirrors the current context synchronously at render time —
  //    project/shot/keyframe/model AND the proposed selection, strength,
  //    and prompt text — so a response landing after any relevant change
  //    (even before passive effects run) is recognized as stale.
  //  • reqSeq is a monotonically increasing request id: an older request
  //    for the same context can never overwrite a newer one, and a stale
  //    request's finally block can't clear a newer request's loading
  //    state.
  const selKey = JSON.stringify(loras ?? null);
  const ctxRef = useRef(
    {pid, shotId, keyframeId, modelKey, selKey, prompt, negativePrompt},
  );
  ctxRef.current =
    {pid, shotId, keyframeId, modelKey, selKey, prompt, negativePrompt};
  const reqSeq = useRef(0);

  useEffect(() => {
    reqSeq.current += 1;   // invalidate anything still in flight
    setResult(null);
    setErr(null);
    setBusy(false);
  }, [pid, shotId, keyframeId, modelKey, selKey, prompt, negativePrompt]);

  async function run() {
    const ctx = ctxRef.current;
    const id = ++reqSeq.current;
    const live = () =>
      id === reqSeq.current &&
      ctx.pid === ctxRef.current.pid &&
      ctx.shotId === ctxRef.current.shotId &&
      ctx.keyframeId === ctxRef.current.keyframeId &&
      ctx.modelKey === ctxRef.current.modelKey &&
      ctx.selKey === ctxRef.current.selKey &&
      ctx.prompt === ctxRef.current.prompt &&
      ctx.negativePrompt === ctxRef.current.negativePrompt;
    setBusy(true);
    setErr(null);
    try {
      const res = await loraPreview(pid, {
        model_key: modelKey,
        shot_id: shotId,
        keyframe_id: keyframeId,
        loras,
        prompt: prompt || undefined,
        negative_prompt: negativePrompt || undefined,
      });
      if (!live()) return;
      setResult(res);
    } catch (e) {
      if (!live()) return;
      setErr(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      if (id === reqSeq.current) setBusy(false);
    }
  }

  return (
    <section
      aria-label="LoRA resolution preview"
      className="rounded-md border border-line bg-card p-3"
    >
      <div className="flex items-center gap-2">
        <h3 className="text-xs font-semibold text-muted">
          Adapter resolution — backend preview
        </h3>
        <span className="rounded bg-line/40 px-1.5 py-0.5 text-[10px] text-faint">
          read-only · advisory
        </span>
      </div>
      <p className="mt-1 text-[11px] text-faint">
        {shotId || keyframeId
          ? "Resolves this context's stored cast bindings through the same backend checks a real generation would run — nothing is queued."
          : "Checks a proposed adapter selection through the same backend checks a real generation would run — nothing is queued."}
      </p>
      <button
        type="button"
        onClick={() => void run()}
        disabled={busy}
        className="mt-2 rounded-md border border-line px-3 py-1.5 text-xs text-fg hover:border-teal disabled:opacity-50"
      >
        {busy ? "Checking…" : "Check what the backend would accept"}
      </button>

      {err && (
        <p role="alert"
           className="mt-2 rounded border border-rose/40 bg-rose/10 px-2 py-1 text-xs text-rose">
          {err}
        </p>
      )}

      {result && (
        <div className="mt-3 space-y-2">
          <p className={`text-xs font-medium ${
            result.accepted ? "text-teal" : "text-amber"}`}>
            {result.accepted
              ? (result.resolved.length
                ? `Backend accepts ${result.resolved.length} adapter` +
                  `${result.resolved.length === 1 ? "" : "s"}.`
                : "Accepted — the request carries no adapters.")
              : "Backend refuses this selection."}
          </p>

          {[...result.errors, ...result.prompt_errors].length > 0 && (
            <ul role="alert" className="space-y-1">
              {result.errors.map((e, i) => (
                <li key={`e${i}`}
                    className="rounded border border-amber/40 bg-amber/10 px-2 py-1 text-[11px] text-amber">
                  {e}
                </li>
              ))}
              {result.prompt_errors.map((e, i) => (
                <li key={`p${i}`}
                    className="rounded border border-amber/40 bg-amber/10 px-2 py-1 text-[11px] text-amber">
                  {e}
                </li>
              ))}
            </ul>
          )}

          {result.requested.length > 0 && (
            <div>
              <h4 className="text-[10px] font-semibold uppercase tracking-wide text-faint">
                Requested
              </h4>
              <ul className="mt-0.5 space-y-0.5">
                {result.requested.map((r, i) => (
                  <li key={i} className="text-[11px] text-muted">
                    <code>{String(r.key ?? "—")}</code>
                    {r.strength != null && <> ×{String(r.strength)}</>}
                    {r.shape === "invalid" && (
                      <span className="text-amber"> (malformed entry)</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {result.resolved.length > 0 && (
            <div>
              <h4 className="text-[10px] font-semibold uppercase tracking-wide text-faint">
                Accepted adapters
              </h4>
              <ul className="mt-1 space-y-1">
                {result.resolved.map((a) => (
                  <AdapterRow key={a.key} a={a} />
                ))}
              </ul>
            </div>
          )}

          <div>
            <h4 className="text-[10px] font-semibold uppercase tracking-wide text-faint">
              Pins &amp; provenance
            </h4>
            <ul className="mt-0.5 space-y-0.5 text-[11px] text-muted">
              {Object.entries(result.hashes.adapter_sha256).map(([k, sha]) => (
                <li key={k}>
                  <code>{k}</code> → <code>{sha12(sha)}</code>
                </li>
              ))}
              {result.hashes.base_model_sha256 && (
                <li>base model → <code>{sha12(result.hashes.base_model_sha256)}</code></li>
              )}
              {result.hashes.prompt_sha256 && (
                <li>supplied prompt →{" "}
                  <code>{sha12(result.hashes.prompt_sha256)}</code>
                </li>
              )}
              {result.hashes.runtime_prompt_sha256 && (
                <li>runtime prompt (supplied text + tags) →{" "}
                  <code>{sha12(result.hashes.runtime_prompt_sha256)}</code>
                </li>
              )}
              {result.hashes.unavailable.map((u, i) => (
                <li key={i} className="text-faint">unavailable — {u}</li>
              ))}
            </ul>
          </div>

          {result.est_vram_lora_overhead_bytes != null
              && result.est_vram_lora_overhead_bytes > 0 && (
            <p className="text-[10px] text-faint">
              Estimated adapter VRAM overhead:{" "}
              {(result.est_vram_lora_overhead_bytes / 1e9).toFixed(1)} GB
            </p>
          )}

          <ul className="space-y-0.5">
            {result.notes.map((n, i) => (
              <li key={i} className="text-[10px] italic text-faint">{n}</li>
            ))}
          </ul>
          <p className="text-[10px] text-faint">{LORA_RUNTIME_NOTE}</p>
          <p className="text-[10px] text-faint">
            This result reflects state at check time — re-check after
            changing cast bindings, models, or installs.
          </p>
        </div>
      )}
    </section>
  );
}
