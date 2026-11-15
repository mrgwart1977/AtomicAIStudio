// N2-B — waveform client: level selection, manifest/slice caching,
// honest missing-vs-error states, bounded FIFO slice cache.
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiFetch = vi.fn();
vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));

import {
  clipWaveWindow, getWaveformManifest, getWaveformSlice,
  invalidateWaveform, pickLevel,
} from "./waveform";
import type { WaveManifest } from "./waveform";
import type { TimelineClip } from "../../api/types";

const MANIFEST: WaveManifest = {
  format: "ade-wave/1", built_sha256: "ab".repeat(32),
  sample_rate: 48000, channels: 2, samples: 48000,
  levels: [
    { spb: 256, buckets: 188, sha256: "c".repeat(64) },
    { spb: 1024, buckets: 47, sha256: "d".repeat(64) },
  ],
};

beforeEach(() => {
  apiFetch.mockReset();
});

describe("pickLevel", () => {
  it("picks the first level at/above samples-per-pixel", () => {
    expect(pickLevel(MANIFEST, 200)?.spb).toBe(256);
    expect(pickLevel(MANIFEST, 256)?.spb).toBe(256);
    expect(pickLevel(MANIFEST, 300)?.spb).toBe(1024);
    expect(pickLevel(MANIFEST, 99999)?.spb).toBe(1024); // coarsest
  });

  it("handles empty level lists", () => {
    expect(pickLevel({ ...MANIFEST, levels: [] }, 100)).toBeNull();
  });
});

describe("getWaveformManifest", () => {
  it("maps 409 to 'missing' and other errors to 'error'", async () => {
    apiFetch.mockRejectedValueOnce(new Error("409: no waveform built"));
    await expect(getWaveformManifest("p1", "a1"))
      .resolves.toBe("missing");
    invalidateWaveform("p1", "a1");
    apiFetch.mockRejectedValueOnce(new Error("500: exploded"));
    await expect(getWaveformManifest("p1", "a1"))
      .resolves.toBe("error");
    invalidateWaveform("p1", "a1");
    apiFetch.mockResolvedValueOnce(MANIFEST);
    await expect(getWaveformManifest("p1", "a1"))
      .resolves.toBe(MANIFEST);
  });

  it("caches the promise; invalidate refetches", async () => {
    apiFetch.mockResolvedValue(MANIFEST);
    const p1 = getWaveformManifest("p2", "a2");
    const p2 = getWaveformManifest("p2", "a2");
    expect(p1).toBe(p2);
    expect(apiFetch).toHaveBeenCalledTimes(1);
    invalidateWaveform("p2", "a2");
    void getWaveformManifest("p2", "a2");
    expect(apiFetch).toHaveBeenCalledTimes(2);
  });
});

describe("getWaveformSlice", () => {
  const SLICE = {
    spb: 256, buckets: 188, bucket_start: 0, bucket_end: 10,
    channels: 2, sample_rate: 48000,
    peaks: [[[0, 1]], [[0, -1]]],
  };

  it("fetches once per (asset, spb, range) key", async () => {
    apiFetch.mockResolvedValue(SLICE);
    await getWaveformSlice("p", "a", 256, 0, 10);
    await getWaveformSlice("p", "a", 256, 0, 10);
    expect(apiFetch).toHaveBeenCalledTimes(1);
    await getWaveformSlice("p", "a", 256, 10, 20);
    expect(apiFetch).toHaveBeenCalledTimes(2);
  });

  it("returns null on fetch failure — never throws", async () => {
    apiFetch.mockRejectedValueOnce(new Error("404"));
    await expect(getWaveformSlice("p", "a9", 256, 0, 5))
      .resolves.toBeNull();
  });

  it("slice cache is FIFO-bounded", async () => {
    apiFetch.mockResolvedValue(SLICE);
    for (let i = 0; i < 300; i++) {
      await getWaveformSlice("p", "cap", 256, i, i + 1);
    }
    // Early keys evicted — refetching bucket 0 hits the network again.
    const calls = apiFetch.mock.calls.length;
    await getWaveformSlice("p", "cap", 256, 0, 1);
    expect(apiFetch.mock.calls.length).toBe(calls + 1);
    // Recent keys still cached — no new fetch.
    await getWaveformSlice("p", "cap", 256, 299, 300);
    expect(apiFetch.mock.calls.length).toBe(calls + 1);
  });
});

// ---------- clipWaveWindow — timing/alignment ----------

const CLIP = {
  clip_id: "c1", source_asset_id: "a1", source_version_id: "v1",
  start_frames: 0, in_frame: 0, out_frame: 48000,
  src_fps_num: 48000, src_fps_den: 1, src_frames: 48000,
} as TimelineClip;

