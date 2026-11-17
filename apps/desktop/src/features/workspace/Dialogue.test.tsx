// A5 — dialogue section: list + add, speaker/text/direction editing,
// voice-resolution badges, mock-audio generate preview → approve →
// commit with the dialogue pin, cancel, audio-lane placement.
// All mutations stay inside the command system.
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { DialogueSection } from "./Dialogue";
import type { DialogueSegment } from "../../api/types";

const apiFetch = vi.fn();
const commitCommand = vi.fn();
const previewCommand = vi.fn();
vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
  fetchAssetObjectUrl: () => Promise.resolve("blob:mock"),
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

const SEG: DialogueSegment = {
  id: "d1", sp_scene_id: "spsc1", element_id: null,
  scene_id: null, shot_id: null, character_id: "ch1",
  position: 0, text: "The tide is coming in.", direction: "(quietly)",
  voice_json: {}, language: "en", pronunciation_json: {},
  timing_json: {}, seed: null, model_key: null, settings_json: {},
  result_asset_id: null, generate_job_id: null, status: "active",
  revision: 0, speaker_name: "Anna",
  resolved_voice: { missing: true },
};

function route(url: string) {
  if (url.includes("/dialogue")) return [SEG];
  if (url.endsWith("/character")) return [
    { id: "ch1", name: "Anna" }, { id: "ch2", name: "Ben" }];
  if (url.endsWith("/timelines")) return [{ id: "tl1", name: "Cut 1" }];
  if (url.endsWith("/timelines/tl1")) return {
    id: "tl1", name: "Cut 1", tracks: [
      { id: "tkA", kind: "audio", name: "A1" },
      { id: "tkV", kind: "video", name: "V1" },
    ],
  };
  throw new Error(`unmocked fetch ${url}`);
}

const run = vi.fn();
beforeEach(() => {
  cleanup();
  apiFetch.mockReset(); commitCommand.mockReset();
  previewCommand.mockReset(); run.mockReset();
  apiFetch.mockImplementation((u: string) => Promise.resolve(route(u)));
  commitCommand.mockResolvedValue(
    { command_id: "c1", job_id: null, revision: 8 });
  run.mockResolvedValue(true);
});
afterEach(cleanup);

describe("DialogueSection", () => {
  it("lists segments with speaker and adds via dialogue.create", async () => {
    render(<DialogueSection projectId="p1" spSceneId="spsc1" run={run} />);
    expect(await screen.findByText(/Anna:/)).toBeTruthy();
    fireEvent.click(screen.getByLabelText("Add dialogue segment"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "dialogue.create", { sp_scene_id: "spsc1" }));
  });

  it("shows the missing-voice indicator when no profile resolves", async () => {
    render(<DialogueSection projectId="p1" spSceneId="spsc1" run={run} />);
    expect(await screen.findByText("⚠ no voice profile")).toBeTruthy();
  });

  it("edits text/speaker/direction and saves via dialogue.update", async () => {
    render(<DialogueSection projectId="p1" spSceneId="spsc1" run={run} />);
    fireEvent.click(await screen.findByRole("button",
      { name: /Anna: “The tide/ }));
    fireEvent.change(screen.getByLabelText("Dialogue text"),
      { target: { value: "We should go." } });
    fireEvent.change(screen.getByLabelText("Speaker"),
      { target: { value: "ch2" } });
    fireEvent.change(screen.getByLabelText("Delivery direction"),
      { target: { value: "(urgent)" } });
    fireEvent.click(screen.getByText("Save changes"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "dialogue.update", expect.objectContaining({
        segment_id: "d1", text: "We should go.",
        character_id: "ch2", direction: "(urgent)",
      })));
  });

  it("generate previews the simulated-audio plan, approves with the pin", async () => {
    previewCommand.mockResolvedValueOnce({
      resolved_dialogue: { segment_id: "d1", revision: 0 },
      resolved_audio: {
        simulated: true, text: "The tide is coming in.",
        direction: "(quietly)", duration_s: 2.32, sample_rate: 48000,
        voice: { missing: true },
      },
      warnings: [],
    });
    render(<DialogueSection projectId="p1" spSceneId="spsc1" run={run} />);
    fireEvent.click(await screen.findByLabelText(
      "Generate dialogue audio"));
    expect(await screen.findByText(
      "Simulated audio — deterministic mock")).toBeTruthy();
    expect(screen.getByText(/speaker has no voice profile/)).toBeTruthy();
    expect(commitCommand).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("Approve & generate"));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "dialogue.generate",
      { segment_id: "d1",
        expected_dialogue: { segment_id: "d1", revision: 0 } },
      "screenplay"));
  });

  it("places a produced segment on an audio lane via dialogue.place", async () => {
    apiFetch.mockImplementation((u: string) => Promise.resolve(
      u.includes("/dialogue")
        ? [{ ...SEG, result_asset_id: "a7", result_asset_name: "d.wav",
             result_asset_status: "ready" }]
        : route(u)));
    render(<DialogueSection projectId="p1" spSceneId="spsc1" run={run} />);
    fireEvent.click(await screen.findByRole("button",
      { name: /Anna: “The tide/ }));
    const sel = await screen.findByLabelText("Audio lane for placement");
    // Load lanes for the timeline, then the only audio lane appears.
    fireEvent.focus(sel);
    await waitFor(() =>
      expect((sel as HTMLSelectElement).textContent)
        .toContain("Cut 1 · A1"));
    expect((sel as HTMLSelectElement).textContent)
      .not.toContain("V1");
    fireEvent.change(sel, { target: { value: "tl1|tkA" } });
    fireEvent.click(screen.getByLabelText("Place dialogue on timeline"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "dialogue.place", {
        segment_id: "d1", timeline_id: "tl1", track_id: "tkA" }));
  });

  it("flags a stale result when the source drifted", async () => {
    apiFetch.mockImplementation((u: string) => Promise.resolve(
      u.includes("/dialogue")
        ? [{ ...SEG, result_asset_id: "a7", result_asset_name: "d.wav",
             result_asset_status: "ready", result_stale: true }]
        : route(u)));
    render(<DialogueSection projectId="p1" spSceneId="spsc1" run={run} />);
    fireEvent.click(await screen.findByRole("button",
      { name: /Anna: “The tide/ }));
    await screen.findByText(/stale — source changed/);
  });

  it("archives via dialogue.archive", async () => {
    render(<DialogueSection projectId="p1" spSceneId="spsc1" run={run} />);
    fireEvent.click(await screen.findByLabelText(
      "Archive dialogue segment"));
    await waitFor(() => expect(run).toHaveBeenCalledWith(
      "dialogue.archive", { segment_id: "d1" }));
  });
});
