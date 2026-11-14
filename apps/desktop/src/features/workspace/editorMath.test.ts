import { describe, expect, it } from "vitest";
import {
  clampImage, computePad, editablePixels, outpaintValid, viewToImage,
} from "./editorMath";

describe("viewToImage / zoom+pan mapping", () => {
  it("maps canvas pixels to image pixels under zoom and pan", () => {
    expect(viewToImage(100, 60, { scale: 2, ox: 10, oy: 20 }, 0, 0))
      .toEqual({ x: 45, y: 20 });
    // Pan shift doesn't change image-space coords of the same pixel content.
    const v1 = viewToImage(50, 50, { scale: 1, ox: 0, oy: 0 }, 0, 0);
    const v2 = viewToImage(70, 50, { scale: 1, ox: 20, oy: 0 }, 0, 0);
    expect(v1).toEqual(v2); // panning the view by 20px = pointer +20px
  });
  it("clamps to image bounds at edges", () => {
    expect(clampImage(-5, 999, 10, 8)).toEqual({ x: 0, y: 7 });
    expect(clampImage(9.9, 7.9, 10, 8)).toEqual({ x: 9, y: 7 });
  });
});

describe("computePad mirrors the backend anchors", () => {
  const cases: [string, number[]][] = [
    ["center", [32, 32, 24, 24]], ["top", [32, 32, 0, 48]],
    ["bottom", [32, 32, 48, 0]], ["left", [0, 64, 24, 24]],
    ["right", [64, 0, 24, 24]], ["top_left", [0, 64, 0, 48]],
    ["top_right", [64, 0, 0, 48]], ["bottom_left", [0, 64, 48, 0]],
    ["bottom_right", [64, 0, 48, 0]],
  ];
  for (const [anchor, [l, r, t, b]] of cases) {
    it(`anchor ${anchor}`, () => {
      expect(computePad(64, 48, 128, 96, anchor as never))
        .toEqual({ left: l, right: r, top: t, bottom: b });
    });
  }
  it("pads always sum to the canvas delta", () => {
    for (const a of cases) {
      const p = computePad(64, 48, 200, 150, a[0] as never);
      expect(p.left + p.right).toBe(136);
      expect(p.top + p.bottom).toBe(102);
    }
  });
});

describe("outpaintValid mirrors backend bounds", () => {
  it("accepts expansion, refuses shrink/equal/oversize", () => {
    expect(outpaintValid(64, 48, 128, 48)).toBeNull();
    expect(outpaintValid(64, 48, 32, 48)).toMatch(/smaller/);
    expect(outpaintValid(64, 48, 64, 48)).toMatch(/nothing to expand/);
    expect(outpaintValid(64, 48, 4097, 48)).toMatch(/1–4096/);
    expect(outpaintValid(64, 48, 0, 48)).toMatch(/1–4096/);
  });
});

describe("editablePixels mask convention", () => {
  it("counts luminance >= 128 with alpha", () => {
    const d = new Uint8ClampedArray([
      255, 255, 255, 255,   // editable
      0, 0, 0, 255,         // protected
      127, 127, 127, 255,   // below threshold
      255, 255, 255, 0,     // transparent — not counted
    ]);
    expect(editablePixels(d)).toBe(1);
  });
});