describe("clipWaveWindow", () => {
  const TL = 24; // timeline fps — 1 s of 48 kHz audio = 24 tl-frames
  const EXT = 24; // ceil(48000 × 24 / 48000)

  it("maps full-clip view to the full source range", () => {
    const w = clipWaveWindow(CLIP, TL, 0, EXT, EXT)!;
    expect(w.s0).toBe(0);
    expect(w.s1).toBe(48000); // 24 tl-f × 2000 samples/f
    expect(w.samplesPerTlFrame).toBe(2000);
  });

  it("head trim: discarded samples below in_frame are not shown", () => {
    const clipped = { ...CLIP, in_frame: 12000 };
    const extent = Math.ceil(36000 * TL / 48000); // 18 tl-f
    const w = clipWaveWindow(clipped, TL, 0, extent, extent)!;
    expect(w.s0).toBe(12000); // starts AT the trim, never before
    expect(w.s1).toBe(48000);
  });

  it("tail trim: samples beyond out_frame are not shown", () => {
    const clipped = { ...CLIP, out_frame: 30000 };
    const extent = Math.ceil(30000 * TL / 48000); // 15 tl-f
    // DOM extent pads to ceil — view covering it still clamps at out.
    const w = clipWaveWindow(clipped, TL, 0, extent, extent)!;
    expect(w.s0).toBe(0);
    expect(w.s1).toBe(30000); // clamped AT the out point
  });

  it("nonzero in AND out: window is exactly [in, out]", () => {
    const clipped = { ...CLIP, in_frame: 5000, out_frame: 43000 };
    const extent = Math.ceil(38000 * TL / 48000); // 19 tl-f
    const w = clipWaveWindow(clipped, TL, 0, extent, extent)!;
    expect(w.s0).toBe(5000);
    expect(w.s1).toBe(43000);
  });

  it("split halves: each keeps its own source interval", () => {
    // split at source sample 24000 → A [0,24000) B [24000,48000)
    const extA = 12; // 24000 × 24 / 48000
    const a = { ...CLIP, out_frame: 24000 };
    const b = { ...CLIP, in_frame: 24000, start_frames: extA };
    const wa = clipWaveWindow(a, TL, 0, extA, extA)!;
    const wb = clipWaveWindow(b, TL, extA, EXT, extA)!;
    expect(wa.s0).toBe(0); expect(wa.s1).toBe(24000);
    expect(wb.s0).toBe(24000); expect(wb.s1).toBe(48000);
  });

  it("timeline placement shifts the window, not the samples", () => {
    const moved = { ...CLIP, start_frames: 100 };
    const w = clipWaveWindow(moved, TL, 100, 100 + EXT, EXT)!;
    expect(w.s0).toBe(0);
    expect(w.s1).toBe(48000);
    expect(w.winL).toBe(0);
  });

  it("44.1 kHz source maps proportionally (per-clip rate)", () => {
    const c44 = {
      ...CLIP, out_frame: 44100, src_frames: 44100,
      src_fps_num: 44100,
    };
    const extent = Math.ceil(44100 * TL / 44100); // 24 tl-f
    const w = clipWaveWindow(c44, TL, 0, extent, extent)!;
    expect(w.s0).toBe(0);
    expect(w.s1).toBe(44100);
    expect(w.samplesPerTlFrame).toBeCloseTo(44100 / 24, 5);
  });

  it("fractional timeline rate (24000/1001) does not drift", () => {
    const tlFps = 24000 / 1001; // ≈23.976
    const samplesPerTlFrame = 48000 / tlFps; // 2002
    const extent = Math.ceil(48000 / samplesPerTlFrame); // 24
    const w = clipWaveWindow(CLIP, tlFps, 0, extent, extent)!;
    // Direct multiply — no accumulation, so full range maps exactly.
    expect(w.s1 - w.s0).toBeCloseTo(48000, 6);
    expect(w.samplesPerTlFrame).toBeCloseTo(2002, 6);
  });

  it("zoom changes LOD, not timing — window is ppf-independent", () => {
    // clipWaveWindow takes no pixel scale: the same call at any zoom
    // yields the same source range. What changes is which level
    // pickLevel selects from the manifest.
    const w = clipWaveWindow(CLIP, TL, 0, EXT, EXT)!;
    expect(w.s0).toBe(0); expect(w.s1).toBe(48000);
    // zoomed out (2000 samples/px) → coarse level; zoomed in → fine.
    expect(pickLevel(MANIFEST, 2000)?.spb).toBe(1024);
    expect(pickLevel(MANIFEST, 20)?.spb).toBe(256);
  });

  it("viewport culling: offscreen → null; partial → windowed", () => {
    expect(clipWaveWindow(CLIP, TL, 500, 900, EXT)).toBeNull();
    const w = clipWaveWindow(CLIP, TL, 10, 14, EXT)!;
    expect(w.s0).toBe(20_000); // tl-f 10 × 2000 samples/f
    expect(w.s1).toBe(28_000);
    expect(w.winL).toBe(10);
    expect(w.winW).toBe(4);
  });
});
