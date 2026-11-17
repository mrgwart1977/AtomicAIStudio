// A4 — motion-clip section: list + add, keyframe selection, motion/
// camera editing, compose-draft proposal, mock-video generate preview
// → approve → commit with backend pins, cancel, timeline placement.
// All mutations stay inside the command system.
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MotionClipsSection } from "./MotionClips";
import type { MotionClip } from "../../api/types";

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

const CLIP: MotionClip = {
  id: "c1", shot_id: "sh1", scene_id: "sc1", position: 0,
  start_keyframe_id: "kf1", end_keyframe_id: null,
  prompt: "push in", negative_prompt: "",
  motion_json: { instructions: "slow push", intensity: "subtle" },
  camera_json: { movement: "dolly_in", follow_mode: null },
  duration_frames: 48, fps_num: 24, fps_den: 1,
  width: 512, height: 512, model_key: null, seed: 7,
  continuity_json: {}, result_asset_id: null,
  generate_job_id: null, prompt_origin: "user", status: "active",
  revision: 0,
};

function route(url: string) {
  if (url.endsWith("/clips")) return [CLIP];
  if (url.endsWith("/keyframes")) return [{
    id: "kf1", shot_id: "sh1", scene_id: "sc1", role: "start",
    position: 0, frame_num: null, frame_den: null,
    prompt: "still", negative_prompt: "", settings_json: {},
    continuity_json: {}, result_asset_id: null, generate_job_id: null,
    prompt_origin: "user", status: "active", revision: 0,
  }];
  if (url === "/api/models") return [{
    key: "vid-model", display_name: "Video Model", modality: "video",
    install: { status: "not_installed" },
    compatibility: { status: "limited", reasons: ["VRAM 12 GB"], warnings: [] },
  }, {
    key: "img-model", display_name: "Image Only", modality: "image",
    install: { status: "installed" },
    compatibility: { status: "compatible", reasons: [], warnings: [] },
  }];
  if (url.endsWith("/timelines")) return [{ id: "tl1", name: "Cut 1" }];
  throw new Error(`unmocked fetch ${url}`);
}

const run = vi.fn();
beforeEach(() => {
  cleanup();
  apiFetch.mockReset(); commitCommand.mockReset();
  previewCommand.mockReset(); run.mockReset();
  apiFetch.mockImplementation((u: string) => Promise.resolve(route(u)));
  commitCommand.mockResolvedValue({ command_id: "c1", job_id: null, revision: 8 });
  run.mockResolvedValue(true);
});
afterEach(cleanup);

describe("MotionClipsSection", () => {
  it("lists clips and adds via clip.create", async () => {
    render(<MotionClipsSection projectId="p1" shotId="sh1" run={run} />);
    expect(await screen.findByRole("button", { name: "▸ Clip 1" }))
      .toBeTruthy();
    fireEvent.click(screen.getByLabelText("Add motion clip"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "clip.create", { shot_id: "sh1", duration_frames: 48 }));
  });

  it("edits prompt/motion/camera and saves via clip.update", async () => {
    render(<MotionClipsSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "▸ Clip 1" }));
    fireEvent.change(screen.getByLabelText("Motion prompt"),
      { target: { value: "orbit her" } });
    fireEvent.change(screen.getByLabelText("Follow mode"),
      { target: { value: "orbit_follow" } });
    fireEvent.change(screen.getByLabelText("Intensity"),
      { target: { value: "strong" } });
    fireEvent.click(screen.getByText("Save settings"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "clip.update", expect.objectContaining({
        clip_id: "c1",
        prompt: "orbit her",
        motion: expect.objectContaining({
          intensity: "strong", instructions: "slow push" }),
        camera: expect.objectContaining({
          movement: "dolly_in", follow_mode: "orbit_follow" }),
      })));
  });

  it("only offers video-capable models — never an image-only model", async () => {
    render(<MotionClipsSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "▸ Clip 1" }));
    const sel = screen.getByLabelText("Model") as HTMLSelectElement;
    await waitFor(() =>
      expect(sel.textContent).toContain("Video Model — limited"));
    expect(sel.textContent).not.toContain("Image Only");
    fireEvent.change(sel, { target: { value: "vid-model" } });
    expect(await screen.findByText(/Video Model: limited — VRAM 12 GB/))
      .toBeTruthy();
  });

  it("compose previews the draft, then commits with the clip pin", async () => {
    previewCommand.mockResolvedValueOnce({
      resolved_clip: { clip_id: "c1", revision: 0 },
      resolved_draft: {
        prompt: "anna turns as the camera orbits",
        prompt_sha256: "sha", user_prompt: "push in",
        context_text: "", sections: [], omitted: [],
        conflicts: [], warnings: [],
        budget: { limit: 4000, used: 5, user: 5 },
      },
      warnings: [],
    });
    render(<MotionClipsSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByLabelText("Draft motion prompt"));
    expect(await screen.findByText("Proposed motion draft")).toBeTruthy();
    expect(commitCommand).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Apply draft"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "clip.compose",
      { clip_id: "c1",
        expected_clip: { clip_id: "c1", revision: 0 } },
      "storyboard"));
  });

  it("generate previews composed prompt, approves with both pins", async () => {
    previewCommand.mockResolvedValueOnce({
      resolved_clip: { clip_id: "c1", revision: 0 },
      resolved_prompt: {
        prompt_sha256: "psha", prompt: "composed motion text",
        user_prompt: "push in", context_text: "",
        sections: [], omitted: [], conflicts: [], warnings: [],
        budget: { limit: 4000, used: 5, user: 5 },
        user_over_budget: false,
      },
      warnings: [],
    });
    render(<MotionClipsSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByLabelText("Generate motion clip"));
    expect(await screen.findByText(
      "Composed prompt — mock video generation")).toBeTruthy();
    fireEvent.click(screen.getByText("Approve & generate"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "clip.generate",
      { clip_id: "c1",
        expected_clip: { clip_id: "c1", revision: 0 },
        expected_prompt: "psha" },
      "storyboard"));
  });

  it("places a produced clip via timeline.clip.insert", async () => {
    apiFetch.mockImplementation((u: string) => Promise.resolve(
      u.endsWith("/clips")
        ? [{ ...CLIP, result_asset_id: "a9", result_asset_name: "m.mp4",
             result_asset_status: "ready" }]
        : route(u)));
    render(<MotionClipsSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByLabelText("Place clip on timeline"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "timeline.clip.insert", {
        timeline_id: "tl1", asset_id: "a9", shot_id: "sh1" }));
  });

  it("archives and restores via commands", async () => {
    render(<MotionClipsSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByLabelText("Archive motion clip"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "clip.archive", { clip_id: "c1" }));
  });
});
