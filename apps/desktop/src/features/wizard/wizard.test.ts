// A8 — wizard state model: persistence round-trip, legacy upgrade,
// stale-id reconcile, honesty labels, error translation, and the
// supported paste-import format.
import { describe, expect, it } from "vitest";
import {
  STAGES, STAGE_INDEX, friendlyError, initialState, loadState,
  parseScriptText, reconcileState,
} from "./wizard";

describe("stages", () => {
  it("has 15 stages in spec order", () => {
    expect(STAGES).toHaveLength(15);
    expect(STAGES[0].id).toBe("project");
    expect(STAGES[14].id).toBe("export");
  });
  it("every stage id has an index", () => {
    for (const s of STAGES) {
      expect(STAGE_INDEX[s.id]).toBe(STAGES.indexOf(s));
    }
  });
});

describe("loadState", () => {
  it("round-trips a persisted v2 state", () => {
    const s = initialState();
    s.step = 5; s.furthest = 7; s.mode = "advanced";
    s.screenplay_id = "sp1"; s.timeline_id = "t1";
    const { state, legacy } = loadState({ wizard: s });
    expect(legacy).toBe(false);
    expect(state.step).toBe(5);
    expect(state.mode).toBe("advanced");
    expect(state.screenplay_id).toBe("sp1");
  });
  it("falls back to a fresh state when absent or malformed", () => {
    expect(loadState(undefined).state.step).toBe(0);
    expect(loadState({}).state.step).toBe(0);
    expect(loadState({ wizard: "nope" }).state.step).toBe(0);
    expect(loadState({ wizard: { version: 1, step: 9 } }).state.step).toBe(0);
    expect(loadState({ wizard: { version: 3, step: 9 } }).state.step).toBe(0);
  });
  it("seeds from a legacy first_film blob without consuming it", () => {
    const legacy = { version: 1, step: 5, scene_id: "sc1" };
    const { state, legacy: isLegacy } = loadState({ first_film: legacy });
    expect(isLegacy).toBe(true);
    // old step 5 (generate media) maps onto the motion stage region
    expect(state.step).toBe(STAGE_INDEX.motion);
    expect(state.furthest).toBe(state.step);
    // nothing carried over that could masquerade as live data
    expect(state.screenplay_id).toBeNull();
  });
  it("prefers a live v2 blob over a stale legacy blob", () => {
    const s = initialState(); s.step = 9;
    const { state, legacy } = loadState(
      { wizard: s, first_film: { version: 1, step: 0 } });
    expect(legacy).toBe(false);
    expect(state.step).toBe(9);
  });
});

describe("reconcileState", () => {
  const known = {
    screenplayIds: new Set(["sp1"]),
    timelineIds: new Set(["t1"]),
    planIds: new Set(["pl1"]),
    jobIds: new Set(["j1"]),
  };
  it("keeps live ids untouched", () => {
    const s = { ...initialState(), screenplay_id: "sp1", timeline_id: "t1", plan_id: "pl1", export_job_id: "j1" };
    const { state, notices } = reconcileState(s, known);
    expect(notices).toEqual([]);
    expect(state.screenplay_id).toBe("sp1");
    expect(state.timeline_id).toBe("t1");
    expect(state.plan_id).toBe("pl1");
    expect(state.export_job_id).toBe("j1");
  });
  it("drops deleted ids with notices", () => {
    const s = { ...initialState(), screenplay_id: "gone", timeline_id: "gone", plan_id: "gone" };
    const { state, notices } = reconcileState(s, known);
    expect(state.screenplay_id).toBeNull();
    expect(state.timeline_id).toBeNull();
    expect(state.plan_id).toBeNull();
    expect(notices.length).toBe(3);
  });
  it("drops a missing export job silently (diagnostic only)", () => {
    const s = { ...initialState(), export_job_id: "gone" };
    const { state, notices } = reconcileState(s, known);
    expect(state.export_job_id).toBeNull();
    expect(notices.length).toBe(0);
  });
});

describe("friendlyError", () => {
  it("maps technical errors to plain language and keeps the detail", () => {
    expect(friendlyError(new Error("model not installed")).title).toMatch(/isn't downloaded/);
    expect(friendlyError(new Error("insufficient VRAM")).title).toMatch(/graphics card/);
    expect(friendlyError(new Error("revision conflict")).title).toMatch(/re-check the preview/);
    expect(friendlyError(new Error("job cancelled")).title).toMatch(/cancelled/);
    expect(friendlyError(new Error("over_budget")).title).toMatch(/spending limit/);
    expect(friendlyError(new Error("needs_key for provider")).title).toMatch(/account key/);
    expect(friendlyError(new Error("x weird")).detail).toBe("x weird");
  });
});

describe("parseScriptText", () => {
  it("parses headings, action, dialogue, and notes", () => {
    const scenes = parseScriptText(
      "INT. DOCK — NIGHT\nThe crane creaks.\nANNA: The tide is coming in.\n" +
      "[beat]\nEXT. ROAD — DAY\nA car pulls up.\nBOB (shouting): Wait up!");
    expect(scenes).toHaveLength(2);
    expect(scenes[0].heading).toBe("INT. DOCK — NIGHT");
    expect(scenes[0].elements.map((e) => e.kind)).toEqual(
      ["action", "dialogue", "note"]);
    expect(scenes[0].elements[1].character).toBe("ANNA");
    expect(scenes[1].elements[1].character).toBe("BOB");
    expect(scenes[1].elements[1].text).toContain("shouting");
  });
  it("collects unheaded content into a first scene", () => {
    const scenes = parseScriptText("Some opening action.\nA line with no heading.");
    expect(scenes).toHaveLength(1);
    expect(scenes[0].elements).toHaveLength(2);
    expect(scenes[0].elements[0].kind).toBe("action");
  });
  it("treats unknown lines as action — nothing dropped", () => {
    const scenes = parseScriptText("# A heading\nrandom words here");
    expect(scenes[0].elements[0].kind).toBe("action");
    expect(scenes[0].elements[0].text).toBe("random words here");
  });
});
