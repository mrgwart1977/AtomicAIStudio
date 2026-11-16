/**
 * N2-F preview plan — pure schedule + envelope math that mirrors the
 * N2-E render contract (backend/app/domain/timeline_export.py):
 *
 *   stream = [in_frame, out_frame) extended into source handles by a
 *   satisfied crossfade; envelopes anchor in SOURCE units on the
 *   extended stream; clip+track gain sum in dB before the single
 *   pow(10,x/20) conversion; automation interpolates in dB ('linear')
 *   or holds ('step'); placement on the timeline is frame-exact.
 *
 * The preview samples the same effective-gain function into a
 * Float32Array the WebAudio engine applies with setValueCurveAtTime —
 * one envelope per clip, applied exactly once. It is the same model,
 * not a second mixing implementation.
 */
import type {
  TimelineClip, TimelineCrossfade, TimelineDetail, TimelineTrack,
} from "../../api/types";

/** Source-unit gain at one local position for a fade — ports
 * domain/timeline.fade_gain (t = pos/n in [0,1]). */
export function fadeGain(pos: number, n: number, curve: string,
                         out: boolean): number {
  if (n <= 0) return 1;
  const t = Math.min(Math.max(pos / n, 0), 1);
  switch (curve) {
    case "linear": return out ? 1 - t : t;
    case "equal_power":
      return out ? Math.cos(t * Math.PI / 2) : Math.sin(t * Math.PI / 2);
    case "s_curve": {
      const s = Math.sin(t * Math.PI / 2), c = Math.cos(t * Math.PI / 2);
      return out ? c * c : s * s;
    }
    default: // exponential — afade exp contract:
      // in e^{-11.5129(1−t)} / out e^{-11.5129·t}
      return Math.exp(-11.512925464970227 * (out ? t : 1 - t));
  }
}

export interface AutoPoint { at_frames: number; gain_db: number }

/** Interpolated dB at a clip-local tl-frame offset — ports
 * domain/timeline.automation_gain_db. */
export function automationGainDb(points: AutoPoint[], at: number,
                                 interp: string): number {
  if (!points.length) return 0;
  if (at <= points[0].at_frames) return points[0].gain_db;
  if (at >= points[points.length - 1].at_frames) {
    return points[points.length - 1].gain_db;
  }
  for (let i = 0; i < points.length - 1; i++) {
    const a = points[i], b = points[i + 1];
    if (a.at_frames <= at && at <= b.at_frames) {
      if (interp === "step") return a.gain_db;
      const span = b.at_frames - a.at_frames;
      const t = span === 0 ? 0 : (at - a.at_frames) / span;
      return a.gain_db + t * (b.gain_db - a.gain_db);
    }
  }
  return points[points.length - 1].gain_db;
}

export interface PreviewClip {
  clipId: string;
  assetId: string;
  sha256: string;
  /** Timeline seconds where the (extended) stream starts/ends. */
  tlStartS: number;
  tlEndS: number;
  /** Source seconds of the stream start (seg_start_src/rate). */
  srcStartS: number;
  /** Stream duration in source seconds. */
  segDurS: number;
  /** Source rate (samples/sec for audio lanes). */
  rate: number;
  /** Video-lane clip contributing its embedded audio — constant
   * track-gain envelope, no fades/automation/xf (render contract:
   * embedded entries carry track_db only). */
  embedded: boolean;
  /** Effective-gain(t) — t = seconds into the stream (0 = start). */
  env: (tS: number) => number;
}

interface PlanClip {
  clip: TimelineClip;
  track: TimelineTrack;
  rate: number;
  segStartSrc: number;
  segEndSrc: number;
  extentSrc: number;
  headSrc: number; headTl: number;
  startTl: number; extentTl: number;
  xf: { role: "in" | "out"; off: number; n: number; curve: string } | null;
}

const q = (v: number) => Math.floor(v + 0.5);

/** N2-C audibility — ports domain/timeline_export._audible:
 * hidden → silent; muted → silent; non-empty solo set silences the
 * rest. Preview only schedules AUDIBLE audio-lane clips. */
export function audibleLanes(detail: TimelineDetail): Set<string> {
  const solo = new Set(detail.tracks
    .filter(t => t.solo && t.kind !== "overlay").map(t => t.id));
  return new Set(detail.tracks.filter(t =>
    t.visible && !t.muted && (!solo.size || solo.has(t.id))).map(t => t.id));
}

/** Crossfade extension — ports domain/timeline_export._apply_xf
 * (window must already be satisfied; caller checks). */
function applyXf(x: TimelineCrossfade, outC: PlanClip, inC: PlanClip,
                 tlFps: number): void {
  const S = x.tl_start, E = S + x.tl_frames;
  const src = (tlF: number, rate: number) => q(tlF * rate / tlFps);
  const outTail = E - (outC.startTl + outC.extentTl);
  outC.segEndSrc += src(outTail, outC.rate);
  outC.xf = {
    role: "out", curve: x.curve,
    off: src(S - outC.startTl, outC.rate), n: src(E - S, outC.rate),
  };
  const head = inC.startTl - S;
  inC.headTl = head;
  inC.headSrc = src(head, inC.rate);
  inC.segStartSrc -= inC.headSrc;
  inC.xf = { role: "in", curve: x.curve, off: 0, n: src(E - S, inC.rate) };
}

export interface PreviewPlan {
  /** Audible-lane clips to schedule (sorted by tlStartS). */
  clips: PreviewClip[];
  /** Timeline fps (num/den). */
  tlFps: number;
  /** Program end in seconds (max clip end). */
  durationS: number;
  /** Unsatisfied crossfades on audible lanes (fail-closed list). */
  detachedXfs: string[];
}

