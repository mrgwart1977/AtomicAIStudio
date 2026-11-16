import { describe, it, expect, vi } from "vitest";
import {
  AudioPreviewEngine, clipsInWindow, SCRUB_WINDOW_S,
  SCRUB_MIN_INTERVAL_MS,
} from "./audioPreview";
import type { PreviewClip, PreviewPlan } from "./previewPlan";
import type { TimelineDetail } from "../../api/types";

vi.mock("../../api/client", () => ({
  fetchAssetBytes: vi.fn(),
}));
import { fetchAssetBytes } from "../../api/client";

const clip = (id: string, tlStartS: number, tlEndS: number,
              env: (t: number) => number = () => 1): PreviewClip => ({
  clipId: id, assetId: "a-" + id, sha256: "s-" + id,
  tlStartS, tlEndS, srcStartS: 0, segDurS: tlEndS - tlStartS,
  rate: 48000, embedded: false, env,
});
const planOf = (...clips: PreviewClip[]): PreviewPlan =>
  ({ clips } as PreviewPlan);

/* --- fake WebAudio graph --- */
class FakeSrc {
  buffer: { duration: number; numberOfChannels: number } | null = null;
  started: { at: number; off: number; dur?: number } | null = null;
  stopped = false; onended: (() => void) | null = null;
  connect(d: unknown) { return d as never; }
  disconnect() { /* counted via ctx */ }
  start(at: number, off: number, dur?: number) {
    this.started = { at, off, dur };
  }
  stop() { this.stopped = true; }
}
class FakeGain {
  curves: { n: number; t: number; d: number }[] = [];
  gain = {
    setValueCurveAtTime: (c: Float32Array, t: number, d: number) =>
      this.curves.push({ n: c.length, t, d }),
  };
  connect(d: unknown) { return d as never; }
  disconnect() { /* */ }
}
class FakeCtx {
  currentTime = 0; state: AudioContextState = "running";
  sources: FakeSrc[] = []; destination = {};
  createBufferSource() { const s = new FakeSrc(); this.sources.push(s); return s; }
  createGain() { return new FakeGain(); }
  async resume() { this.state = "running"; }
  async close() { this.state = "closed"; }
}
type EnginePriv = {
  ctx: FakeCtx; plan: PreviewPlan;
  buffers: Map<string, { duration: number; numberOfChannels: number }>;
  fireAudition: () => Promise<void>;
  live: { src: FakeSrc }[];
};
const wire = (plan: PreviewPlan, clips: string[]) => {
  const eng = new AudioPreviewEngine();
  const p = eng as unknown as EnginePriv;
  const ctx = new FakeCtx();
  p.ctx = ctx;
  p.plan = plan;
  p.buffers = new Map(clips.map((id) =>
    [id, { duration: 30, numberOfChannels: 2 }]));
  (eng as unknown as { master: unknown }).master = ctx.destination;
  return { eng, ctx };
};

describe("clipsInWindow", () => {
  const plan = planOf(
    clip("a", 0.0, 2.0), clip("b", 2.0, 4.0), clip("c", 10.0, 12.0));
  it("selects only clips intersecting [s, s+W)", () => {
    expect(clipsInWindow(plan, 0.5, 0.35).map(c => c.clipId)).toEqual(["a"]);
    expect(clipsInWindow(plan, 1.9, 0.35).map(c => c.clipId))
      .toEqual(["a", "b"]);           // boundary straddle
    expect(clipsInWindow(plan, 5.0, 0.35)).toEqual([]);   // gap → none
    expect(clipsInWindow(plan, 2.0, 0.35).map(c => c.clipId))
      .toEqual(["b"]);               // half-open: ends-at-s excluded
    expect(clipsInWindow(plan, 1.65, 0.35).map(c => c.clipId))
      .toEqual(["a"]);               // b starts exactly at s+W → out
    expect(clipsInWindow(plan, 1.7, 0.35).map(c => c.clipId))
      .toEqual(["a", "b"]);
  });
});

