import {
  memo, useCallback, useEffect, useMemo, useRef, useState,
} from "react";
import type {
  TimelineClip, TimelineDetail, TimelineTrack,
} from "../../api/types";
import {
  acceptsClipKind, clipTlEnd, clipTlExtent, compatibleTrack,
  dropError, extentOverlap, fmtTc, frameToPx, laneClips, laneGaps,
  programEndFrames, snapStart, splitSrcOffset, tickInterval,
  TRACK_KINDS, KIND_LABEL,
} from "./timelineMath";
import { apiFetch } from "../../api/client";
import {
  clipWaveWindow, getWaveformManifest, getWaveformSlice,
  invalidateWaveform, pickLevel,
} from "./waveform";
import type { ManifestResult } from "./waveform";

/** N1-B — multitrack timeline editor: lanes with track controls,
 * ruler + playhead, zoom/scroll, and pixel-exact drag placement.
 * Drags mutate nothing until pointerup commits `timeline.clip.move`;
 * Escape cancels. The backend stays authoritative — the UI mirrors
 * its rules (locked lanes, overlap, compatibility) only to give
 * honest live feedback.
 *
 * N1-C adds: split-at-playhead (source-frame-boundary checked),
 * explicit ripple mode (track scope, disclosed), right-edge trim
 * handles with live duration feedback, and gap indicators with
 * close/insert-space controls — all through the same preview/commit
 * path, pinned with expected_timeline for stale-approval safety. */

const HEADER_W = 176;   // px — track header column
const RULER_H = 28;     // px — ruler row
const LANE_H = 56;      // px — one lane
const TAIL_F = 48;      // blank frames of scroll room past program end

