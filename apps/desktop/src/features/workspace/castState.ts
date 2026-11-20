// Shot-cast editable state — mirrors shot_characters columns. LoRA
// bindings are held as lossless normalized refs and emitted back to the
// backend `_lora_list` contract on save.
import type { CastMember } from "../../api/types";
import { emitLoraRefs, normalizeLoraEntries, type LoraRef } from "./lora";

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

export const castPayload = (s: CastState) => ({
  schema_version: 1,
  wardrobe_variant: s.wardrobe_variant, blocking: s.blocking,
  expression: s.expression, emotion: s.emotion,
  action_instructions: s.action_instructions, dialogue: s.dialogue,
  voice_override: s.voice_override, model_ref: s.model_ref,
  loras: emitLoraRefs(s.lora_refs),
});
