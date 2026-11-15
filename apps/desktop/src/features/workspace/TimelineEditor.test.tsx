// N1-B — multitrack editor: lane rendering/ordering, track controls,
// selection, drag placement, snapping, invalid drops, lock/visibility,
// keyboard access, project switching, and empty state.
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cleanup, fireEvent, render, screen, waitFor,
} from "@testing-library/react";
import { createElement } from "react";
import { TimelineEditor } from "./TimelineEditor";
import type { TimelineClip, TimelineDetail, TimelineTrack } from "../../api/types";

const apiFetch = vi.fn();
vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));

const TRACK = (id: string, over: Partial<TimelineTrack> = {}): TimelineTrack => ({
  id, project_id: "p", timeline_id: "t", kind: "video",
  name: id.toUpperCase(), position: 0,
  visible: true, locked: false, muted: false,
  gain_db: 0, solo: false,
  created_at: "2026-01-01", ...over,
});

const CLIP = (id: string, over: Partial<TimelineClip> = {}): TimelineClip => ({
  id, project_id: "p", timeline_id: "t", track_id: "v1",
  start_frames: 0, position: 0,
  source_asset_id: "a", source_version_id: "v",
  source_sha256: "ab".repeat(32),
  in_frame: 0, out_frame: 72,
  src_frames: 72, src_fps_num: 24, src_fps_den: 1,
  src_duration_s: 3, gain_db: 0, shot_id: null, scene_id: null,
  created_at: "2026-01-01", ...over,
});

const DETAIL = (over: Partial<TimelineDetail> = {}): TimelineDetail => ({
  id: "t", project_id: "p", name: "Cut", description: "",
  fps_num: 24, fps_den: 1, status: "active", revision: 3,
  created_at: "2026-01-01", updated_at: "2026-01-01",
  tracks: [TRACK("v1")],
  clips: [CLIP("c0"), CLIP("c1", { start_frames: 72 })],
  total_duration_s: 6,
  undo_label: null, redo_label: null,
  ...over,
});

/** jsdom returns zero-size rects — stub lane geometry so pointer→lane
 * and pointer→frame math has real values. */
function stubLaneRects() {
  const rects: Record<string, { top: number; bottom: number }> = {};
  for (const el of document.querySelectorAll("[data-testid^='lane-body-']")) {
    const id = el.getAttribute("data-testid")!.slice("lane-body-".length);
    rects[id] = rects[id] ?? { top: 0, bottom: 56 };
  }
  // Assign successive vertical bands per lane in document order.
  let top = 0;
  for (const el of document.querySelectorAll("[data-testid^='lane-body-']")) {
    const id = el.getAttribute("data-testid")!.slice("lane-body-".length);
    const band = { left: 0, top, bottom: top + 56, right: 1200, width: 1200, height: 56 };
    rects[id] = band;
    top += 56;
    vi.spyOn(el, "getBoundingClientRect")
      .mockReturnValue(band as DOMRect);
  }
  return rects;
}

