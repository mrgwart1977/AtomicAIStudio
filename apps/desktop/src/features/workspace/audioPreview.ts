/**
 * N2-F audio preview engine — WebAudio playback of the preview plan
 * (previewPlan.ts): audio-lane clips plus video-lane clips whose
 * source carries audio (embedded). The effective-gain model is the
 * same as the N2-E render (one envelope per clip, applied once).
 * Not sample-exact vs. the AAC render — see docs/n2f-preview.md
 * for measured tolerances.
 *
 * Lifecycle: `engine.load(detail)` → `play(fromS)` → `pause()` /
 * `stop()` / `scrubTo(s)`. `stop()` disconnects every node and
 * revokes object URLs via `dispose()`. The engine owns ONE
 * AudioContext for its lifetime (reused across plays).
 */
import { fetchAssetBytes } from "../../api/client";
import type { TimelineDetail } from "../../api/types";
import { buildPreviewPlan, PreviewClip, PreviewPlan } from "./previewPlan";

export type PreviewState = "idle" | "loading" | "ready" | "playing"
  | "scrubbing" | "error";

const ENVELOPE_HZ = 64;   // envelope samples per second (smooth fades)
/** N3-B — bounded audition window per scrub position. Each pointer
 * move schedules at most this much audio (per contributing clip), so
 * rapid drags never queue an unbounded source sequence. */
export const SCRUB_WINDOW_S = 0.35;
/** Min interval between audition generations — coalesces pointer
 * streams faster than this into the latest position. */
export const SCRUB_MIN_INTERVAL_MS = 45;
/** Bound for the latency sample rings — enough for a session of
 * acceptance runs without unbounded growth. */
const LAT_SAMPLES = 600;

interface LatSummary { n: number; p50: number; p95: number; max: number }

function pushLat(ring: number[], v: number): void {
  ring.push(v);
  if (ring.length > LAT_SAMPLES) ring.shift();
}

function summarizeLat(ring: number[]): LatSummary {
  if (!ring.length) return { n: 0, p50: 0, p95: 0, max: 0 };
  const s = [...ring].sort((a, b) => a - b);
  return {
    n: s.length,
    p50: s[Math.floor((s.length - 1) * 0.50)],
    p95: s[Math.floor((s.length - 1) * 0.95)],
    max: s[s.length - 1],
  };
}

export class AudioPreviewEngine {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private buffers = new Map<string, AudioBuffer>(); // clipId → buf
  private live: { src: AudioBufferSourceNode; gain: GainNode }[] = [];
  private plan: PreviewPlan | null = null;
  private anchorCtxT = 0;   // ctx.currentTime at play start
  private anchorTlS = 0;    // timeline seconds at play start
  private detailKey = "";
  private scrubActive = false;      // pointer held down / scrub mode
  private scrubWasPlaying = false;  // resume playback on release
  private scrubStartS = 0;          // position when the scrub began
  private scrubGen = 0;             // supersedes pending auditions
  private scrubTimer: ReturnType<typeof setTimeout> | null = null;
  private pendingS: number | null = null;
  private pendingTs = 0;            // handler-entry time of pending call
  private lastAuditionAt = 0;
  /** Live counters for acceptance measurement — read-only surface.
   * Latency rings are bounded (LAT_SAMPLES) and split the gesture→
   * onset path into objectively measurable phases:
   *   inputMs — pointer event.timeStamp → scrubAudition entry
   *   queueMs — handler entry → audition scheduling begins (coalesce)
   *   schedMs — the scheduling loop itself
   *   leadMs  — scheduling call → scheduled start (ctx.currentTime→at)
   * None of these is AUDIBLE latency: the true onset additionally
   * includes the device output path, estimated via ctx.outputLatency
   * (see latencyReport()) — reported as an estimate, never measured
   * audible latency. */
  readonly scrubStats = {
    calls: 0, auditions: 0, maxLive: 0,
    lastScheduleMs: 0, lastLeadMs: 0,
    inputMs: [] as number[], queueMs: [] as number[],
    schedMs: [] as number[], leadMs: [] as number[],
  };
  state: PreviewState = "idle";
  error: string | null = null;
  onStateChange: ((s: PreviewState) => void) | null = null;

