// G2-B/B-3a — LoRA data-layer tests: lossless lora_json normalization,
// role/family classification, and the advisory state machine. All states
// are display-only — backend `_check_loras` stays authoritative.
import { describe, expect, it } from "vitest";
import type { ModelCatalogEntry } from "../../api/types";
import {
  boundLoraKeys, duplicateLoraKeys, eligibleIdentityDefaults,
  emitLoraRefs, invalidLoraCount, isLoraEntry, loraCatalogState,
  loraKeys, loraRole, normalizeLoraEntries, refsBoundKeys,
} from "./lora";

function entry(over: Partial<ModelCatalogEntry> = {}): ModelCatalogEntry {
  return {
    key: "char.x", display_name: "X", family: "sd1.5", version: "1",
    adapter_id: "gen.image.sdcpp", compatible_adapters: [],
    format: "safetensors", source: {},
    license: { name: "l", url: "", commercial_ok: true, notes: "" },
    files: [], download_bytes: 0, requirements: {},
    capabilities: { lora_role: "identity" },
    runtime_key: "sdcpp", support: "tested", modality: "lora",
    modes: [], notes: "", install: { status: "installed" },
    compatibility: { status: "compatible", reasons: [], warnings: [],
                     suggested: {}, estimate: { vram_bytes: null, basis: "" } },
    ...over,
  };
}

describe("isLoraEntry / loraRole — catalog recognition", () => {
  it("recognizes modality lora and rejects generatable modalities", () => {
    expect(isLoraEntry(entry())).toBe(true);
    for (const m of ["image", "video", "audio", "multimodal"] as const) {
      expect(isLoraEntry(entry({ modality: m }))).toBe(false);
    }
  });

  it("reads the declared adapter role and tolerates missing/invalid", () => {
    expect(loraRole(entry())).toBe("identity");
    expect(loraRole(entry({ capabilities: { lora_role: "style" } }))).toBe("style");
    expect(loraRole(entry({ capabilities: {} }))).toBeNull();
    expect(loraRole(entry({
      capabilities: { lora_role: "bogus" as "identity" },
    }))).toBeNull();
  });
});

describe("loraCatalogState — advisory classification", () => {
  it("planned entries are never installable-looking", () => {
    expect(loraCatalogState(
      entry({ support: "planned", install: { status: "installed" } }),
      "sd1.5")).toBe("planned");
  });

  it("experimental support tier classifies unverified, never validated", () => {
    expect(loraCatalogState(
      entry({ support: "experimental" }), "sd1.5")).toBe("unverified");
  });

  it("a sha-pinned artifact alone does not imply tested support", () => {
    // support tier is a catalog-authored claim; installed+verified file
    // under experimental still reports unverified.
    expect(loraCatalogState(entry({
      support: "experimental",
      install: { status: "installed", verified: true },
    }), "sd1.5")).toBe("unverified");
  });

  it("family mismatch flagged against the selected base", () => {
    expect(loraCatalogState(
      entry({ family: "sdxl" }), "sd1.5")).toBe("family_mismatch");
  });

  it("no base context skips the mismatch verdict", () => {
    expect(loraCatalogState(entry({ family: "sdxl" }), null)).toBe("available");
  });

  it("non-safetensors formats stay gated", () => {
    expect(loraCatalogState(
      entry({ format: "gguf" }), "sd1.5")).toBe("incompatible_format");
  });

  it("explicit restricted license is never available", () => {
    expect(loraCatalogState(entry({
      license: { name: "NC", url: "", commercial_ok: false, notes: "" },
    }), "sd1.5")).toBe("license_restricted");
    expect(loraCatalogState(entry({
      license: { name: "CC0", url: "", commercial_ok: true, notes: "" },
    }), "sd1.5")).toBe("available");
  });

  it("missing license metadata matches the backend's permissive default", () => {
    // Backend: spec.license.get("commercial_ok", True) — absent metadata
    // is permitted where the backend permits it; the license name still
    // displays on the card. Advisory only — never an authorization claim.
    expect(loraCatalogState(entry({
      license: { name: "custom", url: "", commercial_ok: undefined as unknown as boolean, notes: "" },
    }), "sd1.5")).toBe("available");
  });

  it("missing or unsupported roles are never available", () => {
    expect(loraCatalogState(
      entry({ capabilities: {} }), "sd1.5")).toBe("unknown_role");
    expect(loraCatalogState(entry({
      capabilities: { lora_role: "regional" as "identity" },
    }), "sd1.5")).toBe("unknown_role");
    expect(loraCatalogState(entry({
      capabilities: { lora_role: null as unknown as "identity" },
    }), "sd1.5")).toBe("unknown_role");
    expect(loraCatalogState(
      entry({ capabilities: { lora_role: "style" } }), "sd1.5"))
      .toBe("available");
  });

  it("precedence mirrors backend gate order: support → license → family → format → role", () => {
    // experimental + restricted license → unverified (support first)
    expect(loraCatalogState(entry({
      support: "experimental",
      license: { name: "NC", url: "", commercial_ok: false, notes: "" },
    }), "sd1.5")).toBe("unverified");
    // restricted + family mismatch → license first
    expect(loraCatalogState(entry({
      family: "sdxl",
      license: { name: "NC", url: "", commercial_ok: false, notes: "" },
    }), "sd1.5")).toBe("license_restricted");
    // mismatch + bad format → family first
    expect(loraCatalogState(entry({
      family: "sdxl", format: "gguf",
    }), "sd1.5")).toBe("family_mismatch");
    // bad format + missing role → format first
    expect(loraCatalogState(entry({
      format: "gguf", capabilities: {},
    }), "sd1.5")).toBe("incompatible_format");
    // missing role + not installed → role first
    expect(loraCatalogState(entry({
      capabilities: {}, install: { status: "not_installed" },
    }), "sd1.5")).toBe("unknown_role");
  });

  it("in-flight and absent installs report honestly", () => {
    for (const status of ["downloading", "verifying"]) {
      expect(loraCatalogState(
        entry({ install: { status } }), "sd1.5")).toBe("installing");
    }
    expect(loraCatalogState(
      entry({ install: { status: "not_installed" } }), "sd1.5"))
      .toBe("not_installed");
    expect(loraCatalogState(entry({ install: null }), "sd1.5"))
      .toBe("not_installed");
  });

  it("installed + sd1.5 + safetensors + supported tier is available", () => {
    expect(loraCatalogState(entry(), "sd1.5")).toBe("available");
    expect(loraCatalogState(
      entry({ support: "production" }), "sd1.5")).toBe("available");
  });
});

