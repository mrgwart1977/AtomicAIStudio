/** N1-B — pure timeline-editor geometry + layout rules. Component-free
 * so frame math, snapping, and export eligibility stay unit-testable.
 *
 * Authority note: the backend stores exact rational extents (Fraction)
 * and integer `start_frames`; everything here is float display math for
 * pixels/snap only. Committed edits always carry integer frames — the
 * backend re-validates and stays the source of truth. */

import type { TimelineClip, TimelineDetail, TimelineTrack } from "../../api/types";

/** Kinds the data model supports (mirrors backend TRACK_KINDS). */
export const TRACK_KINDS = ["video", "audio", "overlay"] as const;
export type TrackKind = typeof TRACK_KINDS[number];

export const KIND_LABEL: Record<string, string> = {
  video: "Video", audio: "Audio", overlay: "Overlay",
};

const EPS = 1e-6;

/** Exact float extent of a clip in timeline frames
 * ((out-in) · src_den · tl_num / (src_num · tl_den)). */
export function clipTlExtent(
  clip: Pick<TimelineClip, "in_frame" | "out_frame" | "src_fps_num" | "src_fps_den">,
  tl: Pick<TimelineDetail, "fps_num" | "fps_den">,
): number {
  return (clip.out_frame - clip.in_frame) * clip.src_fps_den * tl.fps_num
    / (clip.src_fps_num * tl.fps_den);
}

/** Smallest integer frame ≥ the clip's exact end — mirrors the
 * backend's ceil packing convention (never overlaps). */
export function clipTlEnd(
  clip: Pick<TimelineClip, "in_frame" | "out_frame" | "src_fps_num" | "src_fps_den" | "start_frames">,
  tl: Pick<TimelineDetail, "fps_num" | "fps_den">,
): number {
  return Math.ceil(clip.start_frames + clipTlExtent(clip, tl) - EPS);
}

/** Rightmost integer end across all clips — the program footprint. */
export function programEndFrames(detail: TimelineDetail): number {
  let end = 0;
  for (const c of detail.clips) {
    const e = clipTlEnd(c, detail);
    if (e > end) end = e;
  }
  return end;
}

/** Clips on one lane ordered by (start_frames, id) — the backend's
 * within-track rank. */
export function laneClips(clips: TimelineClip[], trackId: string): TimelineClip[] {
  return clips
    .filter((c) => c.track_id === trackId)
    .sort((a, b) => a.start_frames - b.start_frames || a.id.localeCompare(b.id));
}

export function frameToPx(f: number, ppf: number): number {
  return f * ppf;
}
export function pxToFrame(x: number, ppf: number): number {
  return x / ppf;
}

/** All three lane kinds can hold clips — the kind of clip each lane
 * accepts is checked separately by acceptsClipKind (N2-A). */
export function compatibleTrack(track: TimelineTrack): boolean {
  return track.kind === "video" || track.kind === "overlay"
    || track.kind === "audio";
}

/** N2-A — lane-kind rule mirrors the backend: audio clips only on
 * audio lanes; video clips on video/overlay lanes. `clipKind` is the
 * kind of the clip's current lane (backend guarantees they match). */
export function acceptsClipKind(track: TimelineTrack, clipLaneKind: string): boolean {
  return clipLaneKind === "audio"
    ? track.kind === "audio"
    : track.kind !== "audio";
}

export interface SnapResult { start: number; snappedTo: "edge" | "frame" | null }

/** Snap a candidate start (frames, float) to: other clips' boundaries
 * on the target lane within `snapPx`, then the integer frame grid.
 * Never returns < 0. */
export function snapStart(
  raw: number,
  target: TimelineClip[],
  selfId: string,
  tl: Pick<TimelineDetail, "fps_num" | "fps_den">,
  ppf: number,
  snapPx = 8,
): SnapResult {
  const snapF = snapPx / ppf;
  for (const c of target) {
    if (c.id === selfId) continue;
    for (const edge of [c.start_frames, clipTlEnd(c, tl)]) {
      if (Math.abs(raw - edge) <= snapF) {
        return { start: Math.max(0, edge), snappedTo: "edge" };
      }
    }
  }
  return { start: Math.max(0, Math.round(raw)), snappedTo: "frame" };
}

