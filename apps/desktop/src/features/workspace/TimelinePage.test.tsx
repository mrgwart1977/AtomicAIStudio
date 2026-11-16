// C5-A + N1-B — timeline assembly UI: lane editor, clip ordering,
// trim/remove via the inspector, export-capability messaging, and
// stale-revision recovery all go through commands.
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
let CURRENT = PROJECT;
vi.mock("../../stores/appStore", () => ({
  useApp: (sel: (s: unknown) => unknown) => sel({
    currentProject: () => CURRENT,
    refreshProjectData: () => Promise.resolve(),
  }),
}));
// TimelinePage calls useApp.getState() for the live revision.
import { useApp } from "../../stores/appStore";
(useApp as unknown as { getState: () => unknown }).getState = () => ({
  currentProject: () => CURRENT,
});

const TL = {
  id: "t1", project_id: "p1", name: "Cut 1", description: "",
  fps_num: 24, fps_den: 1, status: "active", revision: 2,
  created_at: "2026-01-01", updated_at: "2026-01-01",
};
const V1 = {
  id: "v1", project_id: "p1", timeline_id: "t1", kind: "video",
  name: "V1", position: 0, visible: true, locked: false, muted: false,
  created_at: "2026-01-01",
};
const CLIP = (i: number) => ({
  id: `c${i}`, project_id: "p1", timeline_id: "t1", track_id: "v1",
  start_frames: i * 72, position: i,
  source_asset_id: `a${i}`, source_version_id: `v${i}`,
  source_sha256: "ab".repeat(32), in_frame: 0, out_frame: 72,
  src_frames: 72, src_fps_num: 24, src_fps_den: 1,
  src_duration_s: 3.0, src_has_audio: true, shot_id: null, scene_id: null,
  created_at: "2026-01-01",
});

function route(url: string) {
  if (url.endsWith("/timelines")) return Promise.resolve([TL]);
  if (url.endsWith("/revisions")) return Promise.resolve([]);
  if (url.includes("/timelines/t1")) {
    return Promise.resolve({
      ...TL, tracks: [V1], clips: [CLIP(0), CLIP(1)],
      total_duration_s: 6.0 });
  }
  return Promise.resolve(null);
}

describe("TimelinePage", () => {
  beforeEach(() => {
    cleanup(); vi.clearAllMocks(); CURRENT = PROJECT;
    apiFetch.mockImplementation(route);
    commitCommand.mockResolvedValue({});
  });

  it("lists timelines and renders lane clips with total duration", async () => {
    render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: /Cut 1/ }));
    const clips = await screen.findAllByTestId("lane-clip");
    expect(clips).toHaveLength(2);
    expect(screen.getByText(/6s total/)).toBeTruthy();
  });

  it("shows the export-capability badge for sequential layouts", async () => {
    render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: /Cut 1/ }));
    const badge = await screen.findByTestId("export-capability");
    expect(badge.textContent).toMatch(/Exports as one video/);
  });

  it("multitrack layout is flagged as needing N1-D — not silently flattened", async () => {
    apiFetch.mockImplementation((url: string) => {
      if (url.endsWith("/timelines")) return Promise.resolve([TL]);
      if (url.endsWith("/revisions")) return Promise.resolve([]);
      if (url.includes("/timelines/t1")) {
        return Promise.resolve({
          ...TL,
          tracks: [V1, { ...V1, id: "o1", kind: "overlay",
                        name: "O1", position: 1 }],
          clips: [CLIP(0), { ...CLIP(1), track_id: "o1" }],
          total_duration_s: 6.0 });
      }
      return Promise.resolve(null);
    });
    render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: /Cut 1/ }));
    const badge = await screen.findByTestId("export-capability");
    // N1-D — two populated lanes composite, not refuse.
    expect(badge.textContent).toMatch(/Exports 2 lanes — composite/);
    fireEvent.click(screen.getByRole("button", { name: "Export…" }));
    expect(screen.queryByTestId("export-blocked")).toBeNull();
    expect(screen.getByRole("button", { name: "Preview export" })
      .getAttribute("disabled")).toBeNull();
  });

  it("keyboard move + inspector remove go through commands", async () => {
    render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: /Cut 1/ }));
    const clips = await screen.findAllByTestId("lane-clip");
    // c1@72 → ArrowRight nudges to 73 (into the gap — legal).
    clips[1].focus();
    fireEvent.keyDown(clips[1], { key: "ArrowRight" });
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "timeline.clip.move",
      expect.objectContaining({
        clip_id: "c1", to_track_id: "v1", start_frames: 73 }), "timeline"));
    // Select → Remove in inspector → confirm dialog.
    fireEvent.click(clips[0]);
    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    fireEvent.click(await screen.findByRole("button", { name: "confirm:Remove clip" }));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "timeline.clip.remove",
      expect.objectContaining({ clip_id: "c0" }), "timeline"));
  });

  it("inspector trim only commits parseable integers", async () => {
    render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: /Cut 1/ }));
    fireEvent.click((await screen.findAllByTestId("lane-clip"))[0]);
    const inField = await screen.findByRole("textbox", { name: "Trim in frame" });
    fireEvent.change(inField, { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply trim" }));
    expect(commitCommand).not.toHaveBeenCalledWith(
      "p1", 7, "timeline.clip.trim", expect.anything(), "timeline");
    fireEvent.change(inField, { target: { value: "5" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Trim out frame" }),
      { target: { value: "50" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply trim" }));
    await waitFor(() => expect(commitCommand).toHaveBeenCalledWith(
      "p1", 7, "timeline.clip.trim",
      expect.objectContaining(
        { timeline_id: "t1", clip_id: "c0", in_frame: 5, out_frame: 50 }),
      "timeline"));
  });

  it("stale-revision error shows a refresh affordance", async () => {
    commitCommand.mockRejectedValue(new Error(
      "422: project revision is stale — re-preview"));
    render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: /Cut 1/ }));
    const clips = await screen.findAllByTestId("lane-clip");
    clips[1].focus();
    fireEvent.keyDown(clips[1], { key: "ArrowRight" });
    const btn = await screen.findByRole("button", { name: "Refresh timeline" });
    fireEvent.click(btn);
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      expect.stringContaining("/timelines/t1")));
  });

  // N3-C C4 regression: a command commit refreshes the project row —
  // the store hands a NEW project object (same id). The editor must not
  // remount: the mount effect's callbacks key on project.id, not the
  // object identity, or setLoading(true) wipes editor state (selection,
  // inspector, playhead) on every commit.
  it("does not remount the editor when the project object refreshes", async () => {
    const { rerender } = render(createElement(TimelinePage));
    fireEvent.click(await screen.findByRole("button", { name: /Cut 1/ }));
    const editor = await screen.findByTestId("timeline-editor");
    fireEvent.click((await screen.findAllByTestId("lane-clip"))[0]);
    await screen.findByTestId("clip-inspector");
    // simulate refreshProjectData(): same project id, new identity.
    CURRENT = { ...PROJECT, revision: 8 };
    rerender(createElement(TimelinePage));
    await waitFor(() =>
      expect(screen.getByTestId("timeline-editor")).toBe(editor));
    expect(screen.getByTestId("clip-inspector")).toBeTruthy();
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
