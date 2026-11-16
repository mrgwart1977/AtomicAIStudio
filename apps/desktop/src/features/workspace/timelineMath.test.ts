// N1-B — pure editor-math coverage: extents mirror the backend's
// rational conversion, snapping/drop rules mirror commit validation,
// export eligibility mirrors v1_layout_reason.
import { describe, expect, it } from "vitest";
import {
  clipTlEnd, clipTlExtent, compatibleTrack, dropError,
  exportEligibility, exportMode, extentOverlap, fmtTc, laneClips, laneGaps,
  programEndFrames, snapStart, splitSrcOffset, tickInterval,
} from "./timelineMath";
import type { TimelineClip, TimelineDetail, TimelineTrack } from "../../api/types";

const TL = { fps_num: 24, fps_den: 1 } as TimelineDetail;

const TRACK = (id: string, over: Partial<TimelineTrack> = {}): TimelineTrack => ({
  id, project_id: "p", timeline_id: "t", kind: "video", name: id,
  position: 0, visible: true, locked: false, muted: false,
  created_at: "2026-01-01", ...over,
});

const CLIP = (id: string, over: Partial<TimelineClip> = {}): TimelineClip => ({
  id, project_id: "p", timeline_id: "t", track_id: "v1",
  start_frames: 0, position: 0,
  source_asset_id: "a", source_version_id: "v",
  source_sha256: "ab".repeat(32),
  in_frame: 0, out_frame: 72,
  src_frames: 72, src_fps_num: 24, src_fps_den: 1,
  src_duration_s: 3, src_has_audio: true, shot_id: null, scene_id: null,
  created_at: "2026-01-01", ...over,
});

describe("clip extents (source→timeline conversion)", () => {
  it("integer rate: 72f@24 on a 24fps grid is exactly 72 frames", () => {
    expect(clipTlExtent(CLIP("c"), TL)).toBeCloseTo(72, 9);
  });
  it("30000/1001 source on 24fps grid — rational extent, ceil end", () => {
    const c = CLIP("c", { out_frame: 60, src_fps_num: 30000, src_fps_den: 1001 });
    // 60 × (1001/30000) × 24 = 48.048 frames → occupies until frame 49
    expect(clipTlExtent(c, TL)).toBeCloseTo(48.048, 3);
    expect(clipTlEnd(c, TL)).toBe(49);
  });
  it("24000/1001 source on 24fps grid ≈ 1.001× extent", () => {
    const c = CLIP("c", { out_frame: 72, src_fps_num: 24000, src_fps_den: 1001 });
    expect(clipTlExtent(c, TL)).toBeCloseTo(72.072, 3);
  });
  it("non-integer timeline grid: 240/11 rate", () => {
    const tl = { fps_num: 240, fps_den: 11 } as TimelineDetail;
    const c = CLIP("c", { out_frame: 72 });
    // 72 × 1/24 × 240/11 = 720/11 ≈ 65.45
    expect(clipTlExtent(c, tl)).toBeCloseTo(65.4545, 3);
  });
});

describe("laneClips ordering + program end", () => {
  it("orders by (start_frames, id)", () => {
    const a = CLIP("b", { start_frames: 10 });
    const b = CLIP("a", { start_frames: 10 });
    const c = CLIP("c", { start_frames: 0 });
    expect(laneClips([a, b, c], "v1").map((x) => x.id)).toEqual(["c", "a", "b"]);
  });
  it("excludes clips on other lanes", () => {
    expect(laneClips([CLIP("x", { track_id: "v2" }), CLIP("y")], "v1")
      .map((c) => c.id)).toEqual(["y"]);
  });
  it("programEndFrames is the rightmost ceil end", () => {
    const d = { ...TL, clips: [
      CLIP("a", { start_frames: 0 }),
      CLIP("b", { start_frames: 100, out_frame: 60, src_fps_num: 30000,
                  src_fps_den: 1001 }),
    ] } as TimelineDetail;
    // b: 100 + 48.048 → 149
    expect(programEndFrames(d)).toBe(149);
  });
});

describe("snapStart", () => {
  it("rounds to the integer frame grid", () => {
    expect(snapStart(10.4, [], "c", TL, 6).start).toBe(10);
    expect(snapStart(10.6, [], "c", TL, 6).start).toBe(11);
  });
  it("snaps to a neighbor edge within the pixel threshold", () => {
    const other = CLIP("o", { start_frames: 48 }); // end = 120
    const r = snapStart(120.9, [other], "c", TL, 6); // 0.9f × 6ppf = 5.4px < 8
    expect(r).toEqual({ start: 120, snappedTo: "edge" });
  });
  it("ignores the dragged clip's own edges", () => {
    const self = CLIP("self", { start_frames: 48 });
    const r = snapStart(120.9, [self], "self", TL, 6);
    expect(r.snappedTo).toBe("frame");
    expect(r.start).toBe(121);
  });
  it("never returns below zero", () => {
    expect(snapStart(-3, [], "c", TL, 6).start).toBe(0);
  });
});

