/** C3-D4 — pure canvas/geometry helpers for the visual image editor.
 * Kept component-free so coordinate math and placement rules are unit-
 * testable and stay in lockstep with the backend convention
 * (app/domain/image_edit.py). */

export interface ViewTransform { scale: number; ox: number; oy: number }

/** Canvas-pixel → source-image pixel (image space, 0..w-1/0..h-1). */
export function viewToImage(cx: number, cy: number, v: ViewTransform,
                            _w: number, _h: number): { x: number; y: number } {
  return { x: (cx - v.ox) / v.scale, y: (cy - v.oy) / v.scale };
}

/** Clamp an image-space point inside the source bounds (edge strokes
 * must not stamp outside the mask bitmap). */
export function clampImage(x: number, y: number, w: number, h: number) {
  return { x: Math.min(w - 1, Math.max(0, x)), y: Math.min(h - 1, Math.max(0, y)) };
}

/** Mirror of backend compute_pad: anchor → directional pad. Expansion
 * concentrates opposite the anchor; center splits (odd px → right/bottom). */
export const PLACEMENTS = [
  "center", "top", "bottom", "left", "right",
  "top_left", "top_right", "bottom_left", "bottom_right",
] as const;
export type Placement = typeof PLACEMENTS[number];

export function computePad(srcW: number, srcH: number, outW: number,
                           outH: number, placement: Placement) {
  const dw = outW - srcW, dh = outH - srcH;
  const left: Record<string, number> = {
    left: 0, top_left: 0, bottom_left: 0,
    right: dw, top_right: dw, bottom_right: dw };
  const top: Record<string, number> = {
    top: 0, top_left: 0, top_right: 0,
    bottom: dh, bottom_left: dh, bottom_right: dh };
  const l = placement in left ? left[placement] : Math.floor(dw / 2);
  const t = placement in top ? top[placement] : Math.floor(dh / 2);
  return { left: l, right: dw - l, top: t, bottom: dh - t };
}

/** Outpaint bounds — mirror of backend validate_outpaint. */
export const MAX_OUT_DIM = 4096;
export function outpaintValid(srcW: number, srcH: number,
                              outW: number, outH: number): string | null {
  if (!(outW >= 1 && outW <= MAX_OUT_DIM && outH >= 1 && outH <= MAX_OUT_DIM))
    return `dimensions must be 1–${MAX_OUT_DIM} px`;
  if (outW < srcW || outH < srcH)
    return "canvas cannot be smaller than the source — expansion only";
  if (outW === srcW && outH === srcH) return "nothing to expand";
  return null;
}

/** Editable-region count of a mask bitmap (luminance >= 128 — the
 * documented internal convention, white = editable). */
export function editablePixels(data: Uint8ClampedArray): number {
  let n = 0;
  for (let i = 0; i + 2 < data.length; i += 4)
    if ((data[i] + data[i + 1] + data[i + 2]) / 3 >= 128 && data[i + 3] > 0) n++;
  return n;
}
