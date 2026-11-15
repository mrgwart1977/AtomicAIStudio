// N1-B — pure editor-math coverage: extents mirror the backend's
// rational conversion, snapping/drop rules mirror commit validation,
// export eligibility mirrors v1_layout_reason.
import { describe, expect, it } from "vitest";
import {
  clipTlEnd, clipTlExtent, compatibleTrack, dropError,
  exportEligibility, fmtTc, laneClips, programEndFrames, snapStart,
  tickInterval,
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
  src_duration_s: 3, shot_id: null, scene_id: null,
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
  it("refuses audio lanes (no audio sources yet)", () => {
    expect(dropError(TRACK("a", { kind: "audio" }), 0, moving, [], TL))
      .toMatch(/audio/);
    expect(compatibleTrack(TRACK("a", { kind: "audio" }))).toBe(false);
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