describe("dropError", () => {
  const moving = CLIP("m");
  it("refuses locked lanes", () => {
    expect(dropError(TRACK("v", { locked: true }), 0, moving, [], TL))
      .toMatch(/locked/);
  });
  it("refuses video clips on audio lanes (N2-A kind rule)", () => {
    expect(dropError(TRACK("a", { kind: "audio" }), 0, moving, [], TL,
                     "video")).toMatch(/audio clips only/);
    // an audio clip is accepted on an audio lane
    expect(dropError(TRACK("a", { kind: "audio" }), 0, moving, [], TL,
                     "audio")).toBeNull();
    // an audio clip is refused on a video lane
    expect(dropError(TRACK("v"), 0, moving, [], TL, "audio"))
      .toMatch(/audio lanes only/);
    expect(compatibleTrack(TRACK("a", { kind: "audio" }))).toBe(true);
    expect(compatibleTrack(TRACK("o", { kind: "overlay" }))).toBe(true);
  });
  it("refuses negative starts", () => {
    expect(dropError(TRACK("v"), -1, moving, [], TL)).toMatch(/frame 0/);
  });
  it("refuses same-lane overlap but ignores the dragged clip", () => {
    const lane = [CLIP("m"), CLIP("o", { start_frames: 200 })];
    expect(dropError(TRACK("v"), 150, moving, lane, TL)).toMatch(/overlap/);
    expect(dropError(TRACK("v"), 0, moving, lane, TL)).toBeNull();
  });
  it("allows gaps and separate-lane overlaps implicitly", () => {
    const lane = [CLIP("o", { start_frames: 200 })];
    expect(dropError(TRACK("v"), 100, moving, lane, TL)).toBeNull();
  });
});

