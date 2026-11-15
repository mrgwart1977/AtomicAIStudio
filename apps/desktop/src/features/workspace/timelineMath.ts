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

/** A clip source may sit on visual lanes only — video and overlay.
 * Audio lanes wait for real audio sources (N1-D territory); refusing
 * here keeps the honest "no silent semantic change" rule. */
export function compatibleTrack(track: TimelineTrack): boolean {
  return track.kind === "video" || track.kind === "overlay";
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
): string | null {
  if (track.locked) return `${track.name} is locked — unlock it to drop here`;
  if (!compatibleTrack(track)) {
    return `${track.name} is an audio lane — audio sources aren't supported yet`;
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

/** Mirror of the backend's v1_layout_reason — drives the export-
 * capability badge. The backend remains authoritative; this is honest
 * UI messaging, never a promise. */
export function exportEligibility(detail: TimelineDetail): {
  ok: boolean; reason: string | null;
} {
  const populated = detail.tracks.filter(
    (t) => laneClips(detail.clips, t.id).length > 0);
  if (populated.length === 0) return { ok: true, reason: null };
  if (populated.length > 1) {
    return { ok: false, reason: "multiple tracks hold clips — "
      + "the current exporter renders a single sequential video lane" };
  }
  const t = populated[0];
  if (t.kind !== "video") {
    return { ok: false, reason: `the only populated track is '${t.kind}' — `
      + "the exporter renders video lanes only" };
  }
  if (!t.visible) {
    return { ok: false, reason: "the populated track is hidden — "
      + "unhide it before exporting" };
  }
  if (t.muted) {
    return { ok: false, reason: "the populated track is muted — "
      + "unmute it before exporting" };
  }
  let runEnd = 0;
  for (const c of laneClips(detail.clips, t.id)) {
    if (c.start_frames < runEnd - EPS) {
      return { ok: false, reason: "clips overlap on the track" };
    }
    const packed = Math.ceil(runEnd - EPS);
    if (c.start_frames !== packed) {
      return { ok: false, reason: "clips aren't contiguous — the exporter "
        + "can't render gaps (multitrack export arrives with N1-D)" };
    }
    runEnd = c.start_frames + clipTlExtent(c, detail);
  }
  return { ok: true, reason: null };
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