/** Why a drop can't commit on this lane, or null if it can. Mirrors
 * the backend rules so UI feedback matches what commit will do. */
export function dropError(
  track: TimelineTrack,
  startF: number,
  moving: TimelineClip,
  lane: TimelineClip[],
  tl: Pick<TimelineDetail, "fps_num" | "fps_den">,
  clipLaneKind?: string,
): string | null {
  if (track.locked) return `${track.name} is locked — unlock it to drop here`;
  if (!compatibleTrack(track)) {
    return `${track.name} cannot hold clips`;
  }
  if (clipLaneKind !== undefined
      && !acceptsClipKind(track, clipLaneKind)) {
    return clipLaneKind === "audio"
      ? `${track.name} — audio clips live on audio lanes only`
      : `${track.name} is an audio lane — it accepts audio clips only`;
  }
  if (startF < 0) return "clip can't start before frame 0";
  const ext = clipTlExtent(moving, tl);
  const end = startF + ext;
  for (const c of lane) {
    if (c.id === moving.id) continue;
    const cEnd = c.start_frames + clipTlExtent(c, tl);
    if (startF < cEnd - EPS && end > c.start_frames + EPS) {
      return "would overlap a clip on this lane";
    }
  }
  return null;
}

/** Mirror of the backend's v1_layout_reason — "sequential" means the
 * layout is ade-edl/1-representable. The backend remains authoritative;
 * this is honest UI messaging, never a promise. */
export function exportEligibility(detail: TimelineDetail): {
  ok: boolean; reason: string | null;
} {
  const populated = detail.tracks.filter(
    (t) => laneClips(detail.clips, t.id).length > 0);
  if (populated.length === 0) return { ok: true, reason: null };
  if (populated.length > 1) {
    return { ok: false, reason: "multiple tracks hold clips" };
  }
  const t = populated[0];
  if (t.kind !== "video") {
    return { ok: false, reason: `the only populated track is '${t.kind}'` };
  }
  if (!t.visible) {
    return { ok: false, reason: "the populated track is hidden" };
  }
  if (t.muted) {
    return { ok: false, reason: "the populated track is muted" };
  }
  let runEnd = 0;
  for (const c of laneClips(detail.clips, t.id)) {
    if (c.start_frames < runEnd - EPS) {
      return { ok: false, reason: "clips overlap on the track" };
    }
    const packed = Math.ceil(runEnd - EPS);
    if (c.start_frames !== packed) {
      return { ok: false, reason: "clips aren't contiguous" };
    }
    runEnd = c.start_frames + clipTlExtent(c, detail);
  }
  return { ok: true, reason: null };
}

/** N1-D/N2-E — what the exporter will actually do with this layout.
 * "sequential" → ade-edl/1 (copy/encode paths).
 * "composite"  → ade-edl/2 multitrack render: every VISIBLE lane with
 * clips composites (hidden lanes contribute nothing; muted lanes still
 * show video but no audio). N2-E: audible audio lanes mix into the one
 * output — `audio` reports what the mix contains.
 * "blocked"    → nothing to render, or an unsatisfied crossfade sits
 * on an audible lane (the exporter fails closed — fix it first). */
export function exportMode(detail: TimelineDetail): {
  mode: "sequential" | "composite" | "blocked";
  lanes: number;
  audio: "none" | "embedded" | "tracks";
  reason: string | null;
} {
  const none = { audio: "none" as const };
  if (detail.clips.length === 0) {
    return { mode: "blocked", lanes: 0, ...none,
             reason: "the timeline holds no clips" };
  }
  if (exportEligibility(detail).ok) {
    return { mode: "sequential", lanes: 1, ...none, reason: null };
  }
  const visible = detail.tracks.filter(
    (t) => t.visible && laneClips(detail.clips, t.id).length > 0);
  if (visible.length === 0) {
    return { mode: "blocked", lanes: 0, ...none,
             reason: "no visible lane holds clips — nothing to export" };
  }
  // Audibility mirrors the backend: visible + unmuted + in the solo
  // set when any lane is soloed.
  const soloSet = new Set(
    detail.tracks.filter((t) => t.solo && t.kind !== "overlay")
      .map((t) => t.id));
  const audible = (t: TimelineTrack) =>
    t.visible && !t.muted && (soloSet.size === 0 || soloSet.has(t.id));
  // A crossfade that detached from its pair fails the export closed —
  // surface it here instead of letting the job red.
  const badXf = (detail.crossfades ?? []).find((x) => {
    if (x.satisfied) return false;
    const tr = detail.tracks.find((t) => t.id === (
      detail.clips.find((c) => c.id === x.out_clip_id)?.track_id));
    return tr != null && audible(tr);
  });
  if (badXf) {
    return { mode: "blocked", lanes: 0, ...none,
             reason: "a crossfade is detached from its clips — undo "
               + "the move or clear the crossfade before exporting" };
  }
  const audibleAudio = visible.some(
    (t) => t.kind === "audio" && audible(t));
  const audibleVideo = visible.some(
    (t) => t.kind !== "audio" && audible(t));
  return {
    mode: "composite", lanes: visible.length,
    audio: audibleAudio ? "tracks" : audibleVideo ? "embedded" : "none",
    reason: null };
}