  private setState(s: PreviewState) {
    this.state = s;
    this.onStateChange?.(s);
  }

  /** Decode all audible clips' source bytes (authenticated fetch).
   * Re-loads only when the snapshot key changes. */
  async load(detail: TimelineDetail, projectId: string): Promise<void> {
    this.plan = buildPreviewPlan(detail);
    // Buffer cache keys on source bytes only — moves/edits rebuild
    // the plan but don't re-decode unchanged sources.
    const key = this.plan.clips.map(c => `${c.clipId}:${c.sha256}`)
      .join("|");
    if (key === this.detailKey && this.buffers.size) {
      this.setState(this.scrubActive ? "scrubbing" : "ready");
      return;
    }
    this.stop();                       // clear any running graph
    this.disposeBuffers();
    this.detailKey = key;
    if (!this.plan.clips.length) {
      this.setState(this.scrubActive ? "scrubbing" : "ready");
      return;
    }
    this.setState("loading");
    try {
      const ctx = this.audioCtx();
      const settled = await Promise.allSettled(this.plan.clips.map(
        async (c) => {
          const bytes = await fetchAssetBytes(projectId, c.assetId);
          this.buffers.set(c.clipId, await ctx.decodeAudioData(bytes));
        }));
      const failed = settled.findIndex(r => r.status === "rejected");
      if (failed >= 0) {
        throw new Error("an audio source failed to load or decode "
                        + "(check the asset still exists)");
      }
      this.error = null;
      this.setState(this.scrubActive ? "scrubbing" : "ready");
    } catch (e) {
      this.error = e instanceof Error ? e.message : String(e);
      this.setState("error");
    }
  }