/** Build the preview schedule from a timeline detail snapshot —
 * ports composite_plan's audio section: audible dedicated lanes get
 * the full envelope; audible video-lane clips with `src_has_audio`
 * contribute their embedded audio at constant track gain. */
export function buildPreviewPlan(detail: TimelineDetail): PreviewPlan {
  const tlFps = detail.fps_num / detail.fps_den;
  const audible = audibleLanes(detail);
  const byId = new Map<string, PlanClip>();
  const embedded: PreviewClip[] = [];
  for (const c of detail.clips) {
    const t = detail.tracks.find(tr => tr.id === c.track_id);
    if (!t || !audible.has(t.id)) continue;
    const srcRate = c.src_fps_num / c.src_fps_den;
    if (t.kind !== "audio") {
      // Video/overlay lane — embedded audio only, track gain is the
      // whole envelope (clip_db/fades/automation/xf don't apply).
      if (!c.src_has_audio || !srcRate) continue;
      const g = Math.pow(10, (t.gain_db || 0) / 20);
      const t0 = c.start_frames / tlFps;
      const dur = (c.out_frame - c.in_frame) / srcRate;
      embedded.push({
        clipId: c.id, assetId: c.source_asset_id,
        sha256: c.source_sha256,
        tlStartS: t0, tlEndS: t0 + dur,
        srcStartS: c.in_frame / srcRate, segDurS: dur,
        rate: 0, embedded: true, env: () => g,
      });
      continue;
    }
    // Audio lanes: src fps is the sample rate (N2-A unit contract).
    const rate = srcRate || 48000;
    byId.set(c.id, {
      clip: c, track: t, rate,
      segStartSrc: c.in_frame, segEndSrc: c.out_frame,
      extentSrc: c.out_frame - c.in_frame,
      headSrc: 0, headTl: 0,
      startTl: c.start_frames,
      extentTl: (c.out_frame - c.in_frame) * tlFps / rate,
      xf: null,
    });
  }
  const detachedXfs: string[] = [];
  const embeddedIds = new Set(embedded.map(c => c.clipId));
  for (const x of detail.crossfades) {
    const o = byId.get(x.out_clip_id), i = byId.get(x.in_clip_id);
    if (!o && !i
        && !embeddedIds.has(x.out_clip_id)
        && !embeddedIds.has(x.in_clip_id)) continue; // lane inaudible
    // Export raises on a crossfade touching an embedded (video-lane)
    // entry — fail closed identically here.
    if (!o || !i || !x.satisfied) {
      detachedXfs.push(x.id);
      continue;
    }
    applyXf(x, o, i, tlFps);
  }
  const clips: PreviewClip[] = [];
  for (const p of byId.values()) {
    const c = p.clip;
    const dbSum = c.gain_db + p.track.gain_db;
    const auto = parseAutomation(c.automation_json);
    const interp = c.automation_interp || "linear";
    const t0 = (p.startTl - p.headTl) / tlFps;
    const segUnits = p.segEndSrc - p.segStartSrc;
    const env = (tS: number): number => {
      const u = tS * p.rate;               // stream-local source idx
      if (u < 0 || u >= segUnits + 1) return 0;
      // automation keyed at clip-local tl frames: occupancy starts
      // at head_src on the extended stream.
      const uTl = (u - p.headSrc) * tlFps / p.rate;
      let gDb = dbSum + automationGainDb(auto, uTl, interp);
      let g = Math.pow(10, gDb / 20);
      const fin = c.fade_in_units, fout = c.fade_out_units;
      if (fin > 0 && u < p.headSrc + fin) {
        g *= fadeGain(u - p.headSrc, fin, c.fade_in_curve, false);
      }
      if (fout > 0 && u >= p.headSrc + p.extentSrc - fout) {
        g *= fadeGain(u - (p.headSrc + p.extentSrc - fout), fout,
                      c.fade_out_curve, true);
      }
      if (p.xf && u >= p.xf.off && u < p.xf.off + p.xf.n) {
        g *= fadeGain(u - p.xf.off, p.xf.n, p.xf.curve,
                      p.xf.role === "out");
      }
      return g;
    };
    clips.push({
      clipId: c.id, assetId: c.source_asset_id, sha256: c.source_sha256,
      tlStartS: t0, tlEndS: t0 + segUnits / p.rate,
      srcStartS: p.segStartSrc / p.rate,
      segDurS: segUnits / p.rate,
      rate: p.rate, embedded: false, env,
    });
  }
  clips.push(...embedded);
  clips.sort((a, b) => a.tlStartS - b.tlStartS);
  const durationS = Math.max(0, ...detail.clips.map(c =>
    (c.start_frames
     + (c.out_frame - c.in_frame) * tlFps
       / (c.src_fps_num / c.src_fps_den || tlFps)) / tlFps));
  return { clips, tlFps, durationS, detachedXfs };
}

function parseAutomation(json: string): AutoPoint[] {
  try {
    const pts = JSON.parse(json || "[]");
    return Array.isArray(pts)
      ? pts.map(p => ({ at_frames: Number(p.at_frames),
                        gain_db: Number(p.gain_db) }))
           .filter(p => Number.isFinite(p.at_frames)
                     && Number.isFinite(p.gain_db))
           .sort((a, b) => a.at_frames - b.at_frames)
      : [];
  } catch {
    return [];
  }
}
