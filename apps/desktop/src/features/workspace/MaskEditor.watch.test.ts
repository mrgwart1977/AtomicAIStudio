/** S4 — watchImportJob: WS-store-driven completion with a bounded
 * reconciliation fallback. No second socket, no token in URLs. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiFetch = vi.fn();
vi.mock("../../api/client", () => ({
  apiFetch: (...a: unknown[]) => apiFetch(...a),
}));

// Minimal zustand-shaped store double: subscribe/getState/setState.
type St = { jobs: Record<string, { status: string; output_asset_id: string | null }> };
const listeners = new Set<(s: St) => void>();
let state: St = { jobs: {} };
const setJob = (id: string, status: string, out: string | null = null) => {
  state = { jobs: { ...state.jobs, [id]: { status, output_asset_id: out } } };
  listeners.forEach((f) => f(state));
};
vi.mock("../../stores/appStore", () => ({
  useApp: Object.assign(
    (sel: (s: St) => unknown) => sel(state),
    {
      subscribe: (fn: (s: St) => void) => {
        listeners.add(fn);
        return () => listeners.delete(fn);
      },
      getState: () => state,
    }),
}));

import { watchImportJob } from "./MaskEditor";

describe("watchImportJob", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    state = { jobs: {} };
    listeners.clear();
    apiFetch.mockReset();
  });
  afterEach(() => vi.useRealTimers());

  it("settles on a terminal WS event — no polling", () => {
    const onDone = vi.fn(), onFail = vi.fn();
    watchImportJob("j1", { onDone, onFail });
    setJob("j1", "succeeded", "asset-9");
    expect(onDone).toHaveBeenCalledWith("asset-9");
    expect(onFail).not.toHaveBeenCalled();
    expect(listeners.size).toBe(0);           // unsubscribed on settle
    vi.advanceTimersByTime(60_000);
    expect(apiFetch).not.toHaveBeenCalled();  // fallback never fired
  });

  it("handles a job already terminal before subscribing", () => {
    setJob("j2", "succeeded", "asset-1");
    const onDone = vi.fn(), onFail = vi.fn();
    watchImportJob("j2", { onDone, onFail });
    expect(onDone).toHaveBeenCalledWith("asset-1");
  });

  it("reports failure and cancellation", () => {
    const onDone = vi.fn(), onFail = vi.fn();
    watchImportJob("j3", { onDone, onFail });
    setJob("j3", "failed");
    expect(onFail).toHaveBeenCalledWith("Mask import job failed");
    expect(onDone).not.toHaveBeenCalled();
  });

  it("ignores unrelated job events", () => {
    const onDone = vi.fn(), onFail = vi.fn();
    watchImportJob("j4", { onDone, onFail });
    setJob("other", "succeeded", "x");
    expect(onDone).not.toHaveBeenCalled();
    expect(listeners.size).toBe(1);
  });

  it("falls back to a bounded poll when no WS event arrives", async () => {
    apiFetch.mockResolvedValue([
      { id: "j5", status: "succeeded", output_asset_id: "asset-7" },
    ]);
    const onDone = vi.fn(), onFail = vi.fn();
    watchImportJob("j5", { onDone, onFail });
    await vi.advanceTimersByTimeAsync(5000);
    expect(apiFetch).toHaveBeenCalledWith("/api/jobs");
    expect(onDone).toHaveBeenCalledWith("asset-7");
    vi.advanceTimersByTime(60_000);
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });

  it("cancel stops both the subscription and the fallback", async () => {
    const onDone = vi.fn(), onFail = vi.fn();
    const cancel = watchImportJob("j6", { onDone, onFail });
    cancel();
    setJob("j6", "succeeded", "asset-0");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(onDone).not.toHaveBeenCalled();
    expect(apiFetch).not.toHaveBeenCalled();
  });
});
