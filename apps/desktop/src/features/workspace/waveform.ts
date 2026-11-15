/** N2-B — waveform envelope fetching + client cache.
 *
 * The backend stores deterministic min/max bucket levels in CAS
 * (ade-wave/1). The timeline asks for the MANIFEST once per asset and
 * viewport-bounded level slices on demand — a long recording never
 * lands in memory wholesale. Slices are cached by (asset, spb, range)
 * so scroll/zoom revisits are free; the cache is FIFO-bounded.
 */

import { apiFetch } from "../../api/client";
import type { TimelineClip } from "../../api/types";

export interface WaveLevel {
  spb: number;
  buckets: number;
  sha256: string;
}

export interface WaveManifest {
  format: string;
  built_sha256: string;
  sample_rate: number;
  channels: number;
  samples: number;
  levels: WaveLevel[];
}

export interface WaveSlice {
  spb: number;
  buckets: number;
  bucket_start: number;
  bucket_end: number;
  channels: number;
  sample_rate: number;
  peaks: [number, number][][]; // [channel][bucket][min,max] int16
}

const manifestCache = new Map<string, Promise<ManifestResult>>();
const sliceCache = new Map<string, Promise<WaveSlice | null>>();
const SLICE_CAP = 256;

function boundedSet<V>(map: Map<string, V>, key: string, value: V) {
  if (map.size >= SLICE_CAP) {
    const first = map.keys().next().value;
    if (first !== undefined) map.delete(first);
  }
  map.set(key, value);
}

export type ManifestResult = WaveManifest | "missing" | "error";

/** Manifest for an asset's current version. 409 means no waveform
 * built (or built for a stale version) — "missing"; anything else
 * failing is "error" so the UI can say so honestly. */
export function getWaveformManifest(
  projectId: string, assetId: string,
): Promise<ManifestResult> {
  const key = `${projectId}:${assetId}`;
  let p = manifestCache.get(key);
  if (!p) {
    p = apiFetch<WaveManifest>(
      `/api/projects/${projectId}/assets/${assetId}/waveform`)
      .catch((e: unknown) =>
        e instanceof Error && e.message.startsWith("409")
          ? "missing" : "error");
    manifestCache.set(key, p);
  }
  return p;
}

/** Drop cached manifest+slices for an asset — call after a rebuild
 * job completes so the next paint fetches fresh levels. */
export function invalidateWaveform(projectId: string, assetId: string) {
  manifestCache.delete(`${projectId}:${assetId}`);
  for (const k of [...sliceCache.keys()]) {
    if (k.startsWith(`${projectId}:${assetId}:`)) sliceCache.delete(k);
  }
}

/** A bucket-range slice of one level, or null on error/absence. */
export function getWaveformSlice(
  projectId: string, assetId: string, spb: number,
  startBucket: number, endBucket: number,
): Promise<WaveSlice | null> {
  const key =
    `${projectId}:${assetId}:${spb}:${startBucket}:${endBucket}`;
  let p = sliceCache.get(key);
  if (!p) {
    p = apiFetch<WaveSlice>(
      `/api/projects/${projectId}/assets/${assetId}/waveform/level`
      + `?level=${spb}&start_bucket=${startBucket}`
      + `&end_bucket=${endBucket}`)
      .catch(() => null);
    boundedSet(sliceCache, key, p);
  }
  return p;
}

export interface WaveWindow {
  /** Source-sample range actually visible — clamped to
   * [clip.in_frame, clip.out_frame] so head/tail trims never display
   * discarded or beyond-out samples. */
  s0: number;
  s1: number;
  /** Visible window inside the clip, in timeline frames. */
  winL: number;
  winW: number;
  samplesPerTlFrame: number;
}

/** Map the visible timeline range of an audio clip onto source samples.
 * Pure — unit-tested. `viewL`/`viewR` are timeline-frame viewport
 * bounds; `tlFps` is the timeline rate (fps_num/fps_den — fractional
 * rates work unchanged since nothing accumulates). */
export function clipWaveWindow(
  clip: TimelineClip, tlFps: number,
  viewL: number, viewR: number, tlExtent: number,
): WaveWindow | null {
  const rate = clip.src_fps_num / clip.src_fps_den;
  const samplesPerTlFrame = rate / tlFps;
  const clipEnd = clip.start_frames + tlExtent;
  const v0 = Math.max(viewL, clip.start_frames);
  const v1 = Math.min(viewR, clipEnd);
  if (v1 <= v0) return null;
  const s0 = Math.max(clip.in_frame,
    clip.in_frame + (v0 - clip.start_frames) * samplesPerTlFrame);
  const s1 = Math.min(clip.out_frame,
    clip.in_frame + (v1 - clip.start_frames) * samplesPerTlFrame);
  if (s1 <= s0) return null;
  return {
    s0, s1,
    winL: v0 - clip.start_frames,
    winW: v1 - v0,
    samplesPerTlFrame,
  };
}

/** Choose the level whose samples-per-bucket best matches the pixels
 * on screen: the first level at/above `samplesPerPx` (cheap and
 * sufficient), else the finest available. */
export function pickLevel(
  manifest: WaveManifest, samplesPerPx: number,
): WaveLevel | null {
  const levels = [...manifest.levels].sort((a, b) => a.spb - b.spb);
  if (levels.length === 0) return null;
  for (const l of levels) {
    if (l.spb >= samplesPerPx) return l;
  }
  return levels[levels.length - 1];
}
