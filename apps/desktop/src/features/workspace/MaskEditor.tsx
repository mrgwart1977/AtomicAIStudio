/** C3-D4 — beginner-friendly visual mask editor.
 *
 * Paints the editable region over a project image at 1:1 source
 * resolution: strokes land in an offscreen mask bitmap the size of the
 * source image (never view-scaled), so zoom/pan cannot distort the
 * exported mask. Export follows the documented convention — white
 * (luminance ≥128) = editable — and the PNG uploads through
 * assets/upload-source → asset.import, landing as a normal project
 * asset the inpaint mode selects.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "../../api/client";
import { commitCommand } from "../../api/commands";
import { clampImage, editablePixels, viewToImage } from "./editorMath";
import type { ViewTransform } from "./editorMath";

const btnCls = "rounded-md bg-teal px-3 py-1.5 text-sm font-medium text-black hover:bg-teal/80 disabled:opacity-50";
const ghostBtn = "rounded-md border border-line px-3 py-1.5 text-sm text-fg hover:bg-panel disabled:opacity-50";

interface Props {
  pid: string;
  /** Project revision for the asset.import command. */
  revision: number;
  /** URL of the source asset image bytes (served by the backend). */
  imageUrl: string | null;
  /** Called when the painted mask has been imported as a project asset. */
  onMaskImported: (assetId: string) => void;
  onNotice?: (msg: string) => void;
  onError?: (msg: string) => void;
}