describe("scrub audition lifecycle", () => {
  it("schedules bounded windows only, one envelope each", async () => {
    const plan = planOf(clip("a", 0, 4), clip("b", 1, 5));
    const { eng, ctx } = wire(plan, ["a", "b"]);
    eng.beginScrub();
    eng.scrubAudition(1.2);            // window [1.2,1.55) hits both clips
    await (eng as unknown as EnginePriv).fireAudition();
    // FakeCtx manual fire: pendingS consumed
    expect(ctx.sources.length).toBe(2);
    for (const s of ctx.sources) {
      expect(s.started!.dur!).toBeLessThanOrEqual(SCRUB_WINDOW_S + 1e-9);
      expect(s.stopped).toBe(false);
    }
    expect(eng.scrubStats.maxLive).toBe(2);
  });

  it("each generation stops the previous — live nodes stay bounded",
    async () => {
      const plan = planOf(clip("a", 0, 60), clip("b", 0, 60));
      const { eng, ctx } = wire(plan, ["a", "b"]);
      eng.beginScrub();
      for (let i = 0; i < 12; i++) {
        eng.scrubAudition(i * 0.3);
        await (eng as unknown as EnginePriv).fireAudition();
      }
      // 12 gens × 2 clips = 24 sources created, but live set ≤ 2 —
      // every prior gen was stopped before the next scheduled.
      expect(ctx.sources.length).toBe(24);
      expect(ctx.sources.slice(0, -2).every(s => s.stopped)).toBe(true);
      expect(eng.scrubStats.maxLive).toBeLessThanOrEqual(2);
      expect(eng.state).toBe("scrubbing");
    });

  it("throttle coalesces rapid calls to the latest position", async () => {
    vi.useFakeTimers();
    try {
      const plan = planOf(clip("a", 0, 60));
      const { eng, ctx } = wire(plan, ["a"]);
      eng.beginScrub();
      eng.scrubAudition(1.0);            // fires immediately
      await vi.advanceTimersByTimeAsync(SCRUB_MIN_INTERVAL_MS + 1);
      for (let i = 0; i < 20; i++) eng.scrubAudition(2 + i * 0.01);
      await vi.advanceTimersByTimeAsync(SCRUB_MIN_INTERVAL_MS + 1);
      // 21 calls → ≤ 3 auditions (first + coalesced bursts)
      expect(eng.scrubStats.calls).toBe(21);
      expect(eng.scrubStats.auditions).toBeLessThanOrEqual(3);
      expect(ctx.sources.length).toBeLessThanOrEqual(3);
    } finally { vi.useRealTimers(); }
  });

  it("endScrub stops all live nodes — no stale audio", async () => {
    const plan = planOf(clip("a", 0, 60), clip("b", 0, 60));
    const { eng, ctx } = wire(plan, ["a", "b"]);
    eng.beginScrub();
    eng.scrubAudition(1);
    await (eng as unknown as EnginePriv).fireAudition();
    eng.endScrub();
    expect(ctx.sources.every(s => s.stopped)).toBe(true);
    expect((eng as unknown as EnginePriv).live.length).toBe(0);
    expect(eng.state).toBe("ready");
  });

  it("endScrub resumes playback when the scrub interrupted it",
    async () => {
      const plan = planOf(clip("a", 0, 60));
      const { eng, ctx } = wire(plan, ["a"]);
      await eng.play(10);
      eng.beginScrub();
      eng.scrubAudition(20);
      await (eng as unknown as EnginePriv).fireAudition();
      eng.endScrub();
      expect(eng.state).toBe("playing");
      // resumed sources play UNbounded (no window dur cap)
      const resumed = ctx.sources.filter(s => !s.stopped);
      expect(resumed.length).toBe(1);
      expect(resumed[0].started!.dur).toBeGreaterThan(SCRUB_WINDOW_S);
    });

  it("auditionOnce schedules a blip without entering scrub mode",
    async () => {
      const plan = planOf(clip("a", 0, 60));
      const { eng, ctx } = wire(plan, ["a"]);
      eng.state = "ready";
      await eng.auditionOnce(5);
      expect(ctx.sources.length).toBe(1);
      expect(ctx.sources[0].started!.dur!)
        .toBeLessThanOrEqual(SCRUB_WINDOW_S + 1e-9);
      expect(eng.state).toBe("ready");
    });

  it("auditionOnce is a no-op while playing", async () => {
    const plan = planOf(clip("a", 0, 60));
    const { eng, ctx } = wire(plan, ["a"]);
    await eng.play(0);
    const n0 = ctx.sources.length;
    await eng.auditionOnce(5);
    expect(ctx.sources.length).toBe(n0);
  });

  it("scrub in a gap schedules nothing; resume at release point",
    async () => {
      const plan = planOf(clip("a", 0, 2), clip("b", 8, 10));
      const { eng, ctx } = wire(plan, ["a", "b"]);
      eng.beginScrub();
      eng.scrubAudition(5);
      await (eng as unknown as EnginePriv).fireAudition();
      expect(ctx.sources.length).toBe(0);          // gap → silence
      eng.endScrub();
      expect(eng.positionS()).toBeCloseTo(5);
    });
});

describe("decode-error feedback", () => {
  it("load() maps a failed fetch/decode to state=error + message",
    async () => {
      vi.mocked(fetchAssetBytes).mockRejectedValue(new Error("404"));
      const eng = new AudioPreviewEngine();
      const detail = {
        id: "t1", project_id: "p1", name: "T", fps_num: 30, fps_den: 1,
        status: "active", revision: 1,
        tracks: [{ id: "tr1", timeline_id: "t1", kind: "audio",
                   name: "A1", sort_order: 0, visible: true, locked: false,
                   muted: false, solo: false, gain_db: 0,
                   automation_json: "[]", automation_interp: "linear" }],
        clips: [{ id: "c1", timeline_id: "t1", track_id: "tr1",
                  source_asset_id: "a1", source_sha256: "x",
                  source_rate: 48000, source_fps_num: 30, source_fps_den: 1,
                  start_frames: 0, in_frame: 0, out_frame: 48000,
                  gain_db: 0, automation_json: "[]",
                  automation_interp: "step",
                  fade_in_units: 0, fade_in_curve: "linear",
                  fade_out_units: 0, fade_out_curve: "linear",
                  src_has_audio: true }],
        crossfades: [],
      } as unknown as TimelineDetail;
      await eng.load(detail, "p1");
      expect(eng.state).toBe("error");
      expect(eng.error).toBeTruthy();   // surfaced to the UI's error span
    });
});
