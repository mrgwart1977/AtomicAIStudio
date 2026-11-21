// B-3d — explicit per-request LoRA selection for local image generation.
// One identity + one style adapter max (the B-1 contract), strength in
// (0, 2.0], explicit removal, advisory catalog/install badges, and cast
// or character-default SUGGESTIONS that apply only on a click — never
// automatically. Selection is request-scoped state in GeneratePage: it
// is sent on the loras field, never written back to shot cast bindings.
import type { ModelCatalogEntry } from "../../api/types";
import {
  LORA_RUNTIME_NOTE,
  LORA_STATE_BADGE,
  isLoraEntry,
  loraCatalogState,
  loraRole,
  type LoraRole,
  type LoraSelection,
} from "./lora";

export interface LoraSuggestion {
  key: string;
  /** e.g. "bound to Ada" or "Ada default" — where the offer comes from. */
  label: string;
  strength: number | null;
}

const selectCls =
  "w-full rounded-md border border-line bg-card px-2 py-1.5 text-xs text-fg";

function RolePicker({
  role,
  title,
  models,
  baseFamily,
  hasShot,
  suggestions,
  value,
  onChange,
}: {
  role: LoraRole;
  title: string;
  models: ModelCatalogEntry[];
  baseFamily: string | null;
  hasShot: boolean;
  suggestions: LoraSuggestion[];
  value: LoraSelection[typeof role];
  onChange: (v: LoraSelection[typeof role]) => void;
}) {
  const options = models.filter((m) => isLoraEntry(m) && loraRole(m) === role);
  const entry = value ? models.find((m) => m.key === value.key) : undefined;
  const state = entry ? loraCatalogState(entry, baseFamily) : null;
  const roleSuggestions = suggestions.filter((s) => {
    const m = models.find((x) => x.key === s.key);
    return m !== undefined && loraRole(m) === role;
  });

  return (
    <fieldset className="space-y-1.5">
      <legend className="text-[10px] font-semibold uppercase tracking-wide text-faint">
        {title}
      </legend>
      {role === "identity" && !hasShot && (
        <p className="text-[10px] text-amber">
          Attach a shot — identity adapters authorize against the shot's
          cast bindings.
        </p>
      )}
      <div className="flex items-center gap-1.5">
        <select
          aria-label={`${title} adapter`}
          className={selectCls}
          value={value?.key ?? ""}
          onChange={(e) =>
            onChange(e.target.value
              ? { key: e.target.value, strength: value?.strength ?? 1.0 }
              : null)
          }
        >
          <option value="">None</option>
          {options.map((m) => {
            const st = loraCatalogState(m, baseFamily);
            return (
              <option key={m.key} value={m.key}>
                {m.display_name}
                {st !== "available" ? ` (${LORA_STATE_BADGE[st].label})` : ""}
              </option>
            );
          })}
        </select>
        {value && (
          <button
            type="button"
            aria-label={`Remove ${title} adapter`}
            title="Remove selection"
            onClick={() => onChange(null)}
            className="rounded border border-line px-1.5 py-1 text-[10px] text-faint hover:text-fg"
          >
            ✕
          </button>
        )}
      </div>

      {entry && state && (
        <p className="flex flex-wrap items-center gap-1.5 text-[10px] text-faint">
          <span className={`rounded px-1.5 py-0.5 ${LORA_STATE_BADGE[state].cls}`}>
            {LORA_STATE_BADGE[state].label}
          </span>
          <span>{entry.family}</span>
          <span>· {entry.format}</span>
          <span>· {entry.license?.name ?? "license unknown"}</span>
          {entry.license?.commercial_ok === false && (
            <span className="text-rose">commercial use restricted</span>
          )}
        </p>
      )}

      {value && (
        <label className="flex items-center gap-2 text-[10px] text-muted">
          Strength
          <input
            type="number"
            aria-label={`${title} strength`}
            min={0.05}
            max={2}
            step={0.05}
            value={value.strength ?? 1.0}
            onChange={(e) =>
              onChange({ key: value.key, strength: Number(e.target.value) })
            }
            className="w-20 rounded border border-line bg-card px-2 py-0.5 text-xs text-fg"
          />
          <span className="text-faint">(0, 2.0]</span>
        </label>
      )}

      {roleSuggestions.length > 0 && (
        <div className="flex flex-wrap items-center gap-1">
          <span className="text-[10px] text-faint">From cast/defaults:</span>
          {roleSuggestions.map((s) => (
            <button
              key={`${s.key}:${s.label}`}
              type="button"
              title={`Select ${s.key} for this request only`}
              onClick={() => onChange({ key: s.key, strength: s.strength ?? 1.0 })}
              className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted hover:border-teal hover:text-fg"
            >
              {s.key} <span className="text-faint">· {s.label}</span>
            </button>
          ))}
        </div>
      )}

      {role === "identity" && (
        <p className="text-[10px] text-faint">
          One identity LoRA per generation — it conditions the whole
          image; per-character spatial blending is not supported.
        </p>
      )}
    </fieldset>
  );
}

export function LoraSelectionPanel({
  models,
  baseFamily,
  hasShot,
  suggestions,
  value,
  onChange,
}: {
  /** Full catalog including modality:"lora" entries. */
  models: ModelCatalogEntry[];
  /** Selected base model's family — drives the family-compat badge. */
  baseFamily: string | null;
  /** A shot context is attached (identity authorization needs one). */
  hasShot: boolean;
  /** Cast-binding / character-default offers — applied only on click. */
  suggestions: LoraSuggestion[];
  value: LoraSelection;
  onChange: (sel: LoraSelection) => void;
}) {
  const hasLoras = models.some((m) => isLoraEntry(m));
  return (
    <section
      aria-label="LoRA adapter selection"
      className="space-y-3 rounded-md border border-line bg-card p-3"
    >
      <div className="flex items-center gap-2">
        <h3 className="text-xs font-semibold text-muted">
          LoRA adapters — this generation only
        </h3>
        <span className="rounded bg-line/40 px-1.5 py-0.5 text-[10px] text-faint">
          optional
        </span>
      </div>

      {baseFamily !== null && baseFamily !== "sd1.5" && (
        <p className="rounded border border-amber/40 bg-amber/10 px-2 py-1 text-[11px] text-amber">
          LoRA adapters apply to SD 1.5 image models only — the backend
          refuses them on '{baseFamily}' bases.
        </p>
      )}

      {!hasLoras && (
        <p className="text-[11px] text-faint">
          No LoRA adapters in the catalog yet — nothing to select.
        </p>
      )}

      {hasLoras && (
        <>
          <RolePicker
            role="identity"
            title="Identity LoRA"
            models={models}
            baseFamily={baseFamily}
            hasShot={hasShot}
            suggestions={suggestions}
            value={value.identity}
            onChange={(v) => onChange({ ...value, identity: v })}
          />
          <RolePicker
            role="style"
            title="Style LoRA"
            models={models}
            baseFamily={baseFamily}
            hasShot={hasShot}
            suggestions={suggestions}
            value={value.style}
            onChange={(v) => onChange({ ...value, style: v })}
          />
        </>
      )}

      <p className="text-[10px] text-faint">
        Selection applies to this generation request only — saved cast
        bindings are unchanged.
      </p>
      <p className="text-[10px] text-faint">{LORA_RUNTIME_NOTE}</p>
    </section>
  );
}