describe("exportMode (N1-D composite eligibility)", () => {
  const det = (tracks: TimelineTrack[], clips: TimelineClip[]) =>
    ({ ...TL, tracks, clips, clips_length: clips.length } as TimelineDetail);

  it("sequential lane → sequential", () => {
    const d = det([TRACK("v1")], [CLIP("a"), CLIP("b", { start_frames: 72 })]);
    expect(exportMode(d).mode).toBe("sequential");
  });
  it("two populated lanes → composite with lane count", () => {
    const d = det([TRACK("v1"), TRACK("o1", { kind: "overlay", position: 1 })],
      [CLIP("a"), CLIP("b", { track_id: "o1" })]);
    const m = exportMode(d);
    expect(m.mode).toBe("composite");
    expect(m.lanes).toBe(2);
  });
  it("gapped single lane → composite", () => {
    const d = det([TRACK("v1")], [CLIP("a"), CLIP("b", { start_frames: 100 })]);
    expect(exportMode(d).mode).toBe("composite");
  });
  it("hidden lanes don't count — all-hidden → blocked", () => {
    const d = det([TRACK("v1", { visible: false })], [CLIP("a")]);
    const m = exportMode(d);
    expect(m.mode).toBe("blocked");
    expect(m.lanes).toBe(0);
    expect(m.reason).toMatch(/no visible lane/);
  });
  it("muted lanes still composite (video only)", () => {
    const d = det([TRACK("v1"), TRACK("o1", { kind: "overlay", position: 1,
                                            muted: true })],
      [CLIP("a"), CLIP("b", { track_id: "o1" })]);
    expect(exportMode(d).mode).toBe("composite");
  });
  it("empty timeline → blocked", () => {
    expect(exportMode(det([TRACK("v1")], [])).mode).toBe("blocked");
  });

  // ---------- N2-E — audio lanes composite; audibility reported ----
  it("video + audible audio lane → composite with 'tracks' audio", () => {
    const d = det([TRACK("v1"), TRACK("a1", { kind: "audio", position: 1,
                                           solo: false })],
      [CLIP("a"), CLIP("b", { track_id: "a1" })]);
    const m = exportMode(d);
    expect(m.mode).toBe("composite");
    expect(m.audio).toBe("tracks");
  });
  it("audio-only timeline composites — black canvas + mix", () => {
    const d = det([TRACK("a1", { kind: "audio" })],
      [CLIP("a", { track_id: "a1" })]);
    const m = exportMode(d);
    expect(m.mode).toBe("composite");
    expect(m.audio).toBe("tracks");
  });
  it("muted audio lane → video's own audio still counts ('embedded')", () => {
    const d = det([TRACK("v1"),
                   TRACK("a1", { kind: "audio", position: 1,
                                 muted: true })],
      [CLIP("a"), CLIP("b", { track_id: "a1" })]);
    const m = exportMode(d);
    expect(m.mode).toBe("composite");
    expect(m.audio).toBe("embedded");
  });
  it("every lane muted → audio reported 'none'", () => {
    const d = det([TRACK("v1", { muted: true }),
                   TRACK("a1", { kind: "audio", position: 1,
                                 muted: true })],
      [CLIP("a"), CLIP("b", { track_id: "a1" })]);
    const m = exportMode(d);
    expect(m.mode).toBe("composite");
    expect(m.audio).toBe("none");
  });
  it("soloed video lane silences the audio lane → 'embedded'", () => {
    const d = det([TRACK("v1", { solo: true }),
                   TRACK("a1", { kind: "audio", position: 1 })],
      [CLIP("a"), CLIP("b", { track_id: "a1" })]);
    const m = exportMode(d);
    expect(m.audio).toBe("embedded");
  });
  it("unsatisfied crossfade on an audible lane → blocked", () => {
    const A = CLIP("A", { track_id: "a1" });
    const B = CLIP("B", { track_id: "a1", start_frames: 48 });
    const d = { ...det([TRACK("a1", { kind: "audio" })], [A, B]),
      crossfades: [{ id: "x1", out_clip_id: "A", in_clip_id: "B",
                     tl_start: 40, tl_frames: 8, curve: "equal_power",
                     satisfied: false }] } as TimelineDetail;
    const m = exportMode(d);
    expect(m.mode).toBe("blocked");
    expect(m.reason).toMatch(/crossfade/);
  });
  it("unsatisfied crossfade on a muted lane is inert — still composite", () => {
    const A = CLIP("A", { track_id: "a1" });
    const B = CLIP("B", { track_id: "a1", start_frames: 48 });
    const d = { ...det([TRACK("v1"),
                        TRACK("a1", { kind: "audio", position: 1,
                                      muted: true })], [CLIP("v"), A, B]),
      crossfades: [{ id: "x1", out_clip_id: "A", in_clip_id: "B",
                     tl_start: 40, tl_frames: 8, curve: "equal_power",
                     satisfied: false }] } as TimelineDetail;
    expect(exportMode(d).mode).toBe("composite");
  });
});

describe("exportEligibility (mirrors v1_layout_reason)", () => {
  const det = (tracks: TimelineTrack[], clips: TimelineClip[]) =>
    ({ ...TL, tracks, clips, clips_length: clips.length } as TimelineDetail);

  it("sequential video lane exports", () => {
    const d = det([TRACK("v1")],
      [CLIP("a"), CLIP("b", { start_frames: 72 })]);
    expect(exportEligibility(d).ok).toBe(true);
  });
  it("empty timeline exports", () => {
    expect(exportEligibility(det([TRACK("v1")], [])).ok).toBe(true);
  });
  it("two populated lanes need N1-D", () => {
    const d = det([TRACK("v1"), TRACK("o1", { kind: "overlay", position: 1 })],
      [CLIP("a"), CLIP("b", { track_id: "o1" })]);
    const r = exportEligibility(d);
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/multiple tracks/);
  });
  it("a gap refuses with a gap-specific reason", () => {
    const d = det([TRACK("v1")],
      [CLIP("a"), CLIP("b", { start_frames: 100 })]);
    expect(exportEligibility(d).reason).toMatch(/contiguous|gaps/);
  });
  it("hidden sole lane refuses", () => {
    const d = det([TRACK("v1", { visible: false })], [CLIP("a")]);
    expect(exportEligibility(d).reason).toMatch(/hidden/);
  });
  it("muted sole lane refuses", () => {
    const d = det([TRACK("v1", { muted: true })], [CLIP("a")]);
    expect(exportEligibility(d).reason).toMatch(/muted/);
  });
  it("non-video sole lane refuses", () => {
    const d = det([TRACK("o1", { kind: "overlay" })], [CLIP("a", { track_id: "o1" })]);
    expect(exportEligibility(d).reason).toMatch(/overlay/);
  });
  it("mixed-rate contiguous lane still exports (ceil packing)", () => {
    // 48f@24 (end 48) + 60f@30000/1001 starting at 48 (end 96.048)
    // + 48f@24 packed at ceil(96.048)=97 — contiguous under the rule.
    const d = det([TRACK("v1")], [
      CLIP("a", { out_frame: 48 }),
      CLIP("b", { start_frames: 48, out_frame: 60,
                  src_fps_num: 30000, src_fps_den: 1001 }),
      CLIP("c", { start_frames: 97, out_frame: 48 }),
    ]);
    expect(exportEligibility(d).ok).toBe(true);
  });
});

