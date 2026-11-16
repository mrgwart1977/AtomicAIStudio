// N2-F — preview plan parity with the N2-E render contract:
// audibility (mute/solo/hidden), source-unit envelope anchors,
// crossfade handle extension, step/linear automation, fail-closed
// detached crossfades. Pure-math coverage — no AudioContext needed.
import { describe, expect, it } from "vitest";
import {
  audibleLanes, automationGainDb, buildPreviewPlan, fadeGain,
} from "./previewPlan";
import type {
  TimelineClip, TimelineCrossfade, TimelineDetail, TimelineTrack,
} from "../../api/types";

const TRACK = (id: string, over: Partial<TimelineTrack> = {}): TimelineTrack => ({
  id, project_id: "p", timeline_id: "t", kind: "audio", name: id,
  position: 0, visible: true, locked: false, muted: false, solo: false,
  gain_db: 0, created_at: "2026-01-01", ...over,
});

const CLIP = (id: string, track_id: string,
              over: Partial<TimelineClip> = {}): TimelineClip => ({
  id, project_id: "p", timeline_id: "t", track_id,
  start_frames: 0, position: 0,
  source_asset_id: `asset-${id}`, source_version_id: "v",
  source_sha256: "ab".repeat(32),
  in_frame: 0, out_frame: 48000,
  src_frames: 144000, src_fps_num: 48000, src_fps_den: 1,
  src_duration_s: 3, src_has_audio: true, gain_db: 0,
  fade_in_units: 0, fade_in_curve: "linear",
  fade_out_units: 0, fade_out_curve: "linear",
  automation_json: "[]", automation_interp: "linear",
  shot_id: null, scene_id: null,
  created_at: "2026-01-01", ...over,
});

const TL = (tracks: TimelineTrack[], clips: TimelineClip[],
            crossfades: TimelineCrossfade[] = []): TimelineDetail => ({
  id: "t", project_id: "p", name: "T", description: "",
  fps_num: 24, fps_den: 1, status: "active", revision: 1,
  created_at: "2026-01-01", updated_at: "2026-01-01",
  clips, tracks, crossfades, total_duration_s: 0,
  undo_label: null, redo_label: null,
});

const XF = (over: Partial<TimelineCrossfade> = {}): TimelineCrossfade => ({
  id: "x1", project_id: "p", timeline_id: "t",
  out_clip_id: "a", in_clip_id: "b",
  tl_start: 30, tl_frames: 12, curve: "equal_power", satisfied: true,
  created_at: "2026-01-01", ...over,
});

describe("fade_gain — curve contracts", () => {
  it("linear ramps 0→1 / 1→0; empty fade is unity", () => {
    expect(fadeGain(50, 100, "linear", false)).toBeCloseTo(0.5);
    expect(fadeGain(50, 100, "linear", true)).toBeCloseTo(0.5);
    expect(fadeGain(0, 0, "linear", false)).toBe(1);
  });
  it("equal_power is the sin/cos pair (unity power)", () => {
    expect(fadeGain(25, 100, "equal_power", false))
      .toBeCloseTo(Math.sin(Math.PI * 0.125));
    expect(fadeGain(25, 100, "equal_power", true))
      .toBeCloseTo(Math.cos(Math.PI * 0.125));
  });
  it("s_curve is the sin²/cos² S-curve", () => {
    expect(fadeGain(50, 100, "s_curve", false)).toBeCloseTo(0.5);
    expect(fadeGain(25, 100, "s_curve", false))
      .toBeCloseTo(Math.sin(Math.PI * 0.125) ** 2);
  });
  it("exponential follows the afade exp contract", () => {
    expect(fadeGain(50, 100, "exponential", false))
      .toBeCloseTo(Math.exp(-11.512925464970227 * 0.5));
    expect(fadeGain(0, 100, "exponential", true)).toBeCloseTo(1);
  });
});

