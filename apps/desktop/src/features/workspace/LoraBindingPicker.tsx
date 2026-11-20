// G2-B/B-3b — shared LoRA binding picker: catalog-backed selection with
// role/family/license/install state badges, per-adapter strength, an
// explicit character-default prefill offer, and lossless display of
// legacy/unknown/invalid stored references (removal is a deliberate
// user action — nothing is dropped, converted, or activated silently).
// Every state shown is advisory; backend `_check_loras` decides.
import type { ModelCatalogEntry } from "../../api/types";
import {
  invalidLoraCount, isLoraEntry, loraCatalogState, loraRole,
  refsBoundKeys, LORA_RUNTIME_NOTE, LORA_STATE_BADGE,
  type LoraRef, type LoraRole,
} from "./lora";

const ROLE_HEADING: Record<LoraRole, string> = {
  identity: "Identity adapters — tied to this cast member",
  style: "Style adapters — linked to this project through this cast row",
};

export function LoraBindingPicker({
  refs,
  onChange,
  models,
  roles,
  baseFamily = null,
  defaults = [],
  defaultsLabel = "Apply character defaults",
  idPrefix,
}: {
  /** Working copy of the stored lora_json entries (lossless refs). */
  refs: LoraRef[];
  onChange: (refs: LoraRef[]) => void;
  /** /api/models catalog entries. */
  models: ModelCatalogEntry[];
  /** Roles offered as catalog picks (cast: both; character: identity). */
  roles: LoraRole[];
  /** Selected base family for mismatch badges; null → info chip only. */
  baseFamily?: string | null;
  /** Eligible character-default refs offered as a prefill button. */
  defaults?: LoraRef[];
  defaultsLabel?: string;
  /** Unique id prefix for label association. */
  idPrefix: string;
}) {
  const bound = refsBoundKeys(refs);
  const catalog = models.filter(isLoraEntry);
  const catalogKeys = new Set(catalog.map((m) => m.key));
  const invalid = invalidLoraCount(refs);
  const other = refs
    .map((r, i) => ({ r, i }))
    .filter(({ r }) => r.kind === "invalid" || !catalogKeys.has(r.key));
  const offerable = defaults.filter(
    (d) => d.kind === "structured" && !bound.has(d.key));

  function toggle(key: string, checked: boolean) {
    if (checked) {
      if (!bound.has(key)) {
        onChange([...refs, { kind: "structured", key, strength: null, raw: { key } }]);
      }
    } else {
      onChange(refs.filter((r) => r.kind === "invalid" || r.key !== key));
    }
  }

  function setStrength(key: string, text: string) {
    const t = text.trim();
    onChange(refs.map((r): LoraRef => {
      if (r.kind === "invalid" || r.key !== key) return r;
      // Editing strength on a legacy string ref upgrades it to the
      // structured form — an explicit user action, never automatic.
      const base: Record<string, unknown> =
        r.kind === "structured" ? r.raw : { key: r.key };
      if (t === "") return { kind: "structured", key, strength: null, raw: base };
      const n = Number(t);
      if (!Number.isFinite(n)) return r;
      return { kind: "structured", key, strength: n, raw: base };
    }));
  }

  const remove = (i: number) => onChange(refs.filter((_, x) => x !== i));

  return (
    <fieldset className="rounded-md border border-line p-3">
      <legend className="px-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
        Adapters (LoRA)
      </legend>
      <p className="text-[11px] text-faint" id={`${idPrefix}-honesty`}>
        {LORA_RUNTIME_NOTE}
      </p>

      {roles.map((role) => {
        const options = catalog.filter((m) => loraRole(m) === role);
        // One identity adapter per binding context — a second identity
        // pick can't mean anything for this cast member (the backend
        // also refuses >1 identity in any generation request).
        const identityTaken = options.some(
          (o) => o.key !== undefined && bound.has(o.key));
        return (
          <div key={role} className="mt-2">
            <h5 className="text-[10px] font-semibold uppercase tracking-wide text-faint">
              {ROLE_HEADING[role]}
            </h5>
            {options.length === 0 ? (
              <p className="mt-1 text-[11px] text-faint">
                No {role} adapters are cataloged yet.
              </p>
            ) : (
              <ul className="mt-1 space-y-1">
                {options.map((m) => {
                  const state = loraCatalogState(m, baseFamily);
                  const badge = LORA_STATE_BADGE[state];
                  const checked = bound.has(m.key);
                  const selectable = state === "available"
                    && (role !== "identity" || checked || !identityTaken);
                  const ref = refs.find((r) => r.kind !== "invalid" && r.key === m.key);
                  const stId = `${idPrefix}-${role}-${m.key}-strength`;
                  return (
                    <li key={m.key} className="flex flex-wrap items-center gap-1.5 rounded border border-line bg-card px-2 py-1">
                      <label className="flex items-center gap-1.5 text-xs text-fg">
                        <input
                          type="checkbox"
                          checked={checked}
                          disabled={!selectable}
                          onChange={(e) => toggle(m.key, e.target.checked)}
                          aria-describedby={`${idPrefix}-honesty`}
                        />
                        {m.display_name}
                      </label>
                      <span className="text-[10px] text-faint">v{m.version} · for {m.family} bases</span>
                      <span className={`rounded px-1.5 py-0.5 text-[10px] ${badge.cls}`}>{badge.label}</span>
                      {checked && (
                        <label htmlFor={stId} className="ml-auto flex items-center gap-1 text-[10px] text-muted">
                          strength
                          <input
                            id={stId}
                            inputMode="decimal"
                            value={ref?.kind === "structured" && ref.strength !== null ? String(ref.strength) : ""}
                            placeholder="1.0"
                            title="0 < strength ≤ 2.0 — empty uses the default 1.0"
                            onChange={(e) => setStrength(m.key, e.target.value)}
                            className="w-14 rounded border border-line bg-panel px-1.5 py-0.5 text-[11px] text-fg"
                          />
                        </label>
                      )}
                    </li>
                  );
                })}
              </ul>
            )}
          </div>
        );
      })}

      {offerable.length > 0 && (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => onChange([
              ...refs,
              ...offerable.map((d) => ({ ...d })),
            ])}
            className="rounded border border-teal/40 px-2 py-0.5 text-[11px] text-teal hover:border-teal"
          >
            {defaultsLabel} ({offerable.map((d) => (d.kind === "invalid" ? "?" : d.key)).join(", ")})
          </button>
        </div>
      )}

      {other.length > 0 && (
        <div className="mt-2">
          <h5 className="text-[10px] font-semibold uppercase tracking-wide text-faint">
            Stored references
          </h5>
          <ul className="mt-1 space-y-1">
            {other.map(({ r, i }) => (
              <li key={i} className="flex items-center gap-1.5 rounded border border-dashed border-line px-2 py-1 text-[11px]">
                {r.kind === "invalid" ? (
                  <span className="text-amber">
                    invalid stored entry: {JSON.stringify(r.raw)?.slice(0, 60)}
                  </span>
                ) : (
                  <span className="text-muted">
                    “{r.key}” — saved reference, not a catalog adapter
                    {r.kind === "structured" && r.strength !== null && ` · strength ${r.strength}`}
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => remove(i)}
                  title={r.kind === "invalid" ? "Remove this invalid entry" : `Remove "${r.key}"`}
                  className="ml-auto rounded border border-line px-1.5 py-0.5 text-[10px] text-muted hover:text-danger"
                >
                  ✕
                </button>
              </li>
            ))}
          </ul>
          {invalid > 0 && (
            <p className="mt-1 rounded border border-amber/40 bg-amber/10 px-2 py-1 text-[11px] text-amber" role="alert">
              {invalid} invalid stored entr{invalid === 1 ? "y" : "ies"} — left
              untouched {invalid === 1 ? "it's" : "they're"} preserved as-is,
              but adapter changes can't be saved until {invalid === 1 ? "it's" : "they're"}
              removed (✕ above) or corrected outside the app.
            </p>
          )}
        </div>
      )}
    </fieldset>
  );
}
