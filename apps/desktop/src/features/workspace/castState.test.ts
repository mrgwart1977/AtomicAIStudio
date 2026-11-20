// B-3b — shot-cast state: lossless lora_json → refs → payload round-trips,
// and the three-way save plan (omit / block / emit) that keeps malformed
// historical entries preserved on unrelated edits.
import { describe, expect, it } from "vitest";
import {
  castPayload, castStateOf, loraUpdatePlan,
} from "./castState";
import { normalizeLoraEntries } from "./lora";

describe("castStateOf / castPayload — lora_json round-trip", () => {
  it("legacy string refs emit as strings, order preserved", () => {
    const s = castStateOf({ lora_json: ["old-one", "other"] });
    expect(castPayload(s).loras).toEqual(["old-one", "other"]);
  });

  it("structured entries emit as {key, strength?} dicts", () => {
    const s = castStateOf({ lora_json: [
      { key: "a", strength: 0.8 }, { key: "b" },
    ] });
    expect(castPayload(s).loras).toEqual([
      { key: "a", strength: 0.8 }, { key: "b" },
    ]);
  });

  it("mixed + unknown entries round-trip losslessly", () => {
    const s = castStateOf({ lora_json: [
      "unknown-legacy", { key: "char.x" }, "another",
    ] });
    expect(castPayload(s).loras).toEqual([
      "unknown-legacy", { key: "char.x" }, "another",
    ]);
  });

  it("duplicates and unknown catalog keys are preserved, not deduplicated", () => {
    const s = castStateOf({ lora_json: ["dup", { key: "dup" }] });
    expect(castPayload(s).loras).toEqual(["dup", { key: "dup" }]);
  });

  it("malformed stored entries stay visible in state; emit is clean but preservation is the plan's job", () => {
    const s = castStateOf({ lora_json: [42 as never, { nokey: 1 } as never, "ok"] });
    expect(s.lora_refs.map((r) => r.kind)).toEqual(
      ["invalid", "invalid", "key"]);
    // emit is still clean — invalids never reach the payload when a
    // deliberate lora change IS made (block rule lives in loraUpdatePlan).
    expect(castPayload(s).loras).toEqual(["ok"]);
  });

  it("empty and absent lora_json emit an explicit empty list (unbind)", () => {
    expect(castPayload(castStateOf({})).loras).toEqual([]);
    expect(castPayload(castStateOf({ lora_json: [] })).loras).toEqual([]);
  });

  it("non-lora fields pass through untouched", () => {
    const s = castStateOf({
      expression: "smile", emotion: "calm",
      lora_json: [{ key: "a" }],
    });
    const p = castPayload(s);
    expect(p.expression).toBe("smile");
    expect(p.emotion).toBe("calm");
    expect(p.schema_version).toBe(1);
  });

  it("includeLoras:false omits the key entirely (partial-update preserve)", () => {
    const p = castPayload(castStateOf({ lora_json: ["x"] }),
      { includeLoras: false });
    expect("loras" in p).toBe(false);
    expect(p.expression).toBeDefined();
  });
});

describe("loraUpdatePlan — omit / block / emit decisions", () => {
  const origWithInvalid = normalizeLoraEntries([42, "ok"]);

  // A + B + E — unrelated edits (dialogue/wardrobe/anything else) never
  // touch lora_refs, so the plan omits the key and the stored bytes
  // (including malformed entries) survive the partial update verbatim.
  it("unchanged refs → omit — stored lora_json preserved byte-for-byte", () => {
    const next = castStateOf({ lora_json: [42, "ok"] }).lora_refs;
    // identical normalize output = untouched
    expect(loraUpdatePlan(next, origWithInvalid)).toEqual({ kind: "omit" });
    expect(loraUpdatePlan([], [])).toEqual({ kind: "omit" });
  });

  // C — the user removed ONE invalid entry; the other remains → block,
  // the remaining entry is never silently dropped.
  it("touched refs with invalids remaining → block", () => {
    const next = normalizeLoraEntries([{ nokey: 1 }, "ok"]);
    expect(loraUpdatePlan(next, origWithInvalid)).toEqual({
      kind: "block", invalid: 1 });
  });

  // F — removing a valid ref while an invalid remains → still block.
  it("removing a valid ref with invalids remaining → block", () => {
    const next = normalizeLoraEntries([42]);
    expect(loraUpdatePlan(next, origWithInvalid)).toEqual({
      kind: "block", invalid: 1 });
  });

  // G — the user removed ALL invalid entries intentionally → clean emit.
  it("all invalids intentionally removed → emit the remaining list", () => {
    const next = normalizeLoraEntries(["ok", { key: "a", strength: 0.5 }]);
    expect(loraUpdatePlan(next, origWithInvalid)).toEqual({
      kind: "emit", loras: ["ok", { key: "a", strength: 0.5 }] });
  });

  // H — explicit unbind: the user unchecked everything → emit [].
  it("clearing every ref → emit [] (explicit unbind)", () => {
    expect(loraUpdatePlan([], origWithInvalid)).toEqual({
      kind: "emit", loras: [] });
  });

  // K — legacy strings kept untouched: no silent conversion to dicts.
  it("untouched legacy strings → omit (no conversion)", () => {
    const orig = normalizeLoraEntries(["csv-old"]);
    expect(loraUpdatePlan(
      normalizeLoraEntries(["csv-old"]), orig)).toEqual({ kind: "omit" });
  });
});
