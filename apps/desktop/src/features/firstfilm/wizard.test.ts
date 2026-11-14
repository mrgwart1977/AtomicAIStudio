// C5-D — wizard state: persistence round-trip, stale-entity reconcile,
// step gating, capability classification, and error translation.
import { describe, expect, it } from "vitest";
import {
  friendlyError, initialState, loadState, methodOptions,
  reconcileState, stepReady,
} from "./wizard";
import type { ModelCatalogEntry, ProviderDetail, ProviderSummary } from "../../api/types";

const mk = (over: object) => ({ ...initialState(), ...over }) as ReturnType<typeof initialState>;

describe("loadState", () => {
  it("round-trips a persisted state", () => {
    const s = initialState("Demo");
    s.step = 3; s.furthest = 4; s.scene_id = "sc1";
    const loaded = loadState({ first_film: s });
    expect(loaded?.step).toBe(3);
    expect(loaded?.scene_id).toBe("sc1");
    expect(loaded?.film.name).toBe("Demo");
  });
  it("returns null when absent or malformed", () => {
    expect(loadState(undefined)).toBeNull();
    expect(loadState({})).toBeNull();
    expect(loadState({ first_film: "nope" })).toBeNull();
    expect(loadState({ first_film: { version: 2, step: 0 } })).toBeNull();
  });
});

describe("reconcileState", () => {
  const known = {
    characterIds: new Set(["c1"]),
    sceneIds: new Set(["sc1"]),
    shotIds: new Set(["sh1"]),
    timelineIds: new Set(["t1"]),
    assetIds: new Set(["a1"]),
  };
  it("keeps live entities untouched", () => {
    const s = mk({ character_ids: ["c1"], scene_id: "sc1", shot_ids: ["sh1"], timeline_id: "t1", shot_assets: { sh1: "a1" } });
    const { state, notices } = reconcileState(s, known);
    expect(notices).toEqual([]);
    expect(state.scene_id).toBe("sc1");
    expect(state.timeline_id).toBe("t1");
  });
  it("drops deleted entities with a notice", () => {
    const s = mk({
      character_ids: ["c1", "gone"], scene_id: "gone-scene",
      shot_ids: ["sh1", "gone-shot"], timeline_id: "gone-tl",
      shot_assets: { sh1: "gone-asset" },
    });
    const { state, notices } = reconcileState(s, known);
    expect(state.character_ids).toEqual(["c1"]);
    expect(state.scene_id).toBeNull();
    expect(state.shot_ids).toEqual(["sh1"]);
    expect(state.timeline_id).toBeNull();
    expect(state.shot_assets).toEqual({});
    expect(notices.length).toBe(5);
  });
});

describe("stepReady", () => {
  it("gates required steps", () => {
    const s = initialState();
    expect(stepReady(s, "setup")).toBe(true);          // name prefilled
    expect(stepReady(s, "scene")).toBe(false);
    expect(stepReady(s, "shots")).toBe(false);
    expect(stepReady(s, "method")).toBe(false);
    expect(stepReady(s, "timeline")).toBe(false);
    const s2 = mk({ scene_id: "sc1", shot_ids: ["sh1"], method: "import", timeline_id: "t1" });
    expect(stepReady(s2, "scene")).toBe(true);
    expect(stepReady(s2, "shots")).toBe(true);
    expect(stepReady(s2, "method")).toBe(true);
    expect(stepReady(s2, "timeline")).toBe(true);
  });
  it("media step needs every shot covered", () => {
    const s = mk({ shot_ids: ["sh1", "sh2"], shot_assets: { sh1: "a1" } });
    expect(stepReady(s, "media")).toBe(false);
    s.shot_assets.sh2 = "a2";
    expect(stepReady(s, "media")).toBe(true);
  });
  it("generate step is satisfied for import, else needs assets", () => {
    const s = mk({ method: "import", shot_ids: ["sh1"] });
    expect(stepReady(s, "generate")).toBe(true);
    const s2 = mk({ method: "image", shot_ids: ["sh1"] });
    expect(stepReady(s2, "generate")).toBe(false);
    s2.shot_assets = { sh1: "a1" };
    expect(stepReady(s2, "generate")).toBe(true);
  });
});

describe("methodOptions", () => {
  const prov = { key: "pk", label: "Provider", category: "image" } as ProviderSummary;
  const detail = { models: [{ id: "m1", capability: "image" }] } as ProviderDetail;
  const video = (over: object) => ({
    key: "v1", display_name: "VidGen", modality: "video", support: "tested",
    compatibility: { status: "compatible" }, install: null, download_bytes: 2e9,
    ...over,
  } as unknown as ModelCatalogEntry);
  it("always offers import; image models are labelled simulated", () => {
    const opts = methodOptions([prov], { pk: detail }, []);
    expect(opts[0].id).toBe("import");
    expect(opts[0].status).toBe("ready");
    expect(opts[1].status).toBe("simulated");
    expect(opts[1].detail).toBe("pk|m1");
  });
  it("classifies video models by install + support", () => {
    const opts = methodOptions([], {}, [
      video({ key: "inst", install: { status: "installed" } }),
      video({ key: "plan", support: "planned" }),
      video({ key: "not" }),
    ]);
    const by = Object.fromEntries(opts.slice(1).map((o) => [o.detail, o.status]));
    expect(by.inst).toBe("ready");
    expect(by.plan).toBe("planned");
    expect(by.not).toBe("uninstalled");
  });
});

describe("friendlyError", () => {
  it("maps technical errors to plain language", () => {
    expect(friendlyError(new Error("model not installed")).title).toMatch(/isn't downloaded/);
    expect(friendlyError(new Error("insufficient VRAM")).title).toMatch(/graphics card/);
    expect(friendlyError(new Error("not enough disk space")).title).toMatch(/disk space/);
    expect(friendlyError(new Error("prompt context changed since preview")).title).toMatch(/re-check the preview/);
    expect(friendlyError(new Error("job cancelled")).title).toMatch(/cancelled/);
    expect(friendlyError(new Error("not a valid image payload")).title).toMatch(/couldn't be read as media/);
    // technical detail is preserved verbatim
    expect(friendlyError(new Error("x weird")).detail).toBe("x weird");
  });
});