describe("normalizeLoraEntries — lossless stored-data view", () => {
  it("null and empty normalize to an empty list", () => {
    expect(normalizeLoraEntries(null)).toEqual([]);
    expect(normalizeLoraEntries(undefined)).toEqual([]);
    expect(normalizeLoraEntries([])).toEqual([]);
  });

  it("legacy string refs keep string kind and raw value", () => {
    const refs = normalizeLoraEntries(["old-name", "  padded  "]);
    expect(refs).toEqual([
      { kind: "key", key: "old-name", raw: "old-name" },
      { kind: "key", key: "padded", raw: "  padded  " },
    ]);
  });

  it("structured entries keep dict shape and detail", () => {
    const raw = { key: "char.a", strength: 0.8, extra: "kept-in-raw" };
    const refs = normalizeLoraEntries([raw]);
    expect(refs).toEqual([
      { kind: "structured", key: "char.a", strength: 0.8, raw },
    ]);
  });

  it("mixed lists preserve order and per-entry shape", () => {
    const refs = normalizeLoraEntries(
      ["legacy", { key: "new" }, "other-legacy"]);
    expect(refs.map((r) => r.kind)).toEqual(["key", "structured", "key"]);
    expect(refs.map((r) => (r.kind !== "invalid" ? r.key : null)))
      .toEqual(["legacy", "new", "other-legacy"]);
  });

  it("duplicates are preserved, not deduplicated", () => {
    const refs = normalizeLoraEntries(["dup", { key: "dup" }, "dup"]);
    expect(refs).toHaveLength(3);
    expect(duplicateLoraKeys(["dup", { key: "dup" }, "solo"]))
      .toEqual(new Set(["dup"]));
  });

  it("malformed values become visible invalid refs, never dropped", () => {
    const refs = normalizeLoraEntries(
      [42, { nokey: 1 }, { key: "" }, { key: "x", strength: "bad" }, ""]);
    expect(refs.map((r) => r.kind)).toEqual(
      ["invalid", "invalid", "invalid", "invalid", "invalid"]);
    expect(refs).toHaveLength(5);
  });

  it("non-list stored data surfaces as one invalid ref", () => {
    expect(normalizeLoraEntries("oops"))
      .toEqual([{ kind: "invalid", raw: "oops" }]);
  });

  it("structured entries with non-finite or absent strength read safely", () => {
    expect(normalizeLoraEntries([{ key: "k", strength: null }])[0])
      .toMatchObject({ kind: "structured", strength: null });
    expect(normalizeLoraEntries([{ key: "k" }])[0])
      .toMatchObject({ kind: "structured", strength: null });
    expect(normalizeLoraEntries([{ key: "k", strength: NaN }])[0].kind)
      .toBe("invalid");
    expect(normalizeLoraEntries([{ key: "k", strength: Infinity }])[0].kind)
      .toBe("invalid");
  });
});

