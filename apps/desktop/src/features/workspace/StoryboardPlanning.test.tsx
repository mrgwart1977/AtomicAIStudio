import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render } from "@testing-library/react";

afterEach(cleanup);
import {
  draftFromPlanning, planningFromDraft,
  PlanningSection, ShotMediaSection,
} from "./StoryboardPlanning";
import type { Entity, ShotPlanning } from "../../api/types";

const CHARS: Entity[] = [
  { id: "c1", name: "Anna", status: "active" } as Entity,
];

describe("planning draft round-trip", () => {
  it("serializes to nothing when unchanged", () => {
    const p: ShotPlanning = {
      camera: { angle: "low", follow_mode: "orbit_follow",
                follow_params: { distance_m: 2 } },
      framing: "wide", notes: "n",
    };
    const d = draftFromPlanning(p);
    expect(planningFromDraft(d, draftFromPlanning(p))).toEqual({});
  });

  it("emits changed fields only, with null clears", () => {
    const orig = draftFromPlanning({
      camera: { angle: "low", movement: "pan" },
      framing: "wide", notes: "keep", lighting: "old",
    });
    const d = { ...orig, angle: "high", framing: "", lighting: "new" };
    expect(planningFromDraft(d, orig)).toEqual({
      camera: { angle: "high" },
      framing: null,          // cleared → explicit null, not omission
      lighting: "new",
    });
  });

  it("round-trips fps and follow targets", () => {
    const d = draftFromPlanning(null);
    const edited = {
      ...d, fpsNum: "24000", fpsDen: "1001", fpsFrames: "96",
      followTargets: [{ kind: "character" as const, id: "c1" }],
    };
    expect(planningFromDraft(edited, d)).toEqual({
      camera: { follow_targets: [{ kind: "character", id: "c1" }] },
      fps: { num: 24000, den: 1001, frames: 96 },
    });
  });

  it("clears fps entirely when fields emptied", () => {
    const orig = draftFromPlanning({ fps: { num: 24, den: 1 } });
    const d = { ...orig, fpsNum: "", fpsDen: "", fpsFrames: "" };
    expect(planningFromDraft(d, orig)).toEqual({ fps: null });
  });
});

describe("PlanningSection", () => {
  it("edits call onChange with merged draft", () => {
    const onChange = vi.fn();
    const d = draftFromPlanning(null);
    const { getByLabelText } = render(
      <PlanningSection value={d} onChange={onChange}
        characters={CHARS} locations={[]} assets={[]} refSets={[]} />);
    fireEvent.change(getByLabelText("Camera angle"), { target: { value: "low" } });
    expect(onChange).toHaveBeenCalledWith({ ...d, angle: "low" });
    fireEvent.change(getByLabelText("Follow / tracking"),
      { target: { value: "drone_follow" } });
    expect(onChange).toHaveBeenCalledWith({ ...d, followMode: "drone_follow" });
  });
});

describe("ShotMediaSection", () => {
  const entry = {
    shot: { id: "sh1" },
    media: {
      assets: [{
        link_id: "lnk1", asset_id: "a1", role: "reference",
        label: "board", asset_name: "ref.png", asset_status: "missing",
        missing: true,
      }],
      missing: ["lnk1"],
    },
  };

  it("shows missing-media flag and unlinks via command", async () => {
    const run = vi.fn().mockResolvedValue(true);
    const { getByText, getByLabelText } = render(
      <ShotMediaSection entry={entry} imageAssets={[]} run={run} />);
    expect(getByText(/relink or remove/)).toBeTruthy();
    fireEvent.click(getByLabelText("Remove ref.png"));
    await vi.waitFor(() => expect(run).toHaveBeenCalledWith(
      "entity.assets.unlink", { link_id: "lnk1" }));
  });

  it("links a picked asset as a shot entity", async () => {
    const run = vi.fn().mockResolvedValue(true);
    const { getByRole, getByText } = render(
      <ShotMediaSection entry={{ shot: { id: "sh1" }, media: { assets: [], missing: [] } }}
        imageAssets={[{ id: "a9", name: "board.png" }]} run={run} />);
    fireEvent.change(getByRole("combobox", { name: "Pick media asset" }),
      { target: { value: "a9" } });
    fireEvent.click(getByText("Pin"));
    await vi.waitFor(() => expect(run).toHaveBeenCalledWith(
      "entity.assets.link",
      { entity_type: "shot", entity_id: "sh1",
        asset_id: "a9", role: "reference", label: "" }));
  });
});
