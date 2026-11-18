// A6 — first-cut assembly panel: plan preview → approve → commit,
// apply preview with per-item dispositions + stale refusal, review
// votes and notes, history/revert. All mutations via commands.
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { AssemblyPanel } from "./AssemblyPanel";
import type { AssemblyPlan } from "../../api/types";

const apiFetch = vi.fn();
const commitCommand = vi.fn();
const previewCommand = vi.fn();
vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));
vi.mock("../../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
  previewCommand: (...a: unknown[]) => previewCommand(...a),
}));
vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel({ jobs: {} }),
}));
import { useApp } from "../../stores/appStore";
(useApp as unknown as { getState: () => unknown }).getState = () => ({
  currentProject: () => ({ id: "p1", revision: 7 }),
});

const PLAN: AssemblyPlan = {
  id: "pl1", project_id: "p1", timeline_id: "tl1", name: "First cut",
  status: "draft", revision: 2,
  created_at: "2026-01-01", updated_at: "2026-01-01",
  sources_stale: false,
  items: [
    { id: "i1", key: "shot:s1", kind: "shot", position: 0,
      scene_id: "sc1", shot_id: "s1", dialogue_id: null,
      motion_clip_id: "mc1", asset_id: "a1", track_id: "t1",
      timeline_clip_id: null, on_timeline: false,
      start_frames: 0, extent_frames: 48,
      review_status: "pending", notes: "",
      flags: { missing: null, conflicts: [] },
      stale: false, source_gone: false },
    { id: "i2", key: "dialogue:d1", kind: "dialogue", position: 1,
      scene_id: "sc1", shot_id: "s1", dialogue_id: "d1",
      motion_clip_id: null, asset_id: null, track_id: null,
      timeline_clip_id: null, on_timeline: false,
      start_frames: 0, extent_frames: 55,
      review_status: "pending", notes: "",
      flags: { missing: "no audio result — generate dialogue first",
               conflicts: [] },
      stale: false, source_gone: false },
  ],
  counts: { items: 2, missing: 1, conflicts: 0, stale: 0 },
};

function route(url: string) {
  if (url.includes("/assemblies/pl1")) return PLAN;
  if (url.includes("/assemblies?")) return [PLAN];
  if (url.includes("/assemblies")) return [PLAN];
  if (url.includes("/versions")) return [];
  throw new Error(`unmocked fetch ${url}`);
}

beforeEach(() => {
  cleanup();
  apiFetch.mockReset(); commitCommand.mockReset();
  previewCommand.mockReset();
  apiFetch.mockImplementation((u: string) => Promise.resolve(route(u)));
  commitCommand.mockResolvedValue(
    { command_id: "c1", job_id: null, revision: 8 });
});
afterEach(cleanup);