export function TimelineEditor({ detail, active, busy, onRun, onRemoveClip }: {
  detail: TimelineDetail;
  active: boolean;
  busy: boolean;
  onRun: (type: string, payload: Record<string, unknown>) => Promise<boolean>;
  onRemoveClip: (clip: TimelineClip, ripple: boolean) => void;
}) {
  const [ppf, setPpf] = useState(6);                 // px per frame (zoom)
  const [playheadF, setPlayheadF] = useState(0);
  const [selClipId, setSelClipId] = useState<string | null>(null);
  const [renamingTrack, setRenamingTrack] = useState<string | null>(null);
  const [ripple, setRipple] = useState(false);
  const [gapFrames, setGapFrames] = useState("24");
  const [audioAssets, setAudioAssets] = useState<
    { id: string; name: string }[] | null>(null);
  const [dragView, setDragView] = useState<{
    clipId: string; trackId: string; startF: number;
    error: string | null; snapped: boolean;
  } | null>(null);
  const [trimView, setTrimView] = useState<{
    clipId: string; trackId: string; endF: number; outF: number;
    error: string | null;
  } | null>(null);
  const [addingKind, setAddingKind] = useState<string>("video");

  const lanesRef = useRef<Map<string, HTMLDivElement>>(new Map());
  const scrollRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<{
    clip: TimelineClip; grabF: number; pointerId: number;
    orig: { trackId: string; startF: number };
    // The exact listener instances registered — identity-stable across
    // re-renders so cleanup can always remove them.
    move: (e: PointerEvent) => void;
    up: () => void;
    key: (e: KeyboardEvent) => void;
  } | null>(null);

  const tracks = useMemo(
    () => [...detail.tracks].sort((a, b) => a.position - b.position),
    [detail.tracks]);
  const clipsByTrack = useMemo(() => {
    const m = new Map<string, TimelineClip[]>();
    for (const t of tracks) m.set(t.id, laneClips(detail.clips, t.id));
    return m;
  }, [detail.clips, tracks]);
  const orphans = useMemo(
    () => detail.clips.filter(
      (c) => !c.track_id || !tracks.some((t) => t.id === c.track_id)),
    [detail.clips, tracks]);
  const programEnd = useMemo(() => programEndFrames(detail), [detail]);
  const contentW = Math.max(1, (programEnd + TAIL_F) * ppf);
  // Visible pixel window for clip/gap culling; {l:0,r:Inf} until the
  // first scroll measurement so tests/SSR render everything.
  const [viewX, setViewX] = useState<{ l: number; r: number }>(
    { l: 0, r: Number.POSITIVE_INFINITY });
  useEffect(() => {
    const el = scrollRef.current;
    if (el && el.clientWidth > 0) {
      setViewX({ l: Math.max(0, el.scrollLeft - el.clientWidth),
                 r: el.scrollLeft + el.clientWidth * 2 });
    }
  }, [contentW]);
  const inView = useCallback((startF: number, endF: number) =>
    endF * ppf >= viewX.l && startF * ppf <= viewX.r, [ppf, viewX]);
  const selClip = detail.clips.find((c) => c.id === selClipId) ?? null;

  /* ---------------- N1-C: expected-revision pin ---------------- */
  // Every clip edit pins the revision we actually read — a stale pin
  // 422s and the page offers a refresh instead of overwriting a
  // concurrent edit. (Track ops stay unpinned: they don't shift clips.)
  const pin = useCallback(() => ({
    expected_timeline: { id: detail.id, revision: detail.revision },
  }), [detail.id, detail.revision]);

  // Selection/drag state must not survive a project or timeline switch.
  useEffect(() => {
    setSelClipId(null); setDragView(null); setPlayheadF(0);
    dragRef.current = null;
  }, [detail.id]);
  // A committed refresh can drop the selected clip (remove/revert).
  useEffect(() => {
    if (selClipId && !detail.clips.some((c) => c.id === selClipId)) {
      setSelClipId(null);
    }
  }, [detail.clips, selClipId]);

  /* ---------------- drag (pointer-based, pixel-exact) ---------------- */

  const trackAtClientY = useCallback((clientY: number): TimelineTrack | null => {
    for (const t of tracks) {
      const el = lanesRef.current.get(t.id);
      if (!el) continue;
      const r = el.getBoundingClientRect();
      if (clientY >= r.top && clientY < r.bottom) return t;
    }
    return null;
  }, [tracks]);

  const endDrag = useCallback(() => {
    const d = dragRef.current;
    dragRef.current = null;
    dragViewRef.current = null;
    setDragView(null);
    if (d) {
      window.removeEventListener("pointermove", d.move);
      window.removeEventListener("pointerup", d.up);
      window.removeEventListener("keydown", d.key);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep the latest drag view visible to the pointerup handler without
  // re-binding listeners every frame.
  const dragViewRef = useRef(dragView);
  dragViewRef.current = dragView;

  const onPtrMove = useCallback((e: PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const track = trackAtClientY(e.clientY) ??
      tracks.find((t) => t.id === d.orig.trackId) ?? null;
    if (!track) return;
    const lane = lanesRef.current.get(track.id);
    if (!lane) return;
    const rect = lane.getBoundingClientRect();
    // Pointer position in frames minus the grab offset inside the clip.
    const rawF = (e.clientX - rect.left) / ppf - d.grabF;
    const laneClipList = clipsByTrack.get(track.id) ?? [];
    const snap = snapStart(rawF, laneClipList, d.clip.id, detail, ppf);
    const clipLaneKind = tracks.find(
      (t) => t.id === d.orig.trackId)?.kind;
    const error = dropError(track, snap.start, d.clip, laneClipList,
                            detail, clipLaneKind);
    const view = {
      clipId: d.clip.id, trackId: track.id, startF: snap.start,
      error, snapped: snap.snappedTo === "edge",
    };
    // Synchronous ref update — a pointerup landing before React commits
    // the render must still see the drop target.
    dragViewRef.current = view;
    setDragView(view);
  }, [clipsByTrack, detail, ppf, trackAtClientY, tracks]);

  const onPtrUp = useCallback(() => {
    const d = dragRef.current;
    const view = dragViewRef.current;
    endDrag();
    if (!d || !view) return;
    const changed = view.trackId !== d.orig.trackId
      || view.startF !== d.orig.startF;
    if (!changed) return;
    if (view.error) {
      // Invalid drop: no mutation — the clip visually snaps back because
      // nothing was ever written.
      return;
    }
    void onRun("timeline.clip.move", {
      timeline_id: detail.id, clip_id: d.clip.id,
      to_track_id: view.trackId, start_frames: view.startF,
      ...pin(),
    });
  }, [detail.id, endDrag, onRun, pin]);

  const onPtrKey = useCallback((e: KeyboardEvent) => {
    if (e.key === "Escape") endDrag();
  }, [endDrag]);

  const beginDrag = useCallback((e: React.PointerEvent, clip: TimelineClip) => {
    if (!active || busy) return;
    const track = tracks.find((t) => t.id === clip.track_id);
    if (!track || track.locked) return;
    const lane = lanesRef.current.get(track.id);
    if (!lane) return;
    const rect = lane.getBoundingClientRect();
    // Stable wrapper instances — cleanup can remove exactly what was
    // registered even if the callbacks re-bind mid-drag.
    const move = (ev: PointerEvent) => moveRef.current(ev);
    const up = () => upRef.current();
    const key = (ev: KeyboardEvent) => keyRef.current(ev);
    dragRef.current = {
      clip, pointerId: e.pointerId,
      grabF: Math.max(0, (e.clientX - rect.left) / ppf - clip.start_frames),
      orig: { trackId: track.id, startF: clip.start_frames },
      move, up, key,
    };
    const view = {
      clipId: clip.id, trackId: track.id, startF: clip.start_frames,
      error: null, snapped: false,
    };
    dragViewRef.current = view;
    setDragView(view);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("keydown", key);
    e.preventDefault();
  }, [active, busy, ppf, tracks]);

  // Latest-callback refs so the stable wrappers always run fresh logic.
  const moveRef = useRef(onPtrMove); moveRef.current = onPtrMove;
  const upRef = useRef(onPtrUp); upRef.current = onPtrUp;
  const keyRef = useRef(onPtrKey); keyRef.current = onPtrKey;

  useEffect(() => () => {
    const d = dragRef.current;
    if (d) {
      window.removeEventListener("pointermove", d.move);
      window.removeEventListener("pointerup", d.up);
      window.removeEventListener("keydown", d.key);
    }
  }, []);

  /* ---------------- keyboard clip ops ---------------- */

  const fps = detail.fps_num / detail.fps_den;
  const clipKey = (e: React.KeyboardEvent, clip: TimelineClip) => {
    if (!active || busy) return;
    const track = tracks.find((t) => t.id === clip.track_id);
    if (!track) return;
    const step = e.shiftKey ? Math.max(1, Math.round(fps)) : 1;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      if (track.locked) return;
      e.preventDefault();
      const startF = Math.max(0, clip.start_frames
        + (e.key === "ArrowRight" ? step : -step));
      if (startF === clip.start_frames) return; // no-op ≠ revision bump
      const lane = clipsByTrack.get(track.id) ?? [];
      if (dropError(track, startF, clip, lane, detail)) return;
      void onRun("timeline.clip.move", {
        timeline_id: detail.id, clip_id: clip.id,
        to_track_id: track.id, start_frames: startF, ...pin(),
      });
    } else if ((e.ctrlKey || e.metaKey)
               && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      e.preventDefault();
      const idx = tracks.findIndex((t) => t.id === track.id);
      const next = tracks[idx + (e.key === "ArrowDown" ? 1 : -1)];
      const clipLaneKind = tracks.find(
        (x) => x.id === clip.track_id)?.kind ?? "video";
      if (!next || next.locked || !compatibleTrack(next)
          || !acceptsClipKind(next, clipLaneKind)) return;
      const lane = clipsByTrack.get(next.id) ?? [];
      if (dropError(next, clip.start_frames, clip, lane, detail,
                    clipLaneKind)) return;
      void onRun("timeline.clip.move", {
        timeline_id: detail.id, clip_id: clip.id,
        to_track_id: next.id, start_frames: clip.start_frames, ...pin(),
      });
    } else if (e.key === "Delete" || e.key === "Backspace") {
      if (track.locked) return;
      e.preventDefault();
      onRemoveClip(clip, ripple);
    } else if ((e.key === "s" || e.key === "S")
               && !e.ctrlKey && !e.metaKey && !e.altKey) {
      if (track.locked) return;
      if (splitSrcOffset(clip, detail, playheadF) === null) return;
      e.preventDefault();
      void onRun("timeline.clip.split", {
        timeline_id: detail.id, clip_id: clip.id,
        at_frame: playheadF, ...pin(),
      });
    }
  };

  /* ---------------- zoom / seek ---------------- */

  const zoom = (f: number) => {
    setPpf((p) => Math.min(40, Math.max(0.5, p * f)));
  };
  const zoomFit = () => {
    const w = scrollRef.current?.clientWidth ?? 600;
    setPpf(Math.min(40, Math.max(0.5,
      (w - HEADER_W - 24) / Math.max(1, programEnd + TAIL_F))));
  };
  const onWheel = (e: React.WheelEvent) => {
    if (!e.ctrlKey && !e.metaKey) return;
    e.preventDefault();
    zoom(e.deltaY < 0 ? 1.2 : 1 / 1.2);
  };

  const seek = (e: React.PointerEvent | React.MouseEvent) => {
    const el = e.currentTarget as HTMLElement;
    const r = el.getBoundingClientRect();
    setPlayheadF(Math.max(0, Math.round((e.clientX - r.left) / ppf)));
  };

  /* ---------------- track ops ---------------- */

  const moveTrack = (t: TimelineTrack, dir: -1 | 1) => {
    void onRun("timeline.track.reorder", {
      timeline_id: detail.id, track_id: t.id,
      to_position: t.position + dir,
    });
  };
  const patchTrack = (t: TimelineTrack, fields: Record<string, unknown>) => {
    void onRun("timeline.track.update", {
      timeline_id: detail.id, track_id: t.id, ...fields,
    });
  };
  const removeTrack = (t: TimelineTrack) => {
    void onRun("timeline.track.remove", {
      timeline_id: detail.id, track_id: t.id,
    });
  };
  const addTrack = () => {
    void onRun("timeline.track.add", {
      timeline_id: detail.id, kind: addingKind,
      name: `${KIND_LABEL[addingKind] ?? addingKind} `
        + `${tracks.filter((t) => t.kind === addingKind).length + 1}`,
    });
  };

  /* ---------------- N1-C: trim-end drag ---------------- */

  const trimRef = useRef<{
    clip: TimelineClip; trackId: string;
    origOut: number;
    move: (e: PointerEvent) => void;
    up: () => void;
    key: (e: KeyboardEvent) => void;
  } | null>(null);
  const trimViewRef = useRef<typeof trimView>(null);
  trimViewRef.current = trimView;

  const endTrim = useCallback(() => {
    const t = trimRef.current;
    trimRef.current = null;
    trimViewRef.current = null;
    setTrimView(null);
    if (t) {
      window.removeEventListener("pointermove", t.move);
      window.removeEventListener("pointerup", t.up);
      window.removeEventListener("keydown", t.key);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const onTrimMove = useCallback((e: PointerEvent) => {
    const t = trimRef.current;
    if (!t) return;
    const lane = lanesRef.current.get(t.trackId);
    if (!lane) return;
    const rect = lane.getBoundingClientRect();
    const clip = t.clip;
    // Snap in SOURCE frames (authoritative) — the timeline extent
    // follows from the chosen out_frame, never the other way round.
    const pointerF = (e.clientX - rect.left) / ppf;
    const srcPerTl = (clip.src_fps_num * detail.fps_den)
      / (clip.src_fps_den * detail.fps_num);
    const off = Math.max(1, Math.min(
      clip.src_frames - clip.in_frame,
      Math.round((pointerF - clip.start_frames) * srcPerTl)));
    const outF = clip.in_frame + off;
    const extent = off / srcPerTl;
    const laneList = clipsByTrack.get(t.trackId) ?? [];
    const error = !ripple
      && extentOverlap(clip.start_frames, extent, laneList, clip.id, detail)
      ? "Overlaps the next clip — enable Ripple or trim shorter"
      : null;
    const view = {
      clipId: clip.id, trackId: t.trackId,
      endF: clip.start_frames + extent, outF, error,
    };
    trimViewRef.current = view;
    setTrimView(view);
  }, [clipsByTrack, detail, ppf, ripple]);

  const onTrimUp = useCallback(() => {
    const t = trimRef.current;
    const view = trimViewRef.current;
    endTrim();
    if (!t || !view || view.error || view.outF === t.origOut) return;
    void onRun("timeline.clip.trim", {
      timeline_id: detail.id, clip_id: t.clip.id,
      out_frame: view.outF,
      ...(ripple ? { ripple_scope: "track" } : {}),
      ...pin(),
    });
  }, [detail.id, endTrim, onRun, pin, ripple]);

  const onTrimKey = useCallback((e: KeyboardEvent) => {
    if (e.key === "Escape") endTrim();
  }, [endTrim]);

  const beginTrim = useCallback((e: React.PointerEvent, clip: TimelineClip) => {
    if (!active || busy) return;
    const track = tracks.find((t) => t.id === clip.track_id);
    if (!track || track.locked) return;
    e.stopPropagation(); // the edge handle must not start a move drag
    const move = (ev: PointerEvent) => tMoveRef.current(ev);
    const up = () => tUpRef.current();
    const key = (ev: KeyboardEvent) => tKeyRef.current(ev);
    trimRef.current = {
      clip, trackId: track.id, origOut: clip.out_frame, move, up, key,
    };
    const view = {
      clipId: clip.id, trackId: track.id,
      endF: clip.start_frames + clipTlExtent(clip, detail),
      outF: clip.out_frame, error: null,
    };
    trimViewRef.current = view;
    setTrimView(view);
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("keydown", key);
    e.preventDefault();
  }, [active, busy, detail, tracks]);

  const tMoveRef = useRef(onTrimMove); tMoveRef.current = onTrimMove;
  const tUpRef = useRef(onTrimUp); tUpRef.current = onTrimUp;
  const tKeyRef = useRef(onTrimKey); tKeyRef.current = onTrimKey;

  useEffect(() => () => {
    const t = trimRef.current;
    if (t) {
      window.removeEventListener("pointermove", t.move);
      window.removeEventListener("pointerup", t.up);
      window.removeEventListener("keydown", t.key);
    }
  }, []);

  /* ---------------- N1-C: split / gap ops ---------------- */

  // Split target: the selected clip when the playhead sits inside it
  // on a whole source frame, else the topmost visible clip that does.
  const splitTarget = useCallback((): TimelineClip | null => {
    if (selClip && splitSrcOffset(selClip, detail, playheadF) !== null) {
      return selClip;
    }
    for (const t of tracks) {
      if (!t.visible || t.locked) continue;
      const lane = clipsByTrack.get(t.id) ?? [];
      const hit = lane.find(
        (c) => splitSrcOffset(c, detail, playheadF) !== null);
      if (hit) return hit;
    }
    return null;
  }, [clipsByTrack, detail, playheadF, selClip, tracks]);

  const doSplit = (clip: TimelineClip) => {
    if (splitSrcOffset(clip, detail, playheadF) === null) return;
    void onRun("timeline.clip.split", {
      timeline_id: detail.id, clip_id: clip.id,
      at_frame: playheadF, ...pin(),
    });
  };

  // N1-E — undo/redo ride the same pinned command path; the labels
  // come from the authoritative per-timeline stacks on the server.
  const doUndo = useCallback(() => {
    if (busy || !detail.undo_label) return;
    void onRun("timeline.undo", { timeline_id: detail.id, ...pin() });
  }, [busy, detail, onRun, pin]);
  const doRedo = useCallback(() => {
    if (busy || !detail.redo_label) return;
    void onRun("timeline.redo", { timeline_id: detail.id, ...pin() });
  }, [busy, detail, onRun, pin]);

  // Ctrl+Z / Ctrl+Shift+Z (and Ctrl+Y) — skip when typing in a field.
  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA"
                 || el.isContentEditable)) return;
      const k = e.key.toLowerCase();
      if (k === "z" && e.shiftKey) { e.preventDefault(); doRedo(); }
      else if (k === "z") { e.preventDefault(); doUndo(); }
      else if (k === "y") { e.preventDefault(); doRedo(); }
    };
    window.addEventListener("keydown", key);
    return () => window.removeEventListener("keydown", key);
  }, [doUndo, doRedo]);

  // Gap ops act on the selected clip's lane when it's editable, else
  // the first editable video/overlay lane — the disclosed scope.
  const gapTrack = useCallback((): TimelineTrack | null => {
    const selT = selClip
      ? tracks.find((t) => t.id === selClip.track_id) : null;
    if (selT && selT.visible && !selT.locked && compatibleTrack(selT)) {
      return selT;
    }
    return tracks.find(
      (t) => t.visible && !t.locked && compatibleTrack(t)) ?? null;
  }, [selClip, tracks]);

  const insertSpace = () => {
    const t = gapTrack();
    const n = parseInt(gapFrames, 10);
    if (!t || !Number.isInteger(n) || n <= 0) return;
    void onRun("timeline.gap.insert", {
      timeline_id: detail.id, track_id: t.id,
      at_frame: playheadF, frames: n, ...pin(),
    });
  };

  // N2-A — audio lanes: "+ audio" places a ready audio asset at the
  // playhead. Assets are fetched lazily the first time the picker
  // opens; the backend revalidates kind/probe anyway.
  const ensureAudioAssets = async () => {
    if (audioAssets !== null) return;
    try {
      const r = await apiFetch<{ items:
        { id: string; name: string; status: string }[] }>(
        `/api/projects/${detail.project_id}/assets?kind=audio&limit=100`);
      setAudioAssets(r.items.filter((a) => a.status === "ready"));
    } catch { setAudioAssets([]); }
  };
  const placeAudio = (t: TimelineTrack, assetId: string) => {
    void onRun("timeline.clip.place", {
      timeline_id: detail.id, track_id: t.id, asset_id: assetId,
      start_frames: playheadF, allow_duplicate: true, ...pin(),
    });
  };

  const closeGapAt = (t: TimelineTrack, atFrame: number) => {
    void onRun("timeline.gap.close", {
      timeline_id: detail.id, track_id: t.id,
      at_frame: atFrame, ...pin(),
    });
  };

  const tick = tickInterval(detail, ppf);
  const ticks: number[] = [];
  for (let f = 0; f <= programEnd + TAIL_F; f += tick) ticks.push(f);
  const draggingClip = dragRef.current?.clip ?? null;
  const trimmingClip = trimRef.current?.clip ?? null;

  /* ---------------- render ---------------- */

  return (
    <div className="mt-4" data-testid="timeline-editor">
      {/* toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <div className="flex items-center gap-1" role="group" aria-label="Zoom">
          <button onClick={() => zoom(1 / 1.4)} aria-label="Zoom out"
            className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted hover:text-fg">−</button>
          <button onClick={() => zoom(1.4)} aria-label="Zoom in"
            className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted hover:text-fg">+</button>
          <button onClick={zoomFit}
            className="rounded border border-line px-1.5 py-0.5 text-[11px] text-muted hover:text-fg">Fit</button>
        </div>
        <span className="text-[11px] text-faint" data-testid="playhead-readout">
          Playhead {fmtTc(playheadF, detail)}
        </span>
        <div className="flex items-center gap-1" role="group" aria-label="History">
          <button
            disabled={busy || !detail.undo_label}
            onClick={() => doUndo()}
            title={detail.undo_label
              ? `Undo: ${detail.undo_label} (Ctrl+Z)`
              : "Nothing to undo"}
            className="rounded border border-line px-2 py-0.5 text-[11px] text-muted hover:text-teal disabled:opacity-40"
            data-testid="undo-btn"
          >Undo</button>
          <button
            disabled={busy || !detail.redo_label}
            onClick={() => doRedo()}
            title={detail.redo_label
              ? `Redo: ${detail.redo_label} (Ctrl+Shift+Z)`
              : "Nothing to redo"}
            className="rounded border border-line px-2 py-0.5 text-[11px] text-muted hover:text-teal disabled:opacity-40"
            data-testid="redo-btn"
          >Redo</button>
        </div>
        {active && (
          <div className="flex flex-wrap items-center gap-2" role="group"
            aria-label="Editing">
            <button
              disabled={busy || !splitTarget()}
              onClick={() => { const c = splitTarget(); if (c) doSplit(c); }}
              title={splitTarget()
                ? "Split the clip under the playhead into two clips (S)"
                : "Place the playhead inside a clip on a whole source frame to split"}
              className="rounded border border-line px-2 py-0.5 text-[11px] text-muted hover:text-teal disabled:opacity-40"
              data-testid="split-at-playhead"
            >Split at playhead</button>
            <label
              className="flex items-center gap-1 rounded border border-line px-2 py-0.5 text-[11px] text-muted"
              title="Ripple: trimming or removing a clip shifts later clips on the SAME lane to fill or make room. Other lanes never move.">
              <input
                type="checkbox"
                checked={ripple}
                onChange={(e) => setRipple(e.target.checked)}
                data-testid="ripple-toggle"
              />
              Ripple edits (this lane)
            </label>
            <input
              value={gapFrames}
              onChange={(e) =>
                setGapFrames(e.target.value.replace(/[^0-9]/g, ""))}
              aria-label="Insert space frames"
              title="Frames to insert"
              className="w-12 rounded border border-line bg-panel px-1.5 py-0.5 text-[11px]"
            />
            <button
              disabled={busy || !gapTrack()}
              onClick={insertSpace}
              title="Insert this many empty frames at the playhead on the selected lane (or first editable lane) — later clips shift right"
              className="rounded border border-line px-2 py-0.5 text-[11px] text-muted hover:text-teal disabled:opacity-40"
              data-testid="insert-space"
            >Insert space</button>
            <button
              disabled={busy || !gapTrack()}
              onClick={() => { const t = gapTrack(); if (t) closeGapAt(t, playheadF); }}
              title="Close the gap at the playhead on the selected lane (or first editable lane) — later clips shift left"
              className="rounded border border-line px-2 py-0.5 text-[11px] text-muted hover:text-teal disabled:opacity-40"
              data-testid="close-gap"
            >Close gap</button>
          </div>
        )}
        <span className="ml-auto text-[10px] text-faint">
          Drag clips along a lane or between lanes · ←/→ nudges 1 frame
          (Shift = 1 s) · S splits at the playhead · drag a clip's right
          edge to trim · Esc cancels · History reverts the whole timeline
        </span>
      </div>
      {(dragView?.error ?? trimView?.error) && (
        <p role="alert" data-testid="drop-error"
          className="mt-1 rounded border border-red-500/40 bg-red-500/10 px-2 py-1 text-[11px] text-red-300">
          {dragView?.error ?? trimView?.error}
        </p>
      )}
      {trimView && !trimView.error && (
        <p data-testid="trim-status"
          className="mt-1 text-[11px] text-muted">
          Trim end → source frame {trimView.outF}
          {ripple ? " · later clips on this lane will shift" : ""}
        </p>
      )}

      {/* scroll surface: sticky headers + ruler inside one scroller */}
      <div
        ref={scrollRef}
        onWheel={onWheel}
        onScroll={(e) => {
          const el = e.currentTarget;
          // N1-E — viewport culling: only clips intersecting the
          // visible x-range (plus a one-screen draw margin) mount.
          setViewX((v) => {
            const l = Math.max(0, el.scrollLeft - el.clientWidth);
            const r = el.scrollLeft + el.clientWidth * 2;
            return v.l === l && v.r === r ? v : { l, r };
          });
        }}
        className="mt-2 max-h-[480px] overflow-auto rounded border border-line bg-panel"
        data-testid="timeline-scroll"
      >
        <div style={{ width: HEADER_W + contentW, position: "relative" }}>
          {/* ruler row */}
          <div className="flex" style={{ height: RULER_H }}>
            <div
              className="sticky left-0 z-30 flex items-center border-b border-r border-line bg-panel px-2 text-[10px] text-faint"
              style={{ width: HEADER_W }}
            >
              {detail.name} · {detail.fps_num}/{detail.fps_den} fps
            </div>
            <div
              className="sticky top-0 z-20 border-b border-line bg-panel"
              style={{ width: contentW, height: RULER_H, position: "relative", cursor: "pointer" }}
              onPointerDown={seek}
              role="slider"
              aria-label="Timeline ruler — set playhead"
              aria-valuemin={0}
              aria-valuemax={programEnd + TAIL_F}
              aria-valuenow={playheadF}
              data-testid="timeline-ruler"
            >
              {ticks.map((f) => (
                <span key={f}
                  className="absolute border-l border-line pl-1 text-[9px] text-faint"
                  style={{ left: frameToPx(f, ppf), top: 6, height: RULER_H - 6 }}>
                  {fmtTc(f, detail)}
                </span>
              ))}
            </div>
          </div>

          {/* lanes */}
          {tracks.map((t, ti) => {
            const lane = clipsByTrack.get(t.id) ?? [];
            const dimmed = !t.visible;
            return (
              <div key={t.id} className="flex" style={{ height: LANE_H }}
                data-testid={`lane-${t.id}`}>
                {/* track header — sticky left */}
                <div
                  className={`sticky left-0 z-10 flex flex-col justify-center gap-0.5 border-b border-r border-line bg-card px-2 ${dimmed ? "opacity-60" : ""}`}
                  style={{ width: HEADER_W }}
                >
                  <div className="flex items-center gap-1">
                    <span className={`rounded px-1 text-[9px] font-semibold uppercase ${
                      t.kind === "video" ? "bg-teal/15 text-teal"
                      : t.kind === "audio" ? "bg-action/20 text-action"
                      : "bg-card text-muted border border-line"}`}>
                      {t.kind === "video" ? `V${ti + 1}`
                        : t.kind === "audio" ? `A${ti + 1}` : `O${ti + 1}`}
                    </span>
                    {renamingTrack === t.id ? (
                      <input
                        autoFocus
                        defaultValue={t.name}
                        aria-label={`Rename ${t.name}`}
                        onKeyDown={(e) => {
                          const v = (e.target as HTMLInputElement).value.trim();
                          if (e.key === "Enter" && v) {
                            patchTrack(t, { name: v });
                            setRenamingTrack(null);
                          }
                          if (e.key === "Escape") setRenamingTrack(null);
                        }}
                        onBlur={() => setRenamingTrack(null)}
                        className="min-w-0 flex-1 rounded border border-line bg-panel px-1 text-[11px]"
                      />
                    ) : (
                      <button
                        onDoubleClick={() => setRenamingTrack(t.id)}
                        title={`${t.name} — double-click to rename`}
                        className="min-w-0 flex-1 truncate text-left text-[11px] text-muted hover:text-fg"
                      >{t.name}</button>
                    )}
                  </div>
                  <div className="flex items-center gap-0.5 text-[10px]">
                    <button
                      onClick={() => patchTrack(t, { visible: !t.visible })}
                      aria-label={`${t.visible ? "Hide" : "Show"} ${t.name}`}
                      title={t.visible
                        ? "Hide lane — clips are kept but not shown"
                        : "Show lane"}
                      className={`rounded border px-1 ${t.visible
                        ? "border-line text-muted hover:text-fg"
                        : "border-teal/50 text-teal"}`}
                    >{t.visible ? "Hide" : "Show"}</button>
                    <button
                      onClick={() => patchTrack(t, { locked: !t.locked })}
                      aria-label={`${t.locked ? "Unlock" : "Lock"} ${t.name}`}
                      title={t.locked
                        ? "Unlock lane — allow edits"
                        : "Lock lane — clips can't be moved, trimmed, or removed"}
                      className={`rounded border px-1 ${t.locked
                        ? "border-teal/50 text-teal"
                        : "border-line text-muted hover:text-fg"}`}
                    >{t.locked ? "Unlock" : "Lock"}</button>
                    <button
                      onClick={() => patchTrack(t, { muted: !t.muted })}
                      aria-label={`${t.muted ? "Unmute" : "Mute"} ${t.name}`}
                      title={t.muted
                        ? "Unmute lane"
                        : "Mute lane — silences this track's sound"}
                      className={`rounded border px-1 ${t.muted
                        ? "border-teal/50 text-teal"
                        : "border-line text-muted hover:text-fg"}`}
                    >{t.muted ? "Unmute" : "Mute"}</button>
                    <span className="mx-0.5 text-faint">·</span>
                    <button disabled={busy || ti === 0} onClick={() => moveTrack(t, -1)}
                      aria-label={`Move ${t.name} up`}
                      className="rounded border border-line px-1 text-muted hover:text-fg disabled:opacity-30">↑</button>
                    <button disabled={busy || ti === tracks.length - 1}
                      onClick={() => moveTrack(t, 1)}
                      aria-label={`Move ${t.name} down`}
                      className="rounded border border-line px-1 text-muted hover:text-fg disabled:opacity-30">↓</button>
                    <button
                      disabled={busy || lane.length > 0}
                      onClick={() => removeTrack(t)}
                      aria-label={`Remove ${t.name}`}
                      title={lane.length > 0
                        ? "Lane has clips — remove or move them first"
                        : "Remove this empty lane"}
                      className="rounded border border-line px-1 text-muted hover:text-red-300 disabled:opacity-30">✕</button>
                    {t.locked && (
                      <span className="text-[9px] text-faint">locked</span>
                    )}
                    {dimmed && <span className="text-[9px] text-faint">hidden</span>}
                    {t.muted && <span className="text-[9px] text-faint">muted</span>}
                    {t.kind === "audio" && (
                      <select
                        aria-label={`Add audio clip to ${t.name}`}
                        data-testid={`audio-pick-${t.id}`}
                        disabled={busy || t.locked || !active}
                        value=""
                        onFocus={() => void ensureAudioAssets()}
                        onChange={(e) => {
                          if (e.target.value) placeAudio(t, e.target.value);
                        }}
                        title="Place an audio clip at the playhead"
                        className="rounded border border-line bg-panel px-1 text-muted disabled:opacity-30"
                      >
                        <option value="">+ audio…</option>
                        {(audioAssets ?? []).map((a) => (
                          <option key={a.id} value={a.id}>{a.name}</option>))}
                      </select>
                    )}
                  </div>
                </div>
                {/* lane body */}
                <div
                  ref={(el) => {
                    if (el) lanesRef.current.set(t.id, el);
                    else lanesRef.current.delete(t.id);
                  }}
                  className={`relative border-b border-line ${dimmed ? "opacity-50" : ""}`}
                  style={{ width: contentW, height: LANE_H }}
                  data-testid={`lane-body-${t.id}`}
                >
                  {lane.length === 0 && (
                    <p className="absolute inset-0 flex items-center pl-3 text-[10px] text-faint">
                      {t.kind === "audio"
                        ? "Empty — pick an audio clip with “+ audio” above"
                        : "Empty — drag a clip here"}
                    </p>
                  )}
                  {lane.filter((c) => inView(
                    c.start_frames, clipTlEnd(c, detail)))
                    .map((c) => (
                    <LaneClip
                      key={c.id}
                      clip={c}
                      tl={detail}
                      ppf={ppf}
                      selected={selClipId === c.id}
                      ghosted={dragView?.clipId === c.id
                        || trimView?.clipId === c.id}
                      draggable={active && !busy && !t.locked}
                      onSelect={() => setSelClipId(c.id)}
                      onPointerDown={(e) => beginDrag(e, c)}
                      onTrimDown={(e) => beginTrim(e, c)}
                      onKeyDown={(e) => clipKey(e, c)}
                      trackKind={t.kind}
                      projectId={detail.project_id}
                      viewL={viewX.l / ppf}
                      viewR={viewX.r / ppf}
                    />
                  ))}
                  {/* N1-C — explicit gaps: rendered, labeled, closable. */}
                  {laneGaps(lane, detail).filter((g) => inView(
                    g.start, g.start + g.frames)).map((g) => (
                    <button
                      key={`gap-${g.start}`}
                      type="button"
                      disabled={!active || busy || t.locked}
                      onClick={() => closeGapAt(t, Math.floor(g.start + g.frames / 2))}
                      title={t.locked
                        ? `Gap — ${g.frames} frame${g.frames === 1 ? "" : "s"} (lane locked)`
                        : `Gap — ${g.frames} frame${g.frames === 1 ? "" : "s"} — click to close`}
                      className="absolute top-1.5 rounded border border-dashed border-line text-[9px] text-faint hover:border-teal/60 hover:text-teal disabled:cursor-default disabled:hover:border-line disabled:hover:text-faint"
                      style={{
                        left: frameToPx(g.start, ppf) + 2,
                        width: Math.max(6, g.frames * ppf - 4),
                        height: LANE_H - 12,
                      }}
                      data-testid="lane-gap"
                    >
                      {g.frames * ppf >= 44 ? `gap ${g.frames}f` : ""}
                    </button>
                  ))}
                  {trimView && trimmingClip && trimView.trackId === t.id && (
                    <div
                      className={`absolute top-1.5 rounded border-2 ${
                        trimView.error
                          ? "border-red-400/70 bg-red-400/10"
                          : "border-teal bg-teal/20"}`}
                      style={{
                        left: frameToPx(trimmingClip.start_frames, ppf),
                        width: Math.max(4,
                          (trimView.endF - trimmingClip.start_frames) * ppf),
                        height: LANE_H - 12,
                      }}
                      data-testid="trim-ghost"
                    />
                  )}
                  {dragView && draggingClip && dragView.trackId === t.id && (
                    <div
                      className={`absolute top-1.5 rounded border-2 border-dashed ${
                        dragView.error
                          ? "border-red-400/70 bg-red-400/10"
                          : "border-teal/70 bg-teal/10"}`}
                      style={{
                        left: frameToPx(dragView.startF, ppf),
                        width: Math.max(4,
                          clipTlExtent(draggingClip, detail) * ppf),
                        height: LANE_H - 12,
                      }}
                      data-testid="drag-ghost"
                    />
                  )}
                </div>
              </div>
            );
          })}

          {/* orphan clip warning — defensive; shouldn't occur in N1-A+ */}
          {orphans.length > 0 && (
            <p role="alert" className="border-t border-red-500/40 bg-red-500/10 px-3 py-1 text-[11px] text-red-300">
              {orphans.length} clip(s) aren't on a lane — refresh or check
              History; the backend never drops them, but they need a track.
            </p>
          )}

          {/* playhead */}
          <div
            aria-hidden
            className="pointer-events-none absolute bg-red-400/80"
            style={{
              left: HEADER_W + frameToPx(playheadF, ppf) - 1,
              top: 0, bottom: 0, width: 2, zIndex: 25,
            }}
            data-testid="playhead"
          />
        </div>
      </div>

      {/* add lane */}
      {active && (
        <div className="mt-2 flex items-center gap-1">
          <select
            value={addingKind}
            onChange={(e) => setAddingKind(e.target.value)}
            aria-label="New lane kind"
            className="rounded border border-line bg-panel px-1.5 py-0.5 text-[11px] text-muted"
          >
            {TRACK_KINDS.map((k) => (
              <option key={k} value={k}>{KIND_LABEL[k]}</option>
            ))}
          </select>
          <button onClick={addTrack} disabled={busy}
            className="rounded border border-line px-2 py-0.5 text-[11px] text-muted hover:text-teal disabled:opacity-40">
            + Add lane
          </button>
        </div>
      )}

      {/* selected-clip inspector */}
      {selClip && (
        <ClipInspector
          clip={selClip}
          tl={detail}
          track={tracks.find((t) => t.id === selClip.track_id) ?? null}
          active={active}
          busy={busy}
          ripple={ripple}
          onClose={() => setSelClipId(null)}
          onRun={onRun}
          onRemove={() => onRemoveClip(selClip, ripple)}
        />
      )}
      {!selClip && detail.clips.length === 0 && (
        <p className="mt-4 text-center text-xs text-muted"
          data-testid="editor-empty">
          This timeline is empty. Generate a video clip on the Storyboard,
          then use “Send to timeline” — it'll land on the first video lane.
        </p>
      )}
    </div>
  );
}

