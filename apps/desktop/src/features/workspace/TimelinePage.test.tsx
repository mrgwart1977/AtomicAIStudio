// C5-A — timeline assembly UI: clip ordering, trim controls, removal,
// and the send-to-timeline provenance path all go through commands.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { TimelinePage } from "./TimelinePage";

const apiFetch = vi.fn();
const commitCommand = vi.fn();

vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));
vi.mock("../../api/commands", () => ({
  commitCommand: (...a: unknown[]) => commitCommand(...a),
}));
vi.mock("../../components/ConfirmDialog", () => ({
  ConfirmDialog: ({ open, title, onConfirm }: {
    open: boolean; title: string; onConfirm: () => void }) =>
    open ? createElement("button", { onClick: onConfirm }, `confirm:${title}`) : null,
}));

const PROJECT = { id: "p1", revision: 7, name: "Demo", status: "active" };
vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel({
    currentProject: () => PROJECT,
    refreshProjectData: () => Promise.resolve(),
  }),
}));
// TimelinePage calls useApp.getState() for the live revision.
import { useApp } from "../../stores/appStore";
(useApp as unknown as { getState: () => unknown }).getState = () => ({
  currentProject: () => PROJECT,
});

const TL = {
  id: "t1", project_id: "p1", name: "Cut 1", description: "",
  fps_num: 24, fps_den: 1, status: "active", revision: 2,
  created_at: "2026-01-01", updated_at: "2026-01-01",
};
const CLIP = (i: number) => ({
  id: `c${i}`, project_id: "p1", timeline_id: "t1", position: i,
  source_asset_id: `a${i}`, source_version_id: `v${i}`,
  source_sha256: "ab".repeat(32), in_frame: 0, out_frame: 72,
  src_frames: 72, src_fps_num: 24, src_fps_den: 1,
  src_duration_s: 3.0, shot_id: null, scene_id: null,
  created_at: "2026-01-01",
});

function route(url: string) {
  if (url.endsWith("/timelines")) return Promise.resolve([TL]);
  if (url.endsWith("/revisions")) return Promise.resolve([]);
  if (url.includes("/timelines/t1")) {
    return Promise.resolve({ ...TL, clips: [CLIP(0), CLIP(1)], total_duration_s: 6.0 });
  }
  return Promise.resolve(null);
}

describe("TimelinePage", () => {
  beforeEach(() => {
    cleanup(); vi.clearAllMocks();
    apiFetch.mockImplementation(route);
    commitCommand.mockResolvedValue({});
  });

  it("lists timelines and renders ordered clips with total duration", async () => {
    render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: /Cut 1/ }));
    const clips = await screen.findAllByTestId("timeline-clip");
    expect(clips).toHaveLength(2);
    expect(screen.getByText(/6s total/)).toBeTruthy();
  });

  it("move/remove go through timeline.clip commands", async () => {
    render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: /Cut 1/ }));
    await screen.findAllByTestId("timeline-clip");
    fireEvent.click(screen.getAllByRole("button", { name: "Move clip earlier" })[1]);
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "timeline.clip.move",
      expect.objectContaining({ clip_id: "c1", to_index: 0 }), "timeline"));
    fireEvent.click(screen.getAllByRole("button", { name: "Remove clip" })[0]);
    fireEvent.click(await screen.findByRole("button", { name: "confirm:Remove clip" }));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "timeline.clip.remove",
      expect.objectContaining({ clip_id: "c0" }), "timeline"));
  });

  it("trim inputs only commit parseable integers", async () => {
    render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: /Cut 1/ }));
    await screen.findAllByTestId("timeline-clip");
    fireEvent.click(screen.getAllByRole("button", { name: "Trim" })[0]);
    const inField = screen.getByRole("textbox", { name: "Trim in frame" });
    fireEvent.change(inField, { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(commitCommand).not.toHaveBeenCalledWith(
      "p1", 7, "timeline.clip.trim", expect.anything(), "timeline");
    fireEvent.change(inField, { target: { value: "5" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Trim out frame" }),
      { target: { value: "50" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "timeline.clip.trim",
      { timeline_id: "t1", clip_id: "c0", in_frame: 5, out_frame: 50 },
      "timeline"));
  });

  it("create goes through timeline.create", async () => {
    render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: "+ New" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Timeline name" }),
      { target: { value: "Rough cut" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "timeline.create", { name: "Rough cut" }, "timeline"));
  });
});
