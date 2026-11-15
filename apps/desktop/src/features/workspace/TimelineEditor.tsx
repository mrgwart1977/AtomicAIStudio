import {
  memo, useCallback, useEffect, useMemo, useRef, useState,
} from "react";
import type {
  TimelineClip, TimelineDetail, TimelineTrack,
} from "../../api/types";
import {
  clipTlEnd, clipTlExtent, compatibleTrack, dropError, fmtTc,
  frameToPx, laneClips, programEndFrames, snapStart, tickInterval,
  TRACK_KINDS, KIND_LABEL,
} from "./timelineMath";

/** N1-B — multitrack timeline editor: lanes with track controls,
 * ruler + playhead, zoom/scroll, and pixel-exact drag placement.
 * Drags mutate nothing until pointerup commits `timeline.clip.move`;
 * Escape cancels. The backend stays authoritative — the UI mirrors
 * its rules (locked lanes, overlap, compatibility) only to give
 * honest live feedback. */

const HEADER_W = 176;   // px — track header column
const RULER_H = 28;     // px — ruler row
const LANE_H = 56;      // px — one lane
const TAIL_F = 48;      // blank frames of scroll room past program end

export function TimelineEditor({ detail, active, busy, onRun, onRemoveClip }: {
  detail: TimelineDetail;
  active: boolean;
  busy: boolean;
  onRun: (type: string, payload: Record<string, unknown>) => Promise<boolean>;
  onRemoveClip: (clip: TimelineClip) => void;
}) {
  const [ppf, setPpf] = useState(6);                 // px per frame (zoom)
  const [playheadF, setPlayheadF] = useState(0);
  const [selClipId, setSelClipId] = useState<string | null>(null);
  const [renamingTrack, setRenamingTrack] = useState<string | null>(null);
  const [dragView, setDragView] = useState<{
    clipId: string; trackId: string; startF: number;
    error: string | null; snapped: boolean;
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
  const selClip = detail.clips.find((c) => c.id === selClipId) ?? null;

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
    const error = dropError(track, snap.start, d.clip, laneClipList, detail);
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
    });
  }, [detail.id, endDrag, onRun]);

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
        to_track_id: track.id, start_frames: startF,
      });
    } else if ((e.ctrlKey || e.metaKey)
               && (e.key === "ArrowUp" || e.key === "ArrowDown")) {
      e.preventDefault();
      const idx = tracks.findIndex((t) => t.id === track.id);
      const next = tracks[idx + (e.key === "ArrowDown" ? 1 : -1)];
      if (!next || next.locked || !compatibleTrack(next)) return;
      const lane = clipsByTrack.get(next.id) ?? [];
      if (dropError(next, clip.start_frames, clip, lane, detail)) return;
      void onRun("timeline.clip.move", {
        timeline_id: detail.id, clip_id: clip.id,
        to_track_id: next.id, start_frames: clip.start_frames,
      });
    } else if (e.key === "Delete" || e.key === "Backspace") {
      if (track.locked) return;
      e.preventDefault();
      onRemoveClip(clip);
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

  const tick = tickInterval(detail, ppf);
  const ticks: number[] = [];
  for (let f = 0; f <= programEnd + TAIL_F; f += tick) ticks.push(f);
  const draggingClip = dragRef.current?.clip ?? null;

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
        <span className="ml-auto text-[10px] text-faint">
          Drag clips along a lane or between lanes · ←/→ nudges 1 frame
          (Shift = 1 s) · Esc cancels a drag · no undo — use History to
          revert the whole timeline
        </span>
      </div>
      {dragView?.error && (
        <p role="alert" data-testid="drop-error"
          className="mt-1 rounded border border-red-500/40 bg-red-500/10 px-2 py-1 text-[11px] text-red-300">
          {dragView.error}
        </p>
      )}

      {/* scroll surface: sticky headers + ruler inside one scroller */}
      <div
        ref={scrollRef}
        onWheel={onWheel}
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
                      {compatibleTrack(t)
                        ? "Empty — drag a clip here"
                        : "Audio lane — audio clips aren't supported yet"}
                    </p>
                  )}
                  {lane.map((c) => (
                    <LaneClip
                      key={c.id}
                      clip={c}
                      tl={detail}
                      ppf={ppf}
                      selected={selClipId === c.id}
                      ghosted={dragView?.clipId === c.id}
                      draggable={active && !busy && !t.locked}
                      onSelect={() => setSelClipId(c.id)}
                      onPointerDown={(e) => beginDrag(e, c)}
                      onKeyDown={(e) => clipKey(e, c)}
                    />
                  ))}
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
          onClose={() => setSelClipId(null)}
          onRun={onRun}
          onRemove={() => onRemoveClip(selClip)}
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

const LaneClip = memo(function LaneClip({ clip, tl, ppf, selected, ghosted,
  draggable, onSelect, onPointerDown, onKeyDown }: {
  clip: TimelineClip;
  tl: TimelineDetail;
  ppf: number;
  selected: boolean;
  ghosted: boolean;
  draggable: boolean;
  onSelect: () => void;
  onPointerDown: (e: React.PointerEvent) => void;
  onKeyDown: (e: React.KeyboardEvent) => void;
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
      <span className="truncate">
        {clip.source_sha256.slice(0, 10)}… · {clipTlEnd(clip, tl) - clip.start_frames}f
      </span>
    </div>
  );
});

function ClipInspector({ clip, tl, track, active, busy, onClose, onRun, onRemove }: {
  clip: TimelineClip;
  tl: TimelineDetail;
  track: TimelineTrack | null;
  active: boolean;
  busy: boolean;
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
        trims {clip.in_frame}–{clip.out_frame} of {clip.src_frames}
        {" @ "}{clip.src_fps_num}/{clip.src_fps_den} fps
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
                  in_frame: i, out_frame: o });
              }
            }}
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