  private audioCtx(): AudioContext {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      this.master = this.ctx.createGain();
      this.master.connect(this.ctx.destination);
    }
    return this.ctx;
  }

  /** Current timeline position in seconds (live while playing). */
  positionS(): number {
    if (this.state !== "playing" || !this.ctx) return this.anchorTlS;
    return this.anchorTlS + (this.ctx.currentTime - this.anchorCtxT);
  }

  /** Start (or restart) playback at timeline second `fromS`.
   * Schedules each clip's source at its tl start with per-clip
   * effective-gain envelope. */
  async play(fromS: number): Promise<void> {
    if (!this.plan || this.state === "loading" || this.scrubActive) return;
    this.stopLive();
    const ctx = this.audioCtx();
    if (ctx.state === "suspended") await ctx.resume();
    const at = ctx.currentTime + 0.05;   // small schedule lead
    this.anchorCtxT = at;
    this.anchorTlS = fromS;
    for (const c of this.plan.clips) {
      const buf = this.buffers.get(c.clipId);
      if (!buf) continue;
      this.schedule(ctx, c, buf, at, fromS);
    }
    this.setState("playing");
  }

  private schedule(ctx: BaseAudioContext, c: PreviewClip, buf: AudioBuffer,
                   at: number, fromS: number, windowS?: number): void {
    const nodes = scheduleClip(ctx, c, buf, at, fromS, this.master!, windowS);
    if (!nodes) return;
    this.live.push(nodes);
    nodes.src.onended = () => {
      this.live = this.live.filter(l => l.src !== nodes.src);
    };
  }

  /** Pause — keeps buffers + plan; position() freezes. */
  pause(): number {
    const pos = this.positionS();
    this.stopLive();
    this.anchorTlS = pos;
    this.setState(this.buffers.size ? "ready" : "idle");
    return pos;
  }

  /** Stop and rewind to 0 (timeline position reset is the caller's
   * job — engine returns the held position). */
  stop(): void {
    this.stopLive();
    this.anchorTlS = 0;
    if (this.state === "playing") this.setState("ready");
  }

  /** Short-range scrub: reposition while playing by restarting the
   * graph at the new offset (≤ a few seconds of latency — measured,
   * not claimed sample-accurate). */
  async scrubTo(s: number): Promise<void> {
    if (this.state === "playing") { await this.play(s); return; }
    this.anchorTlS = s;
  }

  /* ---------- N3-B continuous scrubbing ---------- */

  /** Enter scrub mode (pointer captured on the ruler). Playback stops
   * and is remembered — endScrub() resumes it. Safe to call when the
   * engine is idle/loading; auditions start once buffers exist. */
  beginScrub(): void {
    if (this.scrubActive) return;
    this.scrubActive = true;
    this.scrubWasPlaying = this.state === "playing";
    this.scrubStartS = this.positionS();
    if (this.scrubWasPlaying) this.stopLive();
    this.scrubGen++;
    this.setState("scrubbing");
  }

  /** Audition a bounded window at timeline second `s`. Pointer streams
   * faster than SCRUB_MIN_INTERVAL_MS coalesce to the latest position;
   * each fired generation stops the previous one's nodes, so live
   * sources stay bounded by the clip count under the window.
   * `evTimeStamp` (the pointer event's DOMHighResTimeStamp) feeds the
   * input→handler latency ring. */
  scrubAudition(s: number, evTimeStamp?: number): void {
    if (!this.scrubActive || !this.plan) return;
    const now = performance.now();
    if (evTimeStamp !== undefined && evTimeStamp > 0) {
      pushLat(this.scrubStats.inputMs, now - evTimeStamp);
    }
    this.anchorTlS = s;
    this.scrubStats.calls++;
    this.pendingS = s;
    this.pendingTs = now;
    if (this.scrubTimer !== null) return;
    const wait = Math.max(0, SCRUB_MIN_INTERVAL_MS
                          - (now - this.lastAuditionAt));
    this.scrubTimer = setTimeout(() => {
      this.scrubTimer = null;
      void this.fireAudition();
    }, wait);
  }

  private async fireAudition(): Promise<void> {
    if (!this.scrubActive || !this.plan || this.pendingS === null) return;
    const s = this.pendingS;
    this.pendingS = null;
    const gen = ++this.scrubGen;
    this.stopLive();
    const t0 = performance.now();
    pushLat(this.scrubStats.queueMs, t0 - this.pendingTs);
    let ctx: AudioContext;
    try { ctx = this.audioCtx(); }
    catch {
      this.error = "audio preview is not available in this environment";
      return;
    }
    if (ctx.state === "suspended") await ctx.resume();
    if (gen !== this.scrubGen) return;      // superseded while awaiting
    const at = ctx.currentTime + 0.02;
    for (const c of clipsInWindow(this.plan, s, SCRUB_WINDOW_S)) {
      const buf = this.buffers.get(c.clipId);
      if (buf) this.schedule(ctx, c, buf, at, s, SCRUB_WINDOW_S);
    }
    this.lastAuditionAt = performance.now();
    this.scrubStats.auditions++;
    this.scrubStats.lastScheduleMs = this.lastAuditionAt - t0;
    this.scrubStats.lastLeadMs = (at - ctx.currentTime) * 1000;
    pushLat(this.scrubStats.schedMs, this.scrubStats.lastScheduleMs);
    pushLat(this.scrubStats.leadMs, this.scrubStats.lastLeadMs);
    this.scrubStats.maxLive = Math.max(this.scrubStats.maxLive,
                                       this.live.length);
  }

  /** Phase-latency summary for acceptance: p50/p95/max per measurable
   * phase (input→handler, handler→scheduling, schedule-loop cost,
   * scheduling→expected onset). `estOutputLatencyMs` is the
   * AudioContext's own device-latency estimate — the closest honest
   * proxy for onset→audible, reported separately because audible
   * latency is not objectively measurable here (no loopback). */
  latencyReport(): {
    input: LatSummary; queue: LatSummary; sched: LatSummary;
    lead: LatSummary; estOutputLatencyMs: number | null;
  } {
    return {
      input: summarizeLat(this.scrubStats.inputMs),
      queue: summarizeLat(this.scrubStats.queueMs),
      sched: summarizeLat(this.scrubStats.schedMs),
      lead: summarizeLat(this.scrubStats.leadMs),
      estOutputLatencyMs: this.ctx
        ? ((this.ctx.outputLatency ?? this.ctx.baseLatency ?? 0) * 1000)
        : null,
    };
  }

  /** Single bounded audition without entering scrub mode — click and
   * keyboard seeks. Sources self-terminate at the window end; the
   * next transport op kills them first if it lands earlier. */
  async auditionOnce(s: number): Promise<void> {
    if (!this.plan || this.state === "playing"
        || this.state === "loading" || this.scrubActive) return;
    this.stopLive();
    let ctx: AudioContext;
    try { ctx = this.audioCtx(); }
    catch {
      this.error = "audio preview is not available in this environment";
      return;
    }
    if (ctx.state === "suspended") await ctx.resume();
    const at = ctx.currentTime + 0.02;
    this.scrubStats.calls++;
    for (const c of clipsInWindow(this.plan, s, SCRUB_WINDOW_S)) {
      const buf = this.buffers.get(c.clipId);
      if (buf) this.schedule(ctx, c, buf, at, s, SCRUB_WINDOW_S);
    }
    this.scrubStats.auditions++;
    this.scrubStats.maxLive = Math.max(this.scrubStats.maxLive,
                                       this.live.length);
  }

  /** Leave scrub mode: every scheduled source is stopped immediately —
   * no stale audio survives pointer release. Resumes playback if the
   * scrub interrupted it (smooth scrub→playback transition). */
  endScrub(resume = true): void {
    if (!this.scrubActive) return;
    this.scrubActive = false;
    this.scrubGen++;
    if (this.scrubTimer !== null) {
      clearTimeout(this.scrubTimer);
      this.scrubTimer = null;
    }
    this.pendingS = null;
    this.stopLive();
    const pos = this.anchorTlS;
    const was = this.scrubWasPlaying && resume;
    this.scrubWasPlaying = false;
    this.setState(this.buffers.size ? "ready" : "idle");
    if (was) void this.play(pos);
  }

  /** Cancel the gesture rather than release it: restores the position
   * AND the transport state from before the scrub began — the
   * deterministic Escape/blur state. A scrub that interrupted
   * playback resumes from the pre-scrub position; a stopped/paused
   * scrub snaps the playhead back and stays stopped. Returns the
   * restored position, or null when no scrub was active. */
  cancelScrub(): number | null {
    if (!this.scrubActive) return null;
    const pos0 = this.scrubStartS;
    this.scrubActive = false;
    this.scrubGen++;
    if (this.scrubTimer !== null) {
      clearTimeout(this.scrubTimer);
      this.scrubTimer = null;
    }
    this.pendingS = null;
    this.stopLive();
    const was = this.scrubWasPlaying;
    this.scrubWasPlaying = false;
    this.anchorTlS = pos0;
    this.setState(this.buffers.size ? "ready" : "idle");
    if (was) void this.play(pos0);
    return pos0;
  }

  private stopLive(): void {
    for (const l of this.live) {
      try { l.src.onended = null; l.src.stop(); } catch { /* stopped */ }
      l.src.disconnect(); l.gain.disconnect();
    }
    this.live = [];
  }

  private disposeBuffers(): void {
    this.buffers.clear();
    this.detailKey = "";
  }

  /** Full teardown — close the AudioContext, drop buffers. */
  async dispose(): Promise<void> {
    this.scrubActive = false;
    this.scrubGen++;
    if (this.scrubTimer !== null) {
      clearTimeout(this.scrubTimer);
      this.scrubTimer = null;
    }
    this.pendingS = null;
    this.stopLive();
    this.disposeBuffers();
    this.plan = null;
    if (this.ctx) {
      try { await this.ctx.close(); } catch { /* already closed */ }
      this.ctx = null; this.master = null;
    }
    this.setState("idle");
  }
}

