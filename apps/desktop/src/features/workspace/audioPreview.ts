/**
 * N2-F audio preview engine — WebAudio playback of the preview plan
 * (previewPlan.ts). Audio-lane clips only; the effective-gain model
 * is the same as the N2-E render (one envelope per clip, applied
 * once). Not sample-exact vs. the AAC render — see docs/n2f-preview.md
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
  | "error";

const ENVELOPE_HZ = 64;   // envelope samples per second (smooth fades)

export class AudioPreviewEngine {
  private ctx: AudioContext | null = null;
  private master: GainNode | null = null;
  private buffers = new Map<string, AudioBuffer>(); // clipId → buf
  private live: { src: AudioBufferSourceNode; gain: GainNode }[] = [];
  private plan: PreviewPlan | null = null;
  private anchorCtxT = 0;   // ctx.currentTime at play start
  private anchorTlS = 0;    // timeline seconds at play start
  private detailKey = "";
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
    const key = this.plan.clips.map(c => `${c.clipId}:${c.sha256}`
      + `:${c.tlStartS}`).join("|");
    if (key === this.detailKey && this.buffers.size) {
      this.setState("ready");
      return;
    }
    this.stop();                       // clear any running graph
    this.disposeBuffers();
    this.detailKey = key;
    if (!this.plan.clips.length) { this.setState("ready"); return; }
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
      this.setState("ready");
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
    if (!this.plan || this.state === "loading") return;
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

  private schedule(ctx: AudioContext, c: PreviewClip, buf: AudioBuffer,
                   at: number, fromS: number): void {
    if (fromS >= c.tlEndS) return;                 // already past
    const src = ctx.createBufferSource();
    src.buffer = buf;
    const gain = ctx.createGain();
    src.connect(gain).connect(this.master!);
    const lead = Math.max(0, c.tlStartS - fromS);
    const startAt = at + lead;
    const streamOffsetS = fromS > c.tlStartS ? fromS - c.tlStartS : 0;
    const srcOffset = c.srcStartS + streamOffsetS;
    const dur = Math.min(c.segDurS - streamOffsetS,
                         buf.duration - srcOffset);
    if (dur <= 0) { src.disconnect(); gain.disconnect(); return; }
    // Envelope: sample the plan's env() at ENVELOPE_HZ over the played
    // span — one setValueCurveAtTime per clip, applied exactly once.
    const n = Math.max(2, Math.ceil(dur * ENVELOPE_HZ));
    const curve = new Float32Array(n);
    for (let i = 0; i < n; i++) {
      curve[i] = c.env(streamOffsetS + (i / (n - 1)) * dur);
    }
    gain.gain.setValueCurveAtTime(curve, startAt, dur);
    src.start(startAt, srcOffset, dur);
    this.live.push({ src, gain });
    src.onended = () => {
      this.live = this.live.filter(l => l.src !== src);
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
