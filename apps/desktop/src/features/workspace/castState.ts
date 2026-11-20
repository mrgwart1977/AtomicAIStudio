// Shot-cast editable state — mirrors shot_characters columns. LoRA
// bindings are held as lossless normalized refs; the save path decides
// per row whether loras are emitted, omitted (stored value preserved)
// or blocked (intentional edit with malformed entries remaining).
import type { CastMember } from "../../api/types";
import {
  emitLoraRefs, invalidLoraCount, normalizeLoraEntries, type LoraRef,
} from "./lora";

export interface CastState {
  wardrobe_variant: string;
  blocking: string;
  expression: string;
  emotion: string;
  action_instructions: string;
  dialogue: string;
  voice_override: string;
  model_ref: string;
  lora_refs: LoraRef[];
}

export const castStateOf = (c?: Partial<CastMember>): CastState => ({
  wardrobe_variant: c?.wardrobe_variant ?? "",
  blocking: c?.blocking ?? "",
  expression: c?.expression ?? "",
  emotion: c?.emotion ?? "",
  action_instructions: c?.action_instructions ?? "",
  dialogue: c?.dialogue ?? "",
  voice_override: c?.voice_override ?? "",
  model_ref: c?.model_ref ?? "",
  lora_refs: normalizeLoraEntries(c?.lora_json),
});

/** LoRA save decision for a PARTIAL update (`shot.cast.update`,
 *  `character.update` data merge) — the three honest outcomes:
 *    omit  — refs unchanged → keep `loras` out of the payload entirely;
 *            the backend merge preserves the stored lora_json verbatim,
 *            including malformed historical entries.
 *    block — refs changed AND invalid entries remain → refuse the save;
 *            the user must explicitly remove the invalid entries.
 *    emit  — deliberate lora change with a clean list → emit the
 *            backend `_lora_list` shape (strings stay strings; a fully
 *            cleared list emits [] = explicit unbind). */
export type LoraUpdatePlan =
  | { kind: "omit" }
  | { kind: "block"; invalid: number }
  | { kind: "emit"; loras: (string | { key: string; strength?: number })[] };

export function loraUpdatePlan(next: LoraRef[], orig: LoraRef[]): LoraUpdatePlan {
  if (JSON.stringify(next) === JSON.stringify(orig)) return { kind: "omit" };
  const invalid = invalidLoraCount(next);
  if (invalid > 0) return { kind: "block", invalid };
  return { kind: "emit", loras: emitLoraRefs(next) };
}

export const castPayload = (s: CastState, opts?: { includeLoras?: boolean }) => {
  const p: Record<string, unknown> = {
    schema_version: 1,
    wardrobe_variant: s.wardrobe_variant, blocking: s.blocking,
    expression: s.expression, emotion: s.emotion,
    action_instructions: s.action_instructions, dialogue: s.dialogue,
    voice_override: s.voice_override, model_ref: s.model_ref,
  };
  if (opts?.includeLoras !== false) p.loras = emitLoraRefs(s.lora_refs);
  return p;
};