/* ---------------- N2-B — audio waveform rendering ---------------- */

/** Canvas peaks for one audio clip. Bucket math is absolute-source
 * (bucket b = samples [b*spb,(b+1)*spb)) so a clip's sample trims map
 * to bucket ranges with plain arithmetic — no drift, no per-clip
 * re-bucketing. Fetches are viewport-bounded via the level API. */
const WaveformCanvas = memo(function WaveformCanvas({
  clip, tl, ppf, projectId, viewL, viewR,
}: {
  clip: TimelineClip;
  tl: TimelineDetail;
  ppf: number;
  projectId: string;
  viewL: number;
  viewR: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [state, setState] = useState<"loading" | "missing" | "error" | "ok">("loading");
  const [tries, setTries] = useState(0);
  const tlFps = tl.fps_num / tl.fps_den;
  const extent = clipTlExtent(clip, tl);
  const win = clipWaveWindow(clip, tlFps, viewL, viewR, extent);
  const samplesPerPx = win ? win.samplesPerTlFrame / ppf : 1;

  const [manifest, setManifest] = useState<ManifestResult | null>(null);
  // Poll gently while no waveform exists so a just-finished build
  // appears without a reload — capped at ~2.5 min, then it rests.
  // A real fetch failure shows "waveform unavailable" immediately.
  useEffect(() => {
    let dead = false;
    let attempts = 0;
    const ask = () => {
      if (dead) return;
      // Bypass the manifest cache — a cached "missing" would hide a
      // waveform whose build job just completed.
      invalidateWaveform(projectId, clip.source_asset_id);
      void getWaveformManifest(projectId, clip.source_asset_id)
        .then((m) => {
          if (dead) return;
          if (m === "missing") {
            if (++attempts < 40) setTimeout(ask, 4000);
            else setState("missing");
          } else {
            setManifest(m);
            setState(m === "error" ? "error" : "ok");
          }
        });
    };
    ask();
    return () => { dead = true; };
  }, [projectId, clip.source_asset_id, tries]);

  const level = manifest && manifest !== "error" && manifest !== "missing"
    ? pickLevel(manifest, samplesPerPx) : null;
  const b0 = level && win ? Math.max(0, Math.floor(win.s0 / level.spb)) : 0;
  const b1 = level && win
    ? Math.min(level.buckets, Math.ceil(win.s1 / level.spb)) : 0;
  // Canvas covers ONLY the visible window — bitmap stays bounded even
  // for multi-hour clips, and scroll/zoom refetches just that range.
  const dpr = Math.max(1, Math.min(4,
    typeof window === "undefined" ? 1 : window.devicePixelRatio || 1));
  const canvasW = win ? Math.min(MAX_BMP, Math.round(win.winW * ppf)) : 0;

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !level || !win || b1 <= b0) return;
    setState("loading");
    let dead = false;
    void getWaveformSlice(
      projectId, clip.source_asset_id, level.spb, b0, b1)
      .then((sl) => {
        if (dead) return;
        if (!sl) { setState("error"); return; }
        const ctx = canvas.getContext("2d");
        if (!ctx) { setState("ok"); return; }
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        const H = 44, W = canvasW;
        ctx.clearRect(0, 0, W, H);
        ctx.fillStyle = "rgba(45, 212, 191, 0.55)";
        const rows = Math.max(1, sl.channels);
        const rowH = H / rows;
        const pxPerSample = ppf / win.samplesPerTlFrame;
        for (let ch = 0; ch < rows; ch++) {
          const mid = rowH * ch + rowH / 2;
          const half = rowH / 2 - 1;
          sl.peaks[ch].forEach(([mn, mx], i) => {
            const b = sl.bucket_start + i;
            const x = (b * level.spb - clip.in_frame) * pxPerSample
              - win.winL * ppf;
            const w = Math.max(1, level.spb * pxPerSample);
            const y0 = mid + (mn / 32768) * half;
            const y1 = mid + (mx / 32768) * half;
            ctx.fillRect(x, y0, w, Math.max(1, y1 - y0));
          });
        }
        setState("ok");
      });
    return () => { dead = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId, clip.source_asset_id, clip.in_frame, clip.out_frame,
      level?.spb, b0, b1, ppf, canvasW, win?.winL]);

  return (
    <div className="pointer-events-none absolute inset-0" aria-hidden>
      {win && (
        <canvas ref={canvasRef}
          width={Math.max(1, Math.round(canvasW * dpr))}
          height={Math.round(44 * dpr)}
          className="absolute top-0 h-full opacity-70"
          style={{ left: win.winL * ppf, width: canvasW }} />
      )}
      {state !== "ok" && (
        <span className="absolute bottom-0.5 right-1 text-[8px] text-faint">
          {state === "loading" ? "waveform…"
            : state === "missing" ? "no waveform"
            : "waveform unavailable"}
        </span>
      )}
      {manifest === "error" && (
        <button
          className="pointer-events-auto absolute bottom-0.5 left-1 text-[8px] text-teal"
          onClick={(e) => { e.stopPropagation(); setTries((x) => x + 1); }}
        >retry</button>
      )}
    </div>
  );
});
const MAX_BMP = 16384;