/** Frame → "SS:FF" label using the timeline rate (seconds:frames). */
export function fmtTc(
  f: number,
  tl: Pick<TimelineDetail, "fps_num" | "fps_den">,
): string {
  const fps = tl.fps_num / tl.fps_den;
  const s = Math.floor(f / fps);
  const ff = Math.round(f - s * fps);
  return `${s}:${String(ff).padStart(2, "0")}`;
}

/** Pick a ruler tick interval (frames) so ticks are ≥ ~72 px apart. */
export function tickInterval(tl: Pick<TimelineDetail, "fps_num" | "fps_den">,
                             ppf: number): number {
  const fps = tl.fps_num / tl.fps_den;
  for (const s of [1, 2, 5, 10, 30, 60, 120, 300]) {
    if (s * fps * ppf >= 72) return Math.round(s * fps);
  }
  return Math.round(300 * fps);
}

/* ---------------- N1-C: split / gap / trim helpers ---------------- */

export interface LaneGap { start: number; frames: number }

/** Integer-grid gaps on a lane: [ceil(prevEnd), nextStart) using the
 * same ceil convention the backend packs with. The leading gap
 * (0 → first clip) is included when the lane doesn't start at 0. */
export function laneGaps(
  lane: TimelineClip[],
  tl: Pick<TimelineDetail, "fps_num" | "fps_den">,
): LaneGap[] {
  const gaps: LaneGap[] = [];
  let runEnd = 0;
  for (const c of lane) {
    const gapStart = Math.max(0, Math.ceil(runEnd - EPS));
    if (c.start_frames > gapStart) {
      gaps.push({ start: gapStart, frames: c.start_frames - gapStart });
    }
    runEnd = Math.max(runEnd, c.start_frames + clipTlExtent(c, tl));
  }
  return gaps;
}

/** Mirror of the backend's split rule: timeline frame `at` must land
 * strictly inside the clip AND map to a whole SOURCE frame — else the
 * split would cut mid-frame, which the backend refuses. Returns the
 * integer source-frame offset when legal, else null. */
export function splitSrcOffset(
  clip: TimelineClip,
  tl: Pick<TimelineDetail, "fps_num" | "fps_den">,
  at: number,
): number | null {
  if (at <= clip.start_frames || at >= clip.start_frames
      + clipTlExtent(clip, tl)) return null;
  const off = (at - clip.start_frames)
    * tl.fps_den * clip.src_fps_num / (tl.fps_num * clip.src_fps_den);
  const i = Math.round(off);
  if (Math.abs(off - i) > EPS || i <= 0
      || i >= clip.out_frame - clip.in_frame) return null;
  return i;
}

/** Mirror of the backend's overlap rule for a trim preview: would an
 * extent starting at `start` collide with another clip on the lane? */
export function extentOverlap(
  start: number,
  extent: number,
  lane: TimelineClip[],
  selfId: string,
  tl: Pick<TimelineDetail, "fps_num" | "fps_den">,
): boolean {
  const end = start + extent;
  for (const c of lane) {
    if (c.id === selfId) continue;
    const cEnd = c.start_frames + clipTlExtent(c, tl);
    if (start < cEnd - EPS && end > c.start_frames + EPS) return true;
  }
  return false;
}
