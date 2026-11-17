// A3 — keyframe section: list + add, prompt editing, compose-draft
// proposal flow, mock generate preview→approve→commit with backend
// pins, pin/archive commands. All mutations stay inside the command
// system — the section never calls a resource endpoint directly.
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { KeyframesSection } from "./StoryboardKeyframes";
import type { Keyframe } from "../../api/types";

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

const KF: Keyframe = {
  id: "kf1", shot_id: "sh1", scene_id: "sc1", role: "start",
  position: 0, frame_num: null, frame_den: null,
  prompt: "opening still", negative_prompt: "",
  settings_json: { seed: 7, model_key: null },
  continuity_json: {}, result_asset_id: null,
  generate_job_id: null, prompt_origin: "user", status: "active",
  revision: 0,
};

function kfRoute(url: string) {
  if (url.endsWith("/keyframes")) return [KF];
  if (url === "/api/models") return [{
    key: "img-model", display_name: "Img Model", modality: "image",
    install: { status: "not_installed" },
    compatibility: { status: "compatible", reasons: [], warnings: [] },
  }];
  throw new Error(`unmocked fetch ${url}`);
}

const run = vi.fn();
beforeEach(() => {
  cleanup();
  apiFetch.mockReset(); commitCommand.mockReset();
  previewCommand.mockReset(); run.mockReset();
  apiFetch.mockImplementation((u: string) => Promise.resolve(kfRoute(u)));
  commitCommand.mockResolvedValue({ command_id: "c1", job_id: null, revision: 8 });
  run.mockResolvedValue(true);
});
afterEach(cleanup);

describe("KeyframesSection", () => {
  it("lists keyframes and adds via keyframe.create", async () => {
    render(<KeyframesSection projectId="p1" shotId="sh1" run={run} />);
    expect(await screen.findByRole("button", { name: "▸ Start" })).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Add End keyframe"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "keyframe.create", { shot_id: "sh1", role: "end" }));
  });

  it("edits prompt/seed and saves via keyframe.update", async () => {
    render(<KeyframesSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "▸ Start" }));
    const prompt = screen.getByLabelText("Keyframe prompt");
    fireEvent.change(prompt, { target: { value: "edited beat" } });
    fireEvent.change(screen.getByLabelText("Seed"), { target: { value: "11" } });
    fireEvent.click(screen.getByText("Save prompt"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "keyframe.update", {
        keyframe_id: "kf1",
        prompt: "edited beat",
        negative_prompt: "",
        settings: { seed: 11, model_key: null },
      }));
  });

  it("compose previews the draft, then commits with the keyframe pin", async () => {
    previewCommand.mockResolvedValueOnce({
      resolved_keyframe: { keyframe_id: "kf1", revision: 0 },
      resolved_draft: {
        prompt: "opening frame: she turns",
        prompt_sha256: "sha",
        user_prompt: "opening frame: opening still",
        context_text: "", sections: [{ s: 1 }], omitted: [],
        conflicts: [], warnings: ["context note"],
        budget: { limit: 4000, used: 10, user: 10 },
      },
      warnings: [],
    });
    render(<KeyframesSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByLabelText("Draft prompt for Start keyframe"));
    // Proposal shown — nothing committed yet
    expect(await screen.findByText("Proposed draft")).toBeTruthy();
    expect(screen.getByText(/opening frame: she turns/)).toBeTruthy();
    expect(commitCommand).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Apply draft"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "keyframe.compose",
      { keyframe_id: "kf1",
        expected_keyframe: { keyframe_id: "kf1", revision: 0 } },
      "storyboard"));
  });

  it("generate previews composed prompt, approves with both pins", async () => {
    previewCommand.mockResolvedValueOnce({
      resolved_keyframe: { keyframe_id: "kf1", revision: 0 },
      resolved_prompt: {
        prompt_sha256: "psha", prompt: "composed text",
        user_prompt: "opening still", context_text: "",
        sections: [], omitted: [], conflicts: [], warnings: [],
        budget: { limit: 4000, used: 5, user: 5 },
        user_over_budget: false,
      },
      warnings: [],
    });
    render(<KeyframesSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByLabelText("Generate Start keyframe"));
    expect(await screen.findByText("Composed prompt — mock generation"))
      .toBeTruthy();
    expect(screen.getByText("composed text")).toBeTruthy();
    fireEvent.click(screen.getByText("Approve & generate"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "keyframe.generate",
      { keyframe_id: "kf1",
        expected_keyframe: { keyframe_id: "kf1", revision: 0 },
        expected_prompt: "psha" },
      "storyboard"));
  });

  it("pins and archives via commands", async () => {
    apiFetch.mockImplementation((u: string) => Promise.resolve(
      u.endsWith("/keyframes")
        ? [{ ...KF, result_asset_id: "a1", result_asset_name: "out.png",
             result_asset_status: "ready" }]
        : kfRoute(u)));
    render(<KeyframesSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByLabelText("Pin Start result"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "keyframe.pin", { keyframe_id: "kf1" }));
    fireEvent.click(screen.getByLabelText("Archive Start keyframe"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "keyframe.archive", { keyframe_id: "kf1" }));
  });

  it("shows model compatibility in the picker", async () => {
    render(<KeyframesSection projectId="p1" shotId="sh1" run={run} />);
    fireEvent.click(await screen.findByRole("button", { name: "▸ Start" }));
    const sel = screen.getByLabelText("Model") as HTMLSelectElement;
    await waitFor(() =>
      expect(sel.textContent).toContain("Img Model — compatible"));
    fireEvent.change(sel, { target: { value: "img-model" } });
    expect(await screen.findByText(/not installed \(mock output regardless\)/))
      .toBeTruthy();
  });
});
