// N2-B — waveform client: level selection, manifest/slice caching,
// honest missing-vs-error states, bounded FIFO slice cache.
import { beforeEach, describe, expect, it, vi } from "vitest";

const apiFetch = vi.fn();
vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));

import {
  getWaveformManifest, getWaveformSlice, invalidateWaveform,
  pickLevel,
} from "./waveform";
import type { WaveManifest } from "./waveform";

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