const LaneClip = memo(function LaneClip({ clip, tl, ppf, selected, ghosted,
  draggable, onSelect, onPointerDown, onTrimDown, onKeyDown,
  trackKind, projectId, viewL, viewR }: {
  clip: TimelineClip;
  tl: TimelineDetail;
  ppf: number;
  selected: boolean;
  ghosted: boolean;
  draggable: boolean;
  onSelect: () => void;
  onPointerDown: (e: React.PointerEvent) => void;
  onTrimDown: (e: React.PointerEvent) => void;
  onKeyDown: (e: React.KeyboardEvent) => void;
  trackKind?: string;
  projectId: string;
  viewL?: number;
  viewR?: number;
}) {
  const w = Math.max(4, clipTlExtent(clip, tl) * ppf);
  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`Clip ${clip.source_sha256.slice(0, 10)} at frame ${clip.start_frames}`}
      aria-pressed={selected}
      data-testid="lane-clip"
      onClick={onSelect}
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
      title={`${clip.source_sha256.slice(0, 12)}… · starts frame ${clip.start_frames} · ${fmtTc(clip.start_frames, tl)}`}
      className={`absolute top-1.5 flex h-[44px] min-w-0 items-center overflow-hidden rounded border px-1 text-[10px] ${
        draggable ? "cursor-grab" : "cursor-default"
      } ${
        selected
          ? "border-teal bg-teal/15 text-teal ring-1 ring-teal"
          : "border-line bg-card text-muted hover:border-teal/60"
      } ${ghosted ? "opacity-40" : ""}`}
      style={{ left: frameToPx(clip.start_frames, ppf), width: w }}
    >
      {trackKind === "audio" && (
        <WaveformCanvas
          clip={clip} tl={tl} ppf={ppf}
          projectId={projectId} viewL={viewL ?? clip.start_frames}
          viewR={viewR ?? clipTlEnd(clip, tl)} />
      )}
      <span className="relative truncate">
        {clip.source_sha256.slice(0, 10)}… · {clipTlEnd(clip, tl) - clip.start_frames}f
      </span>
      {draggable && w >= 18 && (
        <div
          role="separator"
          aria-label={`Trim end of clip ${clip.source_sha256.slice(0, 10)}`}
          title="Drag to trim the clip's end — source-frame exact"
          onPointerDown={onTrimDown}
          className="absolute right-0 top-0 h-full w-2 cursor-ew-resize bg-teal/0 hover:bg-teal/40"
          data-testid="trim-handle"
        />
      )}
    </div>
  );
});