describe("N1-C — laneGaps (mirrors the backend's ceil packing)", () => {
  it("finds mid-lane gaps on the integer grid", () => {
    const lane = [CLIP("a"), CLIP("b", { start_frames: 96 })];
    // a: 0–72, b: 96–168 → gap [72, 96) = 24 frames
    expect(laneGaps(lane, TL)).toEqual([{ start: 72, frames: 24 }]);
  });
  it("reports a leading gap when the lane doesn't start at 0", () => {
    expect(laneGaps([CLIP("a", { start_frames: 48 })], TL))
      .toEqual([{ start: 0, frames: 48 }]);
  });
  it("contiguous and empty lanes have no gaps", () => {
    expect(laneGaps([CLIP("a"), CLIP("b", { start_frames: 72 })], TL))
      .toEqual([]);
    expect(laneGaps([], TL)).toEqual([]);
  });
  it("gaps under fractional extents use ceil boundaries", () => {
    // a occupies [0, 48.048) → next free integer slot is 49
    const a = CLIP("a", { out_frame: 60,
      src_fps_num: 30000, src_fps_den: 1001 });
    const b = CLIP("b", { start_frames: 60 });
    expect(laneGaps([a, b], TL)).toEqual([{ start: 49, frames: 11 }]);
  });
});

describe("N1-C — splitSrcOffset (mirrors backend source-boundary rule)", () => {
  it("same-rate: interior whole frames split, boundaries don't", () => {
    const c = CLIP("c"); // [0,72) on a 24fps timeline
    expect(splitSrcOffset(c, TL, 36)).toBe(36);
    expect(splitSrcOffset(c, TL, 0)).toBeNull();
    expect(splitSrcOffset(c, TL, 72)).toBeNull();
    expect(splitSrcOffset(c, TL, 100)).toBeNull();
    expect(splitSrcOffset(c, TL, 36.6)).toBeNull();
  });
  it("respects in_frame offsets", () => {
    const c = CLIP("c", { in_frame: 10, out_frame: 82 });
    expect(splitSrcOffset(c, TL, 30)).toBe(30); // src offset 30 → out=40
  });
  it("mixed rates only split on whole source frames", () => {
    const c = CLIP("c", { out_frame: 60, src_fps_num: 30000,
      src_fps_den: 1001 }); // extent 48.048 on a 24fps grid
    // tl frame 8 → src 8×30000/24024 ≈ 9.99 → not whole
    expect(splitSrcOffset(c, TL, 8)).toBeNull();
    // tl frame 1001 → would be whole but outside the clip anyway
    // a 48fps source on a 24fps grid: every tl frame = 2 src frames
    const d = CLIP("d", { out_frame: 96, src_fps_num: 48 });
    expect(splitSrcOffset(d, TL, 20)).toBe(40);
  });
});

describe("N1-C — extentOverlap (trim preview rule)", () => {
  it("growing into the next clip overlaps, shrinking never does", () => {
    const lane = [CLIP("me"), CLIP("next", { start_frames: 72 })];
    expect(extentOverlap(0, 50, lane, "me", TL)).toBe(false);
    expect(extentOverlap(0, 80, lane, "me", TL)).toBe(true);  // end 80 > 72
    expect(extentOverlap(0, 72, lane, "me", TL)).toBe(false); // touching ok
  });
  it("skips the trimmed clip itself", () => {
    const lane = [CLIP("me")];
    expect(extentOverlap(0, 200, lane, "me", TL)).toBe(false);
  });
});

describe("fmtTc + tickInterval", () => {
  it("formats seconds:frames", () => {
    expect(fmtTc(0, TL)).toBe("0:00");
    expect(fmtTc(72, TL)).toBe("3:00");
    expect(fmtTc(84, TL)).toBe("3:12");
  });
  it("tick interval keeps labels ≥ ~72px apart", () => {
    expect(tickInterval(TL, 6)).toBe(24);    // 1s ticks = 144px
    expect(tickInterval(TL, 1)).toBe(120);   // 5s ticks = 120px (1s=24px)
    expect(tickInterval(TL, 40)).toBe(24);   // still 1s when zoomed in
  });
});