describe("TimelineEditor", () => {
  const onRun = vi.fn();
  const onRemoveClip = vi.fn();

  const setup = (detail = DETAIL(), active = true) => {
    onRun.mockResolvedValue(true);
    return render(createElement(TimelineEditor, {
      detail, active, busy: false, onRun, onRemoveClip,
    }));
  };

  beforeEach(() => { cleanup(); vi.clearAllMocks(); });

  it("renders lanes in position order with kind badges and names", () => {
    setup(DETAIL({
      tracks: [
        TRACK("o1", { kind: "overlay", name: "Titles", position: 0 }),
        TRACK("v1", { name: "Main", position: 1 }),
        TRACK("a1", { kind: "audio", name: "Music", position: 2 }),
      ],
    }));
    const lanes = screen.getAllByTestId(/^lane-(?!body|clip)/)
      .map((el) => el.getAttribute("data-testid"));
    expect(lanes).toEqual(["lane-o1", "lane-v1", "lane-a1"]);
    expect(screen.getByText("Titles")).toBeTruthy();
    expect(screen.getByText("Music")).toBeTruthy();
  });

  it("places clips at their start_frames", () => {
    setup();
    const clips = screen.getAllByTestId("lane-clip");
    expect(clips).toHaveLength(2);
    expect(clips[0].style.left).toBe("0px");
    expect(clips[1].style.left).toBe(`${72 * 6}px`); // ppf default 6
  });

  it("track controls send track.update / reorder / remove", async () => {
    setup(DETAIL({
      tracks: [TRACK("v1", { name: "Main" }), TRACK("o1", {
        kind: "overlay", name: "Top", position: 1 })],
    }));
    fireEvent.click(screen.getByRole("button", { name: "Hide Main" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.track.update",
      expect.objectContaining({ track_id: "v1", visible: false })));
    fireEvent.click(screen.getByRole("button", { name: "Lock Main" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.track.update",
      expect.objectContaining({ track_id: "v1", locked: true })));
    fireEvent.click(screen.getByRole("button", { name: "Mute Main" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.track.update",
      expect.objectContaining({ track_id: "v1", muted: true })));
    fireEvent.click(screen.getByRole("button", { name: "Move Top up" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.track.reorder",
      expect.objectContaining({ track_id: "o1", to_position: 0 })));
    fireEvent.click(screen.getByRole("button", { name: "Remove Top" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.track.remove",
      expect.objectContaining({ track_id: "o1" })));
  });

  it("N2-C: solo toggle + track gain slider commit one update each", async () => {
    setup(DETAIL({
      tracks: [TRACK("a1", { kind: "audio", name: "Music" })],
      clips: [],
    }));
    fireEvent.click(screen.getByRole("button", { name: "Solo Music" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.track.update",
      expect.objectContaining({ track_id: "a1", solo: true })));
    // Drag the lane gain slider — commits once on release.
    const slider = screen.getByRole("slider", { name: "Music volume" });
    fireEvent.change(slider, { target: { value: "-6" } });
    expect(onRun).not.toHaveBeenCalledWith(
      "timeline.track.update",
      expect.objectContaining({ gain_db: expect.anything() }));
    fireEvent.pointerUp(slider);
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.track.update",
      expect.objectContaining({ track_id: "a1", gain_db: -6 })));
  });

  it("N2-C: gain readout resets to unity on click", async () => {
    setup(DETAIL({
      tracks: [TRACK("a1", { kind: "audio", name: "Music",
        gain_db: -8 })],
      clips: [],
    }));
    // The reset control is the dB readout button beside the slider.
    fireEvent.click(screen.getByRole("button", { name: "-8.0dB" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.track.update",
      expect.objectContaining({ track_id: "a1", gain_db: 0 })));
  });

  it("N2-C: audio clip inspector exposes clip gain", async () => {
    // WaveformCanvas polls the manifest — answer "not built".
    apiFetch.mockRejectedValue(new Error("409: no waveform"));
    setup(DETAIL({
      tracks: [TRACK("a1", { kind: "audio", name: "Music" })],
      clips: [CLIP("c0", { track_id: "a1",
        src_fps_num: 48000, src_fps_den: 1, src_frames: 96000,
        in_frame: 0, out_frame: 96000, gain_db: -3 })],
    }));
    fireEvent.click(screen.getByTestId("lane-clip"));
    const slider = await screen.findByRole("slider",
      { name: "Clip volume" });
    fireEvent.change(slider, { target: { value: "-10" } });
    fireEvent.pointerUp(slider);
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.gain",
      expect.objectContaining({ clip_id: "c0", gain_db: -10 })));
    // Effective readout = committed clip + track dB (−3 + 0).
    expect(screen.getByText(/eff .*dB/).textContent)
      .toContain("-3.0");
  });

  it("N2-C: no mix row on overlay lanes", () => {
    setup(DETAIL({
      tracks: [TRACK("o1", { kind: "overlay", name: "Top" })],
      clips: [],
    }));
    expect(screen.queryByTestId("mix-o1")).toBeNull();
    expect(screen.getByRole("button", { name: "Solo Top" }))
      .toBeTruthy(); // toggle exists; backend refuses solo on overlay
  });

  it("non-empty lane removal is disabled", () => {
    setup();
    expect(screen.getByRole("button", { name: "Remove V1" }))
      .toHaveProperty("disabled", true);
  });

  it("add lane sends track.add with the chosen kind", async () => {
    setup();
    fireEvent.change(screen.getByRole("combobox", { name: "New lane kind" }),
      { target: { value: "overlay" } });
    fireEvent.click(screen.getByRole("button", { name: "+ Add lane" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.track.add",
      expect.objectContaining({ kind: "overlay" })));
  });

  it("click selects a clip and shows the inspector", async () => {
    setup();
    fireEvent.click(screen.getAllByTestId("lane-clip")[0]);
    const insp = await screen.findByTestId("clip-inspector");
    expect(insp.textContent).toMatch(/frames 0–72/);
  });

  it("keyboard ←/→ nudges by frames; Delete removes", async () => {
    setup();
    const clips = screen.getAllByTestId("lane-clip");
    // c1@72 → ArrowRight nudges to 73 (into the tail gap — legal).
    clips[1].focus();
    fireEvent.keyDown(clips[1], { key: "ArrowRight" });
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.move",
      expect.objectContaining({
        clip_id: "c1", to_track_id: "v1", start_frames: 73 })));
    // Shift = one timeline second.
    fireEvent.keyDown(clips[1], { key: "ArrowRight", shiftKey: true });
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.move",
      expect.objectContaining({ clip_id: "c1", start_frames: 96 })));
    // c0 can't ArrowRight — it would overlap c1 (client-side refusal).
    fireEvent.keyDown(clips[0], { key: "ArrowRight" });
    expect(onRun).not.toHaveBeenCalledWith(
      "timeline.clip.move",
      expect.objectContaining({ clip_id: "c0", start_frames: 1 }));
    fireEvent.keyDown(clips[0], { key: "Delete" });
    expect(onRemoveClip).toHaveBeenCalledWith(
      expect.objectContaining({ id: "c0" }), false);
  });

  it("drag commits clip.move with snapped integer frames on pointerup", async () => {
    setup();
    stubLaneRects();
    // c1@72 (72f wide) — pointer 462→540 = +78px = +13f → lands at 85.
    const clip = screen.getAllByTestId("lane-clip")[1];
    fireEvent.pointerDown(clip, { clientX: 462, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 540, clientY: 10 });
    fireEvent.pointerUp(window, { clientX: 540, clientY: 10 });
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.move",
      expect.objectContaining({
        timeline_id: "t", clip_id: "c1",
        to_track_id: "v1", start_frames: 85 })));
  });

  it("drag onto a second compatible lane re-lanes the clip", async () => {
    setup(DETAIL({
      tracks: [TRACK("v1"), TRACK("o1", { kind: "overlay", position: 1 })],
    }));
    stubLaneRects();
    const clip = screen.getAllByTestId("lane-clip")[0];
    fireEvent.pointerDown(clip, { clientX: 30, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 90, clientY: 80 }); // o1 band
    fireEvent.pointerUp(window, { clientX: 90, clientY: 80 });
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.move",
      expect.objectContaining({ to_track_id: "o1", start_frames: 10 })));
  });

  it("invalid drop (overlap) shows the reason and commits nothing", async () => {
    setup();
    stubLaneRects();
    const clip = screen.getAllByTestId("lane-clip")[0];
    fireEvent.pointerDown(clip, { clientX: 30, clientY: 10, pointerId: 1 });
    // Drag to ~frame 20 — overlaps c1 at 72? c0 extent is 72f → 20..92
    // overlaps 72 → error expected.
    fireEvent.pointerMove(window, { clientX: 150, clientY: 10 });
    const err = await screen.findByTestId("drop-error");
    expect(err.textContent).toMatch(/overlap/);
    fireEvent.pointerUp(window, { clientX: 150, clientY: 10 });
    expect(onRun).not.toHaveBeenCalledWith(
      "timeline.clip.move", expect.anything());
  });

  it("drop on an audio lane is refused with an honest reason", async () => {
    setup(DETAIL({
      tracks: [TRACK("v1"), TRACK("a1", { kind: "audio", position: 1 })],
    }));
    stubLaneRects();
    const clip = screen.getAllByTestId("lane-clip")[0];
    fireEvent.pointerDown(clip, { clientX: 30, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 90, clientY: 80 });
    const err = await screen.findByTestId("drop-error");
    expect(err.textContent).toMatch(/audio/i);
    fireEvent.pointerUp(window, { clientX: 90, clientY: 80 });
    expect(onRun).not.toHaveBeenCalledWith(
      "timeline.clip.move", expect.anything());
  });

  it("Escape cancels a drag — no commit, visual state restored", async () => {
    setup();
    stubLaneRects();
    const clip = screen.getAllByTestId("lane-clip")[0];
    fireEvent.pointerDown(clip, { clientX: 30, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 200, clientY: 10 });
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.pointerUp(window, { clientX: 200, clientY: 10 });
    expect(onRun).not.toHaveBeenCalledWith(
      "timeline.clip.move", expect.anything());
  });

  it("clips on locked lanes are not draggable", () => {
    setup(DETAIL({ tracks: [TRACK("v1", { locked: true })] }));
    const clip = screen.getAllByTestId("lane-clip")[0];
    fireEvent.pointerDown(clip, { clientX: 30, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 200, clientY: 10 });
    fireEvent.pointerUp(window, { clientX: 200, clientY: 10 });
    expect(onRun).not.toHaveBeenCalled();
  });

  it("hidden lanes keep and show their clips (dimmed)", () => {
    setup(DETAIL({ tracks: [TRACK("v1", { visible: false })] }));
    expect(screen.getAllByTestId("lane-clip")).toHaveLength(2);
  });

  it("inspector trim + move-to-frame commit integer fields", async () => {
    setup();
    fireEvent.click(screen.getAllByTestId("lane-clip")[0]);
    const start = await screen.findByRole("textbox", { name: "Start frame" });
    fireEvent.change(start, { target: { value: "240" } });
    fireEvent.click(screen.getByRole("button", { name: "Move to frame" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.move",
      expect.objectContaining({ clip_id: "c0", start_frames: 240 })));
    fireEvent.change(screen.getByRole("textbox", { name: "Trim in frame" }),
      { target: { value: "6" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Trim out frame" }),
      { target: { value: "60" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply trim" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.trim",
      expect.objectContaining({ clip_id: "c0", in_frame: 6, out_frame: 60 })));
  });

  it("project/timeline switch clears selection", async () => {
    const d1 = DETAIL();
    const { rerender } = setup(d1);
    fireEvent.click(screen.getAllByTestId("lane-clip")[0]);
    await screen.findByTestId("clip-inspector");
    rerender(createElement(TimelineEditor, {
      detail: { ...d1, id: "t2" }, active: true, busy: false,
      onRun, onRemoveClip,
    }));
    expect(screen.queryByTestId("clip-inspector")).toBeNull();
  });

  it("selection clears when the clip disappears after refresh", async () => {
    const d1 = DETAIL();
    const { rerender } = setup(d1);
    fireEvent.click(screen.getAllByTestId("lane-clip")[0]);
    await screen.findByTestId("clip-inspector");
    rerender(createElement(TimelineEditor, {
      detail: { ...d1, clips: d1.clips.slice(1) },
      active: true, busy: false, onRun, onRemoveClip,
    }));
    expect(screen.queryByTestId("clip-inspector")).toBeNull();
  });

  /* ---------------- N1-C: split / ripple / trim / gaps ---------------- */

  it("Split at playhead commits clip.split with the revision pin", async () => {
    setup();
    const btn = screen.getByTestId("split-at-playhead");
    expect(btn).toHaveProperty("disabled", true);   // playhead at 0 = edge
    fireEvent.pointerDown(screen.getByTestId("timeline-ruler"),
      { clientX: 216 });                            // 216px / 6ppf = frame 36
    await waitFor(() =>
      expect(btn).toHaveProperty("disabled", false));
    fireEvent.click(btn);
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.split",
      expect.objectContaining({
        timeline_id: "t", clip_id: "c0", at_frame: 36,
        expected_timeline: { id: "t", revision: 3 } })));
  });

  it("S key on a clip splits it at the playhead; outside does nothing", async () => {
    setup();
    const clip = screen.getAllByTestId("lane-clip")[0];
    fireEvent.keyDown(clip, { key: "s" });          // playhead 0 — boundary
    expect(onRun).not.toHaveBeenCalled();
    fireEvent.pointerDown(screen.getByTestId("timeline-ruler"),
      { clientX: 216 });
    fireEvent.keyDown(clip, { key: "S" });
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.split",
      expect.objectContaining({ clip_id: "c0", at_frame: 36 })));
  });

  it("locked lanes refuse keyboard splits and hide the button", async () => {
    setup(DETAIL({ tracks: [TRACK("v1", { locked: true })] }));
    fireEvent.pointerDown(screen.getByTestId("timeline-ruler"),
      { clientX: 216 });
    expect(screen.getByTestId("split-at-playhead"))
      .toHaveProperty("disabled", true);
    fireEvent.keyDown(screen.getAllByTestId("lane-clip")[0], { key: "s" });
    expect(onRun).not.toHaveBeenCalled();
  });

  it("ripple toggle flows to remove and inspector trim payloads", async () => {
    setup();
    fireEvent.click(screen.getByTestId("ripple-toggle"));
    // Delete now carries the ripple flag to the confirm dialog.
    fireEvent.keyDown(screen.getAllByTestId("lane-clip")[0],
      { key: "Delete" });
    expect(onRemoveClip).toHaveBeenCalledWith(
      expect.objectContaining({ id: "c0" }), true);
    // Inspector trim gets ripple_scope + the revision pin.
    fireEvent.click(screen.getAllByTestId("lane-clip")[0]);
    fireEvent.change(await screen.findByRole("textbox",
      { name: "Trim out frame" }), { target: { value: "60" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply trim" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.trim",
      expect.objectContaining({
        clip_id: "c0", out_frame: 60, ripple_scope: "track",
        expected_timeline: { id: "t", revision: 3 } })));
  });

  it("trim-handle drag commits a source-frame out_frame on pointerup", async () => {
    setup();
    stubLaneRects();
    const handle = screen.getAllByTestId("trim-handle")[0];
    fireEvent.pointerDown(handle, { clientX: 432, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 300 }); // 300px = frame 50
    const status = await screen.findByTestId("trim-status");
    expect(status.textContent).toMatch(/source frame 50/);
    fireEvent.pointerUp(window, { clientX: 300 });
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.trim",
      expect.objectContaining({
        timeline_id: "t", clip_id: "c0", out_frame: 50,
        expected_timeline: { id: "t", revision: 3 } })));
  });

  it("trim drag past the neighbor shows the overlap reason and doesn't commit", async () => {
    // Longer source (200f) leaves head-room to grow into c1@72.
    setup(DETAIL({ clips: [
      CLIP("c0", { src_frames: 200 }), CLIP("c1", { start_frames: 72 }),
    ] }));
    stubLaneRects();
    const handle = screen.getAllByTestId("trim-handle")[0];
    fireEvent.pointerDown(handle, { clientX: 432, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 480 }); // frame 80 → overlaps c1
    const err = await screen.findByTestId("drop-error");
    expect(err.textContent).toMatch(/overlap|Ripple/i);
    fireEvent.pointerUp(window, { clientX: 480 });
    expect(onRun).not.toHaveBeenCalledWith(
      "timeline.clip.trim", expect.anything());
  });

  it("with ripple on, the same growth commits with ripple_scope", async () => {
    setup(DETAIL({ clips: [
      CLIP("c0", { src_frames: 200 }), CLIP("c1", { start_frames: 72 }),
    ] }));
    stubLaneRects();
    fireEvent.click(screen.getByTestId("ripple-toggle"));
    const handle = screen.getAllByTestId("trim-handle")[0];
    fireEvent.pointerDown(handle, { clientX: 432, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 480 });
    fireEvent.pointerUp(window, { clientX: 480 });
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.clip.trim",
      expect.objectContaining({
        clip_id: "c0", out_frame: 80, ripple_scope: "track" })));
  });

  it("Escape cancels a trim drag — no commit", async () => {
    setup();
    stubLaneRects();
    const handle = screen.getAllByTestId("trim-handle")[0];
    fireEvent.pointerDown(handle, { clientX: 432, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 300 });
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.pointerUp(window, { clientX: 300 });
    expect(onRun).not.toHaveBeenCalled();
  });

  it("gap indicators render explicit gaps and click-to-close", async () => {
    setup(DETAIL({ clips: [
      CLIP("c0"), CLIP("c1", { start_frames: 96 }),
    ] }));
    const gap = await screen.findByTestId("lane-gap");
    expect(gap.getAttribute("title")).toMatch(/24 frames/);
    fireEvent.click(gap);
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.gap.close",
      expect.objectContaining({
        timeline_id: "t", track_id: "v1", at_frame: 84,
        expected_timeline: { id: "t", revision: 3 } })));
  });

  it("Insert space commits gap.insert on the first editable lane", async () => {
    setup();
    fireEvent.pointerDown(screen.getByTestId("timeline-ruler"),
      { clientX: 60 });                             // playhead → frame 10
    fireEvent.change(screen.getByRole("textbox",
      { name: "Insert space frames" }), { target: { value: "10" } });
    fireEvent.click(screen.getByTestId("insert-space"));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.gap.insert",
      expect.objectContaining({
        track_id: "v1", at_frame: 10, frames: 10 })));
  });

  it("Close gap commits gap.close at the playhead", async () => {
    setup(DETAIL({ clips: [
      CLIP("c0"), CLIP("c1", { start_frames: 96 }),
    ] }));
    fireEvent.pointerDown(screen.getByTestId("timeline-ruler"),
      { clientX: 480 });                            // playhead → frame 80
    fireEvent.click(screen.getByTestId("close-gap"));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.gap.close",
      expect.objectContaining({ track_id: "v1", at_frame: 80 })));
  });

  it("locked lane disables gap buttons and space ops", () => {
    setup(DETAIL({
      tracks: [TRACK("v1", { locked: true })],
      clips: [CLIP("c0"), CLIP("c1", { start_frames: 96 })],
    }));
    expect(screen.getByTestId("lane-gap")).toHaveProperty("disabled", true);
    expect(screen.getByTestId("insert-space"))
      .toHaveProperty("disabled", true);
    expect(screen.getByTestId("close-gap")).toHaveProperty("disabled", true);
  });

  it("empty timeline shows the beginner empty-state", () => {
    setup(DETAIL({ clips: [] }));
    expect(screen.getByTestId("editor-empty").textContent)
      .toMatch(/Storyboard/);
  });

  it("archived timelines are read-only — no drag, no lane add", () => {
    setup(DETAIL(), false);
    expect(screen.queryByRole("button", { name: "+ Add lane" })).toBeNull();
    const clip = screen.getAllByTestId("lane-clip")[0];
    fireEvent.pointerDown(clip, { clientX: 30, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 200, clientY: 10 });
    fireEvent.pointerUp(window, { clientX: 200, clientY: 10 });
    expect(onRun).not.toHaveBeenCalled();
  });

  it("undo/redo buttons fire the commands with the timeline pin", async () => {
    setup(DETAIL({
      undo_label: "Trim clip", redo_label: null, revision: 9 }));
    const undo = screen.getByTestId("undo-btn");
    const redo = screen.getByTestId("redo-btn");
    expect(redo).toHaveProperty("disabled", true);
    fireEvent.click(undo);
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.undo", expect.objectContaining({
        timeline_id: "t",
        expected_timeline: expect.objectContaining({ revision: 9 }),
      })));
  });

  it("undo is disabled when the stack is empty", () => {
    setup(DETAIL({ undo_label: null }));
    expect(screen.getByTestId("undo-btn")).toHaveProperty("disabled", true);
  });

  it("Ctrl+Z undoes and Ctrl+Shift+Z / Ctrl+Y redoes", async () => {
    setup(DETAIL({ undo_label: "x", redo_label: "y" }));
    fireEvent.keyDown(window, { key: "z", ctrlKey: true });
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.undo", expect.anything()));
    fireEvent.keyDown(window, { key: "Z", ctrlKey: true, shiftKey: true });
    await waitFor(() => expect(onRun).toHaveBeenCalledWith(
      "timeline.redo", expect.anything()));
    fireEvent.keyDown(window, { key: "y", ctrlKey: true });
    await waitFor(() => expect(onRun).toHaveBeenCalledTimes(3));
  });

  it("Ctrl+Z inside a text field does NOT undo", async () => {
    setup(DETAIL({ undo_label: "x" }));
    const input = document.createElement("input");
    document.body.appendChild(input);
    fireEvent.keyDown(input, { key: "z", ctrlKey: true });
    await new Promise((r) => setTimeout(r, 30));
    expect(onRun).not.toHaveBeenCalled();
    input.remove();
  });

  it("viewport culling mounts only clips in the scrolled window", () => {
    // 300 clips × 72f each = 21600f; ppf 6 → 129 600px content — far
    // wider than any viewport. jsdom reports clientWidth 0, so the
    // first mount renders everything; after a fake scroll the window
    // collapses to the visible range.
    const clips = Array.from({ length: 300 }, (_, i) =>
      CLIP(`c${i}`, { start_frames: i * 72 }));
    const { container } = setup(DETAIL({ clips }));
    expect(container.querySelectorAll("[data-testid='lane-clip']")
      .length).toBe(300);
    const scroller = screen.getByTestId("timeline-scroll");
    Object.defineProperty(scroller, "clientWidth",
      { value: 1200, configurable: true });
    Object.defineProperty(scroller, "scrollLeft",
      { value: 60_000, writable: true, configurable: true });
    fireEvent.scroll(scroller);
    const mounted = container.querySelectorAll(
      "[data-testid='lane-clip']").length;
    expect(mounted).toBeLessThan(300);
    expect(mounted).toBeGreaterThan(0);
  });

  it("audio clips fetch waveform manifest+slice; video clips do not", async () => {
    const manifest = {
      format: "ade-wave/1", built_sha256: "ab".repeat(32),
      sample_rate: 48000, channels: 2, samples: 48000,
      levels: [{ spb: 256, buckets: 188, sha256: "c".repeat(64) }],
    };
    const slice = {
      spb: 256, buckets: 188, bucket_start: 0, bucket_end: 20,
      channels: 2, sample_rate: 48000, peaks: [[[0, 1]], [[0, -1]]],
    };
    apiFetch.mockImplementation((url: string) =>
      Promise.resolve(url.includes("/level") ? slice : manifest));
    setup(DETAIL({
      tracks: [
        TRACK("v1"),
        TRACK("a1", { kind: "audio", name: "Snd", position: 1 }),
      ],
      clips: [
        CLIP("v0"),
        // 2 s @ 48 kHz source → 48 tl-frames at 24 fps.
        CLIP("au0", { track_id: "a1", out_frame: 96000,
                      src_frames: 96000,
                      src_fps_num: 48000, src_fps_den: 1,
                      src_duration_s: 2 }),
      ],
    }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      expect.stringContaining("/waveform")));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      expect.stringContaining("/waveform/level")));
    // The audio clip mounted a canvas; the video clip's fetches are
    // zero waveform calls of its own.
    const calls = apiFetch.mock.calls.map((c) => String(c[0]));
    expect(calls.filter((u) => u.includes("/waveform")))
      .toHaveLength(2); // manifest + level, one asset
  });

  it("audio clip keeps polling on 409 — no error label", async () => {
    apiFetch.mockRejectedValue(new Error("409: no waveform built"));
    setup(DETAIL({
      tracks: [TRACK("a1", { kind: "audio", position: 0 })],
      clips: [CLIP("au0", { track_id: "a1", out_frame: 96000,
                            src_frames: 96000,
                            src_fps_num: 48000, src_fps_den: 1,
                            src_duration_s: 2 })],
    }));
    await waitFor(() => expect(apiFetch).toHaveBeenCalledWith(
      expect.stringContaining("/waveform")));
    // Still polling for a build — label shows the hint, not an error.
    expect(screen.queryByText("waveform unavailable")).toBeNull();
    expect(screen.getByText("waveform…")).toBeTruthy();
  });
});