describe("loraKeys / boundLoraKeys — key extraction", () => {
  it("extracts keys from both stored shapes, keeping order + dupes", () => {
    expect(loraKeys(["a", { key: "b" }, "a"])).toEqual(["a", "b", "a"]);
  });

  it("unknown catalog keys extract like any other ref — flagging is the classifier's job", () => {
    expect(loraKeys(["no-such-adapter"])).toEqual(["no-such-adapter"]);
  });

  it("bound membership view deduplicates for checks only", () => {
    expect(boundLoraKeys(["a", { key: "b" }, "a"]))
      .toEqual(new Set(["a", "b"]));
  });

  it("invalid entries contribute no keys", () => {
    expect(loraKeys([42, { key: "ok" }, "fine"])).toEqual(["ok", "fine"]);
  });
});

describe("emitLoraRefs / refsBoundKeys / invalidLoraCount — payload emit", () => {
  it("emits losslessly: strings stay strings, dicts keep strength", () => {
    const refs = normalizeLoraEntries(
      ["legacy", { key: "a", strength: 0.5 }, { key: "b" }]);
    expect(emitLoraRefs(refs)).toEqual(
      ["legacy", { key: "a", strength: 0.5 }, { key: "b" }]);
  });

  it("invalid refs are excluded from emit but counted for warnings", () => {
    const refs = normalizeLoraEntries([42, "ok"]);
    expect(emitLoraRefs(refs)).toEqual(["ok"]);
    expect(invalidLoraCount(refs)).toBe(1);
  });

  it("refsBoundKeys is the membership view over normalized refs", () => {
    const refs = normalizeLoraEntries(["a", { key: "b" }, 42]);
    expect(refsBoundKeys(refs)).toEqual(new Set(["a", "b"]));
  });
});

describe("eligibleIdentityDefaults — character default prefill", () => {
  it("offers only installed, identity-role catalog matches", () => {
    const models = [
      entry({ key: "char.a", capabilities: { lora_role: "identity" } }),
      entry({ key: "style.s", capabilities: { lora_role: "style" } }),
      entry({ key: "char.off", install: { status: "not_installed" } }),
    ];
    const out = eligibleIdentityDefaults(
      ["char.a", { key: "style.s" }, "char.off", "no-cat", 42], models);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ kind: "structured", key: "char.a" });
  });

  it("stored strength carries into the prefill offer", () => {
    const models = [entry({ key: "char.a" })];
    expect(eligibleIdentityDefaults(
      [{ key: "char.a", strength: 0.7 }], models)[0])
      .toMatchObject({ strength: 0.7 });
    // legacy string defaults offer with default (null) strength
    expect(eligibleIdentityDefaults(["char.a"], models)[0])
      .toMatchObject({ strength: null });
  });
});

describe("B-3e-1 curated catalog entries — advisory classification", () => {
  // Mirror of the backend ModelSpec shape for the two curated entries.
  // trigger_tokens ride along in capabilities — advisory only, the
  // backend resolver never reads them.
  it("identity entry classifies unverified with its role readable", () => {
    const e = entry({
      key: "lora.id.babylittlelo", support: "experimental",
      capabilities: {
        lora_role: "identity",
        trigger_tokens: ["babylittlelo \(person\)", "@bblittlelo",
                         "bblittlelo \(person\)"],
      } as ModelCatalogEntry["capabilities"],
    });
    expect(isLoraEntry(e)).toBe(true);
    expect(loraRole(e)).toBe("identity");
    expect(loraCatalogState(e, "sd1.5")).toBe("unverified");
    // even after a future install row exists, tier stays honest
    expect(loraCatalogState({ ...e, install: { status: "installed" } },
                            "sd1.5")).toBe("unverified");
  });

  it("style entry classifies unverified with its role readable", () => {
    const e = entry({
      key: "lora.style.pixelart", support: "experimental",
      capabilities: {
        lora_role: "style",
        trigger_tokens: ["pixel art"],
      } as ModelCatalogEntry["capabilities"],
    });
    expect(isLoraEntry(e)).toBe(true);
    expect(loraRole(e)).toBe("style");
    expect(loraCatalogState(e, "sd1.5")).toBe("unverified");
  });
});