describe("AssemblyPanel", () => {
  it("lists plan items with missing-media and review badges", async () => {
    render(<AssemblyPanel projectId="p1"
      timeline={{ id: "tl1", name: "Cut 1" }} />);
    expect(await screen.findByText("First cut")).toBeTruthy();
    fireEvent.click(screen.getByText("Details"));
    expect(await screen.findByText(
      /no audio result — generate dialogue first/)).toBeTruthy();
    expect(screen.getAllByText("pending").length).toBe(2);
  });

  it("previews the deterministic proposal then commits assembly.plan",
    async () => {
      previewCommand.mockResolvedValueOnce({
        summary: "s", warnings: [],
        resolved_assembly: {
          timeline_id: "tl1",
          lanes: {
            video: { track_id: "t1", track_name: "V1", kind: "video",
                     create: null },
            dialogue: { track_id: null, track_name: null, kind: "audio",
                        create: { kind: "audio", name: "DX" } },
          },
          items: [
            { key: "shot:s1", kind: "shot", title: "Shot 1",
              scene_id: "sc1", shot_id: "s1", asset_id: "a1",
              lane: "video", lane_key: "video", track_id: "t1",
              start_frames: 0, extent_frames: 48,
              flags: { missing: null, conflicts: [] } },
            { key: "dialogue:d1", kind: "dialogue", title: "Line",
              scene_id: "sc1", shot_id: "s1", asset_id: null,
              lane: "dialogue", lane_key: "dialogue", track_id: null,
              start_frames: 0, extent_frames: 55,
              flags: { missing: "no audio result", conflicts: [] } },
          ],
          removed: [], warnings: ["dialogue item 'Line': no audio"],
          program_frames: 96,
          counts: { items: 2, missing: 1, conflicts: 0, removed: 0 },
        },
      });
      render(<AssemblyPanel projectId="p1"
        timeline={{ id: "tl1", name: "Cut 1" }} />);
      await screen.findByText("First cut");
      fireEvent.click(screen.getByText("Re-plan"));
      expect(await screen.findByText(
        /Proposed first cut/)).toBeTruthy();
      expect(screen.getByText(/1 missing media/)).toBeTruthy();
      expect(screen.getByText("DX")).toBeTruthy();
      expect(commitCommand).not.toHaveBeenCalled();
      fireEvent.click(screen.getByText("Approve plan"));
      await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
        "p1", 7, "assembly.plan",
        { timeline_id: "tl1" }, "timeline"));
    });

  it("previews apply dispositions then commits with skip policy",
    async () => {
      previewCommand.mockResolvedValueOnce({
        summary: "s", warnings: [],
        resolved_assembly: {
          plan_id: "pl1", timeline_id: "tl1", sources_stale: false,
          resolve_conflicts: "skip",
          items: [
            { key: "shot:s1", kind: "shot", title: "Shot 1",
              action: "insert", reason: "new clip" },
            { key: "dialogue:d1", kind: "dialogue", title: "Line",
              action: "skip", reason: "no audio result" },
          ],
          counts: { insert: 1, keep: 0, skip: 1, replace: 0 },
        },
      });
      render(<AssemblyPanel projectId="p1"
        timeline={{ id: "tl1", name: "Cut 1" }} />);
      await screen.findByText("First cut");
      fireEvent.click(screen.getByText("Apply…"));
      expect(await screen.findByText(/Apply preview/)).toBeTruthy();
      expect(screen.getByText(/1 insert/)).toBeTruthy();
      fireEvent.click(screen.getByText("Apply to timeline"));
      await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
        "p1", 7, "assembly.apply",
        { timeline_id: "tl1", resolve_conflicts: "skip" }, "timeline"));
    });

  it("blocks apply approval when sources went stale", async () => {
    previewCommand.mockResolvedValueOnce({
      summary: "s",
      warnings: ["sources changed since this plan was computed"],
      resolved_assembly: {
        plan_id: "pl1", timeline_id: "tl1", sources_stale: true,
        resolve_conflicts: "skip",
        items: [], counts: { insert: 0, keep: 0, skip: 0, replace: 0 },
      },
    });
    render(<AssemblyPanel projectId="p1"
      timeline={{ id: "tl1", name: "Cut 1" }} />);
    await screen.findByText("First cut");
    fireEvent.click(screen.getByText("Apply…"));
    expect(await screen.findByText(
      /re-plan for a fresh proposal|Re-plan first/)).toBeTruthy();
    expect((screen.getByText("Apply to timeline") as HTMLButtonElement)
      .disabled).toBe(true);
  });

  it("records a review vote with note via assembly.review", async () => {
    render(<AssemblyPanel projectId="p1"
      timeline={{ id: "tl1", name: "Cut 1" }} />);
    await screen.findByText("First cut");
    fireEvent.click(screen.getByText("Details"));
    const notes = await screen.findAllByLabelText("Review note");
    fireEvent.change(notes[0], { target: { value: "hold for v2" } });
    fireEvent.click(screen.getAllByLabelText("Approve item")[0]);
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "assembly.review",
      { plan_id: "pl1", item_ids: ["i1"], status: "approved",
        note: "hold for v2" }, "timeline"));
  });
});