export function MaskEditor({ pid, revision, imageUrl, onMaskImported, onNotice, onError }: Props) {
  const viewRef = useRef<HTMLCanvasElement>(null);
  const maskRef = useRef<HTMLCanvasElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const strokes = useRef<ImageData[]>([]);       // undo stack
  const redoStack = useRef<ImageData[]>([]);
  // Bounded history: snapshots are w*h*4 bytes each — cap both count
  // and total bytes so a large mask can't exhaust memory through
  // unlimited undo. Oldest entries drop out (linear painting stays).
  const UNDO_MAX = 20;
  const UNDO_MAX_BYTES = 64 * 1024 * 1024;
  const undoBytes = useRef(0);
  const painting = useRef(false);
  const [view, setView] = useState<ViewTransform>({ scale: 1, ox: 0, oy: 0 });
  const [tool, setTool] = useState<"brush" | "erase">("brush");
  const [brush, setBrush] = useState(16);
  const [opacity, setOpacity] = useState(0.5);
  const [dims, setDims] = useState<{ w: number; h: number } | null>(null);
  const [dirty, setDirty] = useState(0);          // bump to repaint
  const [editable, setEditable] = useState(0);
  const [busy, setBusy] = useState(false);

  const pan = useRef<{ x: number; y: number } | null>(null);

  const redraw = useCallback(() => {
    const canvas = viewRef.current, img = imgRef.current, mask = maskRef.current;
    if (!canvas || !img) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.imageSmoothingEnabled = false;
    ctx.save();
    ctx.translate(view.ox, view.oy);
    ctx.scale(view.scale, view.scale);
    ctx.drawImage(img, 0, 0);
    if (mask) {
      ctx.globalAlpha = opacity;
      ctx.drawImage(mask, 0, 0);
      ctx.globalAlpha = 1;
    }
    ctx.restore();
  }, [view, opacity]);

  useEffect(redraw, [redraw, dirty]);

  // Load the source image; size the mask to its exact pixel dims.
  useEffect(() => {
    if (!imageUrl) { imgRef.current = null; setDims(null); return; }
    const img = new Image();
    img.onload = () => {
      imgRef.current = img;
      const w = img.naturalWidth, h = img.naturalHeight;
      const m = document.createElement("canvas");
      m.width = w; m.height = h;
      maskRef.current = m;
      strokes.current = []; redoStack.current = []; undoBytes.current = 0;
      setDims({ w, h }); setEditable(0);
      const canvas = viewRef.current;
      if (canvas) {
        const s = Math.min(canvas.width / w, canvas.height / h, 1);
        setView({ scale: s, ox: (canvas.width - w * s) / 2, oy: (canvas.height - h * s) / 2 });
      }
      setDirty((d) => d + 1);
    };
    img.src = imageUrl;
  }, [imageUrl]);

  const countEditable = useCallback(() => {
    const m = maskRef.current;
    if (!m) return 0;
    const data = m.getContext("2d")!.getImageData(0, 0, m.width, m.height).data;
    return editablePixels(data);
  }, []);

  const stamp = (ix: number, iy: number) => {
    const m = maskRef.current;
    if (!m || !dims) return;
    const ctx = m.getContext("2d")!;
    ctx.fillStyle = tool === "brush" ? "#ffffff" : "#00000000";
    ctx.globalCompositeOperation = tool === "brush" ? "source-over" : "destination-out";
    ctx.beginPath();
    ctx.arc(ix + 0.5, iy + 0.5, brush / 2, 0, Math.PI * 2);
    ctx.fill();
  };

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const m = maskRef.current;
    if (!m || !dims) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    if (e.button === 1 || e.shiftKey) {          // pan
      pan.current = { x: e.clientX, y: e.clientY };
      return;
    }
    const mctx = m.getContext("2d")!;
    const snap = mctx.getImageData(0, 0, m.width, m.height);
    strokes.current.push(snap);
    undoBytes.current += snap.data.byteLength;
    while (strokes.current.length > UNDO_MAX
           || undoBytes.current > UNDO_MAX_BYTES) {
      const old = strokes.current.shift();
      if (!old) break;
      undoBytes.current -= old.data.byteLength;
    }
    redoStack.current = [];
    painting.current = true;
    const r = e.currentTarget.getBoundingClientRect();
    const p = viewToImage(e.clientX - r.left, e.clientY - r.top, view, dims.w, dims.h);
    const c = clampImage(p.x, p.y, dims.w, dims.h);
    stamp(Math.floor(c.x), Math.floor(c.y));
    setEditable(countEditable());
    setDirty((d) => d + 1);
  };

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (pan.current) {
      setView((v) => ({ ...v, ox: v.ox + (e.clientX - pan.current!.x),
                              oy: v.oy + (e.clientY - pan.current!.y) }));
      pan.current = { x: e.clientX, y: e.clientY };
      return;
    }
    if (!painting.current || !dims) return;
    const r = e.currentTarget.getBoundingClientRect();
    const p = viewToImage(e.clientX - r.left, e.clientY - r.top, view, dims.w, dims.h);
    const c = clampImage(p.x, p.y, dims.w, dims.h);
    stamp(Math.floor(c.x), Math.floor(c.y));
    setEditable(countEditable());
    setDirty((d) => d + 1);
  };

  const onPointerUp = () => { painting.current = false; pan.current = null; };

  const onWheel = (e: React.WheelEvent<HTMLCanvasElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    const cx = e.clientX - r.left, cy = e.clientY - r.top;
    const f = e.deltaY < 0 ? 1.15 : 1 / 1.15;
    setView((v) => {
      const scale = Math.min(16, Math.max(0.05, v.scale * f));
      const k = scale / v.scale;                 // zoom around the pointer
      return { scale, ox: cx - (cx - v.ox) * k, oy: cy - (cy - v.oy) * k };
    });
  };

  const undo = () => {
    const m = maskRef.current, prev = strokes.current.pop();
    if (!m || !prev) return;
    undoBytes.current -= prev.data.byteLength;
    redoStack.current.push(m.getContext("2d")!.getImageData(0, 0, m.width, m.height));
    m.getContext("2d")!.putImageData(prev, 0, 0);
    setEditable(countEditable()); setDirty((d) => d + 1);
  };
  const redo = () => {
    const m = maskRef.current, next = redoStack.current.pop();
    if (!m || !next) return;
    const again = m.getContext("2d")!.getImageData(0, 0, m.width, m.height);
    strokes.current.push(again);
    undoBytes.current += again.data.byteLength;
    m.getContext("2d")!.putImageData(next, 0, 0);
    setEditable(countEditable()); setDirty((d) => d + 1);
  };

  const exportAndImport = async () => {
    const m = maskRef.current;
    if (!m || !dims || editable === 0 || busy) return;
    setBusy(true);
    try {
      const blob: Blob = await new Promise((res, rej) =>
        m.toBlob((b) => (b ? res(b) : rej(new Error("mask export failed"))), "image/png"));
      const up = await apiFetch<{ path: string }>(
        `/api/projects/${pid}/assets/upload-source`, {
          method: "POST", body: blob,
          headers: { "Content-Type": "application/octet-stream" },
        });
      const r = await commitCommand(pid, revision, "asset.import", {
        source_path: up.path, name: "painted-mask.png", kind: "image",
      });
      const jobId = (r as { job_id?: string }).job_id;
      if (jobId) {
        // Poll briefly for the durable import job to materialize the row.
        for (let i = 0; i < 40; i++) {
          await new Promise((s) => setTimeout(s, 300));
          const jobs = await apiFetch<{ id: string; status: string; output_asset_id?: string }[]>(
            "/api/jobs");
          const j = jobs.find((x) => x.id === jobId);
          if (j?.status === "succeeded" && j.output_asset_id) {
            onMaskImported(j.output_asset_id);
            onNotice?.("Mask imported — selected as the inpaint mask.");
            setBusy(false);
            return;
          }
          if (j && (j.status === "failed" || j.status === "cancelled")) break;
        }
      }
      onError?.("Mask import job did not complete");
    } catch (e) {
      onError?.(String(e));
    } finally {
      setBusy(false);
    }
  };

  const canExport = editable > 0 && !busy;

  return (
    <div className="mt-2" data-testid="mask-editor">
      <canvas ref={viewRef} width={560} height={360}
        className="w-full cursor-crosshair rounded border border-line bg-black/40"
        data-testid="mask-canvas"
        onPointerDown={onPointerDown} onPointerMove={onPointerMove}
        onPointerUp={onPointerUp} onPointerLeave={onPointerUp}
        onWheel={onWheel} />
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
        <button type="button" className={tool === "brush" ? btnCls : ghostBtn}
          data-testid="me-brush" onClick={() => setTool("brush")}>Brush</button>
        <button type="button" className={tool === "erase" ? btnCls : ghostBtn}
          data-testid="me-erase" onClick={() => setTool("erase")}>Eraser</button>
        <label className="flex items-center gap-1 text-faint">
          Size
          <input type="range" min={2} max={80} value={brush}
            aria-label="brush size" data-testid="me-size"
            onChange={(e) => setBrush(Number(e.target.value))} />
          {brush}px
        </label>
        <label className="flex items-center gap-1 text-faint">
          Overlay
          <input type="range" min={0.1} max={1} step={0.05} value={opacity}
            aria-label="mask overlay opacity" data-testid="me-opacity"
            onChange={(e) => setOpacity(Number(e.target.value))} />
        </label>
        <button type="button" className={ghostBtn} data-testid="me-undo"
          onClick={undo} aria-label="undo brush stroke">Undo</button>
        <button type="button" className={ghostBtn} data-testid="me-redo"
          onClick={redo} aria-label="redo brush stroke">Redo</button>
        <button type="button" className={btnCls} disabled={!canExport}
          data-testid="me-export" onClick={exportAndImport}>
          {busy ? "Importing…" : "Use as inpaint mask"}
        </button>
      </div>
      <p className="mt-1 text-[10px] text-faint" data-testid="me-status">
        {dims ? `${dims.w}×${dims.h} — ` : ""}
        {editable} editable pixel{editable === 1 ? "" : "s"} painted · wheel = zoom ·
        shift-drag or middle-drag = pan · white = editable
      </p>
      {!imageUrl && (
        <p className="mt-1 text-xs text-faint">
          Pick a source image above to start painting the mask.
        </p>
      )}
    </div>
  );
}
