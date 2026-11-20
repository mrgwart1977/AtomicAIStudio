// G2-B/B-3a — LoRA data layer: typed selections, lossless lora_json
// normalization, and advisory catalog-state classification.
// The UI mirrors backend semantics only for display — `_check_loras`
// (backend/app/domain/commands.py) remains the sole authority. A state
// of "available" means catalog+install+family checks pass; it does NOT
// mean real LoRA inference has been validated.
import type { ModelCatalogEntry } from "../../api/types";

/** Request-level LoRA selection — the structured request shape the
 *  backend `loras` field accepts ({key, strength?}). */
export interface LoraSel {
  key: string;
  strength?: number;
}

/** Adapter role declared by a catalog spec's capabilities["lora_role"]. */
export type LoraRole = "identity" | "style";

/** One stored lora_json entry, normalized for display. LOSSLESS:
 *  preserves order, duplicates, and the original submitted value in
 *  `raw` — nothing here converts, deduplicates, or drops history.
 *    key        — legacy bare-string reference (shape kept as stored)
 *    structured — {"key", "strength"?} binding form
 *    invalid    — anything else (malformed history); displayed, never
 *                 activated or silently rewritten */
export type LoraRef =
  | { kind: "key"; key: string; raw: string }
  | { kind: "structured"; key: string; strength: number | null; raw: Record<string, unknown> }
  | { kind: "invalid"; raw: unknown };

/** Normalize a stored `lora_json` list into display refs. Mirrors the
 *  tolerance of backend `_lora_list`/`lora_keys`: strings and shaped
 *  dicts become refs; anything unrecognized is kept as `invalid` so it
 *  can be viewed and intentionally corrected — never silently dropped.
 *  Non-list input (malformed column data) becomes one `invalid` ref. */
export function normalizeLoraEntries(entries: unknown): LoraRef[] {
  if (entries == null) return [];
  if (!Array.isArray(entries)) return [{ kind: "invalid", raw: entries }];
  const out: LoraRef[] = [];
  for (const x of entries) {
    if (typeof x === "string") {
      const k = x.trim();
      if (k) out.push({ kind: "key", key: k, raw: x });
      else out.push({ kind: "invalid", raw: x });
      continue;
    }
    if (x !== null && typeof x === "object" && !Array.isArray(x)) {
      const rec = x as Record<string, unknown>;
      const key = rec["key"];
      if (typeof key === "string" && key.trim()) {
        const s = rec["strength"];
        const strength =
          s === null || s === undefined
            ? null
            : typeof s === "number" && Number.isFinite(s)
              ? s
              : NaN;
        if (!Number.isNaN(strength)) {
          out.push({ kind: "structured", key: key.trim(), strength, raw: rec });
        } else {
          // non-numeric strength — backend rejects on save; surface as
          // invalid rather than fabricating a value.
          out.push({ kind: "invalid", raw: x });
        }
        continue;
      }
    }
    out.push({ kind: "invalid", raw: x });
  }
  return out;
}

/** Ordered key list from stored entries — same extraction as backend
 *  `lora_keys`, but kept as an array so duplicates remain observable. */
export function loraKeys(entries: unknown): string[] {
  return normalizeLoraEntries(entries).flatMap((r) =>
    r.kind === "invalid" ? [] : [r.key]);
}

/** Keys present more than once — backend refuses duplicate adapter keys
 *  at generation; the UI flags them for intentional correction. */
export function duplicateLoraKeys(entries: unknown): Set<string> {
  const seen = new Set<string>();
  const dup = new Set<string>();
  for (const k of loraKeys(entries)) {
    if (seen.has(k)) dup.add(k);
    seen.add(k);
  }
  return dup;
}

/** Distinct key membership view for binding checks. */
export function boundLoraKeys(entries: unknown): Set<string> {
  return new Set(loraKeys(entries));
}

/** True when a catalog entry is an attachable adapter, not a generatable
 *  model. */
export function isLoraEntry(entry: ModelCatalogEntry): boolean {
  return entry.modality === "lora";
}

/** Role a lora spec declares, if any ("identity" | "style"). */
export function loraRole(entry: ModelCatalogEntry): LoraRole | null {
  const r = entry.capabilities?.lora_role;
  return r === "identity" || r === "style" ? r : null;
}

/** Advisory catalog/install state for one LoRA entry.
 *  Order mirrors the backend gates (`_check_loras`) where they concern
 *  the lora itself; `baseFamily` is the SELECTED base model's family —
 *  pass null when no base is in context (e.g. Model Manager) and family
 *  is shown as an info chip instead of a mismatch verdict. */
export type LoraCatalogState =
  | "planned"              // support="planned" — future, never installable
  | "unverified"           // support="experimental" — real artifact, unintegrated path
  | "license_restricted"   // license.commercial_ok === false — backend refuses
  | "family_mismatch"      // entry.family !== baseFamily
  | "incompatible_format"  // fmt !== "safetensors" (gguf LoRAs stay gated)
  | "unknown_role"         // lora_role missing or not identity/style — backend refuses
  | "installing"           // install row in flight (downloading/verifying)
  | "not_installed"        // cataloged + valid, no completed install
  | "available";           // installed — eligible to bind & select

export function loraCatalogState(
  entry: ModelCatalogEntry,
  baseFamily: string | null,
): LoraCatalogState {
  // Precedence mirrors the backend `_check_loras` eligibility order:
  // support tier → license → family → format → role → install state.
  if (entry.support === "planned") return "planned";
  if (entry.support === "experimental") return "unverified";
  // Backend: spec.license.get("commercial_ok", True) — only an explicit
  // restriction refuses; absent metadata is permitted at the same places
  // the backend permits it, and the license name still renders on the
  // card rather than being silently upgraded.
  if (entry.license?.commercial_ok === false) return "license_restricted";
  if (baseFamily !== null && entry.family !== baseFamily) return "family_mismatch";
  if (entry.format !== "safetensors") return "incompatible_format";
  if (loraRole(entry) === null) return "unknown_role";
  const st = entry.install?.status;
  if (st === "downloading" || st === "verifying") return "installing";
  if (st !== "installed") return "not_installed";
  return "available";
}

export const LORA_STATE_BADGE: Record<LoraCatalogState, { label: string; cls: string }> = {
  planned: { label: "Planned", cls: "bg-line/40 text-muted" },
  unverified: { label: "Unverified", cls: "bg-amber/15 text-amber" },
  license_restricted: { label: "Restricted license", cls: "bg-rose/15 text-rose" },
  family_mismatch: { label: "Different base family", cls: "bg-rose/15 text-rose" },
  incompatible_format: { label: "Unsupported format", cls: "bg-rose/15 text-rose" },
  unknown_role: { label: "Role not supported", cls: "bg-rose/15 text-rose" },
  installing: { label: "Installing…", cls: "bg-sky/15 text-sky" },
  not_installed: { label: "Not installed", cls: "bg-line/40 text-muted" },
  available: { label: "Installed", cls: "bg-teal/15 text-teal" },
};

/** Honesty line for every adapter surface — installed/compatible are
 *  file-level facts, not proof of working LoRA inference. */
export const LORA_RUNTIME_NOTE =
  "Cataloged, installed, and compatible describe the adapter file only — " +
  "real LoRA image generation has not been validated in this build.";
