// B-3b — shot-cast state: lossless lora_json → refs → payload round-trips.
import { describe, expect, it } from "vitest";
import { castPayload, castStateOf } from "./castState";

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

  it("malformed stored entries stay visible in state but are not emitted", () => {
    const s = castStateOf({ lora_json: [42 as never, { nokey: 1 } as never, "ok"] });
    expect(s.lora_refs.map((r) => r.kind)).toEqual(
      ["invalid", "invalid", "key"]);
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
});
