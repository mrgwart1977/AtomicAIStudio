// U-2b-1 — WS job.progress payloads carry adapter_id, not the DB `kind`
// field; the frontend must map ops.model.import correctly so JobsPanel
// and Model Manager agree on the job's identity.
import { beforeEach, describe, expect, it, vi } from "vitest";
import { jobKindFor, onWsStateChange } from "./App";
import { useApp } from "./stores/appStore";
import type { Job } from "./api/types";

const apiFetch = vi.fn();
vi.mock("./api/client", async (importOriginal) => {
  const orig = await importOriginal<typeof import("./api/client")>();
  return { ...orig, apiFetch: (...a: unknown[]) => apiFetch(...a) };
});

describe("jobKindFor — WS adapter_id → UI kind mapping", () => {
  it("maps ops model jobs to their UI kinds", () => {
    expect(jobKindFor("ops.model.download")).toBe("model_download");
    expect(jobKindFor("ops.model.benchmark")).toBe("model_benchmark");
    expect(jobKindFor("ops.model.import")).toBe("model_import");
  });

  it("everything else stays the generic image kind", () => {
    expect(jobKindFor("gen.image.sdcpp")).toBe("image");
    expect(jobKindFor("cloud.image.txt2img.sim")).toBe("image");
    expect(jobKindFor("")).toBe("image");
  });
});

const REQUEUED_IMPORT: Job = {
  id: "jimp", project_id: null, adapter_id: "ops.model.import",
  kind: "model_import", status: "queued", progress: 0,
  message: "re-queued after restart", error: null,
  resumable: true, output_asset_id: null, command_id: null,
  created_at: "2026-01-01T00:00:00Z", completed_at: null,
};

describe("onWsStateChange — hydration on authenticated (re)connect", () => {
  beforeEach(() => {
    apiFetch.mockReset();
    useApp.setState({ jobs: {}, wsConnected: false });
  });

  it("hydrates app-level jobs on connect — a requeued import surfaces", async () => {
    apiFetch.mockResolvedValue([REQUEUED_IMPORT]);
    onWsStateChange(true);
    expect(useApp.getState().wsConnected).toBe(true);
    await vi.waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/jobs"));
    await vi.waitFor(() =>
      expect(useApp.getState().jobs.jimp?.status).toBe("queued"));
    expect(useApp.getState().jobs.jimp.message).toBe("re-queued after restart");
  });

  it("does nothing on disconnect", () => {
    onWsStateChange(false);
    expect(apiFetch).not.toHaveBeenCalled();
    expect(useApp.getState().wsConnected).toBe(false);
  });

  it("a hydration snapshot cannot regress a newer WS-derived state", async () => {
    // WS event already landed before the fetch resolved — running@0.9
    // must beat the stale queued@0 snapshot.
    useApp.setState({
      jobs: { jimp: { ...REQUEUED_IMPORT, status: "running", progress: 0.9 } },
    });
    apiFetch.mockResolvedValue([REQUEUED_IMPORT]);
    onWsStateChange(true);
    await vi.waitFor(() => expect(apiFetch).toHaveBeenCalledWith("/api/jobs"));
    await new Promise((r) => setTimeout(r, 0)); // let the hydration set land
    expect(useApp.getState().jobs.jimp.status).toBe("running");
    expect(useApp.getState().jobs.jimp.progress).toBe(0.9);
  });
});