function ClipInspector({ clip, tl, track, active, busy, ripple, onClose, onRun, onRemove }: {
  clip: TimelineClip;
  tl: TimelineDetail;
  track: TimelineTrack | null;
  active: boolean;
  busy: boolean;
  ripple: boolean;
  onClose: () => void;
  onRun: (t: string, p: Record<string, unknown>) => Promise<boolean>;
  onRemove: () => void;
}) {
  const [inF, setInF] = useState(String(clip.in_frame));
  const [outF, setOutF] = useState(String(clip.out_frame));
  const [startF, setStartF] = useState(String(clip.start_frames));
  useEffect(() => {
    setInF(String(clip.in_frame));
    setOutF(String(clip.out_frame));
    setStartF(String(clip.start_frames));
  }, [clip.id, clip.in_frame, clip.out_frame, clip.start_frames]);
  const locked = !active || (track?.locked ?? false);
  const isAudio = track?.kind === "audio";
  const dur = (clip.out_frame - clip.in_frame)
    * clip.src_fps_den / clip.src_fps_num;
  return (
    <div className="mt-3 rounded border border-teal/40 bg-card p-3"
      data-testid="clip-inspector">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold text-teal">Selected clip</span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-muted">
          {clip.source_sha256.slice(0, 12)}… on {track?.name ?? "no lane"}
        </span>
        <button onClick={onClose} aria-label="Close inspector"
          className="rounded border border-line px-1.5 py-0.5 text-[10px] text-muted hover:text-fg">✕</button>
      </div>
      <p className="mt-1 text-[11px] text-muted">
        Timeline frames {clip.start_frames}–{clipTlEnd(clip, tl)}
        {" · "}{fmtTc(clip.start_frames, tl)} · source {Math.round(dur * 100) / 100}s,
        {isAudio
          ? ` trims ${clip.in_frame}–${clip.out_frame} of`
            + ` ${clip.src_frames} samples @ ${clip.src_fps_num} Hz`
          : ` trims ${clip.in_frame}–${clip.out_frame} of`
            + ` ${clip.src_frames} @ ${clip.src_fps_num}/`
            + `${clip.src_fps_den} fps`}
      </p>
      {!locked && (
        <div className="mt-2 flex flex-wrap items-end gap-2 text-[10px]">
          <label className="text-muted">In
            <input value={inF} onChange={(e) => setInF(e.target.value)}
              inputMode="numeric" aria-label="Trim in frame"
              className="mt-0.5 block w-16 rounded border border-line bg-panel px-1 py-0.5" />
          </label>
          <label className="text-muted">Out
            <input value={outF} onChange={(e) => setOutF(e.target.value)}
              inputMode="numeric" aria-label="Trim out frame"
              className="mt-0.5 block w-16 rounded border border-line bg-panel px-1 py-0.5" />
          </label>
          <button
            disabled={busy}
            onClick={() => {
              const i = parseInt(inF, 10), o = parseInt(outF, 10);
              if (Number.isInteger(i) && Number.isInteger(o)) {
                void onRun("timeline.clip.trim", {
                  timeline_id: tl.id, clip_id: clip.id,
                  in_frame: i, out_frame: o,
                  ...(ripple ? { ripple_scope: "track" } : {}),
                  expected_timeline: { id: tl.id, revision: tl.revision } });
              }
            }}
            title={ripple
              ? "Trim with ripple — later clips on this lane shift to fill or make room"
              : "Trim in place — later clips stay put"}
            className="rounded bg-action px-2 py-0.5 text-white disabled:opacity-40"
          >Apply trim</button>
          <label className="text-muted">Start frame
            <input value={startF} onChange={(e) => setStartF(e.target.value)}
              inputMode="numeric" aria-label="Start frame"
              className="mt-0.5 block w-16 rounded border border-line bg-panel px-1 py-0.5" />
          </label>
          <button
            disabled={busy}
            onClick={() => {
              const f = parseInt(startF, 10);
              if (Number.isInteger(f) && f >= 0 && track) {
                void onRun("timeline.clip.move", {
                  timeline_id: tl.id, clip_id: clip.id,
                  to_track_id: track.id, start_frames: f });
              }
            }}
            className="rounded border border-line px-2 py-0.5 text-muted hover:text-teal disabled:opacity-40"
          >Move to frame</button>
          <button
            disabled={busy}
            onClick={onRemove}
            className="rounded border border-line px-2 py-0.5 text-muted hover:text-red-300 disabled:opacity-40"
          >Remove</button>
          {track?.kind === "audio" && (
            <button
              disabled={busy}
              onClick={() => {
                invalidateWaveform(
                  tl.project_id, clip.source_asset_id);
                void onRun("asset.waveform", {
                  asset_id: clip.source_asset_id });
              }}
              title={"Queue a durable job that decodes the audio and "
                + "stores min/max envelope levels in CAS — the "
                + "waveform appears on this clip when it finishes"}
              className="rounded border border-line px-2 py-0.5 text-muted hover:text-teal disabled:opacity-40"
            >Build waveform</button>
          )}
        </div>
      )}
      {locked && (
        <p className="mt-2 text-[10px] text-faint">
          {track?.locked
            ? "This lane is locked — unlock it to trim, move, or remove."
            : "Timeline is archived — read-only."}
        </p>
      )}
    </div>
  );
}