describe("automationGainDb — linear vs step", () => {
  const pts = [{ at_frames: 0, gain_db: 0 },
               { at_frames: 10, gain_db: -12 }];
  it("linear interpolates in dB", () => {
    expect(automationGainDb(pts, 5, "linear")).toBeCloseTo(-6);
  });
  it("step holds until the next point", () => {
    expect(automationGainDb(pts, 5, "step")).toBe(0);
    expect(automationGainDb(pts, 15, "step")).toBe(-12);
  });
});

describe("audibleLanes — N2-C precedence", () => {
  it("muted and hidden lanes are out; solo set silences the rest", () => {
    const d = TL([TRACK("a"), TRACK("m", { muted: true }),
                  TRACK("h", { visible: false }), TRACK("s", { solo: true })],
                 []);
    const lanes = audibleLanes(d);
    expect(lanes.has("s")).toBe(true);
    expect(lanes.has("a")).toBe(false);   // non-soloed while solo set
    expect(lanes.has("m")).toBe(false);
    expect(lanes.has("h")).toBe(false);
  });
  it("no solo set → all visible+unmuted lanes audible", () => {
    const lanes = audibleLanes(TL([TRACK("a"), TRACK("b")], []));
    expect(lanes.size).toBe(2);
  });
});

describe("buildPreviewPlan — schedule + envelopes", () => {
  it("schedules audible clips; video without audio is skipped", () => {
    const d = TL([TRACK("a1"), TRACK("v1", { kind: "video" })],
                 [CLIP("c1", "a1"),
                  CLIP("c2", "a1", { start_frames: 24 }),
                  CLIP("v", "v1", { src_fps_num: 24,
                                    src_has_audio: false })]);
    const p = buildPreviewPlan(d);
    expect(p.clips.map(c => c.clipId)).toEqual(["c1", "c2"]);
    expect(p.clips[0].tlStartS).toBeCloseTo(0);
    expect(p.clips[1].tlStartS).toBeCloseTo(1);
    expect(p.clips[0].segDurS).toBeCloseTo(1);
  });

  it("video-lane clip with audio contributes embedded stream at "
     + "track gain only", () => {
    const d = TL([TRACK("v1", { kind: "video", gain_db: -6 })],
                 [CLIP("v", "v1", { src_fps_num: 24, in_frame: 24,
                                    out_frame: 72, src_frames: 96,
                                    start_frames: 12 })]);
    const p = buildPreviewPlan(d);
    expect(p.clips.length).toBe(1);
    const c = p.clips[0];
    expect(c.embedded).toBe(true);
    expect(c.tlStartS).toBeCloseTo(0.5, 4);
    expect(c.tlEndS).toBeCloseTo(2.5, 4);
    expect(c.srcStartS).toBeCloseTo(1.0, 4);
    expect(c.segDurS).toBeCloseTo(2.0, 4);
    expect(c.env(0.5)).toBeCloseTo(10 ** (-6 / 20), 3);
    expect(c.env(1.9)).toBeCloseTo(10 ** (-6 / 20), 3);
  });

  it("embedded entry honours mute/solo/visible like export", () => {
    const mk = (t: Partial<TimelineTrack>) =>
      TL([TRACK("v1", { kind: "video", ...t })],
         [CLIP("v", "v1", { src_fps_num: 24, out_frame: 48 })]);
    expect(buildPreviewPlan(mk({ muted: true })).clips).toEqual([]);
    expect(buildPreviewPlan(mk({ visible: false })).clips).toEqual([]);
    const solo = TL([TRACK("v1", { kind: "video" }),
                     TRACK("a1", { kind: "audio", solo: true })],
        [CLIP("v", "v1", { src_fps_num: 24, out_frame: 48 }),
         CLIP("a", "a1")]);
    expect(buildPreviewPlan(solo).clips.map(c => c.clipId))
      .toEqual(["a"]);
  });

  it("clip+track dB sum once into the envelope", () => {
    const d = TL([TRACK("a1", { gain_db: -6 })],
                 [CLIP("c1", "a1", { gain_db: -6 })]);
    const p = buildPreviewPlan(d);
    // −12 dB summed → 10^(-12/20) ≈ 0.251, applied once
    expect(p.clips[0].env(0.5)).toBeCloseTo(10 ** (-12 / 20), 3);
  });

  it("fade-in envelope ramps at the stream start (source units)", () => {
    const d = TL([TRACK("a1")],
                 [CLIP("c1", "a1", { fade_in_units: 24000,
                                     fade_in_curve: "linear" })]);
    const p = buildPreviewPlan(d);
    // 24000-sample fade = first 0.5 s of the stream
    expect(p.clips[0].env(0.125)).toBeCloseTo(0.25, 2);
    expect(p.clips[0].env(0.25)).toBeCloseTo(0.5, 2);
    expect(p.clips[0].env(0.75)).toBeCloseTo(1, 2);   // past the fade
  });

  it("step automation holds, linear ramps", () => {
    const base = { automation_json: JSON.stringify(
      [{ at_frames: 0, gain_db: 0 },
       { at_frames: 24, gain_db: -12 }]) };
    const step = buildPreviewPlan(TL([TRACK("a1")],
      [CLIP("c1", "a1", { ...base, automation_interp: "step" })]));
    const lin = buildPreviewPlan(TL([TRACK("a1")],
      [CLIP("c1", "a1", { ...base })]));
    // t=0.5s ↔ tl frame 12 (24fps) — midpoint
    expect(step.clips[0].env(0.5)).toBeCloseTo(1, 2);          // held
    expect(lin.clips[0].env(0.5)).toBeCloseTo(10 ** (-6 / 20), 2);
  });

  it("satisfied crossfade extends handles + anchors both envelopes", () => {
    // A [0,72f) out=72000, B at 36f in=48000 — window [30,42)
    const d = TL([TRACK("a1")],
      [CLIP("a", "a1", { out_frame: 72000 }),
       CLIP("b", "a1", { start_frames: 36, in_frame: 48000,
                         out_frame: 96000 })],
      [XF({ out_clip_id: "a", in_clip_id: "b" })]);
    const p = buildPreviewPlan(d);
    const a = p.clips.find(c => c.clipId === "a")!;
    const b = p.clips.find(c => c.clipId === "b")!;
    // A extends 6 tl-frames past its 72f end (tail handle)
    expect(a.tlEndS).toBeCloseTo(42 / 24, 4);
    // B pulls back 6 tl-frames (head handle) → starts at window S
    expect(b.tlStartS).toBeCloseTo(30 / 24, 4);
    expect(b.srcStartS).toBeCloseTo((48000 - 12000) / 48000, 4);
    // midpoint: cos(π/4)·sin(π/4) envelopes ≈ 0.707 each
    const mid = 36 / 24;
    expect(a.env(mid - a.tlStartS)).toBeCloseTo(Math.SQRT1_2, 2);
    expect(b.env(mid - b.tlStartS)).toBeCloseTo(Math.SQRT1_2, 2);
    // B's own fade-in starts at stream offset 0 (the window start)
    expect(b.env(0.01)).toBeLessThan(0.1);
  });

  it("detached crossfade on an audible lane fails closed", () => {
    const d = TL([TRACK("a1")],
      [CLIP("a", "a1"), CLIP("b", "a1", { start_frames: 36 })],
      [XF({ out_clip_id: "a", in_clip_id: "b", satisfied: false })]);
    const p = buildPreviewPlan(d);
    expect(p.detachedXfs).toEqual(["x1"]);
  });

  it("unsatisfied pair on a muted lane is ignored entirely", () => {
    const d = TL([TRACK("a1", { muted: true })],
      [CLIP("a", "a1"), CLIP("b", "a1", { start_frames: 36 })],
      [XF({ out_clip_id: "a", in_clip_id: "b", satisfied: false })]);
    const p = buildPreviewPlan(d);
    expect(p.clips).toEqual([]);
    expect(p.detachedXfs).toEqual([]);
  });
});