/** Clips that can contribute audio inside [s, s + windowS) — the
 * bounded candidate set a scrub audition may schedule. Clips already
 * carry mute/solo/visibility exclusion from buildPreviewPlan. */
export function clipsInWindow(plan: PreviewPlan, s: number,
                              windowS: number): PreviewClip[] {
  const end = s + windowS;
  return plan.clips.filter(c => c.tlStartS < end && c.tlEndS > s);
}

/** Schedule one clip's buffer through a per-clip GainNode — the
 * single envelope-application point shared by live playback, scrub
 * auditions, and the offline render used by acceptance measurement.
 * `windowS` bounds the scheduled duration (scrub auditions); omitted
 * means "play to the segment end". Returns the live nodes, or null
 * when nothing of the clip falls inside [fromS, ∞).
 */
export function scheduleClip(
  ctx: BaseAudioContext, c: PreviewClip, buf: AudioBuffer,
  at: number, fromS: number, dest: AudioNode, windowS?: number,
): { src: AudioBufferSourceNode; gain: GainNode } | null {
  if (fromS >= c.tlEndS) return null;              // already past
  const src = ctx.createBufferSource();
  src.buffer = buf;
  const gain = ctx.createGain();
  src.connect(gain).connect(dest);
  const lead = Math.max(0, c.tlStartS - fromS);
  const startAt = at + lead;
  const streamOffsetS = fromS > c.tlStartS ? fromS - c.tlStartS : 0;
  const srcOffset = c.srcStartS + streamOffsetS;
  const dur = Math.min(c.segDurS - streamOffsetS,
                       buf.duration - srcOffset,
                       (windowS ?? Infinity) - lead);
  if (dur <= 0) { src.disconnect(); gain.disconnect(); return null; }
  // Envelope: sample the plan's env() at ENVELOPE_HZ over the played
  // span — one setValueCurveAtTime per clip, applied exactly once.
  // Mono parity: the render up-mixes mono→stereo at the center-mix
  // level (L=R=in/√2 — measured −3.01 dB through aformat); WebAudio
  // up-mixes a mono buffer at full gain. Fold the same 1/√2 into the
  // envelope so preview matches the render's power convention.
  const monoScale = buf.numberOfChannels === 1 ? Math.SQRT1_2 : 1;
  const n = Math.max(2, Math.ceil(dur * ENVELOPE_HZ));
  const curve = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    curve[i] = c.env(streamOffsetS + (i / (n - 1)) * dur) * monoScale;
  }
  gain.gain.setValueCurveAtTime(curve, startAt, dur);
  src.start(startAt, srcOffset, dur);
  return { src, gain };
}

/** Render a plan through an OfflineAudioContext using the SAME
 * scheduleClip path live playback uses — the objective PCM the
 * acceptance driver compares against the FFmpeg export. `buffers`
 * is clipId → decoded AudioBuffer (the caller decodes, so this works
 * for both wav lanes and embedded mp4 audio). Renders `durationS`
 * seconds at `rate` Hz starting at timeline second `fromS` — nonzero
 * `fromS` exercises the seek path (envelopes sampled mid-stream,
 * exactly as a live scrub schedules them); `windowS` bounds each
 * clip's scheduled span exactly like a scrub audition. */
export async function renderPlanOffline(
  plan: PreviewPlan, buffers: Map<string, AudioBuffer>,
  durationS: number, rate = 48000, fromS = 0, windowS?: number,
): Promise<AudioBuffer> {
  const off = new OfflineAudioContext(2, Math.ceil(durationS * rate), rate);
  const master = off.createGain();
  master.connect(off.destination);
  for (const c of plan.clips) {
    const buf = buffers.get(c.clipId);
    if (buf) scheduleClip(off, c, buf, 0.02, fromS, master, windowS);
  }
  return off.startRendering();
}
